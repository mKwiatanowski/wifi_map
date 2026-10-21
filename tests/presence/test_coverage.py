from datetime import timedelta

from homesense.core.models import CoveragePlan, CoverageRoom, DeviceObservation
from homesense.presence import coverage

MAC = "02:00:00:00:00:01"


def _obs(ts, signal, connection="5g"):
    return DeviceObservation(ts=ts, mac=MAC, source="router", connection=connection, signal=signal)


def test_summarize_median_count_and_band(clock):
    t = clock.now()
    assert coverage.summarize([]) == (None, 0, "unknown")
    assert coverage.summarize([_obs(t, None)]) == (None, 0, "unknown")
    obs = [_obs(t, -50, "2g"), _obs(t, -61, "2g"), _obs(t, -70, "5g"), _obs(t, -40, "unknown")]
    assert coverage.summarize(obs) == (-55.5, 4, "2.4")
    assert coverage.summarize([_obs(t, -50, "wired")]) == (-50.0, 1, "unknown")


def test_plan_and_points_repository(repos, clock):
    assert repos.coverage.get_plan() is None
    plan = CoveragePlan(10, 8, (1.0, 2.0), (CoverageRoom("Salon", 0, 0, 5, 4),))
    repos.coverage.save_plan(plan)
    assert repos.coverage.get_plan() == plan
    repos.coverage.save_plan(CoveragePlan(6, 5, None))
    assert repos.coverage.get_plan() == CoveragePlan(6.0, 5.0, None, ())

    now = clock.now()
    p = repos.coverage.add_point(MAC.upper(), 1, 1, now, now + timedelta(seconds=60))
    assert p.mac == MAC and not p.done and repos.coverage.measuring() == [p]
    repos.coverage.finish_point(p.id, -60.0, 2, "5")
    assert repos.coverage.measuring() == [] and repos.coverage.get_point(p.id).rssi == -60.0
    assert repos.coverage.clear_points() == 1


def test_finalize_due_and_active_targets(repos, clock):
    now = clock.now()
    repos.coverage.save_plan(CoveragePlan(10, 10, (0.0, 0.0)))
    p = repos.coverage.add_point(MAC, 3, 4, now, now + timedelta(seconds=60))
    repos.observations.add_many([_obs(now + timedelta(seconds=10), -66)])
    assert coverage.active_targets(repos, now) == {MAC: 5.0}
    assert coverage.finalize_due(repos, now + timedelta(seconds=59)) == 0
    assert coverage.live_point(repos, p).samples == 1
    assert coverage.finalize_due(repos, now + timedelta(seconds=60)) == 1
    assert coverage.active_targets(repos, now + timedelta(seconds=60)) == {}
    done = repos.coverage.get_point(p.id)
    assert done.done and done.rssi == -66.0 and done.band == "5"


def test_active_targets_need_a_router(repos, clock):
    now = clock.now()
    repos.coverage.save_plan(CoveragePlan(10, 10, None))
    repos.coverage.add_point(MAC, 3, 4, now, now + timedelta(seconds=60))
    assert coverage.active_targets(repos, now) == {}
