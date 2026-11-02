"""Text-only Mock Board interview, end to end.

    python -m mockboard.cli --fake                      # fully offline (rule-based fake LLM)
    python -m mockboard.cli                             # real Nova Pro on Bedrock (Sydney)
    python -m mockboard.cli --fake --script answers.txt # scripted answers, one per line
    python -m mockboard.cli --mode debate --fake        # debate mode (curated motions)
    python -m mockboard.cli --fake --form-file examples/forms/sample_form.json
    python -m mockboard.cli --fake --script examples/fake_panel.txt   # the UPSC panel, scripted
    python -m mockboard.cli --fake --likely-questions --form-file examples/forms/sample_form.json

Flow: application form (no names) -> opener -> typed answers -> fast analysis graph per
segment (background fact checks) -> delayed fact challenge phrased as a question citing the
card -> move on -> judge report. Every turn appends one JSON line to
`traces/<session_id>.jsonl`.

Each typed answer goes through the same session API as the voice path: it is submitted as
ONE transcript segment (`submit_segment`), then `end_of_turn(deadline)` returns the move and
`speak()` phrases it. Type " | " inside an answer to split it into several segments, as if
Prisma's VAD had split it. Type /quit to end early and go straight to the report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

from pydantic import ValidationError

from mockboard.brain.cards import load_cards_or_builtin
from mockboard.brain.deps import build_deps
from mockboard.brain.fake_responders import make_fake_llm
from mockboard.brain.graphs import TurnAnalyzer
from mockboard.brain.llm import BedrockNovaClient, LLMClient
from mockboard.brain.numbers import Tolerance
from mockboard.brain.schemas import (
    INSUFFICIENT_EVIDENCE,
    CandidateForm,
    DebateReport,
    Move,
    SessionReport,
    Verdict,
)
from mockboard.config import Settings, get_settings
from mockboard.modes import DEFAULT_MODE, mode_ids
from mockboard.modes.debate.report_format import format_debate_report
from mockboard.modes.debate.setup_cli import read_debate_setup
from mockboard.modes.upsc_panel.setup import PanelSetup
from mockboard.obs.trace import TraceRecorder
from mockboard.obs.tracing import langfuse_callbacks
from mockboard.session import SEGMENT_MARK, InterviewSession

FORM_FIELDS: list[tuple[str, str, bool]] = [
    # (field, prompt, required). Ruling 5: no names or identifiers are ever asked.
    ("home_state", "Home state", True),
    ("district", "District (optional, Enter to skip)", False),
    ("degree_subject", "Degree subject", True),
    ("hobbies", "Hobbies, up to 3, comma-separated (optional)", False),
    ("optional_subject", "UPSC optional subject (optional)", False),
    ("work_experience", "Work experience in one line (optional)", False),
    ("interview_language", "Interview language [hinglish/hindi/english] (default hinglish)", False),
]

DEBATE_MODE = "debate"

DEFAULT_FAKE_FORM = {
    "home_state": "Uttar Pradesh",
    "district": "Kanpur Nagar",
    "degree_subject": "Electronics",
    "hobbies": "chess, cycling",
    "interview_language": "hinglish",
}


class InputSource:
    """Answers come from a script file (one per line), from stdin, or interactively."""

    def __init__(self, lines: list[str] | None) -> None:
        self._it: Iterator[str] | None = iter(lines) if lines is not None else None

    def __call__(self, prompt: str) -> str | None:
        if self._it is None:
            try:
                return input(prompt)
            except EOFError:
                return None
        line = next(self._it, None)
        print(f"{prompt}{line if line is not None else ''}")
        return line


def read_form(ask: Callable[[str], str | None], use_defaults: bool) -> CandidateForm:
    """Collect the application form. In scripted --fake runs without form lines, defaults."""
    raw: dict[str, object] = {}
    print("\n== Application form (no names, roll numbers, phone numbers or colleges) ==")
    for name, prompt, required in FORM_FIELDS:
        while True:
            value = ask(f"{prompt}: ")
            if value is None:
                if use_defaults:
                    return CandidateForm.model_validate(_form_values(DEFAULT_FAKE_FORM))
                raise SystemExit("input ended during the application form")
            value = value.strip()
            if value or not required:
                break
            print("  This field is required.")
        if value:
            raw[name] = value
    return CandidateForm.model_validate(_form_values(raw))


def load_form_file(path: str | Path) -> CandidateForm:
    """The application form from a JSON file. Refuses any field `CandidateForm` does not have
    (Ruling 5: no names or identifiers). Messages name fields, never the values in them."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"Could not read form file {path}: {exc.strerror}") from exc
    except ValueError as exc:  # bad JSON, or not UTF-8
        raise SystemExit(f"form file {path} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise SystemExit(f"form file {path} must hold one JSON object")
    values = _form_values(doc)
    if isinstance(doc.get("hobbies"), str):  # no silent cut here: the schema refuses a fourth
        values["hobbies"] = _split_hobbies(doc["hobbies"])
    try:
        return CandidateForm.model_validate(values)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise SystemExit(f"form file {path} was refused: {problems}") from exc


def _split_hobbies(text: str) -> list[str]:
    return [h.strip() for h in text.split(",") if h.strip()]


def _form_values(raw: dict[str, object]) -> dict[str, object]:
    out = dict(raw)
    if isinstance(out.get("hobbies"), str):
        out["hobbies"] = _split_hobbies(str(out["hobbies"]))[:3]
    if "interview_language" in out:
        out["interview_language"] = str(out["interview_language"]).lower()
    return out


def build_llm(settings: Settings, fake: bool, cards, recorder: TraceRecorder) -> LLMClient:
    if fake:
        tol = Tolerance(
            rel=settings.numeric_rel_tolerance, abs_pp=settings.numeric_abs_tolerance_pp
        )
        return make_fake_llm(cards, tol, on_call=recorder.on_call)
    # Real mode: Nova Pro in ap-southeast-2 via the default AWS credential chain.
    try:
        return BedrockNovaClient.from_settings(settings, on_call=recorder.on_call)
    except Exception as exc:  # e.g. botocore ProfileNotFound: fail with a readable message
        raise SystemExit(
            f"Could not create the Bedrock client ({type(exc).__name__}: {exc}). "
            "Check `aws configure` / AWS_PROFILE, or run with --fake."
        ) from exc


def format_report(report: SessionReport | DebateReport, cards) -> str:
    if isinstance(report, DebateReport):
        return format_debate_report(report, cards)
    lines = [
        "",
        "=" * 70,
        f"REPORT  (session {report.session_id}, language {report.report_language.value})",
        "=" * 70,
    ]
    if report.is_fallback:
        lines.append("The judge was unavailable, so no scores are shown (none were guessed).")
    if report.scores:
        lines.append("UPSC personal qualities (1-5, or insufficient evidence):")
        for s in report.scores:
            if s.is_practice_indicator:
                continue
            lines.append(f"  {s.dimension.value:<30} {_score(s.score)}  {s.rationale}")
        lines.append("Mock Board practice indicators (our own, not UPSC criteria):")
        for s in report.scores:
            if s.is_practice_indicator:
                lines.append(f"  {s.dimension.value:<30} {_score(s.score)}  {s.rationale}")
        for s in report.scores:
            for e in s.evidence[:1]:
                lines.append(f'    evidence [{s.dimension.value} / {e.turn_id}]: "{e.quote}"')
    if report.claims:
        lines.append("Claims checked against fact cards:")
        for c in report.claims:
            src = report.card_sources.get(c.card_id or "", "")
            lines.append(
                f"  [{c.verdict.value:<17}] {c.text[:70]}"
                + (f"  <- card {c.card_id} ({src})" if c.card_id else "")
            )
        conflicts = [c for c in report.claims if c.verdict == Verdict.possible_conflict]
        if conflicts:
            lines.append(
                "  (possible_conflict = model-only, non-numeric: shown here, never challenged live)"
            )
    if cards.all_synthetic:
        lines.append("NOTE: every fact card in this run is a SYNTHETIC TEST CARD, not a real fact.")
    if report.model_answer:
        lines.append(f"Model answer: {report.model_answer}")
    if report.discussion:
        lines.append("Board discussion:")
        for line in report.discussion:
            lines.append(f"  {line}")
    if report.by_member:
        lines.append("By panel member:")
        for block in report.by_member:
            lines.append(f"  {block.name} ({block.segment})")
            if block.notes:
                lines.append(f"    {block.notes}")
            if block.weakest_question:
                lines.append(f"    Weakest question: {block.weakest_question}")
            for point in block.outline:
                lines.append(f"      - {point}")
    if report.practice_plan:
        lines.append("Practice plan:")
        for item in report.practice_plan:
            lines.append(f"  - {item}")
    if report.weakest:
        lines.append(
            f"Replay this moment (text): {report.weakest.question}"
        )
    lines.extend(f"note: {n}" for n in report.notes)
    return "\n".join(lines)


def typed_segments(text: str) -> list[str]:
    """One typed answer is one transcript segment, unless " | " splits it. An empty answer
    is silence: no segment at all."""
    return [p.strip() for p in text.split(SEGMENT_MARK) if p.strip()]


def _score(score: int | str) -> str:
    return "  -" if score == INSUFFICIENT_EVIDENCE else f"{score}/5"


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    overrides = {}
    if args.trace_dir:
        overrides["trace_dir"] = Path(args.trace_dir)
    if args.language:
        overrides["report_language"] = args.language
    if overrides:
        settings = settings.model_copy(update=overrides)

    # First, so a bad file stops the run before anything is built or spent.
    file_form = load_form_file(args.form_file) if args.form_file else None
    cards, origin = load_cards_or_builtin(settings.cards_dir)
    lines = None
    if args.script:
        lines = Path(args.script).read_text(encoding="utf-8").splitlines()
    elif not args.likely_questions and not args.replay and not sys.stdin.isatty():
        lines = sys.stdin.read().splitlines()
    ask = InputSource(lines)

    if args.likely_questions:  # P9, Utility A: no interview, no session, no trace
        return await _likely_questions(args, settings, ask, file_form, cards, origin)
    if args.replay:
        return await _replay(args, settings, file_form, cards)

    session_id = args.session_id
    recorder = TraceRecorder(settings.trace_dir, session_id or "pending", enabled=not args.no_trace)
    llm = build_llm(settings, args.fake, cards, recorder)
    deps = build_deps(settings, llm, cards=cards)
    callbacks = langfuse_callbacks(settings.langfuse_enabled)
    analyzer = TurnAnalyzer(deps, callbacks=callbacks)
    rt = deps.mode(args.mode)

    print(
        f"Mock Board (text mode) | LLM: {'FAKE offline' if args.fake else settings.nova_model_id}"
        f" ({'-' if args.fake else settings.aws_region}) | cards: {origin}"
        f" ({len(deps.cards.cards)}, version {deps.cards.version})"
        f" | prefix ~{rt.kit.prefix_tokens_approx} tokens (approx)"
    )
    if not deps.cards.cards:  # ruling 36: test cards never reach a real run's prompts
        print("No real fact cards are loaded, so fact checks are off (synthetic test cards "
              "are used only with --fake).")
    elif deps.cards.all_synthetic:
        print("All fact cards are SYNTHETIC TEST CARDS. Nothing the board cites is a real fact.")

    use_defaults = args.fake and lines is not None
    form, options = None, None
    if args.mode == DEBATE_MODE:
        options = await read_debate_setup(ask, llm=llm, settings=settings,
                                          use_defaults=use_defaults)
    elif file_form is not None:
        form = file_form
        print(f"\nApplication form from {args.form_file}")
    else:
        form = read_form(ask, use_defaults=use_defaults)
    if args.mode != DEBATE_MODE:  # the UPSC panel (P7); --max-answers is a test user's cap
        options = PanelSetup(max_answers=args.max_answers)
    kwargs = {"session_id": session_id} if session_id else {}
    session = InterviewSession(
        deps=deps,
        form=form,
        recorder=recorder,
        analyzer=analyzer,
        max_turns=args.max_turns,
        callbacks=callbacks,
        mode=args.mode,
        mode_options=options,
        **kwargs,
    )
    recorder.session_id = session.session_id
    recorder.path = recorder.path.with_name(f"{session.session_id}.jsonl")

    try:
        return await _interview(args, settings, session, rt, ask, lines, recorder, cards)
    finally:
        await session.close()  # the panel's checkpointer thread, on every exit path


async def _replay(args, settings, file_form, cards) -> int:
    """`--replay <report.json>`: sit the weakest question once more, text only (P10, Ruling 48)."""
    from mockboard.brain.graphs import TurnAnalyzer
    from mockboard.brain.report_panel import TextReplay
    from mockboard.modes.upsc_panel.setup import PanelSetup
    from mockboard.session import InterviewSession

    source = TextReplay()
    report = source.load(Path(args.replay))
    if report.weakest is None:
        print("This report has no weakest moment to replay.")
        return 1
    form = file_form or report.form
    if form is None:
        print("--replay needs the form: pass --form-file or a report that stored it.")
        return 1
    print(f"Replay (text only) | {report.weakest.member or report.weakest.segment}")
    print(f"Question: {report.weakest.question}")
    for point in report.weakest.outline:
        print(f"  - {point}")
    recorder = TraceRecorder(settings.trace_dir, args.session_id or "replay",
                             enabled=not args.no_trace)
    llm = build_llm(settings, args.fake, cards, recorder)
    deps = build_deps(settings, llm, cards=cards)
    session = InterviewSession(
        deps=deps, form=form, recorder=recorder, analyzer=TurnAnalyzer(deps),
        session_id=recorder.session_id, mode=DEFAULT_MODE,
        mode_options=PanelSetup(
            replay_segment=report.weakest.segment,
            replay_question=report.weakest.question,
            skip_judge=True,
        ),
    )
    try:
        line = await session.open()
        print(f"[{session.speaker_name('chairman')}] {line.text}")
        lines = Path(args.script).read_text(encoding="utf-8").splitlines() if args.script else None
        ask = InputSource(lines)
        text = ask("\nYou: ")
        if text:
            for index, part in enumerate(typed_segments(text)):
                session.submit_segment(part, segment_index=index)
            await session.end_of_turn(args.deadline_s or settings.text_move_deadline_s)
            out = await session.speak()
            print(f"[{session.speaker_name(out.move.member)}] {out.line.text}")
        replay_report = await session.finish()
        if session.state.turns:
            compared = source.compare(report.weakest, session.state.turns[-1])
            print(
                f"\nCompare: {compared.before_words} words then, "
                f"{compared.after_words} now ({compared.word_gain:+d}). "
                f"Balance now: {compared.after_balance}."
            )
        print(format_report(replay_report, session.deps.cards))
    finally:
        await session.close()
    return 0


async def _likely_questions(args, settings, ask, file_form, cards, origin) -> int:
    """`--likely-questions`: the questions the panel prepares from the form, with a hint on how to
    approach each. `--template-only` and `--fake` make no real call; a real run calls Nova Pro once
    (the `summary` prompt, credits) and every hint it writes is checked by code."""
    from mockboard.brain.likely_questions import (
        format_likely_questions,
        kit_with_summary,
        likely_questions,
    )

    form = file_form if file_form is not None else read_form(ask, use_defaults=args.fake)
    recorder = TraceRecorder(settings.trace_dir, "likely", enabled=False)
    llm = None if args.template_only else build_llm(settings, args.fake, cards, recorder)
    deps = build_deps(settings, llm or make_fake_llm(cards), cards=cards)
    rt = deps.mode(DEFAULT_MODE)
    names = {p.id: p.name for p in rt.personas.values()}
    kit = kit_with_summary(rt.kit, settings.prompt_summary_version)
    how = (
        "template hints, no model call" if args.template_only
        else "hints from the FAKE offline LLM" if args.fake
        else f"hints from {settings.nova_model_id} ({settings.aws_region}): one call, costs credits"
    )
    print(f"Mock Board likely questions | {how} | cards: {origin}")
    result = await likely_questions(form, names, llm=llm, kit=kit)
    print(format_likely_questions(result))
    if result.report.status == "failed":
        print(f"\nThe model's hints were not used ({result.report.error or 'none was fit'}); "
              "these are the standard hints.")
    return 0


def _print_panel_line(session: InterviewSession, line, tag: str = "") -> None:
    """One `[Name] text` row per speaker, so a hand-off reads as the Chairperson's own line and
    the question as the next member's. The (move, rule) tag sits on the last row."""
    parts = list(line.parts)
    for i, part in enumerate(parts):
        label = f"({tag}) " if tag and i == len(parts) - 1 else ""
        print(f"[{session.speaker_name(part.member)}] {label}{part.text}")


async def _interview(args, settings, session, rt, ask, lines, recorder, cards) -> int:
    opener = await session.open()
    if args.mode != DEBATE_MODE:
        print(f"\n{session.hooks.agenda_note()}\n")
        _print_panel_line(session, opener)
    else:
        print(f"\n[{session.speaker_name(rt.strategy.chairman)}] {opener.text}")
    while not session.state.finished:
        if lines is not None and args.think_s > 0:
            # A scripted candidate answers instantly; a real one takes seconds to think and
            # speak, during which the background fact checks finish. Stand in for that time.
            await session.settle_fact_checks(args.think_s)
        text = ask("\nYou: ")
        if text is None or text.strip() == "/quit":
            break
        for index, part in enumerate(typed_segments(text)):
            session.submit_segment(part, segment_index=index)
        await session.end_of_turn(args.deadline_s or settings.text_move_deadline_s)
        out = await session.speak()
        tag = f"{out.move.move.value}, {out.move.rule_id}"
        if args.mode != DEBATE_MODE:
            _print_panel_line(session, out.line, tag)
        else:
            print(f"[{session.speaker_name(out.move.member)}] ({tag}) {out.line.text}")
        if out.move.move == Move.fact_challenge:
            print(f"   show-your-work: {out.move.reason}")
        if out.fallback_reason:
            print(f"   fallback: {out.fallback_reason}")
    confer = session.closing_line()
    if confer:
        print(f"\n[{session.speaker_name(rt.strategy.chairman)}] {confer}")
    report = await session.finish(args.language)
    if isinstance(report, DebateReport):  # the Moderator reads the verdict aloud
        print(f"[{session.speaker_name(rt.strategy.chairman)}] {report.verdict.text}")
    print(format_report(report, session.deps.cards))
    if args.report_json:
        Path(args.report_json).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    if recorder.enabled:
        print(f"\nTrace: {recorder.path}")
    if args.print_trace and recorder.enabled:
        for line in recorder.path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            keys = ("turn_id", "event", "move", "fallback_reason", "move_wait_ms", "fast_graph_ms")
            summary = {k: row.get(k) for k in keys}
            summary["fact_check_ms"] = {
                f"{f['for_turn']}.{f['segment_id']}": f["fact_check_ms"]
                for f in row.get("fact_checks") or []
            }
            summary["stages_ms"] = row.get("stages_ms")
            print(json.dumps(summary))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mockboard-cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--fake", action="store_true", help="run fully offline with the fake LLM")
    p.add_argument(
        "--mode",
        choices=mode_ids(),
        default=DEFAULT_MODE,
        help=f"session mode (ADR-009; default {DEFAULT_MODE})",
    )
    p.add_argument("--script", help="file with scripted inputs, one per line (form, then answers)")
    p.add_argument(
        "--form-file",
        help="application form as JSON (see examples/forms/sample_form.json); UPSC mode only. "
        "With it, --script holds only the answers, no form lines "
        "(see examples/fake_answers.txt)",
    )
    p.add_argument(
        "--panel",
        action="store_true",
        help=argparse.SUPPRESS,  # a no-op since P7: the UPSC interview is always the panel
    )
    p.add_argument(
        "--max-answers",
        type=int,
        help="UPSC only: cap the interview at this many answers (at least 5, one question for "
        "each member); the agenda spends the cap, so the panel still reaches a proper close. "
        "Default: no cap, as for an owner or reviewer",
    )
    p.add_argument(
        "--likely-questions",
        action="store_true",
        help="UPSC only: print the questions the panel prepares from the form, each with a hint "
        "on how to approach it, and stop (no interview). With --fake the hints come from the "
        "offline stand-in; without it, one Nova Pro call writes them (costs credits); "
        "--template-only makes no call at all",
    )
    p.add_argument(
        "--template-only",
        action="store_true",
        help="with --likely-questions: standard hints only, no model call",
    )
    p.add_argument(
        "--replay",
        help="UPSC only: sit the weakest moment from this report JSON once more, text only "
        "(no audio). Needs --form-file if the report did not store the form",
    )
    p.add_argument("--language", choices=["en", "hi"], help="report language (default: settings)")
    p.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="debate only: hard cap on candidate turns (default 9). The UPSC panel sets its own "
        "cap from its agenda and refuses this flag (use --max-answers to shorten it)",
    )
    p.add_argument(
        "--think-s",
        type=float,
        default=5.0,
        help="scripted runs only: wait up to this long for background fact checks before the "
        "next scripted answer, standing in for the candidate's thinking time (0 = no wait)",
    )
    p.add_argument(
        "--deadline-s",
        type=float,
        help="end-of-turn deadline for the move in seconds (default: TEXT_MOVE_DEADLINE_S)",
    )
    p.add_argument("--session-id", help="fixed session id (default: random)")
    p.add_argument("--trace-dir", help="override TRACE_DIR")
    p.add_argument("--no-trace", action="store_true", help="do not write a trace file")
    p.add_argument("--print-trace", action="store_true", help="print trace summary at the end")
    p.add_argument("--report-json", help="also write the report as JSON to this path")
    args = p.parse_args(argv)
    if args.form_file and args.mode == DEBATE_MODE:
        p.error("--form-file is for the UPSC interview; debate has no application form")
    if args.panel and args.mode == DEBATE_MODE:
        p.error("--panel is for the UPSC interview; debate has no panel")
    if args.likely_questions and args.mode == DEBATE_MODE:
        p.error("--likely-questions is for the UPSC interview; debate has no application form")
    if args.template_only and not args.likely_questions:
        p.error("--template-only goes with --likely-questions")
    if args.replay and args.mode == DEBATE_MODE:
        p.error("--replay is for the UPSC interview")
    if args.replay and args.likely_questions:
        p.error("--replay and --likely-questions cannot be combined")
    if args.mode != DEBATE_MODE and args.max_turns is not None:
        p.error("--max-turns does not apply to the UPSC panel: the agenda sets the turn cap "
                "(use --max-answers to shorten it)")
    if args.max_turns is None:
        args.max_turns = 9
    if args.max_answers is not None and args.mode == DEBATE_MODE:
        p.error("--max-answers is for the UPSC interview; debate has no answer cap")
    if args.max_answers is not None and args.max_answers < 5:
        p.error("--max-answers must be at least 5, one question for each of the five members")
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
