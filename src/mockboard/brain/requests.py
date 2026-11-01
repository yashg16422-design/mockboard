"""Deterministic detector for candidate requests (CTO_NOTES_06 section 3, chunk P1).

`detect_request` reads the raw transcript of a turn and says whether the candidate asked the
board to do something: change the topic, skip, repeat, clarify, or end. It calls no model and
works on Latin Hinglish, English, Devanagari and ASR-garbled spellings, so it still answers when
the listener failed or called the turn garbled. Nothing imports this module yet (P1 is unwired).

Steps: NFC normalise, lowercase, strip punctuation; canonicalise tokens from a variant table in
both scripts; allow edit distance 1 to a short list of key words; match phrase patterns; apply
the position rule; refuse matches whose context shows the words are not a request.

# UNVERIFIED: the variant lists have never met real Prisma transcripts of these phrases. The
# corpus in tests/data/request_corpus.jsonl is synthetic, so its recall says nothing about
# real Prisma text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache


class RequestKind(StrEnum):
    CHANGE_TOPIC = "change_topic"
    SKIP = "skip"
    REPEAT = "repeat"
    CLARIFY_QUESTION = "clarify_question"
    END = "end"


@dataclass(frozen=True)
class RequestHit:
    kind: RequestKind
    avoid_terms: tuple[str, ...]  # topics the candidate refused, as spoken; change_topic only
    pattern_id: str


# A hit counts if the answer is at most SHORT_ANSWER_WORDS, or starts in the last TAIL_WORDS.
SHORT_ANSWER_WORDS = 20
TAIL_WORDS = 12
END_MAX_WORDS = 12  # section 3: an end request is short; a long turn cannot end a paid session
MIN_FUZZY_LETTERS = 5

# ---------------------------------------------------------------------------
# Vocabulary: canonical token -> spellings in both scripts
# ---------------------------------------------------------------------------

_VARIANTS: dict[str, tuple[str, ...]] = {
    "alawa": ("alaawa", "ilawa", "alava", "allawa", "alaava", "elawa", "ilaava", "ilaawa",
              "aalawa", "alwa", "alwaa", "siwa", "siwaay", "sivay", "siva", "अलावा", "इलावा",
              "सिवा", "सिवाय"),
    "topic": ("topics", "topik", "topick", "toppic", "subject", "vishay", "vishaya", "vishai",
              "visay", "टॉपिक", "टोपिक", "टापिक", "विषय", "सब्जेक्ट"),
    "sawal": ("sawaal", "saval", "savaal", "swal", "swaal", "question", "questions", "qestion",
              "kwestion", "kweschan", "prashn", "prashna", "sawl", "सवाल", "प्रश्न", "क्वेश्चन"),
    "baat": ("baath", "bat", "बात"),
    "karte": ("karthe", "kartey", "करते"),
    "karein": ("karen", "kare", "karey", "करें", "करे"),
    "kijiye": ("kariye", "kijie", "karie", "kijiyega", "kariyega", "कीजिए", "कीजिये", "करिए",
               "करिये"),
    "karo": ("करो",),
    "kar": ("कर",),
    "karna": ("करना",),
    "karni": ("करनी",),
    "hain": ("hai", "hein", "hen", "हैं", "है"),
    "kisi": ("kissi", "किसी"),
    "aur": ("aor", "और"),
    "koi": ("कोई",),
    "dusra": ("doosra", "dusre", "doosre", "dusri", "doosri", "दूसरा", "दूसरे", "दूसरी"),
    "kuch": ("kuchh", "कुछ"),
    "par": ("pe", "pr", "पर", "पे"),
    "ke": ("kay", "के"),
    "ka": ("का",),
    "ko": ("को",),
    "se": ("से",),
    "is": ("इस",),
    "bare": ("baare", "बारे"),
    "mein": ("में", "मे"),
    "chhodiye": ("chodiye", "chhodie", "chodie", "छोड़िए", "छोड़िये"),
    "chhodo": ("chodo", "छोड़ो"),
    "chhod": ("chod", "छोड़"),
    "chhodte": ("chodte", "छोड़ते"),
    "chhodein": ("chhoden", "chodein", "छोड़ें"),
    "chhodna": ("chodna", "छोड़ना"),
    "chhodkar": ("chodkar", "chhodke", "chodke", "छोड़कर", "छोड़के"),
    "dete": ("देते",),
    "dijiye": ("dijie", "दीजिए", "दीजिये"),
    "lijiye": ("lijie", "लीजिए", "लीजिये"),
    "aage": ("aagey", "agey", "आगे"),
    "badh": ("barh", "बढ़"),
    "badhte": ("barhte", "badte", "bhadte", "बढ़ते"),
    "badhiye": ("badhie", "barhiye", "बढ़िए", "बढ़िये"),
    "badhein": ("badhen", "barhein", "बढ़ें"),
    "chalte": ("चलते",),
    "chaliye": ("chalie", "चलिए", "चलिये"),
    "next": ("nekst", "नेक्स्ट"),
    "agla": ("agle", "agli", "अगला", "अगले", "अगली"),
    "please": ("plz", "pls", "kripya", "kripaya", "कृपया", "प्लीज़", "प्लीज"),
    "phir": ("fir", "pher", "फिर"),
    "dobara": ("dubara", "dobaara", "दोबारा", "दुबारा"),
    "ek": ("एक",),
    "baar": ("बार",),
    "boliye": ("bolie", "bolye", "boliyega", "बोलिए", "बोलिये"),
    "bolo": ("बोलो",),
    "bataiye": ("bataye", "batayiye", "bataie", "batayie", "batao", "बताइए", "बताइये", "बताएं",
                "बताएँ", "बताओ"),
    "puchiye": ("poochiye", "puchhiye", "pucchiye", "puchie", "puchho", "पूछिए", "पूछिये", "पूछो"),
    "puchna": ("poochna", "puchhna", "पूछना"),
    "kahiye": ("कहिए", "कहिये"),
    "dohraiye": ("dohraye", "dohrayiye", "दोहराइए", "दोहराइये"),
    "samjhaiye": ("samjhaye", "samjhaie", "समझाइए", "समझाइये"),
    "samajh": ("samaj", "samjh", "समझ"),
    "samjha": ("samjhi", "samajha", "samajhi", "समझा", "समझी"),
    "nahi": ("nahin", "nhi", "nai", "nahee", "नहीं", "नही"),
    "aaya": ("aya", "aayi", "aai", "ayi", "आया", "आई", "आयी"),
    "matlab": ("matalab", "mutlab", "मतलब"),
    "kya": ("kia", "kyaa", "क्या"),
    "aapka": ("apka", "aapki", "apki", "aapke", "आपका", "आपकी", "आपके"),
    "aap": ("आप",),
    "aapne": ("apne", "आपने"),
    "kaha": ("कहा",),
    "khatam": ("khatm", "khattam", "khtam", "samapt", "खत्म", "ख़त्म", "समाप्त"),
    "band": ("बंद", "बन्द"),
    "interview": ("intervew", "interveiw", "intrview", "interviw", "intervue", "intervu",
                  "saakshatkar", "sakshatkar",
                  "इंटरव्यू", "इन्टरव्यू", "साक्षात्कार"),
    "yahin": ("yahi", "yaheen", "यहीं", "यही"),
    "mujhe": ("mujhko", "मुझे"),
    "main": ("mai", "मैं"),
    "hum": ("हम",),
    "chahta": ("chahti", "chahte", "चाहता", "चाहती", "चाहते"),
    "hoon": ("hu", "hun", "हूँ", "हूं"),
    "sir": ("सर",),
    "maam": ("mam", "madam", "मैडम"),
    "ji": ("जी",),
    "ok": ("okay", "ओके"),
    "sorry": ("सॉरी",),
    "skip": ("स्किप",),
    "repeat": ("reapeat", "repit", "रिपीट"),
    "change": ("चेंज",),
    "attempt": ("अटेम्प्ट",),
    "jawab": ("javab", "uttar", "जवाब", "उत्तर"),
    "dena": ("देना",),
    "sunai": ("सुनाई",),
    "diya": ("दिया",),
    "cheez": ("cheez", "chiz", "चीज़", "चीज"),
    "shabdon": ("शब्दों",),
    "tareeke": ("tarike", "तरीके"),
    "spasht": ("स्पष्ट",),
    "clear": ("क्लियर",),
    "lets": ("letz",),
    "sakte": ("सकते",),
    "kehna": ("कहना",),
    "keh": ("कह",),
    "rahe": ("रहे",),
    "bas": ("बस",),
    "badal": ("बदल",),
    "badalte": ("बदलते",),
    "badliye": ("बदलिए", "बदलिये"),
    "rehne": ("rahne", "रहने"),
    "jaane": ("jane", "जाने"),
    "talk": ("tock", "tok"),
    "move": ("moov", "muv"),
    "what": ("wat",),
    "this": ("dis",),
    "that": ("dat",),
    "thoda": ("thora", "थोड़ा"),
    "zara": ("ज़रा", "जरा"),
    "ab": ("अब",),
}

# Words ASR runs together. Split before canonicalising.
_RUN_TOGETHER: dict[str, tuple[str, ...]] = {
    "phirse": ("phir", "se"),
    "firse": ("phir", "se"),
    "फिरसे": ("phir", "se"),
    "kisiaur": ("kisi", "aur"),
    "baatkarte": ("baat", "karte"),
    "aagebadhte": ("aage", "badhte"),
    "nextquestion": ("next", "sawal"),
    "samajhnahi": ("samajh", "nahi"),
}

# Words ASR splits apart. Joined before canonicalising.
_SPLIT_APART: dict[tuple[str, str], str] = {
    ("kar", "te"): "karte",
    ("some", "thing"): "something",
    ("any", "thing"): "anything",
    ("a", "lawa"): "alawa",
    ("a", "lava"): "alawa",
    ("al", "awa"): "alawa",
    ("inter", "view"): "interview",
}

# Edit distance 1 applies to these words only. Common English neighbours of a wider list
# ("other"/"mother", "later"/"water") would turn real answers into requests.
_FUZZY_KEYS: tuple[str, ...] = (
    "alawa", "topic", "sawal", "question", "samajh", "matlab", "khatam", "interview", "repeat",
    "dobara", "dusra", "chhodiye", "badhiye", "bataiye", "boliye", "puchiye", "something",
    "kijiye", "please", "टॉपिक", "इंटरव्यू", "बताइए", "बोलिए",
)

# Real words one edit from a key word. They keep their meaning.
_NO_FUZZY: frozenset[str] = frozenset({
    "repeal", "repeals", "tropic", "tonic", "toxic", "topaz", "plead", "pleas", "pleases",
    "pleased", "sawan", "matlabi",
})


def _strip(text: str) -> tuple[str, list[bool]]:
    """Lowercase, drop punctuation, and mark clause breaks. Returns text and is-break per token."""
    text = unicodedata.normalize("NFC", text).casefold().replace("’", "'")
    text = text.replace("'", "")
    out: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if category[0] in "LMN":
            out.append(ch)
        elif category == "Cf":
            continue  # zero-width joiners inside Devanagari
        elif category[0] == "P" and ch not in "-_":
            out.append(" \x00 ")
        else:
            out.append(" ")
    tokens: list[str] = []
    breaks: list[bool] = []
    pending = False
    for tok in "".join(out).split():
        if tok == "\x00":
            pending = True
            continue
        tokens.append(tok)
        breaks.append(pending)
        pending = False
    return " ".join(tokens), breaks


def _norm_key(word: str) -> str:
    return unicodedata.normalize("NFC", word)


_CANON: dict[str, str] = {}
for _canon, _spellings in _VARIANTS.items():
    _CANON[_norm_key(_canon)] = _canon
    for _spelling in _spellings:
        _CANON[_norm_key(_spelling)] = _canon
_RUN_TOGETHER_NORM = {_norm_key(k): v for k, v in _RUN_TOGETHER.items()}
_FUZZY_NORM = tuple(_norm_key(k) for k in _FUZZY_KEYS)


def _within_one_edit(a: str, b: str) -> bool:
    """True when a and b differ by at most one insertion, deletion or substitution."""
    if abs(len(a) - len(b)) > 1:
        return False
    i = 0
    limit = min(len(a), len(b))
    while i < limit and a[i] == b[i]:
        i += 1
    return (
        a[i + 1 :] == b[i + 1 :] or a[i + 1 :] == b[i:] or a[i:] == b[i + 1 :]
    )


@lru_cache(maxsize=4096)
def _canonical(token: str) -> str:
    known = _CANON.get(token)
    if known is not None:
        return known
    if len(token) < MIN_FUZZY_LETTERS or token in _NO_FUZZY:
        return token
    for key in _FUZZY_NORM:
        if _within_one_edit(token, key):
            return _CANON.get(key, key)
    return token


@dataclass(frozen=True)
class _Tokens:
    raw: tuple[str, ...]  # as spoken, for avoid_terms
    canon: tuple[str, ...]
    breaks: tuple[bool, ...]  # True when punctuation came before this token
    text: str  # canon joined by single spaces


def _tokenise(text: str) -> _Tokens:
    stripped, breaks = _strip(text)
    words = stripped.split()
    raw: list[str] = []
    canon: list[str] = []
    out_breaks: list[bool] = []
    i = 0
    while i < len(words):
        word = words[i]
        brk = breaks[i]
        if i + 1 < len(words) and (word, words[i + 1]) in _SPLIT_APART:
            joined = _SPLIT_APART[(word, words[i + 1])]
            raw.append(joined)
            canon.append(_canonical(joined))
            out_breaks.append(brk)
            i += 2
            continue
        parts = _RUN_TOGETHER_NORM.get(word)
        if parts is not None:
            for n, part in enumerate(parts):
                raw.append(part)
                canon.append(_canonical(part))
                out_breaks.append(brk and n == 0)
        else:
            raw.append(word)
            canon.append(_canonical(word))
            out_breaks.append(brk)
        i += 1
    return _Tokens(tuple(raw), tuple(canon), tuple(out_breaks), " ".join(canon))


# ---------------------------------------------------------------------------
# Patterns, written against canonical tokens
# ---------------------------------------------------------------------------

_FILLER = r"(?:sir|maam|ji|please|sorry|ok|haan|acha|achha|um|umm|hmm|ab|bas|to|toh|well|so)"
_START = rf"^(?:{_FILLER} )*"
_END = rf"(?: {_FILLER})*$"
_AUR = r"(?:aur|or|our)"
_ASK = (
    r"(?:(?:can|could|shall|may) (?:we|i)|lets|let us|i (?:want|wish|would like|would prefer) to"
    r"|id (?:like|prefer) to|i would like to|(?:can|could|would|will) you|please)(?: please| now)?"
)
_MODAL = r"(?:can|could|shall|may) (?:we|i)(?: please| now)?"
_BAAT_VERB = (
    r"baat (?:karte hain|karein|kijiye|karo|karenge|kar sakte hain|kar lete hain|kar lijiye"
    r"|karna chahta hoon|karna chahunga|karna chahungi|karna hain|karni hain)"
)
_CHHOD = (
    r"(?:chhodiye|chhodo|chhodein|chhodte hain|chhod dete hain|chhod dijiye|chhod do|chhod dein"
    r"|chhod lete hain|chhod sakte hain|chhodna chahta hoon|chhodna chahunga|chhodna chahungi"
    r"|rehne dijiye|jaane dijiye)"
)
_ASK_VERB = r"(?:puchiye|lijiye|le lijiye|bataiye|kijiye|dijiye|boliye|chahiye)"
_OTHER = rf"(?:kisi {_AUR}|kisi dusra|koi {_AUR}|koi dusra|dusra)"
_SAY_AGAIN = r"(?:repeat|say (?:that|it)(?: again| once more| one more time)|say again)"
_IMPERATIVE = r"(?:kijiye|kar dijiye|kar sakte hain|karein|karo|karte hain|kar dete hain)"
_AGAIN_VERB = (
    r"(?:boliye|bolo|bataiye|puchiye|kahiye|dohraiye|bol dijiye|bata dijiye|puch lijiye)"
)
_REWORD = r"(?:rephrase|clarify|simplify|explain|elaborate|put)"
_NOT_DID = r"(?:did not|didnt|dont|do not|could not|couldnt|cant|cannot)"
_NOT_GOT = r"(?:aaya|aa raha|aa rahi|paya|payi)"

_PATTERNS: tuple[tuple[str, RequestKind, str], ...] = (
    # end: must name the interview, so "I will stop here" (the end of an answer) is not an end.
    ("end_en", RequestKind.END,
     r"(?:(?:i|we) (?:want|wish|would like|need) to|id like to|can we|could we|shall we"
     r"|lets|let us|please)(?: please)? (?:end|stop|finish|close|conclude|quit)"
     r" (?:the|this|my|our)(?: mock)? (?:interview|session)(?: here| now)?" + _END),
    ("end_en_bare", RequestKind.END,
     _START + r"(?:end|stop|finish) (?:the|this)(?: mock)? (?:interview|session)(?: here| now)?"
     + _END),
    ("end_hi", RequestKind.END,
     r"(?:interview|session) (?:ko )?(?:yahin |ab |abhi )?(?:khatam|band)"
     r" (?:karte hain|kijiye|karein|kar dijiye|kar dete hain|karo|kar sakte hain"
     r"|karna chahta hoon|karna hain|karna chahunga|karna chahungi)" + _END),

    # skip
    ("skip_next_bare", RequestKind.SKIP,
     _START + rf"(?:next|agla)(?: sawal)?(?: par)?(?: {_ASK_VERB}| chalte hain| chaliye)?" + _END),
    ("skip_next_en", RequestKind.SKIP,
     _ASK + r" (?:go to |move to |move on to |skip to |have |take |ask |ask me |get )?(?:the )?"
     r"next (?:sawal|one)" + _END),
    ("skip_next_tail", RequestKind.SKIP, r"next sawal(?: sir| maam| ji)? please" + _END),
    ("skip_agla_tail", RequestKind.SKIP,
     rf"agla sawal(?: par)? (?:{_ASK_VERB}|chalte hain|chaliye)" + _END),
    ("skip_ask", RequestKind.SKIP, _ASK + r" skip(?: this| that| the)?(?: sawal| one| it)?" + _END),
    ("skip_bare", RequestKind.SKIP,
     _START + r"skip(?: this| that| the)?(?: sawal| one| it)?" + _END),
    ("skip_hi", RequestKind.SKIP,
     rf"(?<!topic )(?<!topic ko )(?:sawal(?: ko)? )?skip {_IMPERATIVE}" + _END),
    ("skip_pass", RequestKind.SKIP,
     _START + r"(?:i |ill |i will |i would )?pass(?: on)?(?: this| that)?(?: sawal| one)?" + _END),
    ("skip_rather", RequestKind.SKIP,
     r"(?:id|i would|i will|ill) (?:rather|prefer to|prefer) (?:not|skip|pass)"
     r"(?: attempt| answer| take| try| respond to| speak on| comment on| comment)?"
     r"(?: on)?(?: this| that| the| it)?(?: sawal| one)?" + _END),
    ("skip_decline", RequestKind.SKIP,
     r"(?:dont want to|do not want to|would not like to|wouldnt like to|prefer not to"
     r"|cannot|cant|will not|wont) attempt(?: this| that| the| it)?(?: sawal| one)?" + _END),
    ("skip_decline_answer", RequestKind.SKIP,
     r"(?:dont want to|do not want to|would not like to|wouldnt like to|prefer not to)"
     r" answer(?: this| that| the| it)?(?: sawal| one)?" + _END),
    ("skip_chhod", RequestKind.SKIP, rf"sawal(?: ko)? {_CHHOD}"),
    ("skip_chhod_bare", RequestKind.SKIP, _START + _CHHOD + _END),
    ("skip_hi_decline", RequestKind.SKIP,
     r"is sawal (?:ko|ka) (?:\S+ )?(?:attempt|jawab) nahi"
     r" (?:karna|karunga|karungi|dena|dunga|dungi|de sakta|de sakti)"),

    # change_topic
    ("change_other_baat", RequestKind.CHANGE_TOPIC,
     rf"(?:{_OTHER}|kuch {_AUR}|kuch dusra) (?:\S+ ){{0,4}}?{_BAAT_VERB}"),
    ("change_other_topic", RequestKind.CHANGE_TOPIC,
     rf"(?<!mera )(?<!meri )(?<!hamara ){_OTHER} (?:topic|sawal|cheez)"
     rf"(?: par| ke bare mein)?(?: {_ASK_VERB}| chalte hain| chaliye)?" + _END),
    ("change_kuch_aur", RequestKind.CHANGE_TOPIC,
     rf"(?:kuch {_AUR}|kuch bhi|kuch dusra) {_ASK_VERB}" + _END),
    ("change_alawa_tail", RequestKind.CHANGE_TOPIC,
     rf"alawa (?:\S+ )?(?:kisi {_AUR}|koi {_AUR}|kuch {_AUR}|kuch bhi|koi bhi|dusra"
     rf"|something else|some other topic|anything else)(?: topic| sawal| cheez)?"
     rf"(?: par)?(?: {_ASK_VERB})?" + _END),
    ("change_aage", RequestKind.CHANGE_TOPIC,
     r"aage (?:badhte hain|badhiye|badhein|badho|badh sakte hain|badh jaate hain"
     r"|badh jaiye|chalte hain|chaliye)"),
    ("change_topic_chhod", RequestKind.CHANGE_TOPIC, rf"topic(?: ko)? {_CHHOD}"),
    ("change_x_chhod", RequestKind.CHANGE_TOPIC,
     _START + rf"(?:\S+ ){{1,3}}?ko {_CHHOD}(?: \S+){{0,5}}$"),
    ("change_badal", RequestKind.CHANGE_TOPIC,
     r"topic (?:change |badal |switch )?"
     r"(?:kijiye|karein|karte hain|kar dijiye|kar lete hain|kar sakte hain|karo"
     r"|badal dete hain|badal dijiye|badal lete hain|badal sakte hain|badliye|badalte hain"
     r"|dete hain|dijiye)" + _END),
    ("change_topic_skip", RequestKind.CHANGE_TOPIC,
     rf"topic(?: ko)? skip {_IMPERATIVE}" + _END),
    ("change_agla_topic", RequestKind.CHANGE_TOPIC,
     _START + rf"(?:next|agla|dusra) topic(?: par)?(?: {_ASK_VERB}| chalte hain| chaliye)?"
     + _END),
    ("change_en_modal", RequestKind.CHANGE_TOPIC, rf"{_MODAL} move (?:on|one)(?: from \S+)?"),
    ("change_en_move", RequestKind.CHANGE_TOPIC,
     r"(?:lets|let us|i want to|i would like to|id like to|please|i would prefer to)"
     r"(?: please| now)? move (?:on|one)"
     r"(?: from (?:this|that|the|here|\S+)| to (?:this|the next|next|another|something"
     r"|some other|a different|other)(?: \S+){0,2})?" + _END),
    ("change_en_ahead", RequestKind.CHANGE_TOPIC,
     rf"{_ASK} (?:move|go) (?:ahead|forward)" + _END),
    ("change_en_move_bare", RequestKind.CHANGE_TOPIC,
     _START + r"(?:move on|moving on|move one)" + _END),
    ("change_en_bare", RequestKind.CHANGE_TOPIC,
     _START + r"(?:something else|some other topic|another topic|a different topic"
     r"|different topic)" + _END),
    ("change_en_else", RequestKind.CHANGE_TOPIC,
     rf"(?:{_ASK}|we can) (?:talk about|talk on|speak about|speak on|discuss|switch to|move to"
     rf"|move on to|go to|shift to|turn to|ask me about|ask me|ask about|ask|focus on|get to"
     rf"|come to|take up|take) (?:\S+ ){{0,2}}?(?:something else|something different"
     rf"|something other|anything else|anything other"
     rf"|(?:some other|another|a different|other) (?:topic|sawal|area|areas|cheez))"),
    ("change_en_change", RequestKind.CHANGE_TOPIC,
     rf"(?:{_ASK}|we can) (?:change|switch|skip|leave|drop)(?: the| this| our)? topic"),
    ("change_en_instead", RequestKind.CHANGE_TOPIC,
     rf"(?:instead of|other than|apart from|besides) (?:\S+ ){{1,3}}?{_ASK}"
     r" (?:talk about|speak about|discuss|switch to|move to|go to)"),
    ("change_en_anything_but", RequestKind.CHANGE_TOPIC,
     _START + r"(?:something|anything)(?: else)? (?:other than|but|except|besides|apart from)"
     r"(?: \S+){1,3}" + _END),
    ("change_en_enough", RequestKind.CHANGE_TOPIC,
     _START + r"enough (?:about|of|on)(?: \S+){1,3}" + _END),
    ("change_en_refuse", RequestKind.CHANGE_TOPIC,
     r"i (?:dont|do not) (?:want to|wish to) (?:talk about|discuss|speak about)(?: \S+){1,3}"
     + _END),
    ("change_en_rather", RequestKind.CHANGE_TOPIC,
     r"(?:id|i would) rather not (?:talk about|discuss|speak about)(?: \S+){1,3}" + _END),
    ("change_hi_refuse", RequestKind.CHANGE_TOPIC,
     r"mujhe (?:\S+ ){1,3}?(?:par|ke bare mein) baat nahi (?:karni|karna)(?: hain)?" + _END),

    # repeat
    ("repeat_bare", RequestKind.REPEAT,
     _START + rf"(?:(?:can|could|would|will) you (?:please )?)?{_SAY_AGAIN}"
     r"(?: that| it| this| the sawal| your sawal| the last sawal| sawal| again| once more"
     r"| one more time)*" + _END),
    ("repeat_tail", RequestKind.REPEAT,
     rf"(?:can|could|would|will) you (?:please )?{_SAY_AGAIN}"
     r"(?: that| it| this| the sawal| your sawal| the last sawal| sawal| again| once more"
     r"| one more time)*" + _END),
    ("repeat_missed", RequestKind.REPEAT,
     rf"i {_NOT_DID} (?:catch|hear)(?: that| it| you| the sawal| your sawal| what you said)?"
     r"(?: properly| clearly)?" + _END),
    ("repeat_short", RequestKind.REPEAT,
     _START + r"(?:pardon|pardon me|come again|what did you say|what was that|what was the sawal"
     r"|i missed that|i missed the sawal|phir se|dobara|dohraiye|one more time)" + _END),
    ("repeat_hi", RequestKind.REPEAT,
     rf"(?:phir se|dobara|ek baar aur|ek baar phir|ek aur baar)(?: \S+)? {_AGAIN_VERB}"),
    ("repeat_hi_sawal", RequestKind.REPEAT,
     rf"sawal (?:(?:phir se|dobara|ek baar aur)(?: \S+)? )?(?:{_AGAIN_VERB}|repeat {_IMPERATIVE})"),
    ("repeat_hi_verb", RequestKind.REPEAT, _START + rf"repeat {_IMPERATIVE}" + _END),
    ("repeat_hi_unheard", RequestKind.REPEAT,
     r"(?:sunai nahi (?:diya|di)|suna nahi|nahi suna|awaz nahi aaya)" + _END),
    ("repeat_hi_kaha", RequestKind.REPEAT,
     _START + r"(?:aapne kya kaha|kya kaha aapne|kya kaha)" + _END),

    # clarify_question
    ("clarify_mean_start", RequestKind.CLARIFY_QUESTION,
     _START + r"what do you mean(?: by(?: \S+){1,4}| exactly)?" + _END),
    ("clarify_mean_tail", RequestKind.CLARIFY_QUESTION,
     r"what do you mean(?: by that| exactly)?" + _END),
    ("clarify_what", RequestKind.CLARIFY_QUESTION,
     _START + r"what (?:exactly )?(?:are you asking|does that mean|does this mean|does it mean"
     r"|is the sawal asking|do you want me to say)" + _END),
    ("clarify_understand", RequestKind.CLARIFY_QUESTION,
     rf"i {_NOT_DID}(?: quite| really| fully)? (?:understand|get|follow)"
     r"(?: that| it| this| you| the sawal| your sawal| the last sawal"
     r"| what you mean| what you said| what you are asking)*" + _END),
    ("clarify_unsure", RequestKind.CLARIFY_QUESTION,
     r"(?:i am not sure i (?:understand|follow)|i am not clear(?: about)?|i am confused(?: by)?)"
     r"(?: the sawal)?" + _END),
    ("clarify_rephrase", RequestKind.CLARIFY_QUESTION,
     rf"(?:(?:can|could|would|will) you (?:please )?{_REWORD}|{_START}{_REWORD})"
     r"(?: that| it| this| the sawal| your sawal| what you mean| in simpler words| in simple words"
     r"| in other words| in another way| another way| once more| a bit)*" + _END),
    ("clarify_hi_sawal", RequestKind.CLARIFY_QUESTION,
     r"(?:sawal|aapka matlab|aapka baat|baat) (?:\S+ )?samajh nahi " + _NOT_GOT),
    ("clarify_hi_unclear", RequestKind.CLARIFY_QUESTION,
     r"sawal (?:\S+ )?(?:clear|spasht) nahi(?: hain)?" + _END),
    ("clarify_hi_bare", RequestKind.CLARIFY_QUESTION,
     _START + r"(?:mujhe |main |maine |hamein )?(?:\S+ )?(?:samajh nahi "
     + _NOT_GOT + r"|samjha nahi)" + _END),
    ("clarify_hi_ask", RequestKind.CLARIFY_QUESTION,
     r"(?:sawal|ise|isko|yeh|thoda|zara|aap) (?:\S+ )?"
     r"(?:samjhaiye|samjha dijiye|spasht kijiye|clear kijiye|aasan kijiye|saral kijiye)" + _END),
    ("clarify_hi_samjhaiye", RequestKind.CLARIFY_QUESTION, _START + r"samjhaiye" + _END),
    ("clarify_hi_words", RequestKind.CLARIFY_QUESTION,
     r"(?:dusra tareeke se|dusra shabdon mein|aasan shabdon mein|saral shabdon mein"
     r"|simple words mein|easy words mein|simple language mein)"
     r"(?: \S+)? (?:bataiye|puchiye|boliye|samjhaiye|kahiye)"),
    ("clarify_matlab_bare", RequestKind.CLARIFY_QUESTION, _START + r"matlab" + _END),
    ("clarify_matlab_kya", RequestKind.CLARIFY_QUESTION,
     _START + r"(?:aapka |iska |is sawal ka |sawal ka |yeh |is baat ka )?"
     r"(?:kya matlab|matlab kya)(?: hain)?(?: aapka)?" + _END),
    ("clarify_hi_asking", RequestKind.CLARIFY_QUESTION,
     r"aap kya (?:puchna chahta hain|keh rahe hain|kehna chahta hain)" + _END),
)

_COMPILED: tuple[tuple[str, RequestKind, re.Pattern[str]], ...] = tuple(
    (pid, kind, re.compile(rf"(?<!\S)(?:{pattern})(?!\S)")) for pid, kind, pattern in _PATTERNS
)

# Move-along phrases that name no topic and can close an ordinary sentence. Every other
# change_topic pattern carries an explicit topic-change word and counts as strong.
_WEAK_CHANGE: frozenset[str] = frozenset({
    "change_aage", "change_x_chhod", "change_en_modal", "change_en_move", "change_en_ahead",
    "change_en_move_bare",
})

# Polite openers contain "if" or "agar", and must not read as a conditional.
_POLITE = re.compile(
    r"\b(?:if (?:possible|i may|you dont mind|you do not mind|thats ok|that is ok|its ok)"
    r"|agar (?:ho sake|aap chahen|aap chahein|mumkin ho|possible ho|theek ho|ijazat ho)"
    r"(?: to| toh)?"
    r")\b"
)

# Someone else's words or habits, a condition, or a generality: within the same clause, before
# the phrase. Phrases that carry their own subject ("can we", "I want to") ignore _THIRD_PERSON.
# So do strong change_topic patterns: the words before them are usually the refused topic
# ("public administration ke alawa kisi aur topic pe baat karte hain").
_REPORTED = frozenset({
    "said", "says", "say", "asked", "asks", "told", "tells", "kaha", "kehte", "bola", "puchha",
    "pucha", "if", "agar", "jab", "when", "whenever", "unless", "while", "whether", "har",
    "hamesha", "often", "sometimes", "kabhi", "friend", "dost",
})
_THIRD_PERSON = frozenset({
    "log", "logon", "logo", "students", "student", "chhatra", "sarkar", "government", "govt",
    "neta", "netaon", "politicians", "politician", "ministers", "minister", "they", "people",
    "he", "she", "woh", "wo", "ve", "unhone", "unhe", "unko", "usne", "use", "candidates",
    "candidate", "teachers", "teacher", "kisan", "kisanon", "public", "janta", "yuva", "mil",
    "milkar", "saath",
})
_NEGATORS = frozenset({"dont", "not", "never", "mat", "na"})
_FIRST_PERSON_OBJECT = frozenset({"mujhe", "hamein", "hume", "humein", "humko"})
_OWN_SUBJECT = frozenset({"can", "could", "shall", "may", "lets", "let", "i", "id", "ill", "we",
                          "would", "will", "mujhe", "main"})
_AFTER_TOKENS = 3
_AFTER_BLOCK = frozenset({"said", "says", "kaha", "kehte", "mat", "log", "students", "bola"})
_RESULT_WORDS = frozenset({"toh", "then", "tab"})  # "...aage badhte hain to log..." is an "if"
_BEFORE_WINDOW = 6

# ---------------------------------------------------------------------------
# Avoid terms: the topic the candidate refused, in the words spoken
# ---------------------------------------------------------------------------

_MAX_TERM_WORDS = 3
_TERM_STOP = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "it", "is", "ye", "yeh", "ise", "isko",
    "sir", "maam", "ji", "please", "sorry", "ok", "haan", "bas", "ab", "to", "toh", "so", "well",
    "and", "or", "aur", "but", "lekin", "can", "could", "shall", "may", "we", "i", "you", "lets",
    "let", "us", "me", "hum", "main", "mujhe", "aap", "kya", "kisi", "koi", "kuch", "dusra",
    "something", "anything", "some", "another", "other", "else", "ke", "ka", "ko", "se", "par",
    "mein", "bare", "bhi", "ask", "talk", "discuss", "move", "next", "abhi", "here", "now",
    "topic", "sawal", "baat", "chhodkar", "chhodiye", "chhodo", "my", "our", "about", "on", "of",
    "mera", "meri", "hamara",
})
_TERM_NOUNS = frozenset({"topic", "sawal"})
_TERM_BACK = re.compile(
    r"(?<!\S)(?:(?:ke|ka|ki|k) alawa|(?:ko |se |ke )?chhodkar|ko chhodiye|ko chhodo|ko chhodein"
    r"|ko chhod dete hain|ko chhod dijiye|(?:par|ke bare mein) baat nahi)(?!\S)"
)
_TERM_FORWARD = re.compile(
    r"(?<!\S)(?:other than|apart from|aside from|except|besides|instead of|move on from"
    r"|enough about|enough of|enough on|anything but|something but|not about"
    r"|(?:want to|wish to|rather not) (?:talk about|discuss|speak about))(?!\S)"
)


def _token_index(text: str, offset: int) -> int:
    return text.count(" ", 0, offset)


def _term_backward(tokens: _Tokens, end: int) -> str | None:
    i = end - 1
    if i >= 0 and tokens.canon[i] in _TERM_NOUNS:
        i -= 1
    picked: list[str] = []
    while i >= 0 and len(picked) < _MAX_TERM_WORDS and tokens.canon[i] not in _TERM_STOP:
        picked.insert(0, tokens.raw[i])
        i -= 1
    return " ".join(picked) or None


def _term_forward(tokens: _Tokens, start: int) -> str | None:
    i = start
    picked: list[str] = []
    while (
        i < len(tokens.canon)
        and len(picked) < _MAX_TERM_WORDS
        and tokens.canon[i] not in _TERM_STOP
    ):
        picked.append(tokens.raw[i])
        i += 1
    return " ".join(picked) or None


def _avoid_terms(tokens: _Tokens) -> tuple[str, ...]:
    found: list[tuple[int, str]] = []
    for m in _TERM_BACK.finditer(tokens.text):
        idx = _token_index(tokens.text, m.start())
        term = _term_backward(tokens, idx)
        if term:
            found.append((idx, term))
    for m in _TERM_FORWARD.finditer(tokens.text):
        term = _term_forward(tokens, _token_index(tokens.text, m.end()) + 1)
        if term:
            found.append((_token_index(tokens.text, m.start()), term))
    return tuple(dict.fromkeys(term for _, term in sorted(found)))


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def _clause_before(tokens: _Tokens, start: int) -> list[str]:
    """Up to _BEFORE_WINDOW canonical tokens before `start`, stopping at punctuation."""
    if start == 0 or tokens.breaks[start]:
        return []
    begin = start
    while begin > 0 and start - begin < _BEFORE_WINDOW and not tokens.breaks[begin]:
        begin -= 1
    prefix = " ".join(tokens.canon[begin:start])
    return _POLITE.sub(" ", prefix).split()


def _not_a_request(tokens: _Tokens, start: int, end: int, strong_change: bool) -> bool:
    """True when the words around a match show someone is not asking the board for anything."""
    before = _clause_before(tokens, start)
    if before:
        if before[-1] in _NEGATORS:
            return True
        if len(before) >= 2 and before[-1] == "ko" and before[-2] not in _FIRST_PERSON_OBJECT:
            return True
        if any(word in _REPORTED for word in before):
            return True
    after = tokens.canon[end : end + _AFTER_TOKENS]
    if tokens.canon[start] not in _OWN_SUBJECT:
        if not strong_change and any(
            word in _THIRD_PERSON for word in (*before, *tokens.canon[start:end])
        ):
            return True
        if after and after[0] == "to":  # English "move on to X" carries its own subject
            return True
    if after and after[0] in _RESULT_WORDS:
        return True
    return any(word in _AFTER_BLOCK for word in after)


def _position_ok(start: int, total: int) -> bool:
    return total <= SHORT_ANSWER_WORDS or start >= total - TAIL_WORDS


def detect_request(text: str) -> RequestHit | None:
    """Return the candidate's request in a turn's raw transcript, or None if there is none.

    Kinds are tried in the order of _PATTERNS: end, skip, change_topic, repeat, clarify. An
    `end` needs END_MAX_WORDS or fewer. Within a kind, the first pattern with an acceptable
    match wins.
    """
    tokens = _tokenise(text)
    total = len(tokens.canon)
    if total == 0:
        return None
    for pattern_id, kind, pattern in _COMPILED:
        if kind is RequestKind.END and total > END_MAX_WORDS:
            continue
        for m in pattern.finditer(tokens.text):
            start = _token_index(tokens.text, m.start())
            end = _token_index(tokens.text, m.end()) + 1
            strong_change = kind is RequestKind.CHANGE_TOPIC and pattern_id not in _WEAK_CHANGE
            if not _position_ok(start, total) or _not_a_request(
                tokens, start, end, strong_change
            ):
                continue
            avoid = _avoid_terms(tokens) if kind is RequestKind.CHANGE_TOPIC else ()
            return RequestHit(kind, avoid, pattern_id)
    return None
