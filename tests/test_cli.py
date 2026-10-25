"""End-to-end text session through the CLI in --fake mode (offline), and the trace file."""

from __future__ import annotations

import json

from mockboard import cli
from mockboard.config import REPO_ROOT
from mockboard.session import split_segments

SCRIPT = REPO_ROOT / "examples" / "fake_session.txt"


def test_split_segments():
    segs = split_segments("One. Two. Three.", "t0")
    assert [s.text for s in segs] == ["One. Two.", "Three."]
    assert [s.text for s in split_segments("a b | c d", "t0")] == ["a b", "c d"]
    assert split_segments("", "t0")[0].text == ""


def test_cli_fake_end_to_end(tmp_path, capsys):
    report_path = tmp_path / "report.json"
    rc = cli.main(
        [
            "--fake",
            "--script",
            str(SCRIPT),
            "--session-id",
            "pytest-sess",
            "--trace-dir",
            str(tmp_path),
            "--report-json",
            str(report_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "(fact_challenge, R4)" in out
    assert "REPORT" in out and "SYNTHETIC TEST CARD" in out

    rows = [json.loads(x) for x in (tmp_path / "pytest-sess.jsonl").read_text().splitlines()]
    assert rows[0]["event"] == "session_start" and rows[-1]["event"] == "report"
    turns = [r for r in rows if "turn_id" in r]
    assert turns and all(r["prompt_versions"] for r in turns)
    for r in turns:
        stages = r["stages_ms"]
        for stage in ("end_of_turn", "analysis_done", "move_ready", "actor_request", "actor_done"):
            assert stage in stages
        assert stages["move_ready"] <= stages["actor_request"] <= stages["actor_done"]
        assert all(c["model_id"] == "fake-llm" for c in r["llm_calls"])

    challenge = next(r for r in turns if r["move"] == "fact_challenge")
    line = challenge["board_line"].strip()
    assert line.endswith("?") and line[0].isupper()
    assert "test card" not in line.lower()  # the card text is never read aloud (M3)
    assert line.index("30 percent") < line.index("12 percent") and "2023" in line
    assert challenge["target"]
    # The challenged claim came from an EARLIER turn (delayed challenge).
    assert not challenge["target"].startswith(challenge["turn_id"] + ".")

    report = json.loads(report_path.read_text())
    assert report["report_language"] == "en" and len(report["scores"]) == 9
    contradicted = [c for c in report["claims"] if c["verdict"] == "contradicted"]
    assert contradicted and all(c["numeric_check"] == "mismatch" for c in contradicted)


def test_form_never_asks_for_identifiers():
    prompts = " ".join(p.lower() for _, p, _ in cli.FORM_FIELDS)
    for word in ("name", "email", "phone", "roll", "college"):
        assert word not in prompts.replace("no names", "")


def test_read_form_parses_hobbies():
    answers = iter(["Bihar", "", "History", "a, b, c, d", "", "", "Hindi"])
    form = cli.read_form(lambda _p: next(answers), use_defaults=False)
    assert form.hobbies == ["a", "b", "c"] and form.interview_language == "hindi"
    assert form.district is None
