"""Topics the board must not return to: building the list and matching it (CTO_NOTES_09 §9,
CTO_NOTES_11 §8).

`detect_request` gives the refused terms as the candidate said them ("turism", "टूरिज्म"), but the
board writes its own words ("tourism", "paryatan"). So a refusal widens, with no model call:

1. the candidate's terms, cleaned: letters of either script and spaces only, at most 3 words each
   and 5 terms, so a spoken instruction cannot reach a prompt;
2. the alias group of any of those terms (`topic_aliases.yaml`), and the current segment's own
   aliases (the form's place, degree, hobbies), because the refused topic is nearly always the
   one being discussed and "aage badhte hain" carries no term at all.

Matching is per token or phrase, never by substring, on a phonetic key: Devanagari goes to Latin
by a character table and coarse spellings fold together ("tourism", "turism", "टूरिज्म"). Vowels
are kept, so "tourism" never bans "terrorism". A multi-word term also matches its last content
word ("rural tourism" bans "tourism"). Edit distance 1 applies only to keys of 6 or more letters.

# UNVERIFIED: a refused topic that is not the current segment, in a script unlike the actor's, and
# in no alias group can still return. The key is best effort: Devanagari inherent vowels make it
# unreliable for words outside the alias list, and synonyms across languages cannot match
# phonetically at all.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import yaml

ALIASES_PATH = Path(__file__).with_name("topic_aliases.yaml")
MAX_TERM_WORDS = 3
MAX_TERMS = 5
MIN_FUZZY_KEY_LETTERS = 6
_MIN_CONTENT_LETTERS = 3

# Words that carry no topic of their own, in the languages the board speaks.
_STOP = frozenset(
    {
        "a", "an", "the", "of", "in", "on", "at", "to", "and", "or", "for", "from", "with",
        "about", "is", "are", "was", "be", "it", "its", "this", "that", "hai", "hain", "tha",
        "thi", "ka", "ki", "ke", "ko", "se", "me", "mein", "par", "pe", "per", "aur", "bhi",
        "toh", "ya", "na", "nahi", "nahin", "ye", "yeh", "woh", "wo", "us", "kuch", "koi",
        "kisi", "aap", "hum", "main", "mai", "sir", "madam", "please", "nagar", "pradesh",
        "district", "state", "zila", "city", "ilaka",
        "है", "हैं", "था", "थी", "थे", "का", "की", "के", "को", "से", "में", "पर", "और", "भी",
        "तो", "या", "न", "नहीं", "यह", "वह", "कुछ", "कोई", "किसी", "आप", "हम", "मैं",
    }
)  # fmt: skip
_ALLOWED = re.compile(r"^[A-Za-zऀ-ॣ॰-ॿ' -]+$")
_TOKEN = re.compile(r"[A-Za-zऀ-ॣ॰-ॿ]+(?:'[A-Za-z]+)?")

# --- Devanagari to Latin ---------------------------------------------------------------------

_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j",
    "झ": "jh", "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t",
    "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh",
    "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s",
    "ह": "h", "ळ": "l",
}  # fmt: skip
_NUKTA_CONSONANTS = {"क": "k", "ख": "kh", "ग": "g", "ज": "z", "ड": "d", "ढ": "dh", "फ": "f",
                     "य": "y"}  # fmt: skip
_VOWELS = {
    "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri", "ए": "e",
    "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o",
}  # fmt: skip
_MATRAS = {
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e", "ै": "ai",
    "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
}  # fmt: skip
_VIRAMA = "्"
_NUKTA = "़"
_NASAL = {"ं": "n", "ँ": "n"}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_DIGRAPHS = (("ou", "u"), ("oo", "u"), ("ee", "i"), ("ea", "i"), ("ph", "f"), ("sh", "s"))


def _devanagari_to_latin(word: str) -> str:
    """A rough transliteration. The inherent vowel of a final consonant is dropped, as it is
    spoken: पर्यटन is "paryatan"."""
    out: list[str] = []
    i = 0
    chars = unicodedata.normalize("NFC", word)
    # NFC composes some nukta letters ("ज़" is U+095B); decompose them so the nukta is visible.
    chars = unicodedata.normalize("NFD", chars)
    n = len(chars)
    while i < n:
        ch = chars[i]
        if ch in _CONSONANTS:
            base = _CONSONANTS[ch]
            if i + 1 < n and chars[i + 1] == _NUKTA:
                base = _NUKTA_CONSONANTS.get(ch, base)
                i += 1
            nxt = chars[i + 1] if i + 1 < n else ""
            if nxt == _VIRAMA:
                out.append(base)
                i += 1
            elif nxt in _MATRAS:
                out.append(base + _MATRAS[nxt])
                i += 1
            else:
                last = i + 1 >= n or chars[i + 1] in _NASAL
                out.append(base if last else base + "a")
        elif ch in _VOWELS:
            out.append(_VOWELS[ch])
        elif ch in _NASAL:
            out.append("n")
        elif ch.isalpha():
            out.append(ch)
        i += 1
    return "".join(out)


def term_key(word: str) -> str:
    """A coarse phonetic key. Equal keys mean "probably the same word"; see the module note."""
    low = unicodedata.normalize("NFC", word).casefold()
    if _DEVANAGARI.search(low):
        low = _devanagari_to_latin(low)
    low = low.replace("'", "")
    for old, new in _DIGRAPHS:
        low = low.replace(old, new)
    low = low.replace("z", "s").replace("j", "s").replace("w", "v")
    return re.sub(r"(.)\1+", r"\1", low)  # collapse doubled letters


def _within_one_edit(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1 :] == short for i in range(len(long_)))


def _same_key(a: str, b: str) -> bool:
    if a == b:
        return True
    return min(len(a), len(b)) >= MIN_FUZZY_KEY_LETTERS and _within_one_edit(a, b)


# --- Cleaning and expanding ------------------------------------------------------------------


def words_of(text: str) -> list[str]:
    return _TOKEN.findall(text)


def clean_term(term: str) -> str | None:
    """A term fit for a prompt and a match, or None: letters and spaces only, 3 words at most."""
    flat = " ".join(term.replace("-", " ").split())
    if not flat or not _ALLOWED.match(flat):
        return None
    words = flat.split()[:MAX_TERM_WORDS]
    return " ".join(words) if any(len(w) >= _MIN_CONTENT_LETTERS for w in words) else None


def clean_terms(terms: Iterable[str], limit: int = MAX_TERMS) -> tuple[str, ...]:
    """Cleaned terms in order, without duplicates, at most `limit`."""
    seen: dict[str, None] = {}
    for term in terms:
        cleaned = clean_term(term)
        if cleaned is not None:
            seen.setdefault(cleaned, None)
    return tuple(seen)[:limit]


@lru_cache(maxsize=1)
def alias_groups() -> tuple[tuple[str, ...], ...]:
    doc = yaml.safe_load(ALIASES_PATH.read_text(encoding="utf-8")) or {}
    return tuple(tuple(str(w) for w in words) for words in doc.values())


def aliases_of(term: str) -> tuple[str, ...]:
    """The rest of every alias group that holds `term` (a whole word of it, by phonetic key, with
    one slip allowed in a long word: "agriculure" finds agriculture)."""
    keys = {term_key(w) for w in words_of(term) if w.casefold() not in _STOP}
    found: dict[str, None] = {}
    for group in alias_groups():
        if any(_same_key(term_key(member), key) for member in group for key in keys):
            found.update(dict.fromkeys(group))
    return tuple(found)


def expand_terms(spoken: Iterable[str], segment_aliases: Iterable[str] = ()) -> tuple[str, ...]:
    """What the board must not say after a refusal: the candidate's cleaned terms, the current
    segment's aliases, and the alias groups of all of them. The 5-term cap applies to what a
    prompt is shown; matching uses everything."""
    base = clean_terms((*spoken, *segment_aliases), limit=10**6)
    widened: dict[str, None] = dict.fromkeys(base)
    for term in base:
        widened.update(dict.fromkeys(aliases_of(term)))
    return tuple(widened)


# --- Matching --------------------------------------------------------------------------------


def _content_keys(term: str) -> list[str]:
    return [term_key(w) for w in words_of(term)]


def _contains_run(tokens: list[str], run: list[str]) -> bool:
    n = len(run)
    return n > 0 and any(
        all(_same_key(tokens[i + j], run[j]) for j in range(n)) for i in range(len(tokens) - n + 1)
    )


def find_avoided(text: str, terms: Iterable[str]) -> str | None:
    """The first refused term `text` says, or None. Per token or phrase, on the phonetic key."""
    tokens = [term_key(w) for w in words_of(text)]
    if not tokens:
        return None
    for term in terms:
        words = words_of(term)
        keys = [term_key(w) for w in words]
        if not keys or not any(keys):
            continue
        if _contains_run(tokens, keys):
            return term
        content = [w for w in words if w.casefold() not in _STOP and len(w) >= 5]
        if (
            len(words) > 1
            and content
            and words[-1] == content[-1]
            and _contains_run(tokens, [term_key(content[-1])])
        ):
            return term
    return None


def prompt_terms(terms: Iterable[str]) -> tuple[str, ...]:
    """The few refused terms a prompt may show: cleaned, 3 words each, 5 at most."""
    return clean_terms(terms)
