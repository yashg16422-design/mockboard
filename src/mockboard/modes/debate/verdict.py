"""The Moderator's verdict, built by TEMPLATE from the rubric and the scoreboard (DB6).

No model writes the verdict. Every reason maps to something checkable:
* a `strength` or `work_on` reason names one rubric dimension and its verified score, and
  quotes that score's rationale (the judge's sentence, whose evidence passed the citation
  check);
* the `scoreboard` reason reads the code-computed scoreboard.

The template adds no praise of its own, and it never says which side of the motion is right:
it judges how the debate was argued, and says so aloud. A rationale that itself talks about
which side is right is not quoted (`takes_sides`); the reason then names only the score.

The label (CTO_NOTES_04 §4), from the mean of the numeric rubric scores and the scoreboard.
The thresholds are our own choice, not a published standard (UNVERIFIED: tune on real runs):

| Label                   | Condition                                                        |
|-------------------------|------------------------------------------------------------------|
| no_verdict              | the judge was unavailable, or fewer than 3 dimensions were scored |
| carried_on_argument     | mean >= 3.5 and the user answered at least one opponent argument  |
| opponent_held_the_floor | mean < 2.5, or no opponent argument answered and mean < 3.5      |
| close_debate            | otherwise                                                        |

The reasons: the top two scored dimensions ("strength"), the lowest one if it scored 2 or
below ("work_on"), and one scoreboard sentence.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from mockboard.brain.schemas import (
    DebateDimension,
    DebateDimensionScore,
    DebateScoreboard,
    DebateVerdict,
    ReportLanguage,
    VerdictLabel,
    VerdictReason,
)

MIN_SCORED = 3
CARRIED_MEAN = 3.5
HELD_MEAN = 2.5
WORK_ON_MAX = 2  # a "to work on" reason cites only a score of 2 or below

HEADLINES: dict[str, dict[VerdictLabel, str]] = {
    "en": {
        VerdictLabel.carried: "Carried on argument",
        VerdictLabel.close: "Close debate",
        VerdictLabel.opponent_held: "Opponent held the floor",
        VerdictLabel.no_verdict: "No verdict: not enough evidence to judge",
    },
    "hi": {
        VerdictLabel.carried: "तर्क के बल पर बहस आपके नाम रही",
        VerdictLabel.close: "कड़ा मुकाबला",
        VerdictLabel.opponent_held: "विपक्ष ने बढ़त बनाए रखी",
        VerdictLabel.no_verdict: "कोई निर्णय नहीं: निर्णय के लिए पर्याप्त साक्ष्य नहीं",
    },
}

DIMENSION_LABELS: dict[str, dict[DebateDimension, str]] = {
    "en": {
        DebateDimension.clarity_structure: "Clarity and structure",
        DebateDimension.evidence_use: "Use of evidence",
        DebateDimension.rebuttal_quality: "Rebuttal quality",
        DebateDimension.responsiveness: "Responsiveness",
        DebateDimension.fairness_to_other_side: "Fairness to the other side",
        DebateDimension.composure: "Composure",
    },
    "hi": {
        DebateDimension.clarity_structure: "स्पष्टता और संरचना",
        DebateDimension.evidence_use: "साक्ष्य का उपयोग",
        DebateDimension.rebuttal_quality: "खंडन की गुणवत्ता",
        DebateDimension.responsiveness: "प्रश्नों का सीधा उत्तर",
        DebateDimension.fairness_to_other_side: "दूसरे पक्ष के प्रति निष्पक्षता",
        DebateDimension.composure: "संयम",
    },
}

_T: dict[str, dict[str, str]] = {
    "en": {
        "strength": "Strongest: {dim}, {score}/5. {rationale}",
        "work_on": "To work on: {dim}, {score}/5. {rationale}",
        "scoreboard": (
            "You answered {answered} of the opponent's {total} arguments and won {won} "
            "concession(s), so {standing} of its arguments were still standing at the end."
        ),
        "disclaimer": (
            "This verdict judges how the debate was argued, not which side of the motion "
            "is right."
        ),
        "no_judge": "The board could not score this debate, so there is no verdict.",
        "too_little": "Too little of the debate could be scored with verified quotes.",
        "verdict": "The board's verdict: {headline}.",
    },
    "hi": {
        "strength": "सबसे मज़बूत: {dim}, {score}/5. {rationale}",
        "work_on": "सुधार की जगह: {dim}, {score}/5. {rationale}",
        "scoreboard": (
            "आपने विपक्ष के {total} में से {answered} तर्कों का जवाब दिया और {won} बिंदु "
            "मनवाए, इसलिए अंत में उसके {standing} तर्क खड़े रहे।"
        ),
        "disclaimer": (
            "यह निर्णय इस पर है कि बहस कैसे की गई, इस पर नहीं कि प्रस्ताव का कौन सा पक्ष "
            "सही है।"
        ),
        "no_judge": "बोर्ड इस बहस को अंक नहीं दे सका, इसलिए कोई निर्णय नहीं है।",
        "too_little": "बहस के बहुत कम हिस्से को सत्यापित उद्धरणों के साथ आँका जा सका।",
        "verdict": "बोर्ड का निर्णय: {headline}।",
    },
}

# A rationale that declares a side of the motion right or wrong is not quoted in the verdict.
_SIDE_CLAIM = re.compile(
    r"\b(?:motion|side|proposition|opposition|paksh|virodh)\b[^.?!।]{0,40}"
    r"\b(?:is|was|are)\s+(?:clearly\s+)?(?:right|correct|true|wrong|false)\b"
    r"|\b(?:right|correct|winning|losing|wrong)\s+side\b"
    r"|पक्ष[^.?!।]{0,30}(?:सही|ग़लत|गलत)\s+है",
    re.IGNORECASE,
)


def takes_sides(text: str) -> bool:
    return bool(_SIDE_CLAIM.search(text))


def _lang(language: ReportLanguage | str) -> str:
    value = language.value if isinstance(language, ReportLanguage) else str(language)
    return value if value in _T else "en"


def _rationale(text: str) -> str:
    text = " ".join(text.split())
    if not text or takes_sides(text):
        return ""
    return text if text[-1] in ".?!।" else text + "."


def choose_label(
    scores: Sequence[DebateDimensionScore], board: DebateScoreboard, *, judged: bool = True
) -> VerdictLabel:
    numeric = [s.score for s in scores if isinstance(s.score, int)]
    if not judged or len(numeric) < MIN_SCORED:
        return VerdictLabel.no_verdict
    mean = sum(numeric) / len(numeric)
    if mean >= CARRIED_MEAN and board.answered >= 1:
        return VerdictLabel.carried
    if mean < HELD_MEAN or (board.answered == 0 and mean < CARRIED_MEAN):
        return VerdictLabel.opponent_held
    return VerdictLabel.close


def build_verdict(
    scores: Sequence[DebateDimensionScore],
    board: DebateScoreboard,
    language: ReportLanguage | str,
    *,
    judged: bool = True,
) -> DebateVerdict:
    lang = _lang(language)
    t, dims = _T[lang], DIMENSION_LABELS[lang]
    label = choose_label(scores, board, judged=judged)
    headline = HEADLINES[lang][label]
    reasons: list[VerdictReason] = []
    order = {d: i for i, d in enumerate(DebateDimension)}
    numeric = sorted(
        (s for s in scores if isinstance(s.score, int) and s.evidence),
        key=lambda s: (-int(s.score), order[s.dimension]),
    )
    if label != VerdictLabel.no_verdict:
        top = numeric[:2]
        for s in top:
            reasons.append(VerdictReason(
                kind="strength", dimension=s.dimension, score=int(s.score),
                text=t["strength"].format(dim=dims[s.dimension], score=s.score,
                                          rationale=_rationale(s.rationale)).strip(),
            ))
        weakest = numeric[-1] if len(numeric) > len(top) else None
        if weakest is not None and int(weakest.score) <= WORK_ON_MAX \
                and int(weakest.score) < min(int(s.score) for s in top):
            reasons.append(VerdictReason(
                kind="work_on", dimension=weakest.dimension, score=int(weakest.score),
                text=t["work_on"].format(dim=dims[weakest.dimension], score=weakest.score,
                                         rationale=_rationale(weakest.rationale)).strip(),
            ))
    reasons.append(VerdictReason(
        kind="scoreboard",
        text=t["scoreboard"].format(
            answered=board.answered, total=board.opponent_arguments,
            won=board.concessions_won, standing=board.standing_at_end,
        ),
    ))
    if label == VerdictLabel.no_verdict:
        opening = t["no_judge"] if not judged else t["too_little"]
    else:
        opening = t["verdict"].format(headline=headline)
    text = " ".join([opening, *(r.text for r in reasons), t["disclaimer"]])
    return DebateVerdict(label=label, headline=headline, reasons=reasons, text=text)
