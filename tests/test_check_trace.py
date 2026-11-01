"""P11: the panel trace checker passes a good trace and fails a tampered one."""

from __future__ import annotations

import json
from pathlib import Path

from eval.panel.check_trace import check_trace, main


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _turn(tid, segment, member, *, route="next_question", counted=True, **panel):
    return {
        "session_id": "s", "turn_id": tid,
        "panel": {
            "segment_id": segment, "member": member, "route": route, "counted": counted,
            "max_answers": 10, **panel,
        },
    }


def test_a_good_panel_trace_passes(tmp_path):
    path = _write(tmp_path / "good.jsonl", [
        {"event": "session_start", "mode": "upsc_interview", "session_id": "s"},
        _turn("t0", "form_hr", "chairman"),
        _turn("t1", "home_district", "administrator", route="handoff"),
        _turn("t2", "home_district", "administrator", candidate_request="change_topic",
              avoid_terms=["tourism"]),
        _turn("t3", "degree", "economist", route="handoff"),
        _turn("t4", "degree", "economist", route="close", close_reason="agenda"),
        {"event": "report", "session_id": "s"},
    ])
    assert check_trace(path) == []
    assert main([str(path)]) == 0


def test_a_missing_member_fails(tmp_path):
    path = _write(tmp_path / "bad.jsonl", [
        {"event": "session_start", "mode": "upsc_interview"},
        _turn("t0", "form_hr", ""),
        {"event": "report"},
    ])
    problems = check_trace(path)
    assert any("member" in p for p in problems)


def test_a_refused_term_that_returns_fails(tmp_path):
    path = _write(tmp_path / "back.jsonl", [
        {"event": "session_start", "mode": "upsc_interview"},
        _turn("t0", "home_district", "administrator", candidate_request="change_topic",
              avoid_terms=["tourism"]),
        _turn("t1", "degree", "economist", route="handoff",
              parts=[{"member": "economist", "text": "Let us talk about tourism next."}]),
        {"event": "report"},
    ])
    problems = check_trace(path)
    assert any("tourism" in p for p in problems)


def test_over_the_cap_fails(tmp_path):
    rows = [{"event": "session_start", "mode": "upsc_interview"}]
    rows += [_turn(f"t{n}", "form_hr", "chairman") for n in range(11)]
    rows.append({"event": "report"})
    path = _write(tmp_path / "cap.jsonl", rows)
    assert any("cap" in p for p in check_trace(path))


def test_a_debate_trace_is_skipped(tmp_path):
    path = _write(tmp_path / "deb.jsonl", [
        {"event": "session_start", "mode": "debate", "prompt_versions": {"listener_debate": 1}},
        {"turn_id": "t0", "move": "opening_statement"},
    ])
    assert check_trace(path) == []
