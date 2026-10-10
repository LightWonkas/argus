"""Dashboard auth (api.security) tests — pure, offline."""

from __future__ import annotations

from argus.api.security import (
    SessionConfig,
    authenticate,
    hash_password,
    issue_session,
    parse_users,
    verify_password,
    verify_session,
)


def test_hash_and_verify_roundtrip() -> None:
    enc = hash_password("test-passphrase")
    assert enc.startswith("scrypt$")
    assert verify_password("test-passphrase", enc) is True
    assert verify_password("wrong", enc) is False


def test_hash_is_salted_unique() -> None:
    assert hash_password("same") != hash_password("same")  # random salt


def test_verify_malformed_is_false() -> None:
    assert verify_password("x", "malformed") is False
    assert verify_password("x", "bcrypt$1$1$1$00$00") is False  # wrong scheme
    assert verify_password("x", "") is False


def test_parse_users_splits_on_first_colon() -> None:
    # The hash itself contains '$' too; only the first ':' is the separator.
    raw = "ada:scrypt$16384$8$1$aa$bb,bob:scrypt$16384$8$1$cc$dd"
    users = parse_users(raw)
    assert set(users) == {"ada", "bob"}
    assert users["ada"].startswith("scrypt$")


def test_parse_users_skips_blank_and_malformed() -> None:
    users = parse_users(" , ada:h , malformed_entry , :hash , name: ")
    assert set(users) == {"ada"}


def test_authenticate_success_and_failure() -> None:
    users = {"ada": hash_password("p1")}
    assert authenticate(users, "ada", "p1") is True
    assert authenticate(users, "ada", "wrong") is False
    assert authenticate(users, "nobody", "p1") is False  # no such user


def test_session_roundtrip() -> None:
    cfg = SessionConfig(secret=b"test-secret", ttl_seconds=3600)
    token = issue_session(cfg, "ada", now=1000.0)
    assert verify_session(cfg, token, now=1100.0) == "ada"


def test_session_expired_is_none() -> None:
    cfg = SessionConfig(secret=b"test-secret", ttl_seconds=100)
    token = issue_session(cfg, "ada", now=1000.0)
    assert verify_session(cfg, token, now=1101.0) is None  # expired


def test_session_tampered_is_none() -> None:
    cfg = SessionConfig(secret=b"test-secret", ttl_seconds=3600)
    token = issue_session(cfg, "ada", now=1000.0)
    # The signature does not verify with a different secret.
    other = SessionConfig(secret=b"other-secret", ttl_seconds=3600)
    assert verify_session(other, token, now=1100.0) is None


def test_session_malformed_is_none() -> None:
    cfg = SessionConfig(secret=b"s")
    assert verify_session(cfg, "single-part") is None
    assert verify_session(cfg, "ada.notanumber.sig") is None


def test_from_secret_empty_generates_random() -> None:
    a = SessionConfig.from_secret("")
    b = SessionConfig.from_secret("")
    assert a.secret != b.secret  # random
    assert len(a.secret) == 32
