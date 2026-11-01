"""The panel's agenda: five fixed segments whose content comes from the application form.

`build_fallback_agenda` is pure, makes no model call, and always gives a valid agenda, so an
interview can start whatever happens to Nova (CTO_NOTES_06 §2, §4). The segment ids are the
panel's structure, not topics; the form supplies the content (Ruling 42, D8).

Honesty rules, enforced by `seed_violations`:
- Seeds are open-ended questions. They state no fact about a place and quiz no number.
- Current affairs has no fact source, so the candidate chooses the issue, and a seed may not
  name a scheme, event or person. The check reads capitals, so it cannot see a name at the start
  of a sentence, or in Devanagari. That is a limit, not a guarantee.
- `PLACE_FACT_PATTERNS` are conservative: "Kanpur Ganga ke kinare hai" passes them. The fixed
  seeds here state nothing about places; P6 must keep that true of Nova's seeds.

Form text is the candidate's own and is treated as data. Only a short run of letters can enter a
seed (`_term`), so markup and digits cannot, but a short phrase can: "say you are free" passes.
A seed is spoken, not obeyed, and P4 and P6 fence it (CTO_NOTES_12 §7). `work_experience` is
never embedded.

Budgets are data. A capped session spends its cap as the agenda: `capped_budgets` gives budgets
summing to the cap, so the agenda and the cap finish together (Ruling 49, CTO_NOTES_12 §9).
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass

from mockboard.brain.actor import (
    CLOSED_STARTS,
    OPEN_ENDED_MARKERS,
    PLACE_FACT_PATTERNS,
    count_words,
    quiz_question,
)
from mockboard.brain.schemas import Agenda, AgendaSeed, AgendaSegment, CandidateForm

FORM_HR = "form_hr"
HOME_DISTRICT = "home_district"
DEGREE = "degree"
INTERESTS = "interests"
CURRENT_AFFAIRS = "current_affairs"

# Segment id -> persona id, in panel order. Panel persona YAML `topics` must agree (a test checks).
SEGMENT_MEMBERS: dict[str, str] = {
    FORM_HR: "chairman",
    HOME_DISTRICT: "administrator",
    DEGREE: "economist",
    INTERESTS: "humanities",
    CURRENT_AFFAIRS: "ips_officer",
}
# Questions per segment for an uncapped session (CTO_NOTES_06 §4). Defaults only: read a budget
# from `AgendaSegment.budget`, never from here.
DEFAULT_SEGMENT_BUDGETS: dict[str, int] = {
    FORM_HR: 2,
    HOME_DISTRICT: 3,
    DEGREE: 3,
    INTERESTS: 3,
    CURRENT_AFFAIRS: 4,
}
# Extra budget for a cap above one question each, in order. Current affairs grows first, so the
# DM/SP situation has room. Its length is the defaults' total minus one question per member.
_GROW_ORDER: tuple[str, ...] = (
    CURRENT_AFFAIRS, HOME_DISTRICT, DEGREE, INTERESTS, CURRENT_AFFAIRS,
    FORM_HR, HOME_DISTRICT, DEGREE, INTERESTS, CURRENT_AFFAIRS,
)  # fmt: skip
MAX_SEED_WORDS = 28
_MAX_TERM_WORDS = 5
_MAX_TERM_WORD_LETTERS = 24

# Hindi has its own open-ended words; `OPEN_ENDED_MARKERS` is Latin only and guards the opener,
# so it stays as it is.
_OPEN_ENDED_DEVANAGARI = (
    "क्यों", "कैसे", "कैसा", "कैसी", "किस तरह", "कौन", "क्या लगता", "क्या सोचते", "बताइए", "समझाइए",
)  # fmt: skip
_OPEN_ENDED = (*OPEN_ENDED_MARKERS, *_OPEN_ENDED_DEVANAGARI)

# Capitals a current-affairs seed may use without the form backing them.
_CAPITALS_ALLOWED = frozenset({"dm", "sp", "ias", "ips"})
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_SENTENCE = re.compile(r"[.?!।]+(?:\s+|$)")
_DIGIT = re.compile(r"\d")  # any script's digits

# A form value may hold letters of Latin or Devanagari, spaces, hyphens and apostrophes. Dots go
# ("B.Tech" -> "BTech"), "&" reads as "and", and brackets and slashes read as spaces ("B.Tech
# (CSE)", "Computer Science/IT"). Anything else, a comma included, drops the whole value, because
# cutting pieces out of a value could leave text its author did not write.
_TERM = re.compile(r"^[A-Za-zऀ-ॣ॰-ॿ][A-Za-zऀ-ॣ॰-ॿ' -]*$")
_TERM_SPACES = str.maketrans("()/", "   ")
# Instruction-shaped words. A backstop: `_term` is what makes form text inert, and this list only
# stops a seed reading like an order. Word list is ours, not exhaustive.
_INSTRUCTION = re.compile(
    r"\b(?:ignore|disregard|override|bypass|jailbreak|instructions?|prompts?|assistant|"
    r"forget|nirdesh|bhool jao)\b|निर्देश|अनदेखा",
    re.IGNORECASE,
)


def seed_violations(
    text: str, segment_id: str = "", form: CandidateForm | None = None
) -> list[str]:
    """Why a seed question is not acceptable (empty list = acceptable)."""
    problems: list[str] = []
    stripped = text.strip()
    low = stripped.lower()
    if not stripped.endswith("?"):
        problems.append("does not end with a question")
    if count_words(stripped) > MAX_SEED_WORDS:
        problems.append(f"over {MAX_SEED_WORDS} words")
    if _DIGIT.search(stripped):
        problems.append("contains a digit")
    if any(re.search(p, low) for p in PLACE_FACT_PATTERNS):
        problems.append("states a fact about a place")
    if quiz_question(stripped):
        problems.append("quizzes a number")
    last_sentence = re.split(r"[.।]\s+", low)[-1].strip()
    if CLOSED_STARTS.search(last_sentence) or not any(m in low for m in _OPEN_ENDED):
        problems.append("question is not open-ended")
    if _INSTRUCTION.search(stripped):
        problems.append("reads like an instruction")
    if segment_id == CURRENT_AFFAIRS:
        names = _unbacked_capitals(stripped, form)
        if names:
            problems.append(f"names something no source backs: {names}")
    return problems


def _unbacked_capitals(text: str, form: CandidateForm | None) -> list[str]:
    allowed = _CAPITALS_ALLOWED | _form_words(form)
    found: list[str] = []
    for sentence in _SENTENCE.split(text):
        for word in _LATIN_WORD.findall(sentence)[1:]:  # the first word is capitalised anyway
            if word[0].isupper() and word.lower() not in allowed:
                found.append(word)
    return found


def _form_words(form: CandidateForm | None) -> frozenset[str]:
    if form is None:
        return frozenset()
    values = [form.home_state, form.district, form.degree_subject, form.optional_subject,
              form.work_experience, *form.hobbies]  # fmt: skip
    return frozenset(w.lower() for v in values if v for w in _LATIN_WORD.findall(v))


def agenda_violations(agenda: Agenda, form: CandidateForm | None = None) -> list[str]:
    """Why an agenda is not acceptable, across all its segments (empty list = acceptable)."""
    problems: list[str] = []
    seen: set[str] = set()
    must_asks = 0
    for segment in agenda.segments:
        if SEGMENT_MEMBERS.get(segment.id) is None:
            problems.append(f"{segment.id}: unknown segment id")
        elif SEGMENT_MEMBERS[segment.id] != segment.member:
            problems.append(f"{segment.id}: wrong member {segment.member}")
        for seed in segment.seeds:
            problems += [
                f"{segment.id}: {p}: {seed.text!r}"
                for p in seed_violations(seed.text, segment.id, form)
            ]
            if seed.text in seen:
                problems.append(f"{segment.id}: duplicate seed: {seed.text!r}")
            seen.add(seed.text)
            if seed.must_ask:
                must_asks += 1
                if segment.id != CURRENT_AFFAIRS:
                    problems.append(f"{segment.id}: must_ask outside current affairs")
    if must_asks > 1:
        problems.append("more than one must_ask seed")
    return problems


# ---------------------------------------------------------------------------
# Fallback content
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Seed:
    """A question using form terms, and the question to ask if a term is missing or unusable."""

    template: str
    generic: str


_TITLES: dict[str, str] = {
    FORM_HR: "The candidate's form and motivation",
    HOME_DISTRICT: "Home state and district",
    DEGREE: "Degree subject",
    INTERESTS: "Hobbies and optional subject",
    CURRENT_AFFAIRS: "Current affairs and a district situation",
}

_SEEDS: dict[str, dict[str, tuple[_Seed, ...]]] = {
    FORM_HR: {
        "hi-en": (
            _Seed(
                "Aap {place} se hain aur aapne {degree} ki padhai ki hai. "
                "Is safar ne aapko civil services ki taraf kaise modha?",
                "Aapke form ko dekhte hue, aapko civil services ki taraf kis baat ne modha, "
                "aur kaise?",
            ),
            _Seed(
                "Agar civil services ka rasta na bane, to aap samaj ke liye kya karna chahenge, "
                "aur kyun?",
                "Agar civil services ka rasta na bane, to aap samaj ke liye kya karna chahenge, "
                "aur kyun?",
            ),
        ),
        "en": (
            _Seed(
                "You are from {place} and you studied {degree}. "
                "How did that journey bring you to the civil services?",
                "Looking at your form, what drew you to the civil services, and how?",
            ),
            _Seed(
                "If the civil services do not work out, what would you want to do for society, "
                "and why?",
                "If the civil services do not work out, what would you want to do for society, "
                "and why?",
            ),
        ),
        "hi": (
            _Seed(
                "आप {place} से हैं और आपने {degree} की पढ़ाई की है। "
                "इस सफ़र ने आपको सिविल सेवा की ओर कैसे मोड़ा?",
                "आपके आवेदन को देखते हुए, आपको सिविल सेवा की ओर किस बात ने मोड़ा, और कैसे?",
            ),
            _Seed(
                "अगर सिविल सेवा का रास्ता न बने, तो आप समाज के लिए क्या करना चाहेंगे, और क्यों?",
                "अगर सिविल सेवा का रास्ता न बने, तो आप समाज के लिए क्या करना चाहेंगे, और क्यों?",
            ),
        ),
    },
    HOME_DISTRICT: {
        "hi-en": (
            _Seed(
                "{place} mein rehte hue aapne kaunsi ek samasya kareeb se dekhi, "
                "aur use kaise suljhayenge?",
                "Apne ilaake mein rehte hue aapne kaunsi ek samasya kareeb se dekhi, "
                "aur use kaise suljhayenge?",
            ),
            _Seed(
                "Agar aap {place} ke liye ek badlav la sakein, to woh kaunsa hoga, aur kyun?",
                "Agar aap apne ilaake ke liye ek badlav la sakein, to woh kaunsa hoga, aur kyun?",
            ),
            _Seed(
                "{place} ke logon aur sarkari daftaron ke beech bharosa kaise badh sakta hai?",
                "Aapke ilaake ke logon aur sarkari daftaron ke beech bharosa kaise badh sakta hai?",
            ),
        ),
        "en": (
            _Seed(
                "Living in {place}, which one problem have you seen up close, "
                "and how would you solve it?",
                "Living in your area, which one problem have you seen up close, "
                "and how would you solve it?",
            ),
            _Seed(
                "If you could bring one change to {place}, what would it be, and why?",
                "If you could bring one change to your area, what would it be, and why?",
            ),
            _Seed(
                "How can trust grow between the people of {place} and government offices?",
                "How can trust grow between the people of your area and government offices?",
            ),
        ),
        "hi": (
            _Seed(
                "{place} में रहते हुए आपने कौन सी एक समस्या क़रीब से देखी, और उसे कैसे सुलझाएँगे?",
                "अपने इलाक़े में रहते हुए आपने कौन सी एक समस्या क़रीब से देखी, और उसे कैसे सुलझाएँगे?",
            ),
            _Seed(
                "अगर आप {place} के लिए एक बदलाव ला सकें, तो वह कौन सा होगा, और क्यों?",
                "अगर आप अपने इलाक़े के लिए एक बदलाव ला सकें, तो वह कौन सा होगा, और क्यों?",
            ),
            _Seed(
                "{place} के लोगों और सरकारी दफ़्तरों के बीच भरोसा कैसे बढ़ सकता है?",
                "आपके इलाक़े के लोगों और सरकारी दफ़्तरों के बीच भरोसा कैसे बढ़ सकता है?",
            ),
        ),
    },
    DEGREE: {
        "hi-en": (
            _Seed(
                "{degree} ki padhai ne aapke sochne ke tareeke ko kaise badla?",
                "Aapki padhai ne aapke sochne ke tareeke ko kaise badla?",
            ),
            _Seed(
                "{degree} ka koi ek vichar aam logon ki zindagi se kaise judta hai?",
                "Aapke vishay ka koi ek vichar aam logon ki zindagi se kaise judta hai?",
            ),
            _Seed(
                "Prashasan mein {degree} ki samajh aapke kis tarah kaam aayegi?",
                "Prashasan mein aapke vishay ki samajh aapke kis tarah kaam aayegi?",
            ),
        ),
        "en": (
            _Seed(
                "How has studying {degree} changed the way you think?",
                "How have your studies changed the way you think?",
            ),
            _Seed(
                "How does one idea from {degree} connect to ordinary people's lives?",
                "How does one idea from your subject connect to ordinary people's lives?",
            ),
            _Seed(
                "In what way would {degree} help you in administration?",
                "In what way would your subject help you in administration?",
            ),
        ),
        "hi": (
            _Seed(
                "{degree} की पढ़ाई ने आपके सोचने के तरीक़े को कैसे बदला?",
                "आपकी पढ़ाई ने आपके सोचने के तरीक़े को कैसे बदला?",
            ),
            _Seed(
                "{degree} का कोई एक विचार आम लोगों की ज़िंदगी से कैसे जुड़ता है?",
                "आपके विषय का कोई एक विचार आम लोगों की ज़िंदगी से कैसे जुड़ता है?",
            ),
            _Seed(
                "प्रशासन में {degree} की समझ आपके किस तरह काम आएगी?",
                "प्रशासन में आपके विषय की समझ आपके किस तरह काम आएगी?",
            ),
        ),
    },
    INTERESTS: {
        "hi-en": (
            _Seed(
                "Aapko {hobby} se kya seekh mili, aur woh aapke kaam kaise aayegi?",
                "Padhai ke bahar aapko kis kaam mein sachchi khushi milti hai, aur kyun?",
            ),
            _Seed(
                "{hobby2} se aapne apne baare mein kya jaana, aur kaise?",
                "Khaali samay mein aap kya karna pasand karte hain, aur kyun?",
            ),
            _Seed(
                "Aapne {optional} ko optional vishay kyun chuna, aur usse aapki soch kaise badli?",
                "Padhai ke alawa kis vishay ne aapki soch ko badla, aur kaise?",
            ),
        ),
        "en": (
            _Seed(
                "What has {hobby} taught you, and how would it help you at work?",
                "Outside your studies, what work gives you real joy, and why?",
            ),
            _Seed(
                "What has {hobby2} shown you about yourself, and how?",
                "What do you like to do in your free time, and why?",
            ),
            _Seed(
                "Why did you choose {optional} as your optional, and how did it change your "
                "thinking?",
                "Beyond your degree, which subject changed your thinking, and how?",
            ),
        ),
        "hi": (
            _Seed(
                "{hobby} से आपने क्या सीखा, और वह आपके काम कैसे आएगा?",
                "पढ़ाई के बाहर आपको किस काम में सच्ची ख़ुशी मिलती है, और क्यों?",
            ),
            _Seed(
                "{hobby2} से आपने अपने बारे में क्या जाना, और कैसे?",
                "ख़ाली समय में आप क्या करना पसंद करते हैं, और क्यों?",
            ),
            _Seed(
                "आपने {optional} को वैकल्पिक विषय क्यों चुना, और उससे आपकी सोच कैसे बदली?",
                "पढ़ाई के अलावा किस विषय ने आपकी सोच को बदला, और कैसे?",
            ),
        ),
    },
    # The candidate chooses the issue: Nova's knowledge may be stale and no fact source exists.
    CURRENT_AFFAIRS: {
        "hi-en": (
            _Seed(
                "Haal ke samay mein aapne kis rashtriya mudde ko dhyan se follow kiya hai, "
                "aur kyun?",
                "Haal ke samay mein aapne kis rashtriya mudde ko dhyan se follow kiya hai, "
                "aur kyun?",
            ),
            _Seed(
                "Apni pasand ke kisi rashtriya mudde par aap doosre paksh ki baat ko kaise tolte "
                "hain?",
                "Apni pasand ke kisi rashtriya mudde par aap doosre paksh ki baat ko kaise tolte "
                "hain?",
            ),
        ),
        "en": (
            _Seed(
                "Which national issue have you followed closely of late, and why?",
                "Which national issue have you followed closely of late, and why?",
            ),
            _Seed(
                "On a national issue of your choice, how do you weigh the side you disagree "
                "with?",
                "On a national issue of your choice, how do you weigh the side you disagree "
                "with?",
            ),
        ),
        "hi": (
            _Seed(
                "हाल के समय में आपने किस राष्ट्रीय मुद्दे को ध्यान से देखा है, और क्यों?",
                "हाल के समय में आपने किस राष्ट्रीय मुद्दे को ध्यान से देखा है, और क्यों?",
            ),
            _Seed(
                "अपनी पसंद के किसी राष्ट्रीय मुद्दे पर आप दूसरे पक्ष की बात को कैसे तौलते हैं?",
                "अपनी पसंद के किसी राष्ट्रीय मुद्दे पर आप दूसरे पक्ष की बात को कैसे तौलते हैं?",
            ),
        ),
    },
}

# Asked only when the form lists work experience. The text itself never enters a seed.
_WORK_SEEDS: dict[str, str] = {
    "hi-en": "Aapke kaam ke anubhav ne aapko janseva ke baare mein kya sikhaya, aur use aap "
    "kaise istemal karenge?",
    "en": "What has your work experience taught you about public service, and how would you "
    "use it?",
    "hi": "आपके काम के अनुभव ने आपको जनसेवा के बारे में क्या सिखाया, और उसे आप कैसे काम में लाएँगे?",
}

# Hypothetical DM/SP situations, with no communal, party or caste framing, and no real place.
# Chosen by stable hash of the form, so one form always gets the same situation.
SITUATIONS: tuple[dict[str, str], ...] = (
    {  # flood relief
        "hi-en": "Maan lijiye aap ek zile ke DM hain aur baadh ke baad rahat samagri kam pad "
        "gayi hai. Aap kaise tay karenge ki pehle kise mile?",
        "en": "Suppose you are a DM and relief supplies run short after a flood. "
        "How would you decide who receives them first?",
        "hi": "मान लीजिए आप एक ज़िले के DM हैं और बाढ़ के बाद राहत सामग्री कम पड़ गई है। "
        "आप कैसे तय करेंगे कि पहले किसे मिले?",
    },
    {  # contract-labour dispute
        "hi-en": "Maan lijiye aap DM hain aur ek factory ke theke ke mazdoor hadtaal par hain. "
        "Dono pakshon se baat karke aap kaise aage badhenge?",
        "en": "Suppose you are a DM and contract workers at a factory go on strike. "
        "How would you deal with both sides and move forward?",
        "hi": "मान लीजिए आप DM हैं और एक कारख़ाने के ठेका मज़दूर हड़ताल पर हैं। "
        "दोनों पक्षों से बात करके आप कैसे आगे बढ़ेंगे?",
    },
    {  # water sharing between villages
        "hi-en": "Maan lijiye aap SP hain aur do gaon ke beech paani ke bantwaare par tanaav "
        "badh raha hai. Aap sthiti ko kaise sambhalenge?",
        "en": "Suppose you are an SP and tension rises between two villages over sharing "
        "water. How would you handle the situation?",
        "hi": "मान लीजिए आप SP हैं और दो गाँवों के बीच पानी के बँटवारे पर तनाव बढ़ रहा है। "
        "आप स्थिति को कैसे संभालेंगे?",
    },
)


def _term(value: str | None) -> str | None:
    """A form value fit to speak inside a question, or None. See `_TERM`."""
    if not value:
        return None
    spaced = value.translate(_TERM_SPACES).replace("&", " and ").replace(".", "")
    term = " ".join(spaced.split())
    if not _TERM.match(term) or _INSTRUCTION.search(term):
        return None
    if any(re.search(p, term.lower()) for p in PLACE_FACT_PATTERNS):
        return None
    words = term.split()
    if len(words) > _MAX_TERM_WORDS or max(map(len, words)) > _MAX_TERM_WORD_LETTERS:
        return None
    return term


def _form_terms(form: CandidateForm) -> dict[str, str]:
    hobbies = [t for t in (_term(h) for h in form.hobbies) if t]
    found = {
        "place": _term(form.district) or _term(form.home_state),
        "degree": _term(form.degree_subject),
        "optional": _term(form.optional_subject),
        "hobby": hobbies[0] if hobbies else None,
        "hobby2": hobbies[1] if len(hobbies) > 1 else None,
    }
    return {k: v for k, v in found.items() if v}


def _fill(seed: _Seed, terms: dict[str, str], segment_id: str, form: CandidateForm) -> str:
    try:
        text = seed.template.format(**terms)
    except KeyError:
        return seed.generic
    text = text[0].upper() + text[1:]  # "cycling" may open the sentence
    return seed.generic if seed_violations(text, segment_id, form) else text


def _mix(form: CandidateForm) -> str:
    return {"english": "en", "hindi": "hi"}.get(form.interview_language, "hi-en")


def situation_for(form: CandidateForm) -> str:
    """The DM/SP situation for this form. `zlib.crc32`, because `hash()` changes per process."""
    index = zlib.crc32(form.model_dump_json().encode("utf-8")) % len(SITUATIONS)
    return SITUATIONS[index][_mix(form)]


def _seeds(segment_id: str, form: CandidateForm, terms: dict[str, str]) -> list[AgendaSeed]:
    mix = _mix(form)
    texts = [_fill(s, terms, segment_id, form) for s in _SEEDS[segment_id][mix]]
    if segment_id == FORM_HR and form.work_experience:
        texts.insert(1, _WORK_SEEDS[mix])
    seeds = [AgendaSeed(text=t) for t in texts]
    if segment_id == CURRENT_AFFAIRS:
        seeds.append(AgendaSeed(text=situation_for(form), must_ask=True))
    return seeds


# A question that names nothing, for the rare seed no alternative of which is clear of a refused
# topic. Open-ended and free of facts, so it passes `seed_violations`.
NEUTRAL_SEEDS: dict[str, str] = {
    "hi-en": "Is vishay par aap apne anubhav se kya kehna chahenge, aur kyun?",
    "en": "On this subject, what would you like to say from your own experience, and why?",
    "hi": "इस विषय पर आप अपने अनुभव से क्या कहना चाहेंगे, और क्यों?",
}


def seed_alternatives(segment_id: str, index: int, form: CandidateForm) -> list[str]:
    """Other questions that can stand where seed `index` of the fallback agenda stands, best first:
    the form-free wording of the same seed, or the other DM/SP situations. For a board that must
    not say a refused topic. Empty when `index` is not a fallback seed (a Nova seed, work
    experience), and the caller then uses `NEUTRAL_SEEDS`."""
    mix = _mix(form)
    if segment_id == CURRENT_AFFAIRS and index >= len(_SEEDS[segment_id][mix]):
        chosen = situation_for(form)
        return [s[mix] for s in SITUATIONS if s[mix] != chosen]
    shift = 1 if segment_id == FORM_HR and form.work_experience and index >= 1 else 0
    if segment_id == FORM_HR and form.work_experience and index == 1:
        return []  # the work-experience seed names nothing from the form
    seeds = _SEEDS[segment_id][mix]
    position = index - shift
    if not 0 <= position < len(seeds):
        return []
    return [seeds[position].generic]


def resolve_budgets(budgets: Mapping[str, int] | None = None) -> dict[str, int]:
    """Default budgets with the caller's on top. An unknown id is a bug, so it raises."""
    unknown = set(budgets or ()) - set(SEGMENT_MEMBERS)
    if unknown:
        raise ValueError(f"budgets for unknown segments: {sorted(unknown)}")
    return {**DEFAULT_SEGMENT_BUDGETS, **(budgets or {})}


def capped_budgets(max_answers: int | None) -> dict[str, int]:
    """Budgets summing to the cap, so all five members speak: 1/2/2/2/3 at 10 answers.

    None, or a cap at or above the defaults' total, is an uncapped session. A cap too small to
    give each member one question is a configuration bug, so it raises.
    """
    if max_answers is None or max_answers >= sum(DEFAULT_SEGMENT_BUDGETS.values()):
        return dict(DEFAULT_SEGMENT_BUDGETS)
    if max_answers < len(SEGMENT_MEMBERS):
        raise ValueError("max_answers must allow one question per member")
    budgets = dict.fromkeys(SEGMENT_MEMBERS, 1)
    for segment_id in _GROW_ORDER[: max_answers - len(SEGMENT_MEMBERS)]:
        budgets[segment_id] += 1
    return budgets


def _title(segment_id: str, terms: dict[str, str]) -> str:
    detail = {HOME_DISTRICT: terms.get("place"), DEGREE: terms.get("degree")}.get(segment_id)
    return f"{_TITLES[segment_id]}: {detail}" if detail else _TITLES[segment_id]


def _aliases(segment_id: str, form: CandidateForm) -> tuple[str, ...]:
    """The form's own words for what this segment is about: what the board must stop saying if
    the candidate asks to leave it (`brain/avoid_terms.py`). Cleaned by `_term`, so inert."""
    values: dict[str, list[str | None]] = {
        HOME_DISTRICT: [form.district, form.home_state],
        DEGREE: [form.degree_subject],
        INTERESTS: [*form.hobbies, form.optional_subject],
    }
    found = dict.fromkeys(t for t in (_term(v) for v in values.get(segment_id, [])) if t)
    return tuple(found)[:5]


def one_question_agenda(segment_id: str, question: str, form: CandidateForm) -> Agenda:
    """The replay agenda (CTO_NOTES_06 §8): one segment, one seed, budget 1."""
    if segment_id not in SEGMENT_MEMBERS:
        raise ValueError(f"unknown segment {segment_id!r}")
    terms = _form_terms(form)
    return Agenda(
        segments=[
            AgendaSegment(
                id=segment_id,
                member=SEGMENT_MEMBERS[segment_id],
                title=_title(segment_id, terms),
                budget=1,
                seeds=[AgendaSeed(text=question)],
                aliases=_aliases(segment_id, form),
            )
        ]
    )


def build_fallback_agenda(
    form: CandidateForm, budgets: Mapping[str, int] | None = None
) -> Agenda:
    """The deterministic agenda. Pure: no model call, no I/O, same form gives the same agenda."""
    terms = _form_terms(form)
    resolved = resolve_budgets(budgets)
    return Agenda(
        segments=[
            AgendaSegment(
                id=segment_id,
                member=member,
                title=_title(segment_id, terms),
                budget=resolved[segment_id],
                seeds=_seeds(segment_id, form, terms),
                aliases=_aliases(segment_id, form),
            )
            for segment_id, member in SEGMENT_MEMBERS.items()
        ],
    )


def render_agenda(agenda: Agenda) -> str:
    """The agenda as plain text, for people to read."""
    lines = [f"Agenda ({agenda.source}), {agenda.total_budget} questions"]
    for segment in agenda.segments:
        lines.append(f"{segment.id} | {segment.member} | budget {segment.budget} | {segment.title}")
        for seed in segment.seeds:
            lines.append(f"  - {seed.text}{' [must ask]' if seed.must_ask else ''}")
    return "\n".join(lines)
