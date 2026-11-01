"""Quota, reviewer codes, and the signed visitor cookie. Standard library only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

COOKIE = "mb_id"
_TRUE = {"1", "true", "yes", "on"}


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _stamp(now: datetime) -> str:
    return _utc(now).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_bounds(now: datetime) -> tuple[str, str]:
    now = _utc(now)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return _stamp(start), _stamp(end)


def _next_midnight(now: datetime) -> datetime:
    now = _utc(now)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start + timedelta(days=1)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE


def _number(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


def new_visitor_id() -> str:
    return uuid.uuid4().hex


def hash_ip(ip: str, secret: str) -> str:
    """Salted hash. The raw address is not returned and must not be stored."""
    return hmac.new(secret.encode(), b"ip:" + ip.encode(), hashlib.sha256).hexdigest()


def sign_token(visitor_id: str, reviewer: bool, secret: str) -> str:
    payload = json.dumps(
        {"id": visitor_id, "rev": 1 if reviewer else 0}, separators=(",", ":")
    ).encode()
    body = _b64encode(payload)
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + _b64encode(sig)


def read_token(token: str, secret: str) -> tuple[str, bool] | None:
    """(visitor id, reviewer) or None when the cookie is missing or tampered."""
    if not token or not secret or token.count(".") != 1:
        return None
    body, sig_text = token.split(".", 1)
    try:
        given = _b64decode(sig_text)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, given):
        return None
    try:
        payload: Any = json.loads(_b64decode(body))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    visitor_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(visitor_id, str) or not visitor_id or len(visitor_id) > 64:
        return None
    return visitor_id, bool(payload.get("rev"))


@dataclass(frozen=True)
class AccessConfig:
    enabled: bool = False
    free_sessions: int = 5
    max_answers: int = 10
    daily_sessions: int = 30
    secret: str = ""
    codes: tuple[str, ...] = ()
    db_path: Path = Path("runs/access.sqlite")
    code_attempt_limit: int = 8
    code_window_s: int = 600

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AccessConfig:
        import os

        source = os.environ if env is None else env
        raw_codes = source.get("ACCESS_REVIEWER_CODES", "")
        codes = tuple(part.strip() for part in raw_codes.split(",") if part.strip())
        return cls(
            enabled=_flag(source, "ACCESS_ENABLED", False),
            free_sessions=_number(source, "ACCESS_FREE_SESSIONS", 5),
            max_answers=_number(source, "ACCESS_MAX_ANSWERS", 10),
            daily_sessions=_number(source, "ACCESS_DAILY_SESSIONS", 30),
            secret=source.get("ACCESS_SECRET", "").strip(),
            codes=codes,
            db_path=Path(source.get("ACCESS_DB", "runs/access.sqlite")),
        )


@dataclass(frozen=True)
class Identity:
    visitor_id: str
    ip_hash: str
    reviewer: bool


@dataclass
class Decision:
    allowed: bool
    reason: str | None
    used: int
    limit: int
    remaining: int | None
    reviewer: bool
    enabled: bool
    answers_per_interview: int
    daily_limit: int
    daily_used: int
    daily_remaining: int | None
    resets_at: str | None
    _commit: Any = field(default=None, repr=False, compare=False)

    def commit(self) -> bool:
        """Count this session. False means it must not proceed.

        Disabled mode and reviewers return True and write nothing. A test
        visitor writes one row, and only if the quota still holds at this moment.
        """
        if self._commit is None:
            return bool(self.allowed)
        return bool(self._commit())

    def public(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reviewer": self.reviewer,
            "remaining": self.remaining,
            "limit": self.limit,
            "used": self.used,
            "answers_per_interview": self.answers_per_interview,
            "daily_limit": self.daily_limit,
            "daily_used": self.daily_used,
            "daily_remaining": self.daily_remaining,
            "resets_at": self.resets_at,
        }


class Limits:
    def __init__(self, config: AccessConfig, *, clock: Callable[[], datetime] | None = None):
        self.config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        if not config.enabled:
            return
        if not config.secret:
            raise ValueError("ACCESS_SECRET is required when ACCESS_ENABLED is on")
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(config.db_path), check_same_thread=False)
        self._db.isolation_level = None  # we open transactions ourselves
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                visitor_id TEXT NOT NULL,
                ip_hash TEXT NOT NULL,
                counted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS code_attempts (
                ip_hash TEXT NOT NULL,
                at TEXT NOT NULL
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def admit(self, identity: Identity, now: datetime) -> Decision:
        cfg = self.config
        resets = _stamp(_next_midnight(now))
        base = dict(
            reviewer=identity.reviewer,
            enabled=cfg.enabled,
            answers_per_interview=cfg.max_answers,
            daily_limit=cfg.daily_sessions,
            limit=cfg.free_sessions,
        )
        if not cfg.enabled:
            return Decision(
                allowed=True, reason=None, used=0, remaining=None,
                daily_used=0, daily_remaining=None, resets_at=None,
                _commit=lambda: True, **base,
            )
        if identity.reviewer:
            return Decision(
                allowed=True, reason=None, used=0, remaining=None,
                daily_used=0, daily_remaining=None, resets_at=resets,
                _commit=lambda: True, **base,
            )
        with self._lock:
            used, daily = self._counts(identity, now)
        reason, allowed = _reason(used, daily, cfg)
        return Decision(
            allowed=allowed, reason=reason, used=used,
            remaining=max(0, cfg.free_sessions - used),
            daily_used=daily, daily_remaining=max(0, cfg.daily_sessions - daily),
            resets_at=resets,
            _commit=lambda: self._commit(identity, self._clock()),
            **base,
        )

    def redeem(self, ip_hash: str, code: str, now: datetime) -> bool:
        """True when `code` matches a reviewer code. Failures are rate limited.

        A correct code is still refused during the lockout, so guessing cannot
        sprint through the list. The code itself is never written to the database.
        """
        cfg = self.config
        cleaned = code.strip()
        if not cfg.enabled or self._db is None or not cleaned or not cfg.codes:
            return False
        given = hashlib.sha256(cleaned.encode()).digest()
        matched = False
        for candidate in cfg.codes:
            digest = hashlib.sha256(candidate.encode()).digest()
            # compare_digest runs for every code; `or matched` does not skip it.
            matched = hmac.compare_digest(digest, given) or matched
        now = _utc(now)
        with self._lock:
            failures = self._recent_failures(ip_hash, now)
            if failures >= cfg.code_attempt_limit or not matched:
                if failures < cfg.code_attempt_limit:
                    self._record_failure(ip_hash, now)
                return False
        return True

    def _commit(self, identity: Identity, now: datetime) -> bool:
        if not self.config.enabled or identity.reviewer or self._db is None:
            return True
        now = _utc(now)
        with self._lock:
            assert self._db is not None
            self._db.execute("BEGIN IMMEDIATE")
            try:
                used, daily = self._counts(identity, now)
                if used >= self.config.free_sessions or daily >= self.config.daily_sessions:
                    self._db.execute("ROLLBACK")
                    return False
                self._db.execute(
                    "INSERT INTO sessions (visitor_id, ip_hash, counted_at) VALUES (?, ?, ?)",
                    (identity.visitor_id, identity.ip_hash, _stamp(now)),
                )
                self._db.commit()
                return True
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _counts(self, identity: Identity, now: datetime) -> tuple[int, int]:
        assert self._db is not None
        start, end = _day_bounds(now)
        used = self._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE visitor_id = ? OR ip_hash = ?",
            (identity.visitor_id, identity.ip_hash),
        ).fetchone()[0]
        daily = self._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE counted_at >= ? AND counted_at < ?",
            (start, end),
        ).fetchone()[0]
        return int(used), int(daily)

    def _recent_failures(self, ip_hash: str, now: datetime) -> int:
        assert self._db is not None
        cutoff = _stamp(now - timedelta(seconds=self.config.code_window_s))
        row = self._db.execute(
            "SELECT COUNT(*) FROM code_attempts WHERE ip_hash = ? AND at >= ?",
            (ip_hash, cutoff),
        ).fetchone()
        return int(row[0])

    def _record_failure(self, ip_hash: str, now: datetime) -> None:
        assert self._db is not None
        cutoff = _stamp(now - timedelta(seconds=self.config.code_window_s))
        self._db.execute("DELETE FROM code_attempts WHERE at < ?", (cutoff,))
        self._db.execute(
            "INSERT INTO code_attempts (ip_hash, at) VALUES (?, ?)",
            (ip_hash, _stamp(now)),
        )
        self._db.commit()


def _reason(used: int, daily: int, cfg: AccessConfig) -> tuple[str | None, bool]:
    if used >= cfg.free_sessions:
        return "free_used", False
    if daily >= cfg.daily_sessions:
        return "daily_full", False
    return None, True
