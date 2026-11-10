"""Passwords, API keys, CSRF tokens, login throttling, and request principals."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from . import db

# ----------------------------------------------------------------- passwords

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64),
                            n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


# ----------------------------------------------------------------- users

def get_user(user_id: int) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(r) if r else None


def get_user_by_name(username: str) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(r) if r else None


def list_users() -> list[dict]:
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM users ORDER BY username").fetchall()]


def get_user_by_oidc(sub: str) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT * FROM users WHERE oidc_sub=?", (sub,)).fetchone()
    return dict(r) if r else None


def set_oidc_sub(user_id: int, sub: str | None) -> None:
    with db.connect() as c:
        c.execute("UPDATE users SET oidc_sub=NULL WHERE oidc_sub=?", (sub,))
        c.execute("UPDATE users SET oidc_sub=? WHERE id=?", (sub, user_id))


def create_user(username: str, password: str | None, is_admin: bool, can_publish: bool = False,
                can_view_all: bool = False) -> int:
    """New accounts don't publish to Nextcloud and only see their own jobs' output unless allowed (Users page)."""
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at, can_publish, can_view_all) VALUES (?,?,?,?,?,?)",
            (username, hash_password(password) if password else None, int(is_admin), db.now(), int(can_publish),
             int(can_view_all)))
        return int(cur.lastrowid)


USER_FLAGS = ("is_admin", "can_view_all", "can_publish")


def set_user_flag(user_id: int, flag: str, value: bool) -> None:
    if flag not in USER_FLAGS:
        raise ValueError(flag)
    with db.connect() as c:
        c.execute(f"UPDATE users SET {flag}=? WHERE id=?", (int(value), user_id))


def set_can_publish(user_id: int, allowed: bool) -> None:
    set_user_flag(user_id, "can_publish", allowed)


def count_admins() -> int:
    with db.connect() as c:
        return int(c.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0])


def user_can_publish(user: dict | None) -> bool:
    return bool(user) and bool(user.get("can_publish", 1))


def set_password(user_id: int, password: str) -> None:
    with db.connect() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user_id))


def delete_user(user_id: int) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM users WHERE id=?", (user_id,))


def touch_login(user_id: int) -> None:
    with db.connect() as c:
        c.execute("UPDATE users SET last_login_at=? WHERE id=?", (db.now(), user_id))


def ensure_bootstrap_admin(username: str, password: str) -> str | None:
    """Create the first admin from .env when there are no users yet."""
    with db.connect() as c:
        count = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count:
        return None
    if not password:
        password = secrets.token_urlsafe(12)
        create_user(username, password, True, can_publish=True, can_view_all=True)
        return password  # caller logs it once
    create_user(username, password, True, can_publish=True, can_view_all=True)
    return None


# ----------------------------------------------------------------- API keys

def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_api_key(name: str, created_by: str) -> str:
    key = "ttrpg_" + secrets.token_urlsafe(32)
    with db.connect() as c:
        c.execute("INSERT INTO api_keys (name, prefix, key_hash, created_at, created_by) VALUES (?,?,?,?,?)",
                  (name, key[:12], _key_hash(key), db.now(), created_by))
    return key


def list_api_keys(created_by: str | None = None) -> list[dict]:
    """All keys, or only those one user created."""
    with db.connect() as c:
        if created_by is None:
            rows = c.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
        else:
            rows = c.execute("SELECT * FROM api_keys WHERE created_by=? COLLATE NOCASE ORDER BY created_at DESC",
                             (created_by,)).fetchall()
        return [dict(r) for r in rows]


def revoke_api_key(key_id: int, by_user: dict) -> bool:
    """Admins can revoke any key; other users only keys they created."""
    with db.connect() as c:
        if by_user["is_admin"]:
            cur = c.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (db.now(), key_id))
        else:
            cur = c.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL AND created_by=?",
                            (db.now(), key_id, by_user["username"]))
        return cur.rowcount == 1


def lookup_api_key(key: str) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT * FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (_key_hash(key),)).fetchone()
        if r:
            c.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (db.now(), r["id"]))
    return dict(r) if r else None


# ----------------------------------------------------------------- CSRF

def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request, supplied: str | None) -> None:
    expected = request.session.get("csrf")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid. Reload the page and try again.")


# ----------------------------------------------------------------- login throttle

class LoginThrottle:
    """At most `limit` failures per (ip, username) and per ip in `window` seconds."""

    def __init__(self, limit: int = 8, window: float = 900):
        self.limit, self.window = limit, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = time.time() - self.window
        lst = [t for t in self._fails.get(key, []) if t > cutoff]
        self._fails[key] = lst
        return lst

    def blocked(self, ip: str, username: str) -> bool:
        with self._lock:
            return (len(self._recent(f"{ip}|{username.lower()}")) >= self.limit
                    or len(self._recent(ip)) >= self.limit * 3)

    def fail(self, ip: str, username: str) -> None:
        with self._lock:
            t = time.time()
            self._fails.setdefault(f"{ip}|{username.lower()}", []).append(t)
            self._fails.setdefault(ip, []).append(t)

    def success(self, ip: str, username: str) -> None:
        with self._lock:
            self._fails.pop(f"{ip}|{username.lower()}", None)


throttle = LoginThrottle()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ----------------------------------------------------------------- principals

@dataclass
class Principal:
    kind: str          # "user" or "api_key"
    name: str
    is_admin: bool = False
    user_id: int | None = None
    can_publish: bool = True   # may this caller's jobs be published to Nextcloud?
    owner: str | None = None   # account the caller acts for (an API key acts for its creator)
    can_view_all: bool = False  # may see every job's output (admins always can)

    def owns(self, owner: str | None) -> bool:
        return bool(owner) and bool(self.owner) and owner.lower() == self.owner.lower()

    def can_see_output(self, job: dict) -> bool:
        return self.is_admin or self.can_view_all or self.owns(job.get("owner"))

    def can_manage(self, job: dict) -> bool:
        """Cancel, retry, delete, re-sync, upload to: the owner or an admin."""
        return self.is_admin or self.owns(job.get("owner"))

    @property
    def is_human(self) -> bool:
        return self.kind == "user"


def session_user(request: Request) -> dict | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = get_user(int(uid))
    if not user:
        request.session.clear()
    return user


def api_principal(request: Request) -> Principal:
    """For /api routes: API key (machines) or a logged-in session + CSRF header (dashboard JS)."""
    auth = request.headers.get("authorization", "")
    key = request.headers.get("x-api-key") or (auth[7:].strip() if auth.lower().startswith("bearer ") else "")
    if key:
        rec = lookup_api_key(key)
        if not rec:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")
        # A key publishes only if the account that created it may (a deleted creator can't).
        creator = get_user_by_name(rec["created_by"]) if rec.get("created_by") else None
        return Principal(kind="api_key", name=f"key:{rec['name']}", can_publish=user_can_publish(creator),
                         owner=creator["username"] if creator else None)
    user = session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        check_csrf(request, request.headers.get("x-csrf-token"))
    return principal_for_user(user)


def principal_for_user(user: dict) -> Principal:
    return Principal(kind="user", name=user["username"], is_admin=bool(user["is_admin"]), user_id=user["id"],
                     can_publish=user_can_publish(user), owner=user["username"],
                     can_view_all=bool(user["is_admin"]) or bool(user.get("can_view_all", 0)))


def human_only(p: Principal) -> None:
    if not p.is_human:
        raise HTTPException(status_code=403, detail="This action requires a dashboard login, not an API key")
