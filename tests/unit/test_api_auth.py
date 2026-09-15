"""Dashboard authentication, and the attacks it has to refuse.

This is the code standing between the public internet and an emergency-stop button, so
the tests are mostly about what it rejects. Every case below is a way an attacker or a
misconfiguration could otherwise obtain a session.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from jose import jwt

from deepflow.api.app import create_app
from deepflow.api.auth import (
    ALGORITHM,
    Principal,
    Role,
    create_token,
    hash_password,
    verify_password,
    verify_token,
)
from deepflow.config.settings import Settings
from deepflow.core.errors import AuthenticationError

SECRET = "k" * 48
PASSWORD = "s3cret-operator-pass"


@pytest.fixture(scope="module")
def password_hash() -> str:
    """Hashed once: scrypt is deliberately ~100 ms, and 20 tests would pay it 20 times."""
    return hash_password(PASSWORD)


def _client(password_hash: str, *, role: str = "ADMIN", secret: str | None = SECRET) -> TestClient:
    api: dict[str, object] = {
        "operators": {"alice": {"password_hash": password_hash, "role": role}}
    }
    if secret is not None:
        api["jwt_secret"] = secret
    return TestClient(create_app(Settings(_env_file=None, api=api)))  # type: ignore[arg-type]


def _token(client: TestClient) -> str:
    response = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 200
    return str(response.json()["token"])


# --- passwords -----------------------------------------------------------
def test_a_password_round_trips(password_hash: str) -> None:
    assert verify_password(PASSWORD, password_hash)
    assert not verify_password("wrong", password_hash)


def test_the_hash_does_not_contain_the_password(password_hash: str) -> None:
    """Obvious, and worth pinning: a "hash" that embeds its input is a storage format."""
    assert PASSWORD not in password_hash
    assert password_hash.startswith("scrypt$")


def test_a_malformed_stored_hash_rejects_rather_than_raising() -> None:
    """A corrupt hash in configuration must read as "this credential does not
    authenticate", never as an error a caller might retry past."""
    for stored in ("", "garbage", "scrypt$only-two-parts", "$2b$12$bcrypt-style", "scrypt$zz$zz"):
        assert not verify_password("anything", stored)


def test_two_hashes_of_one_password_differ(password_hash: str) -> None:
    """Per-hash salt. Without it, identical passwords are visibly identical in config and
    one cracked hash breaks every account that shared it."""
    assert hash_password(PASSWORD) != password_hash


def test_an_empty_password_cannot_be_hashed() -> None:
    with pytest.raises(ValueError):
        hash_password("")


# --- tokens --------------------------------------------------------------
def test_a_token_round_trips() -> None:
    token = create_token(Principal("alice", Role.ADMIN), secret=SECRET, ttl_seconds=60)
    principal = verify_token(token, secret=SECRET)
    assert principal == Principal("alice", Role.ADMIN)


@pytest.mark.parametrize(
    ("label", "claims"),
    [
        ("unknown role", {"sub": "eve", "role": "SUPERADMIN"}),
        ("no role", {"sub": "eve"}),
        ("no subject", {"role": "ADMIN"}),
        ("empty subject", {"sub": "", "role": "ADMIN"}),
    ],
)
def test_a_token_with_bad_claims_is_refused(label: str, claims: dict[str, object]) -> None:
    """**An unrecognised role must not degrade to VIEWER.**

    A token claiming ``SUPERADMIN`` was issued by something whose rules are not ours.
    Accepting it at reduced privilege treats a foreign issuer as trustworthy; the answer
    is no.
    """
    forged = jwt.encode({**claims, "exp": int(time.time()) + 60}, SECRET, algorithm=ALGORITHM)
    with pytest.raises(AuthenticationError):
        verify_token(forged, secret=SECRET)


def test_a_token_signed_with_another_secret_is_refused() -> None:
    token = create_token(Principal("alice", Role.ADMIN), secret="x" * 48, ttl_seconds=60)
    with pytest.raises(AuthenticationError):
        verify_token(token, secret=SECRET)


def test_an_expired_token_is_refused() -> None:
    token = create_token(Principal("alice", Role.ADMIN), secret=SECRET, ttl_seconds=-1)
    with pytest.raises(AuthenticationError):
        verify_token(token, secret=SECRET)


def test_verification_without_a_secret_is_refused() -> None:
    """Not "allow everything when unconfigured" -- the opposite."""
    token = create_token(Principal("alice", Role.ADMIN), secret=SECRET, ttl_seconds=60)
    with pytest.raises(AuthenticationError):
        verify_token(token, secret="")


def test_issuing_without_a_secret_is_refused() -> None:
    with pytest.raises(ValueError):
        create_token(Principal("alice", Role.ADMIN), secret="", ttl_seconds=60)


def test_the_algorithm_is_pinned() -> None:
    """Accepting the token's own ``alg`` header is how a verifier honours ``alg=none``."""
    token = create_token(Principal("alice", Role.ADMIN), secret=SECRET, ttl_seconds=60)
    header = jwt.get_unverified_header(token)
    assert header["alg"] == ALGORITHM


def test_each_token_is_individually_nameable() -> None:
    """A ``jti`` is what lets an audit trail name one token rather than one user."""
    first = create_token(Principal("alice", Role.ADMIN), secret=SECRET, ttl_seconds=60)
    second = create_token(Principal("alice", Role.ADMIN), secret=SECRET, ttl_seconds=60)
    assert first != second
    claims = jwt.decode(first, SECRET, algorithms=[ALGORITHM])
    assert claims["jti"] and claims["iat"]


# --- roles ---------------------------------------------------------------
def test_roles_are_ordered_and_do_not_leak_upward() -> None:
    assert Principal("a", Role.ADMIN).can(Role.OPERATOR)
    assert Principal("o", Role.OPERATOR).can(Role.VIEWER)
    assert not Principal("v", Role.VIEWER).can(Role.OPERATOR)
    assert not Principal("o", Role.OPERATOR).can(Role.ADMIN)


def test_no_role_can_arm_live_mode() -> None:
    """Hard rule 1. ``Role.ADMIN`` was documented as "may arm live mode"; nothing in the
    API exposes that, and this asserts the absence so adding one has to break a test."""
    import inspect

    from deepflow.api import app as app_module
    from deepflow.api.routers import risk

    for module in (app_module, risk):
        source = inspect.getsource(module).lower()
        assert "live_trading_confirmed" not in source
        assert "arm" not in source.replace("alarm", "")


# --- over HTTP -----------------------------------------------------------
def test_login_issues_a_token_and_whoami_reads_it(password_hash: str) -> None:
    client = _client(password_hash)
    token = _token(client)
    response = client.get("/api/auth/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {
        "subject": "alice",
        "role": "ADMIN",
        "may_operate": True,
        "may_administer": True,
    }


@pytest.mark.parametrize(
    ("username", "password"),
    [("alice", "wrong"), ("eve", PASSWORD), ("eve", "wrong")],
)
def test_bad_credentials_are_indistinguishable(
    password_hash: str, username: str, password: str
) -> None:
    """One response for "no such user" and "wrong password".

    Distinguishing them lets an attacker enumerate operator names before trying a single
    password. The *log* records which it was; the caller does not.
    """
    client = _client(password_hash)
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic abc", "token-without-scheme", "bearer not.a.jwt"],
)
def test_a_missing_or_malformed_credential_is_401(password_hash: str, header: str | None) -> None:
    client = _client(password_hash)
    headers = {} if header is None else {"Authorization": header}
    response = client.get("/api/auth/whoami", headers=headers)
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_without_a_configured_secret_nobody_can_log_in(password_hash: str) -> None:
    """``Settings`` allows PAPER without a JWT secret so a local run needs no credentials.

    That must not mean the controls are open. With no secret there is no session to issue,
    so login is unavailable and every authenticated route stays 401 -- fail-closed on the
    half that acts, usable on the half that only looks.
    """
    client = _client(password_hash, secret=None)
    assert (
        client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code
        == 503
    )
    assert client.get("/api/auth/whoami").status_code == 401


def test_a_role_typo_in_configuration_grants_nothing(password_hash: str) -> None:
    """Refused rather than silently downgraded to VIEWER: a downgrade hides a
    configuration error that someone believes granted more."""
    client = _client(password_hash, role="ADMlN")  # lowercase L, not I
    response = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 500
    assert "unrecognised role" in response.json()["detail"]


def test_the_default_role_is_the_least_privileged() -> None:
    """A configuration that omits the role must grant the least, not the most."""
    from deepflow.config.settings import OperatorConfig

    assert OperatorConfig(password_hash="x" * 16).role == Role.VIEWER.value
