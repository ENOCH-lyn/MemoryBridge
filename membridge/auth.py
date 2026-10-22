"""Password, session and API-token auth for the public Hub.

Uses only the Python standard library (PBKDF2-HMAC-SHA256).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import new_id, utcnow

PBKDF2_ITERATIONS = 260_000
SESSION_DAYS = 7


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password cannot be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _unb64(salt), int(iterations)
        )
        return hmac.compare_digest(digest, _unb64(expected))
    except (ValueError, TypeError):
        return False


def ensure_default_admin(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    if int(row["n"]) == 0:
        now = utcnow()
        conn.execute(
            "INSERT INTO users(id, username, password_hash, role, must_change_password, created_at, updated_at) "
            "VALUES (?, 'admin', ?, 'admin', 1, ?, ?)",
            (new_id("usr"), hash_password("admin"), now, now),
        )
        conn.commit()


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    return dict(row)


def create_session(conn: sqlite3.Connection, user_id: str) -> str:
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SESSION_DAYS)
    conn.execute(
        "INSERT INTO auth_sessions(id, user_id, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
        (
            session_id,
            user_id,
            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )
    conn.commit()
    return session_id


def get_session(conn: sqlite3.Connection, session_id: str | None) -> dict[str, Any] | None:
    if not session_id:
        return None
    row = conn.execute(
        "SELECT s.*, u.username, u.role, u.must_change_password FROM auth_sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] < utcnow():
        conn.execute("DELETE FROM auth_sessions WHERE id = ?", (session_id,))
        conn.commit()
        return None
    conn.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE id = ?", (utcnow(), session_id))
    conn.commit()
    return dict(row)


def delete_session(conn: sqlite3.Connection, session_id: str | None) -> None:
    if session_id:
        conn.execute("DELETE FROM auth_sessions WHERE id = ?", (session_id,))
        conn.commit()


def change_password(conn: sqlite3.Connection, user_id: str, current: str, new: str) -> None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise ValueError("user not found")
    if not verify_password(current, row["password_hash"]):
        raise ValueError("current password is incorrect")
    if len(new) < 8:
        raise ValueError("new password must be at least 8 characters")
    if new == current:
        raise ValueError("new password must differ from current password")
    conn.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = ? WHERE id = ?",
        (hash_password(new), utcnow(), user_id),
    )
    conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
    conn.commit()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_api_token(conn: sqlite3.Connection, user_id: str, name: str) -> tuple[str, str]:
    raw = "mb_" + secrets.token_urlsafe(32)
    token_id = new_id("tok")
    conn.execute(
        "INSERT INTO api_tokens(id, user_id, name, token_hash, prefix, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (token_id, user_id, (name or "mcp").strip()[:80], _token_hash(raw), raw[:10], utcnow()),
    )
    conn.commit()
    return token_id, raw


def verify_api_token(conn: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT t.*, u.username, u.role, u.must_change_password FROM api_tokens t "
        "JOIN users u ON u.id = t.user_id WHERE t.token_hash = ? AND t.revoked_at IS NULL",
        (_token_hash(token),),
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (utcnow(), row["id"]))
    conn.commit()
    return dict(row)


def list_api_tokens(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, prefix, created_at, last_used_at, revoked_at FROM api_tokens "
        "WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def revoke_api_token(conn: sqlite3.Connection, user_id: str, token_id: str) -> bool:
    cur = conn.execute(
        "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (utcnow(), token_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0
