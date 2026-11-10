"""Individual logins for the hosted planner.

* Accounts are created by an administrator; there is no self sign-up.
* Passwords are stored as salted scrypt hashes, never in plain text.
* A login creates a random session token kept in an HttpOnly cookie; only its
  SHA-256 hash is stored, so a copy of the database cannot be replayed.
* Repeated failed logins are slowed down per account and per address.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SESSION_COOKIE = "ps_session"
SESSION_DAYS = 14
MIN_PASSWORD = 10
ROLES = ("member", "admin")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    active INTEGER NOT NULL DEFAULT 1,
    must_change INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_login REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


class AuthError(ValueError):
    """A problem the person can fix (shown to them as-is)."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${hash}".format(
        n=_SCRYPT["n"], r=_SCRYPT["r"], p=_SCRYPT["p"],
        salt=base64.b64encode(salt).decode(), hash=base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        expected_b = base64.b64decode(expected)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected_b))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected_b)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))      # keeps timing alike for unknown accounts


def check_password_rules(password: str):
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"use at least {MIN_PASSWORD} characters for the password")
    if len(password) > 200:
        raise AuthError("that password is too long")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _public(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"],
            "active": bool(row["active"]), "must_change": bool(row["must_change"]),
            "created_at": row["created_at"], "last_login": row["last_login"]}


class Throttle:
    """Slow down guessing: after 5 failures a key is locked for a growing time."""

    def __init__(self, free_attempts: int = 5, base_lock_s: float = 30.0, max_lock_s: float = 900.0):
        self.free, self.base, self.max = free_attempts, base_lock_s, max_lock_s
        self._d: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def wait_s(self, key: str, now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            fails, until = self._d.get(key, (0, 0.0))
        return max(0.0, until - now)

    def fail(self, key: str, now: float | None = None):
        now = time.time() if now is None else now
        with self._lock:
            fails = self._d.get(key, (0, 0.0))[0] + 1
            lock = 0.0 if fails < self.free else min(self.max, self.base * 2 ** (fails - self.free))
            self._d[key] = (fails, now + lock)
            if len(self._d) > 10000:                      # bound memory under a flood
                for k in list(self._d)[:5000]:
                    self._d.pop(k, None)

    def ok(self, key: str):
        with self._lock:
            self._d.pop(key, None)


class Users:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._lock = threading.Lock()
        self.throttle = Throttle()

    # ---- accounts ------------------------------------------------------------
    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def list(self) -> list[dict]:
        return [_public(r) for r in self.conn.execute("SELECT * FROM users ORDER BY active DESC, name COLLATE NOCASE")]

    def get(self, user_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _public(r) if r else None

    def create(self, email: str, name: str, password: str, role: str = "member", must_change: bool = True) -> dict:
        email, name = email.strip(), " ".join(name.split())
        if not _EMAIL_RE.match(email) or len(email) > 200:
            raise AuthError("enter a valid email address")
        if not name or len(name) > 80:
            raise AuthError("enter the person's name")
        if role not in ROLES:
            raise AuthError("role must be member or admin")
        check_password_rules(password)
        with self._lock:
            try:
                with self.conn:
                    cur = self.conn.execute(
                        "INSERT INTO users (email, name, pw_hash, role, must_change, created_at) VALUES (?,?,?,?,?,?)",
                        (email, name, hash_password(password), role, int(must_change), time.time()))
            except sqlite3.IntegrityError:
                raise AuthError("someone already has a login with that email address")
        return self.get(cur.lastrowid)

    def _active_admins(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1").fetchone()["n"]

    def update(self, user_id: int, *, name: str | None = None, role: str | None = None,
               active: bool | None = None, password: str | None = None) -> dict:
        cur = self.get(user_id)
        if cur is None:
            raise AuthError("no such person")
        loses_admin = cur["role"] == "admin" and cur["active"] and (
            (role is not None and role != "admin") or active is False)
        if loses_admin and self._active_admins() <= 1:
            raise AuthError("there must always be at least one active administrator")
        sets, args = [], []
        if name is not None:
            name = " ".join(name.split())
            if not name or len(name) > 80:
                raise AuthError("enter the person's name")
            sets.append("name=?"); args.append(name)
        if role is not None:
            if role not in ROLES:
                raise AuthError("role must be member or admin")
            sets.append("role=?"); args.append(role)
        if active is not None:
            sets.append("active=?"); args.append(int(active))
        if password is not None:
            check_password_rules(password)
            sets += ["pw_hash=?", "must_change=1"]; args.append(hash_password(password))
        if sets:
            with self._lock, self.conn:
                self.conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*args, user_id))
                if active is False or password is not None:          # sign them out everywhere
                    self.conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return self.get(user_id)

    def change_own_password(self, user_id: int, current: str, new: str, keep_token: str | None = None):
        row = self.conn.execute("SELECT pw_hash FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        if row is None or not verify_password(current, row["pw_hash"]):
            raise AuthError("the current password is not right")
        check_password_rules(new)
        if hmac.compare_digest(current, new):
            raise AuthError("choose a password different from the current one")
        with self._lock, self.conn:
            self.conn.execute("UPDATE users SET pw_hash=?, must_change=0 WHERE id=?", (hash_password(new), user_id))
            keep = _token_hash(keep_token) if keep_token else ""
            self.conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user_id, keep))

    # ---- sessions --------------------------------------------------------------
    def login(self, email: str, password: str, client: str = "") -> tuple[str, dict]:
        """Returns (session token, user). Raises AuthError on any failure, with the
        same message whether the account exists or not."""
        email = email.strip()
        keys = [f"e:{email.lower()}", f"c:{client}"] if client else [f"e:{email.lower()}"]
        wait = max(self.throttle.wait_s(k) for k in keys)
        if wait > 0:
            raise AuthError(f"too many attempts; try again in {int(wait) + 1} seconds")
        row = self.conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        ok = verify_password(password, row["pw_hash"] if row else _DUMMY_HASH)
        if not (row and ok and row["active"]):
            for k in keys:
                self.throttle.fail(k)
            raise AuthError("the email address or password is not right")
        for k in keys:
            self.throttle.ok(k)
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
            self.conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                              (_token_hash(token), row["id"], now, now + SESSION_DAYS * 86400))
            self.conn.execute("UPDATE users SET last_login=? WHERE id=?", (now, row["id"]))
        return token, self.get(row["id"])

    def user_for_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        r = self.conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1", (_token_hash(token), time.time())).fetchone()
        return _public(r) if r else None

    def logout(self, token: str | None):
        if token:
            with self._lock, self.conn:
                self.conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))

    # ---- first administrator ---------------------------------------------------
    def bootstrap_admin(self, email: str | None, password: str | None, name: str = "Administrator") -> dict | None:
        """Create the first administrator when the user list is empty. Does nothing afterwards,
        so changing the environment variables later cannot reset anyone's password."""
        if self.count() or not email or not password:
            return None
        return self.create(email, name, password, role="admin", must_change=True)
