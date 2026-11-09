"""Access limits: quota, reviewer bypass, the daily cap, cookies, and a reload.

No browser and no speech service. The route checks need the voice extra and skip
without it. They refuse before any WebRTC work, so a bad SDP never matters.
"""

from __future__ import annotations

import base64
import importlib.util
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mockboard.access import (
    COOKIE,
    AccessConfig,
    Identity,
    Limits,
    hash_ip,
    read_token,
    sign_token,
)
from mockboard.access.limits import REVIEWER_TTL_S

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
IP = "203.0.113.44"
SECRET = "test-secret-not-a-real-key-32-chars"
CODE = "reviewer-code-nine"


def _cfg(tmp_path: Path, **kwargs) -> AccessConfig:
    base = dict(
        enabled=True,
        secret=SECRET,
        db_path=tmp_path / "access.sqlite",
        free_sessions=5,
        max_answers=10,
        daily_sessions=30,
        codes=(CODE,),
    )
    base.update(kwargs)
    return AccessConfig(**base)


def _person(visitor: str = "visitor-a", ip: str = IP, *, reviewer: bool = False) -> Identity:
    return Identity(visitor, hash_ip(ip, SECRET), reviewer)


def _book(tmp_path: Path, **kwargs) -> Limits:
    return Limits(_cfg(tmp_path, **kwargs), clock=lambda: NOW)


def _spend(book: Limits, identity: Identity, n: int, now: datetime = NOW) -> None:
    for _ in range(n):
        decision = book.admit(identity, now)
        assert decision.allowed, decision.reason
        assert decision.commit()


def test_from_env_defaults_to_off(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("ACCESS_"):
            monkeypatch.delenv(key, raising=False)
    cfg = AccessConfig.from_env()
    assert cfg.enabled is False
    assert cfg.free_sessions == 5
    assert cfg.max_answers == 10
    assert cfg.daily_sessions == 30
    assert cfg.codes == ()


def test_disabled_mode_allows_everyone_and_writes_nothing(tmp_path):
    path = tmp_path / "access.sqlite"
    book = Limits(_cfg(tmp_path, enabled=False, db_path=path))
    decision = book.admit(_person(), NOW)
    assert decision.allowed and decision.enabled is False
    assert decision.commit() is True
    assert decision.public()["remaining"] is None
    assert not path.exists()


def test_enabled_without_a_secret_refuses_to_start(tmp_path):
    with pytest.raises(ValueError):
        Limits(_cfg(tmp_path, secret=""))


def test_quota_reached_on_the_sixth_interview(tmp_path):
    book = _book(tmp_path)
    who = _person()
    _spend(book, who, 5)
    blocked = book.admit(who, NOW)
    assert blocked.allowed is False
    assert blocked.reason == "free_used"
    assert blocked.used == 5 and blocked.limit == 5 and blocked.remaining == 0
    assert blocked.commit() is False


def test_a_shared_address_has_its_own_higher_limit(tmp_path):
    book = _book(tmp_path, ip_sessions=7)
    _spend(book, _person("first"), 5)
    second = book.admit(_person("second"), NOW)  # same IP hash, new id
    assert second.allowed and second.used == 0 and second.remaining == 5
    _spend(book, _person("second"), 2)
    third = book.admit(_person("third"), NOW)
    assert third.allowed is False and third.reason == "ip_full"
    assert third.used == 0  # a first visit is never told it used its interviews
    assert third.commit() is False
    assert book.admit(_person("third", ip="198.51.100.7"), NOW).allowed


def test_a_new_network_does_not_reset_the_same_cookie(tmp_path):
    book = _book(tmp_path)
    _spend(book, _person(ip="203.0.113.10"), 5)
    moved = book.admit(_person(ip="203.0.113.11"), NOW)
    assert moved.allowed is False
    assert moved.reason == "free_used"


def test_one_connected_session_counts_once(tmp_path):
    book = _book(tmp_path)
    who = _person()
    assert book.admit(who, NOW).commit()
    again = book.admit(who, NOW)
    assert again.used == 1 and again.remaining == 4


def test_reload_is_not_counted_until_commit(tmp_path):
    book = _book(tmp_path)
    who = _person()
    first = book.admit(who, NOW)
    second = book.admit(who, NOW)  # the reload, still not connected
    assert first.used == 0 and second.used == 0 and second.allowed
    assert second.commit()
    assert book.admit(who, NOW).used == 1
    assert first.used == 0  # admitting the reload did not increment


def test_two_commits_when_one_seat_remains_count_only_one(tmp_path):
    book = _book(tmp_path, free_sessions=1, daily_sessions=30)
    who = _person()
    first = book.admit(who, NOW)
    second = book.admit(who, NOW)
    assert first.commit() is True
    assert second.commit() is False
    assert book.admit(who, NOW).used == 1


def test_reviewer_bypass_ignores_session_and_daily_caps(tmp_path):
    book = _book(tmp_path, free_sessions=1, daily_sessions=1)
    _spend(book, _person("ordinary", ip="198.51.100.2"), 1)
    ordinary = book.admit(_person("ordinary", ip="198.51.100.2"), NOW)
    assert ordinary.reason == "free_used"
    reviewer = book.admit(_person("owner", ip="198.51.100.9", reviewer=True), NOW)
    assert reviewer.allowed and reviewer.reason is None
    assert reviewer.commit() is True
    # The reviewer's sitting is not a row, so the daily cap stays where it was.
    someone_else = book.admit(_person("next", ip="198.51.100.3"), NOW)
    assert someone_else.reason == "daily_full"
    assert someone_else.daily_used == 1


def test_daily_cap_is_shared_by_every_non_reviewer(tmp_path):
    book = _book(tmp_path, free_sessions=5, daily_sessions=2)
    _spend(book, _person("a", ip="198.51.100.1"), 1)
    _spend(book, _person("b", ip="198.51.100.2"), 1)
    refused = book.admit(_person("c", ip="198.51.100.3"), NOW)
    assert refused.allowed is False and refused.reason == "daily_full"
    assert refused.used == 0  # this visitor still has personal interviews left
    tomorrow = NOW + timedelta(days=1)
    assert book.admit(_person("c", ip="198.51.100.3"), tomorrow).allowed


def test_no_raw_ip_and_no_code_is_stored(tmp_path):
    book = _book(tmp_path)
    who = _person()
    assert book.admit(who, NOW).commit()
    assert book.redeem(who.ip_hash, "wrong-code", NOW) is False
    assert book.redeem(who.ip_hash, CODE, NOW) is True
    book.close()
    blob = (tmp_path / "access.sqlite").read_bytes()
    wal = tmp_path / "access.sqlite-wal"
    if wal.exists():
        blob += wal.read_bytes()
    assert IP.encode() not in blob
    assert CODE.encode() not in blob
    assert b"wrong-code" not in blob
    assert who.ip_hash.encode() in blob


def test_tampered_cookie_is_not_a_reviewer():
    token = sign_token("visitor-a", False, SECRET)
    assert read_token(token, SECRET) == ("visitor-a", False)
    forged = sign_token("visitor-a", True, SECRET)
    body, _sig = forged.split(".", 1)
    tampered = body + "." + token.split(".", 1)[1]
    assert read_token(tampered, SECRET) is None
    flipped = token[:-1] + ("a" if not token.endswith("a") else "b")
    assert read_token(flipped, SECRET) is None
    assert read_token(token, "other-secret") is None


def test_reviewer_token_expires_and_dies_with_its_code():
    token = sign_token("owner", True, SECRET, code=CODE, now=NOW)
    assert read_token(token, SECRET, codes=(CODE,), now=NOW) == ("owner", True)
    assert read_token(token, SECRET, codes=("other",), now=NOW) == ("owner", False)
    assert read_token(token, SECRET, now=NOW) == ("owner", False)
    late = NOW + timedelta(seconds=REVIEWER_TTL_S + 1)
    assert read_token(token, SECRET, codes=(CODE,), now=late) == ("owner", False)
    assert CODE.encode() not in base64.urlsafe_b64decode(token.split(".")[0] + "===")


def test_a_short_secret_refuses_to_start(tmp_path):
    with pytest.raises(ValueError, match="32"):
        Limits(_cfg(tmp_path, secret="x" * 31))


@pytest.mark.parametrize("value", ["enabled", "tru", "2"])
def test_an_unrecognised_enabled_value_refuses_to_start(value: str):
    with pytest.raises(ValueError, match="ACCESS_ENABLED"):
        AccessConfig.from_env({"ACCESS_ENABLED": value})


@pytest.mark.parametrize("value", ["false", "off", "0", "no", ""])
def test_a_clear_false_stays_off(value: str):
    assert AccessConfig.from_env({"ACCESS_ENABLED": value}).enabled is False


def _clear_access(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("ACCESS_"):
            monkeypatch.delenv(key, raising=False)


def test_dotenv_turns_limits_on(tmp_path, monkeypatch):
    _clear_access(monkeypatch)
    (tmp_path / ".env").write_text(
        f"ACCESS_ENABLED=true\nACCESS_SECRET={SECRET}\nACCESS_REVIEWER_CODES=a, b\n"
    )
    monkeypatch.chdir(tmp_path)
    cfg = AccessConfig.from_env()
    assert cfg.enabled and cfg.secret == SECRET and cfg.codes == ("a", "b")


def test_dotenv_without_a_secret_refuses_to_start(tmp_path, monkeypatch):
    _clear_access(monkeypatch)
    (tmp_path / ".env").write_text("ACCESS_ENABLED=true\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        Limits(AccessConfig.from_env())


def test_the_real_environment_outranks_dotenv(tmp_path, monkeypatch):
    _clear_access(monkeypatch)
    (tmp_path / ".env").write_text("ACCESS_ENABLED=true\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACCESS_ENABLED", "false")
    assert AccessConfig.from_env().enabled is False


def test_a_test_visitor_is_capped_and_others_are_not(tmp_path):
    book = _book(tmp_path, max_answers=7)
    assert book.admit(_person(), NOW).max_answers == 7
    assert book.admit(_person(reviewer=True), NOW).max_answers is None
    off = Limits(_cfg(tmp_path, enabled=False))
    assert off.admit(_person(), NOW).max_answers is None


def test_check_code_tells_wrong_from_locked(tmp_path):
    book = _book(tmp_path, code_attempt_limit=2)
    ip_hash = hash_ip(IP, SECRET)
    assert book.check_code(ip_hash, CODE, NOW) == "ok"
    assert book.check_code(ip_hash, "nope", NOW) == "wrong"
    assert book.check_code(ip_hash, "nope", NOW) == "wrong"
    assert book.check_code(ip_hash, CODE, NOW) == "locked"


def test_wrong_codes_are_rate_limited_and_the_code_is_compared_in_full(tmp_path):
    book = _book(tmp_path, code_attempt_limit=3, code_window_s=600)
    ip_hash = hash_ip(IP, SECRET)
    assert book.redeem(ip_hash, "nope", NOW) is False
    assert book.redeem(ip_hash, "nope", NOW) is False
    assert book.redeem(ip_hash, "nope", NOW) is False
    assert book.redeem(ip_hash, CODE, NOW) is False  # lockout holds even for the real code
    later = NOW + timedelta(seconds=601)
    assert book.redeem(ip_hash, CODE, later) is True
    book.close()
    blob = (tmp_path / "access.sqlite").read_bytes()
    assert CODE.encode() not in blob


def test_a_blank_code_does_not_burn_an_attempt(tmp_path):
    book = _book(tmp_path, code_attempt_limit=1)
    ip_hash = hash_ip(IP, SECRET)
    assert book.redeem(ip_hash, "   ", NOW) is False
    assert book.redeem(ip_hash, CODE, NOW) is True


@pytest.mark.skipif(
    importlib.util.find_spec("pipecat") is None, reason="voice extra is not installed"
)
def test_offer_refuses_before_webrtc_and_the_page_can_read_status(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("ACCESS_ENABLED", "true")
    monkeypatch.setenv("ACCESS_SECRET", SECRET)
    monkeypatch.setenv("ACCESS_FREE_SESSIONS", "1")
    monkeypatch.setenv("ACCESS_DAILY_SESSIONS", "30")
    monkeypatch.setenv("ACCESS_REVIEWER_CODES", CODE)
    monkeypatch.setenv("ACCESS_DB", str(tmp_path / "access.sqlite"))

    from mockboard.voice import server

    book = Limits(AccessConfig.from_env())
    who = Identity("visitor-route", hash_ip("testclient", SECRET), False)
    assert book.admit(who, NOW).commit()
    book.close()

    argv = ["--mode", "upsc", "--fake-llm", "--fake-speech"]
    client_app = server.create_app(server.parse_args(argv))
    from fastapi.testclient import TestClient

    client = TestClient(client_app)
    token = sign_token("visitor-route", False, SECRET)
    offer = {
        "sdp": "not-a-real-offer",
        "type": "offer",
        "request_data": {"form": {"home_state": "Goa", "degree_subject": "History"},
                         "page_id": "page-1"},
    }
    refused = client.post("/api/offer", json=offer, cookies={COOKIE: token})
    assert refused.status_code == 429
    body = refused.json()
    assert body == {"error": "limit", "reason": "free_used", "used": 1, "limit": 1}

    fresh = client.get("/api/access")
    assert fresh.status_code == 200
    assert fresh.json()["enabled"] is True
    assert fresh.json()["answers_per_interview"] == 10
    assert fresh.json()["reason"] is None
    set_cookie = fresh.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie

    client.cookies.clear()
    token_r = sign_token("owner", True, SECRET, code=CODE)
    reviewer = client.get("/api/access", cookies={COOKIE: token_r})
    assert reviewer.json()["reviewer"] is True
    assert reviewer.json()["remaining"] is None
    assert reviewer.json()["answers_per_interview"] is None  # no answer-limit text

    client.cookies.clear()
    tampered = client.get("/api/access", cookies={COOKIE: token[:-1] + "x"})
    assert tampered.json()["reviewer"] is False


@pytest.mark.skipif(
    importlib.util.find_spec("pipecat") is None, reason="voice extra is not installed"
)
def test_likely_questions_are_counted_separately_and_reviewers_are_free(tmp_path):
    book = _book(tmp_path, utility_per_visitor=2)
    who = _person()
    for _ in range(2):
        decision = book.admit_utility(who, NOW, "likely_questions")
        assert decision.allowed and decision.commit()
    blocked = book.admit_utility(who, NOW, "likely_questions")
    assert blocked.allowed is False and blocked.reason == "likely_used"
    assert blocked.commit() is False
    reviewer = book.admit_utility(_person("owner", reviewer=True), NOW, "likely_questions")
    assert reviewer.allowed and reviewer.commit()
    assert book.admit(who, NOW).allowed  # interviews are a different count


def test_disabled_access_leaves_the_voice_app_unchanged(monkeypatch, settings):
    for key in ("ACCESS_ENABLED", "ACCESS_SECRET", "ACCESS_DB"):
        monkeypatch.delenv(key, raising=False)
    from fastapi.testclient import TestClient

    from mockboard.voice import server

    client = TestClient(server.create_app(
        server.parse_args(["--mode", "upsc", "--fake-llm", "--fake-speech"])))
    status = client.get("/api/access")
    assert status.status_code == 200
    assert status.json()["enabled"] is False
    assert "set-cookie" not in status.headers
    bad = client.post("/api/offer", json={
        "sdp": "x", "type": "offer", "request_data": {"form": {"home_state": "U"}},
    })
    assert bad.status_code == 400 and bad.json()["error"] == "form"
