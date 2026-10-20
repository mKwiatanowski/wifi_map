from homesense.core.config import Settings
from homesense.security import check_secret_key, hash_password, session_fingerprint, verify_password


def test_hash_verify_roundtrip():
    stored = hash_password("s3cret")
    algo, salt, digest = stored.split("$")
    assert algo == "scrypt"
    assert len(bytes.fromhex(salt)) == 16
    assert len(bytes.fromhex(digest)) == 64
    assert verify_password("s3cret", stored)


def test_wrong_password_rejected():
    assert not verify_password("wrong", hash_password("s3cret"))


def test_hash_is_salted():
    assert hash_password("same") != hash_password("same")


def test_malformed_stored_hash_rejected():
    for stored in ["", "plain", "scrypt$zz$zz", "bcrypt$00$00", "scrypt$00"]:
        assert not verify_password("x", stored)


def _settings(tmp_path, **kw) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, **kw)


def test_secret_key_problems(tmp_path):
    assert check_secret_key(_settings(tmp_path, secret_key=""))
    assert check_secret_key(_settings(tmp_path, secret_key="change-me"))
    assert check_secret_key(_settings(tmp_path, secret_key="   "))
    assert check_secret_key(_settings(tmp_path, secret_key="x" * 32)) == []


def test_session_fingerprint_depends_on_key_and_hash():
    fp = session_fingerprint("key", "scrypt$aa$bb")
    assert fp == session_fingerprint("key", "scrypt$aa$bb")
    assert len(fp) == 64
    assert fp != session_fingerprint("other-key", "scrypt$aa$bb")
    assert fp != session_fingerprint("key", "scrypt$aa$cc")
