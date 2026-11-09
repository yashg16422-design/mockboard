"""The study partner's fixed lines and its spoken-line guards. Pure, no model, no I/O.

Fixed lines (no model call): the opener, the request to repeat, the "take your time" line, and
one template question per move, used when the model line fails or a guard cuts it before its
question. Every template that asks something asks exactly one question.

Guards, applied to every model sentence (text and voice alike):
* the line ends at its first question: anything after it is dropped (one question per line);
* a sentence with a figure the speaker never said is dropped (the partner never brings facts or
  numbers of its own).

Languages: "hi-en" Hinglish in Latin script, "en" English, "hi" Hindi in Devanagari, "ta-en"
Tamil-English in Tamil script.
UNVERIFIED: the Tamil wording is ours and needs a fluent speaker's review.
"""

from __future__ import annotations

import re

from mockboard.brain.numbers import nfc, to_ascii_digits
from mockboard.brain.schemas import Move
from mockboard.voice.lines import split_sentences

MIXES: tuple[str, ...] = ("hi-en", "en", "hi", "ta-en")
DEFAULT_MIX = "hi-en"
LANGUAGE_MIX: dict[str, str] = {
    "hinglish": "hi-en",
    "english": "en",
    "hindi": "hi",
    "tanglish": "ta-en",
}

# Move targets the partner uses (BoardMove.target).
RECAP_TARGET = "recap"  # move_on: a short recap, then the next point
CLOSE_TARGET = "close"  # move_on: the close; the session ends
THINK_TARGET = "think"  # reassure: the speaker asked for a moment; fixed line, same question
REPEAT_TARGET = "repeat"  # reassure: the speaker asked to hear the question again; fixed line
SKIP_TARGET = "skip"  # move_on: the speaker asked to leave this point; fixed line, next point
CONFIRM_END_TARGET = "confirm_end"  # reassure: "shall we stop here?" before the close
RESUME_TARGET = "resume"  # reassure: the speaker did not say a clear yes; the question stands
FIXED_TARGETS = frozenset({THINK_TARGET, REPEAT_TARGET, SKIP_TARGET, CONFIRM_END_TARGET,
                           RESUME_TARGET})

OPENERS: dict[str, str] = {
    "hi-en": "Namaste! Aaj hum {topic} par baat karenge. Shuru kijiye: aapka pehla point kya hai?",
    "en": "Hello! Today we will talk about {topic}. Let us begin: what is your first point?",
    "hi": "नमस्ते! आज हम {topic} पर बात करेंगे। शुरू कीजिए: आपका पहला पॉइंट क्या है?",
    # UNVERIFIED: Tamil wording.
    "ta-en": "வணக்கம்! இன்று நாம் {topic} பற்றிப் பேசுவோம். தொடங்குங்கள்: உங்கள் முதல் point என்ன?",
}

# The partner puts the problem on its own side ("I did not catch that"), never on the speaker.
CLARIFY_LINES: dict[str, str] = {
    "hi-en": "Maaf kijiye, main theek se sun nahi paaya. Kya aap apni baat ek baar phir keh "
    "sakte hain?",
    "en": "Sorry, I did not catch that clearly. Could you say your point once more?",
    "hi": "माफ़ कीजिए, मैं ठीक से सुन नहीं पाया। क्या आप अपनी बात एक बार फिर कह सकते हैं?",
    "ta-en": "மன்னிக்கவும், தெளிவாகக் கேட்கவில்லை. இன்னொரு முறை சொல்ல முடியுமா?",
}

# "Take your time": no question, the earlier question stands, and the next turn is a long one.
THINK_LINES: dict[str, str] = {
    "hi-en": "Zaroor, aaram se sochiye, koi jaldi nahi hai. Jab taiyaar hon, tab boliye.",
    "en": "Of course, take your time, there is no hurry. Speak whenever you are ready.",
    "hi": "ज़रूर, आराम से सोचिए, कोई जल्दी नहीं है। जब तैयार हों, तब बोलिए।",
    # UNVERIFIED: Tamil wording.
    "ta-en": "கண்டிப்பாக, நிதானமாக யோசியுங்கள், அவசரம் இல்லை. தயாரானதும் சொல்லுங்கள்.",
}

# "Again, please": a short lead-in, then the question the speaker last heard, word for word.
REPEAT_LEADS: dict[str, str] = {
    "hi-en": "Zaroor, phir se:",
    "en": "Of course, once more:",
    "hi": "ज़रूर, फिर से:",
    # UNVERIFIED: Tamil wording.
    "ta-en": "சரி, மீண்டும்:",
}

# "Let us leave this point": no recap of a point the speaker set aside, one question.
SKIP_LINES: dict[str, str] = {
    "hi-en": "Koi baat nahi, is point ko yahin chhodte hain. Aapka agla point kya hai?",
    "en": "No problem, let us leave this point here. What is your next point?",
    "hi": "कोई बात नहीं, इस पॉइंट को यहीं छोड़ते हैं। आपका अगला पॉइंट क्या है?",
    # UNVERIFIED: Tamil wording.
    "ta-en": "பரவாயில்லை, இந்த point-ஐ இங்கே விட்டுவிடலாம். உங்கள் அடுத்த point என்ன?",
}

# One spoken check before the close, as the board does (Ruling 52): a request to end can be
# misheard, and the close cannot be undone.
CONFIRM_END_LINES: dict[str, str] = {
    "hi-en": "Kya aap aaj ka session yahin khatam karna chahte hain?",
    "en": "Would you like to stop today's session here?",
    "hi": "क्या आप आज का सत्र यहीं समाप्त करना चाहते हैं?",
    # UNVERIFIED: Tamil wording.
    "ta-en": "இன்றைய session-ஐ இங்கே முடிக்க விரும்புகிறீர்களா?",
}

# Anything but a clear yes: carry on, and the question from before the check stands.
RESUME_LEADS: dict[str, str] = {
    "hi-en": "Theek hai, jaari rakhte hain.",
    "en": "All right, let us carry on.",
    "hi": "ठीक है, जारी रखते हैं।",
    # UNVERIFIED: Tamil wording.
    "ta-en": "சரி, தொடரலாம்.",
}

CONFER_LINES: dict[str, str] = {
    "hi-en": "Ek pal, main aapke points likh raha hoon.",
    "en": "One moment, I am writing down your points.",
    "hi": "एक पल, मैं आपके पॉइंट लिख रहा हूँ।",
    # UNVERIFIED: Tamil wording.
    "ta-en": "ஒரு நிமிடம், உங்கள் points-ஐ எழுதுகிறேன்.",
}

# One fixed question per move (and the close, which asks nothing). Used when the model line
# fails, and to finish a line a guard cut before its question.
_TEMPLATES: dict[str, dict[str, str]] = {
    "hi-en": {
        "probe": "Is point ke peeche aapka sabse mazboot kaaran kya hai?",
        "pressure": "Kya aap iska ek thos udaharan de sakte hain?",
        "counter": "Kuch log kahenge ki iska doosra pehlu bhi hai; aap unhe kya jawab denge?",
        "steelman": "Is baat ko dhyaan mein rakhkar, aapka point apne sabse mazboot roop mein "
        "kya hoga?",
        "reassure": "Koi baat nahi, aaram se. Ek shuruaat aise ho sakti hai: yeh vishay aam "
        "logon ki zindagi ko kaise chhoota hai?",
        "recap": "Theek hai, yeh point ab saaf hai. Aapka agla point kya hai?",
        "close": "Theek hai, aaj yahin rukte hain. Aapke points aur agle kadam aapke notes mein "
        "likhe hain. Shukriya!",
    },
    "en": {
        "probe": "What is the main reason behind this point?",
        "pressure": "Could you give one concrete example of this?",
        "counter": "Some would argue there is another side to this; how would you answer them?",
        "steelman": "Keeping that in mind, what is the strongest version of your point?",
        "reassure": "No problem, take it slowly. One way to start: how does this topic touch "
        "ordinary people's lives?",
        "recap": "All right, that point is clearer now. What is your next point?",
        "close": "Let us stop here for today. Your points and next steps are in your notes. "
        "Thank you!",
    },
    "hi": {
        "probe": "इस पॉइंट के पीछे आपका सबसे मज़बूत कारण क्या है?",
        "pressure": "क्या आप इसका एक ठोस उदाहरण दे सकते हैं?",
        "counter": "कुछ लोग कहेंगे कि इसका दूसरा पहलू भी है; आप उन्हें क्या जवाब देंगे?",
        "steelman": "इस बात को ध्यान में रखकर, आपका पॉइंट अपने सबसे मज़बूत रूप में क्या होगा?",
        "reassure": "कोई बात नहीं, आराम से। एक शुरुआत ऐसे हो सकती है: यह विषय आम लोगों की "
        "ज़िंदगी को कैसे छूता है?",
        "recap": "ठीक है, यह पॉइंट अब साफ़ है। आपका अगला पॉइंट क्या है?",
        "close": "ठीक है, आज यहीं रुकते हैं। आपके पॉइंट और अगले कदम आपके नोट्स में लिखे हैं। "
        "शुक्रिया!",
    },
    # UNVERIFIED: Tamil wording, for a fluent speaker's review.
    "ta-en": {
        "probe": "இந்த point-க்குப் பின்னால் உள்ள முக்கியக் காரணம் என்ன?",
        "pressure": "இதற்கு ஒரு தெளிவான உதாரணம் சொல்ல முடியுமா?",
        "counter": "சிலர் இதற்கு இன்னொரு பக்கமும் உண்டு என்று சொல்லலாம்; அவர்களுக்கு உங்கள் "
        "பதில் என்ன?",
        "steelman": "அதையும் மனதில் வைத்து, உங்கள் point-இன் வலுவான வடிவம் என்ன?",
        "reassure": "பரவாயில்லை, நிதானமாக. இப்படித் தொடங்கலாம்: இந்த விஷயம் சாதாரண மக்களின் "
        "வாழ்க்கையை எப்படித் தொடுகிறது?",
        "recap": "சரி, இந்த point இப்போது தெளிவாக இருக்கிறது. உங்கள் அடுத்த point என்ன?",
        "close": "சரி, இன்று இங்கே நிறுத்துவோம். உங்கள் points-உம் அடுத்த படிகளும் உங்கள் "
        "notes-இல் உள்ளன. நன்றி!",
    },
}


def mix_for(language: str) -> str:
    return LANGUAGE_MIX.get(language, DEFAULT_MIX)


def _table(table: dict[str, str], mix: str) -> str:
    return table.get(mix, table[DEFAULT_MIX])


def opener_line(topic: str, mix: str) -> str:
    return _table(OPENERS, mix).format(topic=topic)


def clarify_line(mix: str) -> str:
    return _table(CLARIFY_LINES, mix)


def think_line(mix: str) -> str:
    return _table(THINK_LINES, mix)


def confer_line(mix: str) -> str:
    return _table(CONFER_LINES, mix)


def _with_question(lead: str, question: str, mix: str) -> str:
    """A fixed lead-in and the earlier question; the opener's question if there is none."""
    question = (question or "").strip()
    if not question or "?" not in question:
        question = template_question(Move.move_on, RECAP_TARGET, mix) or ""
    return f"{lead} {question}".strip()


def repeat_line(question: str, mix: str) -> str:
    return _with_question(_table(REPEAT_LEADS, mix), question, mix)


def resume_line(question: str, mix: str) -> str:
    return _with_question(_table(RESUME_LEADS, mix), question, mix)


def skip_line(mix: str) -> str:
    return _table(SKIP_LINES, mix)


def confirm_end_line(mix: str) -> str:
    return _table(CONFIRM_END_LINES, mix)


def template_key(move: Move, target: str | None) -> str:
    """The template a move uses: move_on is a recap or the close; the rest by move name."""
    if move == Move.move_on:
        return CLOSE_TARGET if target == CLOSE_TARGET else RECAP_TARGET
    return move.value if move.value in _TEMPLATES[DEFAULT_MIX] else "probe"


def template_line(move: Move, target: str | None, mix: str) -> str:
    """The move's whole fixed line (the fallback when the model line fails)."""
    table = _TEMPLATES.get(mix, _TEMPLATES[DEFAULT_MIX])
    return table[template_key(move, target)]


def template_question(move: Move, target: str | None, mix: str) -> str | None:
    """The move's one fixed question, to finish a line cut short. None for the close, for the
    "take your time" line (it asks nothing) and for the request lines that carry their own."""
    if move == Move.move_on and target == CLOSE_TARGET:
        return None
    if move == Move.reassure and target in FIXED_TARGETS:
        return None  # think asks nothing; repeat, resume and confirm carry their own question
    if move == Move.move_on and target == SKIP_TARGET:
        return split_sentences(skip_line(mix))[-1]
    if move == Move.clarify:
        return split_sentences(clarify_line(mix))[-1]
    line = template_line(move, target, mix)
    questions = [s for s in split_sentences(line) if s.rstrip().endswith("?")]
    return questions[-1] if questions else None


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
# Tamil digits (rare in speech recognition output) are read like the others.
_TAMIL_DIGITS = str.maketrans("௦௧௨௩௪௫௬௭௮௯", "0123456789")


def _figures(text: str) -> set[str]:
    plain = to_ascii_digits(nfc(text)).translate(_TAMIL_DIGITS)
    return {m.group(0).replace(",", "") for m in _DIGITS.finditer(plain)}


def says_unsaid_figure(sentence: str, speech: str) -> bool:
    """True when the sentence has a figure the speaker never said (any script's digits)."""
    said = _figures(speech)
    return any(f not in said for f in _figures(sentence))


def asks(sentence: str) -> bool:
    return sentence.rstrip().rstrip("\"'”’)").endswith("?")


def question_count(text: str) -> int:
    """How many questions a line asks: sentences ending in a question mark, plus any extra
    question marks inside one sentence ("Why? And how?" is two)."""
    return sum(max(1, s.count("?")) for s in split_sentences(text) if "?" in s)


def clean_line(text: str, speech: str) -> tuple[str, list[str]]:
    """The line as it may be said: sentences up to and including the first question, without
    any sentence carrying a figure the speaker never said. Returns (text, dropped sentences)."""
    kept: list[str] = []
    dropped: list[str] = []
    for sentence in split_sentences(text):
        if says_unsaid_figure(sentence, speech):
            dropped.append(sentence)
            continue
        kept.append(sentence)
        if "?" in sentence:
            break
    # One question per line: a sentence with two question marks keeps only its first question.
    if kept and kept[-1].count("?") > 1:
        kept[-1] = kept[-1][: kept[-1].index("?") + 1]
    return " ".join(kept).strip(), dropped
