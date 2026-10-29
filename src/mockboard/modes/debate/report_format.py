"""Text rendering of the debate report for the CLI, in English or Hindi (DB6)."""

from __future__ import annotations

from mockboard.brain.cards import CardSet
from mockboard.brain.schemas import INSUFFICIENT_EVIDENCE, DebateReport, Verdict
from mockboard.modes.debate.verdict import DIMENSION_LABELS

_L: dict[str, dict[str, str]] = {
    "en": {
        "title": "DEBATE REPORT",
        "motion": "Motion",
        "side": "You argued",
        "sides": "for|against",
        "verdict": "Moderator's verdict",
        "scoreboard": "Scoreboard (computed by code from the debate record, not by a model)",
        "cols": "round|turns|your points|their points answered|their points conceded|"
                "their points still standing|figures questioned",
        "legend": "Your points: the arguments you made. Their points: the opponent's "
                  "case; answering a point does not remove it, only a concession does "
                  "(still standing = their points - conceded).",
        "total": "total",
        "best": "Best line",
        "why": "Why",
        "by_rule": "(chosen by rule: the judge's pick was missing or failed a check)",
        "rubric": "Debate rubric (1-5, or insufficient evidence)",
        "evidence": "evidence",
        "fallback": "The judge was unavailable, so no scores are shown (none were guessed).",
        "claims": "Figures you used, checked against the motion's fact cards",
        "unverifiable": "(no fact cards for this motion: figures are reported as unverifiable)",
        "synthetic": "NOTE: every fact card in this run is a SYNTHETIC TEST CARD, not a real fact.",
    },
    "hi": {
        "title": "बहस की रिपोर्ट",
        "motion": "प्रस्ताव",
        "side": "आपका पक्ष",
        "sides": "पक्ष में|विपक्ष में",
        "verdict": "मॉडरेटर का निर्णय",
        "scoreboard": "स्कोरबोर्ड (बहस के रिकॉर्ड से कोड द्वारा गिना गया, किसी मॉडल द्वारा नहीं)",
        "cols": "दौर|बारी|आपके बिंदु|उनके बिंदु जिनका जवाब दिया|उनके माने गए बिंदु|"
                "उनके खड़े बिंदु|पूछे गए आँकड़े",
        "legend": "आपके बिंदु: आपके रखे गए तर्क। उनके बिंदु: विपक्ष का पक्ष; जवाब देने "
                  "से बिंदु हटता नहीं, केवल विपक्ष के मानने से हटता है (खड़े = उनके बिंदु - "
                  "माने गए)।",
        "total": "कुल",
        "best": "सबसे अच्छी पंक्ति",
        "why": "क्यों",
        "by_rule": "(नियम से चुनी गई: जज का चुनाव नहीं मिला या जाँच में विफल रहा)",
        "rubric": "बहस का मूल्यांकन (1-5, या पर्याप्त साक्ष्य नहीं)",
        "evidence": "साक्ष्य",
        "fallback": "जज उपलब्ध नहीं था, इसलिए कोई अंक नहीं दिखाए गए (कोई अनुमान नहीं लगाया गया)।",
        "claims": "आपके आँकड़े, प्रस्ताव के फ़ैक्ट कार्ड से जाँचे गए",
        "unverifiable": "(इस प्रस्ताव के लिए फ़ैक्ट कार्ड नहीं हैं: आँकड़े असत्यापित माने गए)",
        "synthetic": "नोट: इस रन के सभी फ़ैक्ट कार्ड सिंथेटिक टेस्ट कार्ड हैं, असली तथ्य नहीं।",
    },
}
_ROUNDS = {"hi": {"opening": "ओपनिंग", "clash": "क्लैश", "closing": "क्लोज़िंग"}}


def _row(values: list, widths: list[int]) -> str:
    return ("  " + "".join(str(v).ljust(w) for v, w in zip(values, widths, strict=True))).rstrip()


def _score(score: int | str) -> str:
    return "  -" if score == INSUFFICIENT_EVIDENCE else f"{score}/5"


def format_debate_report(report: DebateReport, cards: CardSet) -> str:
    lang = report.report_language.value if report.report_language.value in _L else "en"
    t = _L[lang]
    sides = dict(zip(("for", "against"), t["sides"].split("|"), strict=True))
    lines = [
        "",
        "=" * 70,
        f"{t['title']}  (session {report.session_id}, language {lang})",
        "=" * 70,
        f"{t['motion']}: {report.motion}",
        f"{t['side']}: {sides[report.user_side]}",
        "",
        f"{t['verdict']}: {report.verdict.headline}",
    ]
    lines += [f"  - {r.text}" for r in report.verdict.reasons]
    lines += ["", t["scoreboard"] + ":"]
    cols = t["cols"].split("|")
    sb = report.scoreboard
    table = [
        [_ROUNDS.get(lang, {}).get(r.round, r.round), r.turns, r.arguments_made, r.answered,
         r.concessions_won, r.standing_after, r.figures_questioned]
        for r in sb.rounds
    ]
    table.append([t["total"], sum(r.turns for r in sb.rounds), sb.arguments_made, sb.answered,
                  sb.concessions_won, sb.standing_at_end, sb.figures_questioned])
    widths = [max(len(str(row[i])) for row in [cols, *table]) + 2 for i in range(len(cols))]
    lines += [_row(row, widths) for row in [cols, *table]]
    lines.append(f"  {t['legend']}")
    if report.best_line is not None:
        b = report.best_line
        lines += ["", f'{t["best"]} [{b.turn_id}]: "{b.quote}"', f"  {t['why']}: {b.reason}"]
        if b.chosen_by == "code":
            lines.append(f"  {t['by_rule']}")
    lines.append("")
    if report.is_fallback:
        lines.append(t["fallback"])
    if report.scores:
        labels = DIMENSION_LABELS[lang]
        lines.append(t["rubric"] + ":")
        for s in report.scores:
            lines.append(f"  {s.dimension.value:<24} {_score(s.score)}  "
                         f"({labels[s.dimension]}) {s.rationale}")
        for s in report.scores:
            for e in s.evidence[:1]:
                lines.append(f'    {t["evidence"]} [{s.dimension.value} / {e.turn_id}]: '
                             f'"{e.quote}"')
    if report.claims:
        lines.append(t["claims"] + ":")
        for c in report.claims:
            src = report.card_sources.get(c.card_id or "", "")
            lines.append(
                f"  [{c.verdict.value:<17}] {c.text[:70]}"
                + (f"  <- card {c.card_id} ({src})" if c.card_id else "")
            )
        if all(c.verdict in (Verdict.pending, Verdict.unverifiable) for c in report.claims) \
                and not report.card_sources:
            lines.append(f"  {t['unverifiable']}")
    if cards.all_synthetic and report.card_sources:
        lines.append(t["synthetic"])
    lines.extend(f"note: {n}" for n in report.notes)
    return "\n".join(lines)
