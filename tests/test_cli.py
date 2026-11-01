"""End-to-end text session through the CLI in --fake mode (offline), and the trace file."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mockboard import cli
from mockboard.config import REPO_ROOT
from mockboard.session import split_segments

SCRIPT = REPO_ROOT / "examples" / "fake_session.txt"
ANSWERS = REPO_ROOT / "examples" / "fake_answers.txt"


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
        # M4 trace fields: the move's wait and fast-graph time; no deadline fallback offline.
        assert r["fallback_reason"] is None and r["deadline_s"] > 0
        assert r["move_wait_ms"] >= 0 and set(r["fast_graph_ms"]) == {"s0"}
        assert all(call["name"] != "librarian" for call in r["llm_calls"])  # not on the move path

    # Fact checks are traced separately (with their own time and librarian calls), on the
    # turn line or report line written after they finished.
    fact_checks = [f for r in rows for f in r.get("fact_checks") or []]
    assert fact_checks and all(f["fact_check_ms"] >= 0 for f in fact_checks)
    assert all(c["name"] == "librarian" for f in fact_checks for c in f["llm_calls"])

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


def test_typed_answer_is_one_segment_unless_split():
    assert cli.typed_segments("One. Two. Three.") == ["One. Two. Three."]
    assert cli.typed_segments("a b | c d") == ["a b", "c d"]
    assert cli.typed_segments("   ") == []  # silence: no segment at all


def test_form_never_asks_for_identifiers():
    prompts = " ".join(p.lower() for _, p, _ in cli.FORM_FIELDS)
    for word in ("name", "email", "phone", "roll", "college"):
        assert word not in prompts.replace("no names", "")


def test_read_form_parses_hobbies():
    answers = iter(["Bihar", "", "History", "a, b, c, d", "", "", "Hindi"])
    form = cli.read_form(lambda _p: next(answers), use_defaults=False)
    assert form.hobbies == ["a", "b", "c"] and form.interview_language == "hindi"
    assert form.district is None


SAMPLE_FORM = REPO_ROOT / "examples" / "forms" / "sample_form.json"


def write_form(tmp_path, doc) -> Path:
    path = tmp_path / "form.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


def test_sample_form_is_the_default_fake_form_plus_an_optional_subject():
    form = cli.load_form_file(SAMPLE_FORM)
    expected = cli.CandidateForm.model_validate(cli._form_values(cli.DEFAULT_FAKE_FORM))
    assert form.optional_subject == "Public Administration"
    assert form.model_copy(update={"optional_subject": None}) == expected


def test_form_file_rejects_an_unknown_field_without_echoing_its_value(tmp_path):
    good = json.loads(SAMPLE_FORM.read_text(encoding="utf-8"))
    path = write_form(tmp_path, {**good, "name": "Synthetic Person"})
    with pytest.raises(SystemExit) as exc:
        cli.load_form_file(path)
    assert "name" in str(exc.value) and "Synthetic Person" not in str(exc.value)


@pytest.mark.parametrize(
    "content",
    ["{not json", '["a", "list"]', '{"home_state": "Uttar Pradesh"}'],
    ids=["bad-json", "not-an-object", "missing-required"],
)
def test_form_file_refuses_a_bad_document_with_a_readable_message(tmp_path, content):
    with pytest.raises(SystemExit, match="form file"):
        cli.load_form_file(write_form(tmp_path, content))


def test_form_file_refuses_a_missing_file(tmp_path):
    with pytest.raises(SystemExit, match="form file"):
        cli.load_form_file(tmp_path / "absent.json")


def test_form_file_accepts_hobbies_as_text_and_any_language_case(tmp_path):
    path = write_form(
        tmp_path,
        {
            "home_state": "Kerala",
            "degree_subject": "History",
            "hobbies": "a, b",
            "interview_language": "HINDI",
        },
    )
    form = cli.load_form_file(path)
    assert form.hobbies == ["a", "b"] and form.interview_language == "hindi"


def test_cli_with_form_file_reads_answers_only_from_the_script(tmp_path, capsys):
    answers = tmp_path / "answers.txt"
    answers.write_text("Mujhe lagta hai local rozgaar zaroori hai.\n/quit\n", encoding="utf-8")
    rc = cli.main(
        ["--fake", "--form-file", str(SAMPLE_FORM), "--script", str(answers), "--no-trace"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "== Application form" not in out and "Home state:" not in out
    assert f"Application form from {SAMPLE_FORM}" in out
    assert "You: Mujhe lagta hai local rozgaar" in out


def test_a_bad_form_file_stops_before_any_llm_is_built(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "build_llm", lambda *a: pytest.fail("LLM built before the form"))
    path = write_form(tmp_path, {"home_state": "Kerala"})
    with pytest.raises(SystemExit, match="form file"):
        cli.main(["--form-file", str(path), "--no-trace"])


def test_debate_refuses_a_form_file(capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["--mode", "debate", "--form-file", str(SAMPLE_FORM)])
    assert "--form-file" in capsys.readouterr().err


@pytest.mark.parametrize(
    "hobbies",
    ["aa, bb, cc, zzqhobby", ["aa", "bb", "cc", "zzqhobby"]],
    ids=["text", "list"],
)
def test_form_file_refuses_more_than_three_hobbies_without_echoing_them(tmp_path, hobbies):
    good = json.loads(SAMPLE_FORM.read_text(encoding="utf-8"))
    path = write_form(tmp_path, {**good, "hobbies": hobbies})
    with pytest.raises(SystemExit, match="hobbies") as exc:
        cli.load_form_file(path)
    # A distinctive value: the message holds the temp path, so a short sentinel could match it.
    assert "zzqhobby" not in str(exc.value)


def test_form_file_accepts_three_hobbies_as_text(tmp_path):
    good = json.loads(SAMPLE_FORM.read_text(encoding="utf-8"))
    form = cli.load_form_file(write_form(tmp_path, {**good, "hobbies": "aa, bb, cc"}))
    assert form.hobbies == ["aa", "bb", "cc"]


def test_typed_forms_still_keep_the_first_three_hobbies():
    assert cli._form_values({"hobbies": "aa, bb, cc, dd"})["hobbies"] == ["aa", "bb", "cc"]


def test_answers_only_example_gives_the_same_board_lines_as_the_form_script(capsys):
    """`fake_answers.txt` is `fake_session.txt` without its seven form lines, and the sample form
    is that form: typing it and passing it as a file are the same interview (the golden's)."""
    assert "Uttar Pradesh" not in ANSWERS.read_text(encoding="utf-8").splitlines()
    assert ANSWERS.read_text(encoding="utf-8") == "\n".join(
        (REPO_ROOT / "examples" / "fake_session.txt").read_text(encoding="utf-8").split("\n")[7:]
    )
    rc = cli.main(
        ["--fake", "--form-file", str(SAMPLE_FORM), "--script", str(ANSWERS), "--no-trace",
         "--session-id", "golden-sess"]
    )
    out = capsys.readouterr().out
    board = "".join(line + "\n" for line in out.splitlines() if line.startswith("["))
    assert rc == 0
    assert hashlib.md5(board.encode("utf-8")).hexdigest() == "e748940f8faa2863b5891d3c71532a67"


# --- Ruling 73 item 5: the panel's turn cap is its agenda, so --max-turns must not pretend ------


def test_upsc_refuses_an_explicit_max_turns(capsys):
    """The panel's turn cap is its agenda (Ruling 73 item 5), and since P7 every UPSC run is the
    panel, with or without the old `--panel` spelling."""
    for extra in ([], ["--panel"]):
        with pytest.raises(SystemExit):
            cli.parse_args(["--fake", *extra, "--max-turns", "4"])
        err = capsys.readouterr().err
        assert "--max-turns" in err and "--max-answers" in err


def test_max_turns_still_works_for_debate_and_defaults_to_nine():
    assert cli.parse_args(["--mode", "debate", "--fake"]).max_turns == 9
    assert cli.parse_args(["--mode", "debate", "--fake", "--max-turns", "4"]).max_turns == 4
    assert cli.parse_args(["--fake"]).max_turns == 9  # unused: the agenda decides


def test_panel_is_a_hidden_no_op_alias_since_the_cutover():
    args = cli.parse_args(["--fake", "--panel"])
    assert args.panel and args.mode == "upsc_interview"
    with pytest.raises(SystemExit):
        cli.parse_args(["--mode", "debate", "--fake", "--panel"])


def test_max_turns_help_states_the_default_and_keeps_the_panel_hidden(capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--max-turns" in help_text and "default 9" in help_text
    assert "--panel" not in help_text  # test_the_help_text_does_not_advertise_the_panel
