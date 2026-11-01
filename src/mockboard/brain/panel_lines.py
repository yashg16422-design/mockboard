"""Fixed lines of the panel, by language mix (CTO_NOTES_06 §4, like `CLARIFY_LINES`).

Formal UPSC register: always "aap". The Chairperson speaks the ritual, hand-offs, confirmation and
closes; a member speaks the acknowledgements and the repeat prefix inside their own segment, so
those lines avoid gendered verbs ("dohrata hoon"). No line states a duration or a number: limits
may change (Rulings 47, 49). "hi-en" is Hinglish in Latin script; whether the board writes
Devanagari follows Ruling 39.

Variants (Ruling 73 item 3). A line that can come up more than once in a session has two or three
wordings per language mix, so a board that says "no problem" twice does not say it the same way
twice. Which wording is chosen is deterministic and needs no random number: the director counts how
often each table has been used, and the wording is `uses % len(variants)`. So the same table never
gives the same wording on two uses in a row, and the first use is always the first wording below.
Every line that is said once at most (the ritual and the closes) keeps a single wording.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from mockboard.brain.actor import CLARIFY_LINES


class LineSet(dict[str, str]):
    """One fixed line by language mix. As a dict it holds the first wording, so `table[mix]` and
    `line_for(table, mix)` are what they always were; `variants` holds every wording by mix, the
    first one equal to that entry, and `id` names the table in the director's use counts."""

    def __init__(self, table_id: str, variants: Mapping[str, Sequence[str]]) -> None:
        super().__init__({mix: wordings[0] for mix, wordings in variants.items()})
        self.id = table_id
        self.variants: dict[str, tuple[str, ...]] = {
            mix: tuple(wordings) for mix, wordings in variants.items()
        }

    def wording(self, mix: str, uses: int = 0) -> str:
        """The wording for the `uses`-th time this table is used in a session."""
        options = self.variants.get(mix) or self.variants["hi-en"]
        return options[uses % len(options)]


def _once(table_id: str, hi_en: str, en: str, hi: str) -> LineSet:
    """A line said at most once in a session: one wording per mix."""
    return LineSet(table_id, {"hi-en": (hi_en,), "en": (en,), "hi": (hi,)})


# The ritual before the first question.
RITUAL_LINES = _once(
    "ritual",
    "Namaste. Please be seated.",
    "Good day. Please be seated.",
    "नमस्ते। कृपया बैठिए।",
)
# Chairperson's hand-off to the next member. Takes the persona's display name. Each wording is
# still two sentences (a thanks, then the call to the member): `test_handoff_stays_whole`.
HANDOFF_LINES = LineSet(
    "handoff",
    {
        "hi-en": (
            "Dhanyavaad. {name}, please.",
            "Shukriya. Ab {name} ki baari hai.",
            "Dhanyavaad. {name}, aap aage badhaiye.",
        ),
        "en": (
            "Thank you. {name}, please.",
            "Thank you. Over to {name}.",
            "Thank you. {name}, would you like to go ahead?",
        ),
        "hi": (
            "धन्यवाद। {name}, कृपया।",
            "शुक्रिया। अब {name} की बारी है।",
            "धन्यवाद। {name}, आप आगे बढ़िए।",
        ),
    },
)
# Acknowledges a topic change before the hand-off.
CHANGE_TOPIC_ACK_LINES = LineSet(
    "change_topic_ack",
    {
        "hi-en": (
            "Zaroor, koi baat nahi.",
            "Jaisa aap kahein, ise yahin rakhte hain.",
            "Bilkul, ise yahin chhod dete hain.",
        ),
        "en": (
            "Certainly, no difficulty at all.",
            "Of course, we can leave that there.",
            "That is quite alright, we will move away from it.",
        ),
        "hi": (
            "ज़रूर, कोई बात नहीं।",
            "जैसा आप कहें, इसे यहीं रखते हैं।",
            "बिल्कुल, इसे यहीं छोड़ देते हैं।",
        ),
    },
)
# Acknowledges a skipped question before the next seed.
SKIP_ACK_LINES = LineSet(
    "skip_ack",
    {
        "hi-en": (
            "Koi baat nahi, ise chhod dete hain.",
            "Theek hai, is sawal ko rehne dete hain.",
            "Jaisa aap chahein, agle sawal par aate hain.",
        ),
        "en": (
            "That is quite all right, we will leave that one.",
            "Of course, we shall set that question aside.",
            "Very well, let us go to the next question.",
        ),
        "hi": (
            "कोई बात नहीं, इसे छोड़ देते हैं।",
            "ठीक है, इस प्रश्न को रहने देते हैं।",
            "जैसा आप चाहें, अगले प्रश्न पर आते हैं।",
        ),
    },
)
# Said before the last question, verbatim.
REPEAT_PREFIX_LINES = LineSet(
    "repeat_prefix",
    {
        "hi-en": (
            "Zaroor, sawal phir se suniye.",
            "Ji, ek baar phir suniye.",
            "Theek hai, sawal dobara suniye.",
        ),
        "en": (
            "Certainly. Here is the question again.",
            "Of course. Let me put it once more.",
            "Certainly, I will ask that again.",
        ),
        "hi": (
            "ज़रूर, प्रश्न फिर से सुनिए।",
            "जी, एक बार फिर सुनिए।",
            "ठीक है, प्रश्न दोबारा सुनिए।",
        ),
    },
)
# Ruling 52: one spoken confirmation before an `end` request closes the interview. The first hi-en
# wording is the ruling's brief, verbatim.
END_CONFIRM_LINES = LineSet(
    "end_confirm",
    {
        "hi-en": (
            "Aap interview yahin samapt karna chahte hain?",
            "Kya aap sach mein interview yahin samapt karna chahte hain?",
            "Aap chahte hain ki hum yahin interview samapt karein?",
        ),
        "en": (
            "Do you wish to end the interview here?",
            "Would you like us to end the interview here?",
            "Shall we bring the interview to a close here?",
        ),
        "hi": (
            "क्या आप साक्षात्कार यहीं समाप्त करना चाहते हैं?",
            "क्या आप सचमुच साक्षात्कार यहीं समाप्त करना चाहते हैं?",
            "क्या हम साक्षात्कार यहीं समाप्त कर दें?",
        ),
    },
)
# Ruling 52: anything but a clear yes resumes, uncounted.
END_DECLINED_LINES = LineSet(
    "end_declined",
    {
        "hi-en": (
            "Theek hai, hum aage badhte hain.",
            "Bilkul, phir jaari rakhte hain.",
            "Achha, to chaliye aage baat karte hain.",
        ),
        "en": (
            "Very well, let us continue.",
            "Of course, we will carry on.",
            "Certainly, let us go on.",
        ),
        "hi": (
            "ठीक है, हम आगे बढ़ते हैं।",
            "बिल्कुल, फिर जारी रखते हैं।",
            "अच्छा, तो आगे बात करते हैं।",
        ),
    },
)
# A garbled turn, or one that was only noise: the panel asks for it again. `actor.CLARIFY_LINES` is
# the first wording (the UPSC board's own line, and Ruling 37's: ask generically, never echo the
# transcript), so a first clarify is what it always was. Not gendered: "could not hear" is said
# of the room, not of the speaker.
CLARIFY_PANEL_LINES = LineSet(
    "clarify",
    {
        "hi-en": (
            CLARIFY_LINES["hi-en"],
            "Kshama kijiye, aawaz saaf nahi aayi. Kya aap dobara bata sakte hain?",
            "Maaf kijiye, yeh mujhe theek se sunai nahi diya. Ek baar phir kahiye?",
        ),
        "en": (
            CLARIFY_LINES["en"],
            "Pardon me, that was not clear to us. Would you say it once more?",
            "I am sorry, the last part did not come through. Could you repeat it?",
        ),
        "hi": (
            CLARIFY_LINES["hi"],
            "क्षमा कीजिए, आवाज़ साफ़ नहीं आई। क्या आप दोबारा बता सकते हैं?",
            "माफ़ कीजिए, यह मुझे ठीक से सुनाई नहीं दिया। एक बार फिर कहिए?",
        ),
    },
)
# The Chairperson's normal close, when the agenda is done.
CLOSE_LINES = _once(
    "close",
    "Thank you, that will be all.",
    "Thank you, that will be all.",
    "धन्यवाद, बस इतना ही।",
)
# After a confirmed `end` request.
END_CLOSE_LINES = _once(
    "end_close",
    "Theek hai, aapke samay ke liye dhanyavaad. Thank you, that will be all.",
    "Very well, thank you for your time. That will be all.",
    "ठीक है, आपके समय के लिए धन्यवाद। बस इतना ही।",
)
# Ruling 47: the spoken close when the interview's time runs out.
WRAP_UP_CLOSE_LINES = _once(
    "wrap_up_close",
    "Hamara samay yahin poora hota hai. Aapka dhanyavaad. Thank you, that will be all.",
    "Our time ends here. Thank you, that will be all.",
    "हमारा समय यहीं पूरा होता है। आपका धन्यवाद। बस इतना ही।",
)
# Ruling 49: the courteous close at a test user's answer cap, truthful about why it ends.
CAP_CLOSE_LINES = _once(
    "cap_close",
    "Is abhyaas interview ki tay seema yahin poori hoti hai. Aapke samay ke liye "
    "dhanyavaad. Thank you, that will be all.",
    "This practice interview has reached its set limit. Thank you for your time. "
    "That will be all.",
    "इस अभ्यास साक्षात्कार की तय सीमा यहीं पूरी होती है। आपके समय के लिए धन्यवाद। बस इतना ही।",
)

ALL_LINE_TABLES: tuple[LineSet, ...] = (
    RITUAL_LINES,
    HANDOFF_LINES,
    CHANGE_TOPIC_ACK_LINES,
    SKIP_ACK_LINES,
    REPEAT_PREFIX_LINES,
    END_CONFIRM_LINES,
    END_DECLINED_LINES,
    CLARIFY_PANEL_LINES,
    CLOSE_LINES,
    END_CLOSE_LINES,
    WRAP_UP_CLOSE_LINES,
    CAP_CLOSE_LINES,
)


def line_for(table: Mapping[str, str], mix: str, uses: int = 0) -> str:
    """The table's line for this mix, Hinglish when the mix is unknown (as `CLARIFY_LINES`).
    `uses` is how often a `LineSet` was used before, which picks the wording (module docstring)."""
    if isinstance(table, LineSet):
        return table.wording(mix, uses)
    return table.get(mix, table["hi-en"])


def handoff_line(name: str, mix: str, uses: int = 0) -> str:
    return line_for(HANDOFF_LINES, mix, uses).format(name=name)
