"""Create an owner (superuser) account, or reset a user's password. There is no signup on the website,
so this is the only way to create the first account. Run it where the app runs, e.g. on the server:

    docker exec -it ads-planner-planner-1 python -m scripts.create_owner

Locally:  .\\.venv\\Scripts\\python.exe -m scripts.create_owner
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from backend.auth.routes import create_user  # noqa: E402
from backend.auth.security import get_password_hash  # noqa: E402
from backend.db import User, get_session, init_db  # noqa: E402


def main() -> None:
    init_db()
    email = input("Email: ").strip().lower()
    if "@" not in email:
        sys.exit("That is not an email address.")
    password = getpass.getpass("Password (min 8 characters, not shown): ")
    if len(password) < 8 or password != getpass.getpass("Repeat password: "):
        sys.exit("Passwords too short or do not match. Nothing changed.")
    with get_session() as s:
        existing = s.scalar(select(User).where(User.email == email))
        if existing:
            existing.hashed_password = get_password_hash(password)
            existing.token_version = int(existing.token_version or 0) + 1  # old sessions stop working
            existing.is_superuser = True
            existing.is_active = True
            s.commit()
            print(f"Password reset; {email} is an owner.")
            return
    create_user(email, password, superuser=True)
    print(f"Owner account created: {email}. Sign in on the website.")


if __name__ == "__main__":
    main()
