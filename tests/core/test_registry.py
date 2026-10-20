import pytest

from homesense.core.registry import Registry


def test_registry():
    r: Registry[int] = Registry()
    r.register("a", 1)
    r.register("b", 2)
    assert r.get("a") == 1 and r.names() == ["a", "b"] and r.items() == [("a", 1), ("b", 2)]
    with pytest.raises(ValueError):
        r.register("a", 3)
