"""Run the hosted planner, or manage its logins from a shell.

    python -m pointersearch.web                          start (host/port/data from the environment)
    python -m pointersearch.web add-user EMAIL "Name" [--admin]
    python -m pointersearch.web reset-password EMAIL

Environment: PORT, POINTERSEARCH_DATA (folder for missions and logins),
POINTERSEARCH_ADMIN_EMAIL / POINTERSEARCH_ADMIN_PASSWORD (first administrator,
used only while no logins exist).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pointersearch.web", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.environ.get("POINTERSEARCH_DATA", "webdata"),
                    help="folder for missions and logins (default: $POINTERSEARCH_DATA or ./webdata)")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="start the web app (default)")
    for p in (ap, s):
        p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
        p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8010")))
    s = sub.add_parser("add-user", help="create a login")
    s.add_argument("email"); s.add_argument("name"); s.add_argument("--admin", action="store_true")
    s = sub.add_parser("reset-password", help="set a new temporary password for a login")
    s.add_argument("email")
    a = ap.parse_args(argv)

    if a.cmd in ("add-user", "reset-password"):
        from .auth import AuthError, Users
        users = Users(Path(a.data) / "users.db")
        pw = getpass.getpass("Temporary password (they must change it at first sign-in): ")
        try:
            if a.cmd == "add-user":
                u = users.create(a.email, a.name, pw, "admin" if a.admin else "member", must_change=True)
                print(f"created {u['role']} login for {u['email']}")
            else:
                match = next((u for u in users.list() if u["email"].lower() == a.email.lower()), None)
                if match is None:
                    raise AuthError("no login with that email address")
                users.update(match["id"], password=pw)
                print(f"password reset for {match['email']}")
        except AuthError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    import uvicorn

    from .app import create_web_app
    app = create_web_app(a.data, os.environ.get("POINTERSEARCH_ADMIN_EMAIL"),
                         os.environ.get("POINTERSEARCH_ADMIN_PASSWORD"), os.environ.get("POINTERSEARCH_ADMIN_NAME"))
    print(f"pointersearch planner on http://{a.host}:{a.port}/  (data: {Path(a.data).resolve()})")
    # behind a hosting provider's proxy: trust its forwarded scheme and client address
    uvicorn.run(app, host=a.host, port=a.port, log_level="info", proxy_headers=True, forwarded_allow_ips="*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
