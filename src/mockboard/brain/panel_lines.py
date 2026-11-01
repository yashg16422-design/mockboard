"""Fixed lines of the panel, by language mix (CTO_NOTES_06 §4, like `CLARIFY_LINES`).

Formal UPSC register: always "aap". Nothing here is wired into a session yet (P3, P4 use them).
The Chairperson speaks the ritual, hand-offs, confirmation and closes; a member speaks the
acknowledgements and the repeat prefix inside their own segment, so those lines avoid gendered
verbs ("dohrata hoon"). No line states a duration or a number: limits may change (Rulings 47, 49).
"hi-en" is Hinglish in Latin script; whether the board writes Devanagari follows Ruling 39.
"""

from __future__ import annotations

# The ritual before the first question.
RITUAL_LINES: dict[str, str] = {
    "hi-en": "Namaste. Please be seated.",
    "en": "Good day. Please be seated.",
    "hi": "नमस्ते। कृपया बैठिए।",
}
# Chairperson's hand-off to the next member. Takes the persona's display name.
HANDOFF_LINES: dict[str, str] = {
    "hi-en": "Dhanyavaad. {name}, please.",
    "en": "Thank you. {name}, please.",
    "hi": "धन्यवाद। {name}, कृपया।",
}
# Acknowledges a topic change before the hand-off.
CHANGE_TOPIC_ACK_LINES: dict[str, str] = {
    "hi-en": "Zaroor, koi baat nahi.",
    "en": "Certainly, no difficulty at all.",
    "hi": "ज़रूर, कोई बात नहीं।",
}
# Acknowledges a skipped question before the next seed.
SKIP_ACK_LINES: dict[str, str] = {
    "hi-en": "Koi baat nahi, ise chhod dete hain.",
    "en": "That is quite all right, we will leave that one.",
    "hi": "कोई बात नहीं, इसे छोड़ देते हैं।",
}
# Said before the last question, verbatim.
REPEAT_PREFIX_LINES: dict[str, str] = {
    "hi-en": "Zaroor, sawal phir se suniye.",
    "en": "Certainly. Here is the question again.",
    "hi": "ज़रूर, प्रश्न फिर से सुनिए।",
}
# Ruling 52: one spoken confirmation before an `end` request closes the interview. The hi-en
# wording is the ruling's brief, verbatim.
END_CONFIRM_LINES: dict[str, str] = {
    "hi-en": "Aap interview yahin samapt karna chahte hain?",
    "en": "Do you wish to end the interview here?",
    "hi": "क्या आप साक्षात्कार यहीं समाप्त करना चाहते हैं?",
}
# Ruling 52: anything but a clear yes resumes, uncounted.
END_DECLINED_LINES: dict[str, str] = {
    "hi-en": "Theek hai, hum aage badhte hain.",
    "en": "Very well, let us continue.",
    "hi": "ठीक है, हम आगे बढ़ते हैं।",
}
# The Chairperson's normal close, when the agenda is done.
CLOSE_LINES: dict[str, str] = {
    "hi-en": "Thank you, that will be all.",
    "en": "Thank you, that will be all.",
    "hi": "धन्यवाद, बस इतना ही।",
}
# After a confirmed `end` request.
END_CLOSE_LINES: dict[str, str] = {
    "hi-en": "Theek hai, aapke samay ke liye dhanyavaad. Thank you, that will be all.",
    "en": "Very well, thank you for your time. That will be all.",
    "hi": "ठीक है, आपके समय के लिए धन्यवाद। बस इतना ही।",
}
# Ruling 47: the spoken close when the interview's time runs out.
WRAP_UP_CLOSE_LINES: dict[str, str] = {
    "hi-en": "Hamara samay yahin poora hota hai. Aapka dhanyavaad. Thank you, that will be all.",
    "en": "Our time ends here. Thank you, that will be all.",
    "hi": "हमारा समय यहीं पूरा होता है। आपका धन्यवाद। बस इतना ही।",
}
# Ruling 49: the courteous close at a test user's answer cap, truthful about why it ends.
CAP_CLOSE_LINES: dict[str, str] = {
    "hi-en": "Is abhyaas interview ki tay seema yahin poori hoti hai. Aapke samay ke liye "
    "dhanyavaad. Thank you, that will be all.",
    "en": "This practice interview has reached its set limit. Thank you for your time. "
    "That will be all.",
    "hi": "इस अभ्यास साक्षात्कार की तय सीमा यहीं पूरी होती है। आपके समय के लिए धन्यवाद। बस इतना ही।",
}

ALL_LINE_TABLES: tuple[dict[str, str], ...] = (
    RITUAL_LINES,
    HANDOFF_LINES,
    CHANGE_TOPIC_ACK_LINES,
    SKIP_ACK_LINES,
    REPEAT_PREFIX_LINES,
    END_CONFIRM_LINES,
    END_DECLINED_LINES,
    CLOSE_LINES,
    END_CLOSE_LINES,
    WRAP_UP_CLOSE_LINES,
    CAP_CLOSE_LINES,
)


def line_for(table: dict[str, str], mix: str) -> str:
    """The table's line for this mix, Hinglish when the mix is unknown (as `CLARIFY_LINES`)."""
    return table.get(mix, table["hi-en"])


def handoff_line(name: str, mix: str) -> str:
    return line_for(HANDOFF_LINES, mix).format(name=name)
