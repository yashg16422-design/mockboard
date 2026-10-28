"""Modes as profiles (ADR-009): the registry, per-mode runtimes, and the UPSC golden transcript.

The golden transcript locks today's UPSC behaviour: `make cli-fake` with a fixed session id
must print exactly `tests/golden/upsc_cli_fake.txt` (timings are not printed without
--print-trace, and the repository path is normalised).
"""

from __future__ import annotations

import pytest

from mockboard import cli
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.schemas import VoiceSignals
from mockboard.brain.strategist import decide_move
from mockboard.config import REPO_ROOT
from mockboard.modes import DEFAULT_MODE, get_profile, mode_ids
from mockboard.modes.upsc import UPSC_MOVES
from mockboard.obs.trace import TraceRecorder
from mockboard.session import InterviewSession

GOLDEN = REPO_ROOT / "tests" / "golden" / "upsc_cli_fake.txt"


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
    return capsys.readouterr().out.replace(str(REPO_ROOT), "<repo>")


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
