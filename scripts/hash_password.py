"""Generate a dashboard password hash for the .env file.

    .venv/bin/python scripts/hash_password.py
    make hash-password

Prompts without echoing, asks twice, and prints the hash and the env line to paste.

**The plaintext is never stored, logged or passed as an argument.** Reading it from a
prompt rather than ``sys.argv`` keeps it out of the shell history and out of every ``ps``
listing on the machine -- which matters more than usual here, because this is the
credential for an emergency-stop button.
"""

from __future__ import annotations

import getpass
import sys

from deepflow.api.auth import Role, hash_password

MIN_LENGTH = 12


def main() -> int:
    print("Dashboard operator password (not echoed).")
    password = getpass.getpass("password: ")
    if len(password) < MIN_LENGTH:
        print(f"refusing: use at least {MIN_LENGTH} characters", file=sys.stderr)
        return 1
    if password != getpass.getpass("again:    "):
        print("refusing: the two entries differ", file=sys.stderr)
        return 1

    username = input("username: ").strip() or "operator"
    print(f"\nroles: {', '.join(r.value for r in Role)}")
    role = (input("role [VIEWER]: ").strip() or Role.VIEWER.value).upper()
    if role not in {r.value for r in Role}:
        print(f"refusing: {role!r} is not a role", file=sys.stderr)
        return 1

    digest = hash_password(password)
    print("\nAdd to .env (one line, single quotes matter):\n")
    print(
        f'DEEPFLOW_API__OPERATORS=\'{{"{username}": '
        f'{{"password_hash": "{digest}", "role": "{role}"}}}}\''
    )
    print("\nAnd a signing secret, if you have not set one:\n")
    print("DEEPFLOW_API__JWT_SECRET=<48+ random characters>")
    print("\nWithout the secret the dashboard serves the read-only panels and refuses")
    print("every control -- which is the intended behaviour, not a misconfiguration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
