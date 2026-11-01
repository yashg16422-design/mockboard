"""Modes as profiles (ADR-009): the registry, per-mode runtimes, and the golden transcripts.

The golden transcripts lock today's behaviour: `make cli-fake` and `make cli-fake-debate` with
a fixed session id must print exactly `tests/golden/upsc_cli_fake.txt` and
`tests/golden/debate_cli_fake.txt` (timings are not printed without --print-trace, and the
repository path and the frozen cards folder are normalised, so both files still read
`<repo>/data/cards`).

The debate lock (CTO_NOTES_06 section 7, Ruling 44) also hashes the shared prefix on both the
fake and the real path, the tool schemas, the prompt files, the debate's spoken templates, and
the ordered model requests of the scripted debate (system, user, token cap). The Panel phase
must not move any of them. If one of these tests fails, debate behaviour changed: find out why
before touching a hash.

Each model request is fingerprinted by call name, tag, system text, user message, token cap,
tool name and temperature (P0.2), so wiring debate to the UPSC listener tool or changing a
stream's temperature goes red. Three scripts are locked:
`examples/fake_debate.txt` (opening_statement, concede_point, move_on, closing_statement) and
the test-only `tests/data/debate_rebut_cross.txt` (cross_question D10, rebut D7 and D4, move_on
D6) and `tests/data/debate_fact_challenge.txt` (fact_challenge D9, cross_question D10, rebut
D12, which needs the synthetic cards and so only runs on the fake path).

What this does not cover: moves no script reaches (steelman, redirect, reassure, clarify,
interrupt), and anything a fake run cannot show, such as a spoken-line gate that only acts on
model text.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from conftest import LOCK_CARDS_DIR
from mockboard import cli
from mockboard.brain.deps import build_deps
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.llm import FakeCall
from mockboard.brain.prompts import PROMPT_DIR
from mockboard.brain.schemas import VoiceSignals
from mockboard.brain.strategist import decide_move
from mockboard.brain.tools import ALL_TOOLS
from mockboard.config import REPO_ROOT, Settings
from mockboard.modes import DEFAULT_MODE, get_profile, mode_ids
from mockboard.modes.debate import actor as debate_actor
from mockboard.modes.upsc import UPSC_MOVES
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

DEBATE_SCRIPT = REPO_ROOT / "examples" / "fake_debate.txt"
DEBATE_REBUT_SCRIPT = REPO_ROOT / "tests" / "data" / "debate_rebut_cross.txt"
DEBATE_FACT_SCRIPT = REPO_ROOT / "tests" / "data" / "debate_fact_challenge.txt"
GOLDEN = REPO_ROOT / "tests" / "golden" / "upsc_cli_fake.txt"
DEBATE_GOLDEN = REPO_ROOT / "tests" / "golden" / "debate_cli_fake.txt"
DEBATE_MODE = "debate"

# md5 of `cli --mode debate --fake --script examples/fake_debate.txt | grep '^\['`.
DEBATE_BOARD_LINES_MD5 = "f2a23f33b90df75103f505a2696e6944"
# sha256 values taken at 7d94936, before any Panel phase code.
DEBATE_PREFIX_SHA256 = "a36d6feb232b9f86209639a161be6a48465e1635140a7bf15a4a402659246555"
DEBATE_TOOLS_SHA256 = {
    "case_file": "9e7a7d51bf572501c0c21c599fab2fc58b3354f8878ab662d887c85ed476ca53",
    "judge_debate": "45b8d93c161ef0eaa2dfdd01bef73fa4194d69117f26ae98bc075fd396fb5425",
    "librarian": "9a896b0d5e9037d1ecbfb6084d10ececbeb982011c4b88fe62a52152497b23c8",
    "listener_debate": "7cc7f0a31f69cba5e15a56c473ac233054c1a289708a473efed31756872be729",
    "motion_screen": "05fe59ca636bc96bd88176470d232cba6fd4a593fa4d0039a8429f4d4d80f98f",
}
DEBATE_PROMPTS_SHA256 = "c03eda960fc0f9339d5b176060be7e378ef579926f54b6ff8037fcbf2e0699a9"
# Taken at 620eac9 (no debate code changed since 7d94936), with the fake recording stream caps.
DEBATE_REAL_PREFIX_SHA256 = "1a53bb5e6c502ffdd7be3af0a14e0ee05625dd593ba4bac6574d75422035e9a9"
DEBATE_TEMPLATES_SHA256 = "145ed67115387a42c421a24cfb3dba75650d2bc6b9b599909ff973a82b5471d4"
# Taken at 37baedf (no debate code changed since 7d94936). The request fingerprint now also
# holds the tool name and the temperature (P0.2); the row it replaced had the same first five
# columns, and the old five-column hash of the same run (3d2df9cf...) was re-derived first to
# prove the run itself had not moved.
DEBATE_REQUESTS_SHA256 = "60b1a1a5c618a7cf5dec88002263a96feacdc3773f975233be7a5912f8f48b32"
DEBATE_REBUT_REQUESTS_SHA256 = "d02fe2d00435f3fafd3a9bc3d501a70ecf75ad1a72b5bf7f063098ebec6d1ebf"
DEBATE_FACT_REQUESTS_SHA256 = "191eb84a7d80035487cf49ffef0284893dd0091e796e5ab77780e90426892373"
LISTENER_TOOL_NAME = "record_debate_analysis"
DEBATE_REQUEST_SHAPE = [
    ("listener", "t0", 1200),
    ("actor", "t0", 210),
    ("listener", "t1", 1200),
    ("actor", "t1", 135),
    ("listener", "t2", 1200),
    ("actor", "t2", 120),
    ("listener", "t3", 1200),
    ("actor", "t3", 180),
    ("judge_debate", "golden-debate", 2500),
]


def normalise_paths(out: str) -> str:
    frozen_cards = str(Settings(_env_file=None).cards_dir)
    return out.replace(frozen_cards, "<repo>/data/cards").replace(str(REPO_ROOT), "<repo>")


def run_upsc_cli(tmp_path, capsys, *extra: str) -> str:
    rc = cli.main(
        [
            "--fake",
            "--script",
            str(REPO_ROOT / "examples" / "fake_session.txt"),
            "--session-id",
            "golden-sess",
            "--no-trace",
            *extra,
        ]
    )
    assert rc == 0
    return normalise_paths(capsys.readouterr().out)


def run_debate_cli(capsys, *extra: str, script=DEBATE_SCRIPT) -> str:
    rc = cli.main(
        [
            "--mode",
            DEBATE_MODE,
            "--fake",
            "--script",
            str(script),
            "--session-id",
            "golden-debate",
            "--no-trace",
            *extra,
        ]
    )
    assert rc == 0
    return normalise_paths(capsys.readouterr().out)


def sha256(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(raw).hexdigest()


def tool_sha256(tool_id: str) -> str:
    tool = ALL_TOOLS[tool_id][0]
    doc = {"name": tool.name, "description": tool.description, "schema": tool.input_schema}
    return sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False))


def prompts_sha256(versions: dict[str, int]) -> str:
    """The version map together with each versioned prompt file's bytes."""
    files = {
        prompt_id: sha256((PROMPT_DIR / f"{prompt_id}.v{version}.md").read_bytes())
        for prompt_id, version in versions.items()
    }
    return sha256(json.dumps({"versions": versions, "files": files}, sort_keys=True))


def test_default_mode_is_upsc_and_wraps_todays_rules():
    assert DEFAULT_MODE == "upsc_interview"
    assert DEFAULT_MODE in mode_ids()
    profile = get_profile(None)
    assert profile.id == DEFAULT_MODE
    assert profile.decide is decide_move
    assert profile.allowed_moves == UPSC_MOVES and len(UPSC_MOVES) == 12
    assert profile.prompt_ids == {
        "shared": "shared",
        "listener": "listener",
        "actor": "actor",
        "judge": "judge",
        "opener": "opener",
    }


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="unknown mode"):
        get_profile("karaoke")


def test_deps_builds_one_runtime_per_mode_and_upsc_reuses_the_default_kit(deps):
    assert set(deps.modes) == set(mode_ids())
    upsc = deps.mode(DEFAULT_MODE)
    assert upsc.kit is deps.kit and upsc.strategy is deps.strategy
    assert upsc.personas is deps.personas
    assert deps.mode(None) is upsc


def test_session_carries_its_mode(deps, form, tmp_path):
    s = InterviewSession(
        deps=deps,
        form=form,
        recorder=TraceRecorder(tmp_path, "m", enabled=False),
        analyzer=TurnAnalyzer(deps),
    )
    assert s.mode == s.state.mode == DEFAULT_MODE
    assert s._ctx("t0", VoiceSignals()).mode == DEFAULT_MODE


def test_upsc_transcript_is_byte_identical_to_the_golden_file(tmp_path, capsys):
    out = run_upsc_cli(tmp_path, capsys)
    assert out == GOLDEN.read_text(encoding="utf-8")


def test_explicit_upsc_mode_flag_prints_the_same_transcript(tmp_path, capsys):
    assert run_upsc_cli(tmp_path, capsys, "--mode", "upsc_interview") == GOLDEN.read_text(
        encoding="utf-8"
    )


def test_debate_transcript_is_byte_identical_to_the_golden_file(capsys):
    assert run_debate_cli(capsys) == DEBATE_GOLDEN.read_text(encoding="utf-8")


def test_debate_golden_file_holds_the_recorded_board_lines():
    """Regenerating the golden cannot quietly accept a change to what the board says."""
    lines = DEBATE_GOLDEN.read_text(encoding="utf-8").splitlines(keepends=True)
    board = "".join(line for line in lines if line.startswith("["))
    assert hashlib.md5(board.encode("utf-8")).hexdigest() == DEBATE_BOARD_LINES_MD5


def scripted_debate_requests(capsys, monkeypatch, script=DEBATE_SCRIPT) -> list[FakeCall]:
    """Every call a scripted debate sends to the model, in order."""
    clients = []
    real_build_llm = cli.build_llm

    def spy(*args):
        clients.append(real_build_llm(*args))
        return clients[-1]

    monkeypatch.setattr(cli, "build_llm", spy)
    run_debate_cli(capsys, script=script)
    return clients[0].calls


def request_rows(calls: list[FakeCall]) -> list[list]:
    return [
        [c.name, c.tag, sha256(c.system), sha256(c.user), c.max_tokens, c.tool, c.temperature]
        for c in calls
    ]


def test_debate_requests_keep_their_order_and_token_caps(capsys, monkeypatch):
    calls = scripted_debate_requests(capsys, monkeypatch)
    assert [(c.name, c.tag, c.max_tokens) for c in calls] == DEBATE_REQUEST_SHAPE


def test_debate_calls_use_the_debate_tools_and_the_stream_temperature(capsys, monkeypatch):
    """Nothing else proves debate *uses* its own listener tool: fake responders ignore it. On
    Nova a UPSC tool would silently drop `arguments`, `rebuts` and `question_to_opponent`."""
    calls = scripted_debate_requests(capsys, monkeypatch)
    assert {c.tool for c in calls if c.name == "listener"} == {LISTENER_TOOL_NAME}
    assert {c.tool for c in calls if c.name == "judge_debate"} == {"record_debate_report"}
    assert {c.temperature for c in calls if c.name == "actor"} == {0.3}
    assert all(c.tool == "" for c in calls if c.name == "actor")


def test_debate_request_hash_is_locked(capsys, monkeypatch):
    """Fake responders do not read prompts, so a changed user message cannot alter the golden
    transcript. This hash is what sees it."""
    rows = request_rows(scripted_debate_requests(capsys, monkeypatch))
    assert sha256(json.dumps(rows)) == DEBATE_REQUESTS_SHA256


def moves_spoken(out: str) -> list[tuple[str, str]]:
    """(move, rule) of each opponent or moderator line that names one."""
    return re.findall(r"^\[[^\]]+\] \((\w+), (D\d+)\)", out, flags=re.MULTILINE)


def test_rebut_script_reaches_cross_question_rebut_and_move_on(capsys, monkeypatch):
    calls = scripted_debate_requests(capsys, monkeypatch, DEBATE_REBUT_SCRIPT)
    assert [(c.name, c.tag) for c in calls][-1] == ("judge_debate", "golden-debate")
    reached = moves_spoken(run_debate_cli(capsys, script=DEBATE_REBUT_SCRIPT))
    assert ("cross_question", "D10") in reached
    assert ("rebut", "D7") in reached and ("rebut", "D4") in reached
    assert ("move_on", "D6") in reached
    assert len(calls) == 13


def test_debate_rebut_request_hash_is_locked(capsys, monkeypatch):
    rows = request_rows(scripted_debate_requests(capsys, monkeypatch, DEBATE_REBUT_SCRIPT))
    assert sha256(json.dumps(rows)) == DEBATE_REBUT_REQUESTS_SHA256


def test_fact_challenge_script_reaches_fact_challenge_cross_question_and_rebut(
    capsys, monkeypatch
):
    calls = scripted_debate_requests(capsys, monkeypatch, DEBATE_FACT_SCRIPT)
    reached = moves_spoken(run_debate_cli(capsys, script=DEBATE_FACT_SCRIPT))
    assert ("fact_challenge", "D9") in reached
    assert ("cross_question", "D10") in reached and ("rebut", "D12") in reached
    assert len(calls) == 12
    assert [c.name for c in calls].count("librarian") == 1


def test_debate_fact_challenge_request_hash_is_locked(capsys, monkeypatch):
    rows = request_rows(scripted_debate_requests(capsys, monkeypatch, DEBATE_FACT_SCRIPT))
    assert sha256(json.dumps(rows)) == DEBATE_FACT_REQUESTS_SHA256


def test_debate_templates_hash_is_locked():
    """Most templates are spoken only when the actor fails, so no scripted run reaches them."""
    doc = {"lines": debate_actor._T, "questions": debate_actor._Q}
    assert sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False)) == DEBATE_TEMPLATES_SHA256


def test_every_test_reads_the_frozen_cards_not_data_cards():
    assert Settings(_env_file=None).cards_dir == LOCK_CARDS_DIR


def test_debate_shared_prefix_hash_is_locked(settings, fake_llm):
    """`cards=None` loads `settings.cards_dir` the way the CLI does: under test that is the
    frozen tests/data/lock_cards, so real cards added to data/cards cannot move this hash.
    It proves nothing yet about card discovery: P7 must run discovery over the frozen folder
    and compare this hash."""
    deps = build_deps(settings, fake_llm)
    assert sha256(deps.mode(DEBATE_MODE).kit.shared_prefix) == DEBATE_PREFIX_SHA256


def test_debate_real_path_prefix_hash_is_locked(settings, fake_llm):
    """The prefix Nova actually sees: the synthetic cards are left out (Ruling 36)."""
    deps = build_deps(settings, fake_llm, allow_synthetic_cards=False)
    prefix = deps.mode(DEBATE_MODE).kit.shared_prefix
    assert sha256(prefix) == DEBATE_REAL_PREFIX_SHA256
    assert "Exampleland" not in prefix


@pytest.mark.parametrize("tool_id", sorted(DEBATE_TOOLS_SHA256))
def test_debate_tool_schema_hash_is_locked(tool_id):
    assert tool_sha256(tool_id) == DEBATE_TOOLS_SHA256[tool_id]


def test_debate_prompt_versions_and_files_hash_is_locked(settings):
    assert prompts_sha256(settings.prompt_versions(DEBATE_MODE)) == DEBATE_PROMPTS_SHA256
