"""Modes as profiles (ADR-009): the registry, per-mode runtimes, and the golden transcripts.

The golden transcripts lock today's behaviour: `make cli-fake` and `make cli-fake-debate` with
a fixed session id must print exactly `tests/golden/upsc_cli_fake.txt` and
`tests/golden/debate_cli_fake.txt` (timings are not printed without --print-trace, and the
repository path and the frozen cards folder are normalised, so both files still read
`<repo>/data/cards`).

The debate lock (CTO_NOTES_06 section 7) also hashes what reaches the model: the shared prefix,
the tool schemas and the prompts. The Panel phase must not move any of them. If one of these
tests fails, debate behaviour changed: find out why before touching a hash.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from conftest import LOCK_CARDS_DIR
from mockboard import cli
from mockboard.brain.deps import build_deps
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.prompts import PROMPT_DIR
from mockboard.brain.schemas import VoiceSignals
from mockboard.brain.strategist import decide_move
from mockboard.brain.tools import ALL_TOOLS
from mockboard.config import REPO_ROOT, Settings
from mockboard.modes import DEFAULT_MODE, get_profile, mode_ids
from mockboard.modes.upsc import UPSC_MOVES
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

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


def run_debate_cli(capsys) -> str:
    rc = cli.main(
        [
            "--mode",
            DEBATE_MODE,
            "--fake",
            "--script",
            str(REPO_ROOT / "examples" / "fake_debate.txt"),
            "--session-id",
            "golden-debate",
            "--no-trace",
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
    assert profile.allowed_moves == UPSC_MOVES and len(UPSC_MOVES) == 9
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


def test_every_test_reads_the_frozen_cards_not_data_cards():
    assert Settings(_env_file=None).cards_dir == LOCK_CARDS_DIR


def test_debate_shared_prefix_hash_is_locked(settings, fake_llm):
    """`cards=None` loads `settings.cards_dir` the way the CLI does: under test that is the
    frozen tests/data/lock_cards, so real cards added to data/cards cannot move this hash.
    It proves nothing yet about card discovery: P7 must run discovery over the frozen folder
    and compare this hash."""
    deps = build_deps(settings, fake_llm)
    assert sha256(deps.mode(DEBATE_MODE).kit.shared_prefix) == DEBATE_PREFIX_SHA256


@pytest.mark.parametrize("tool_id", sorted(DEBATE_TOOLS_SHA256))
def test_debate_tool_schema_hash_is_locked(tool_id):
    assert tool_sha256(tool_id) == DEBATE_TOOLS_SHA256[tool_id]


def test_debate_prompt_versions_and_files_hash_is_locked(settings):
    assert prompts_sha256(settings.prompt_versions(DEBATE_MODE)) == DEBATE_PROMPTS_SHA256
