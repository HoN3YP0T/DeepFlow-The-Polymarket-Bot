"""Dashboard authentication. Section 29.

The dashboard exposes Emergency Stop, Cancel All Orders and Close All Positions. An
unauthenticated dashboard is a remote kill switch for anyone who can reach the port, so
authentication is required in every mode that touches the venue (enforced in
``config.settings``).

Destructive controls require a second, explicit confirmation beyond being authenticated,
and every use is written to the audit log with the actor.

**Two deliberate decisions worth reading before changing anything here.**

*Passwords are hashed with stdlib ``hashlib.scrypt``, not passlib.* ``passlib[bcrypt]`` is
a declared dependency and **does not run in this environment** -- passlib 1.7 reads
``bcrypt.__about__``, which modern bcrypt removed, and its backend probe then dies on a
72-byte limit. A broken hash is worse than a missing one, and scrypt is in the standard
library, memory-hard, and needs no third party at all.

*There is no "arm live mode" control and there must not be.* The role docstring below used
to promise one. Hard rule 1: LIVE is refused in ``Orchestrator.start`` and arming it is not
a dashboard button -- it is a deliberate configuration change with a typed acknowledgement,
made by someone at a terminal who has read why it is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from jose import JWTError, jwt

from deepflow.core.errors import AuthenticationError

#: Signing algorithm. Symmetric because the issuer and the verifier are the same process;
#: an asymmetric pair would add key management for no threat it removes here.
ALGORITHM: Final = "HS256"

#: scrypt parameters. n=2**15 costs ~100 ms per verification on this class of machine,
#: which is negligible for a login and expensive for an attacker holding the hash.
_SCRYPT_N: Final = 2**15
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_SCRYPT_DKLEN: Final = 32
_SALT_BYTES: Final = 16

#: Memory ceiling passed to OpenSSL, which defaults to 32 MiB and refuses anything above it.
#:
#: These parameters need ``128 * N * r`` = 32 MiB exactly, so the default rejects them with
#: "memory limit exceeded" -- an error that looks like a bug in the caller and is really a
#: cap in the library. Stated explicitly with headroom, because the alternative is to weaken
#: ``N`` to fit a limit that has nothing to do with how hard the hash should be.
_SCRYPT_MAXMEM: Final = 64 * 1024 * 1024


class Role(StrEnum):
    VIEWER = "VIEWER"
    """Read-only."""
    OPERATOR = "OPERATOR"
    """May pause, resume and adjust thresholds."""
    ADMIN = "ADMIN"
    """May trigger emergency actions and close the book.

    **Not** "may arm live mode": see the module docstring. No role reaches LIVE from
    here, because no code path does.
    """


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    role: Role

    def can(self, required: Role) -> bool:
        order = (Role.VIEWER, Role.OPERATOR, Role.ADMIN)
        return order.index(self.role) >= order.index(required)


def hash_password(password: str) -> str:
    """Hash a password for storage in configuration, as ``scrypt$salt$key`` in hex.

    Exposed so an operator can generate a hash without a Python session of their own --
    see ``scripts/hash_password.py``. The plaintext is never stored or logged anywhere.
    """
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Whether ``password`` matches ``stored``.

    Returns ``False`` for a malformed stored value rather than raising. A corrupt hash in
    configuration must read as "this credential does not authenticate", never as an error
    a caller might mistake for a transport problem and retry past.

    Compared with :func:`hmac.compare_digest`: a plain ``==`` on the derived key leaks its
    prefix through timing.
    """
    parts = stored.split("$")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    try:
        salt = bytes.fromhex(parts[1])
        expected = bytes.fromhex(parts[2])
    except ValueError:
        return False
    candidate = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=len(expected),
        maxmem=_SCRYPT_MAXMEM,
    )
    return hmac.compare_digest(candidate, expected)


def create_token(principal: Principal, *, secret: str, ttl_seconds: int) -> str:
    """Issue a bearer token for ``principal``.

    Carries a ``jti`` so an individual token is nameable in an audit trail, and an
    ``iat`` so "issued before the secret was rotated" is answerable later.
    """
    if not secret:
        raise ValueError("a signing secret is required to issue a token")
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": principal.subject,
        "role": principal.role.value,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def verify_token(token: str, *, secret: str) -> Principal:
    """Decode and validate a bearer token, or raise :class:`AuthenticationError`.

    Fails closed on everything: a bad signature, an expired token, a missing subject, and
    -- the one worth stating -- a **role this code does not recognise**. A token claiming
    ``role="SUPERADMIN"`` must not degrade to VIEWER and must not be accepted; it means the
    token was issued by something whose rules are not ours.

    The algorithm is pinned. Accepting the token's own ``alg`` header is how a verifier
    ends up honouring ``alg=none``.
    """
    if not secret:
        raise AuthenticationError("no signing secret configured; the API cannot verify tokens")
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM])
    except JWTError as error:
        raise AuthenticationError(f"token rejected: {error}") from error

    subject = claims.get("sub")
    raw_role = claims.get("role")
    if not subject or not raw_role:
        raise AuthenticationError("token carries no subject or role")
    try:
        role = Role(raw_role)
    except ValueError as error:
        raise AuthenticationError(f"token carries an unknown role {raw_role!r}") from error
    return Principal(subject=str(subject), role=role)
