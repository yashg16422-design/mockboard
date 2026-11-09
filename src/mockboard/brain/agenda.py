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
import unicodedata
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
# The owner's order (Nov 9): the Chairperson's introduction, then the degree subject, then the case
# study (a DM/SP situation dug into), then the home district and the hobbies.
SEGMENT_MEMBERS: dict[str, str] = {
    FORM_HR: "chairman",
    DEGREE: "economist",
    CURRENT_AFFAIRS: "ips_officer",
    HOME_DISTRICT: "administrator",
    INTERESTS: "humanities",
}
# The segment whose DM/SP situation is asked first and then followed up (`director.CASE_PROBES`).
CASE_STUDY_SEGMENT = CURRENT_AFFAIRS
# Questions per segment for an uncapped session (CTO_NOTES_06 §4). Defaults only: read a budget
# from `AgendaSegment.budget`, never from here.
DEFAULT_SEGMENT_BUDGETS: dict[str, int] = {  # in panel order
    FORM_HR: 2,
    DEGREE: 3,
    CURRENT_AFFAIRS: 4,
    HOME_DISTRICT: 3,
    INTERESTS: 3,
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
_OPEN_ENDED_TAMIL = ("ஏன்", "எப்படி", "என்ன", "எந்த", "யார்", "எதை", "எவ்வாறு")
# Hinglish question words Nova uses that the opener's list lacks (agenda_plan v5 on Nova, Nov 9:
# "Chess khelne se aapko kya seekhne ko mila?" was refused as closed). Seeds only.
_OPEN_ENDED_HINGLISH = (
    "kya seekh", "kya sikh", "kya sudhar", "kya karenge", "kya connection", "kya fark",
    "kaunse", "konse", "konsa", "konsi", "kis ",
)  # fmt: skip
_OPEN_ENDED = (
    *OPEN_ENDED_MARKERS, *_OPEN_ENDED_HINGLISH, *_OPEN_ENDED_DEVANAGARI, *_OPEN_ENDED_TAMIL
)
# Superlatives ("sabse zyada") are a place fact in a statement and ordinary words in a question,
# the exemption the live line gate makes (`PanelGate`), but stricter: the superlative's own clause
# must not state something ("Kanpur sabse bada shehar hai, kyun?" still fails).
_SUPERLATIVE_PATTERNS = frozenset(PLACE_FACT_PATTERNS[2:5])
_COPULA_AFTER = re.compile(r"^[^,?]*\b(?:hai|hain|tha|thi|the|is|are|was|were)\b")


def _states_a_place_fact(low: str, question: bool) -> bool:
    for pattern in PLACE_FACT_PATTERNS:
        for m in re.finditer(pattern, low):
            if not (question and pattern in _SUPERLATIVE_PATTERNS):
                return True
            if _COPULA_AFTER.search(low[m.end():]):
                return True
    return False

# Capitals a current-affairs seed may use without the form backing them.
_CAPITALS_ALLOWED = frozenset({"dm", "sp", "ias", "ips"})
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_SENTENCE = re.compile(r"[.?!।]+(?:\s+|$)")
_DIGIT = re.compile(r"\d")  # any script's digits

# A form value may hold letters of Latin or Devanagari, spaces, hyphens and apostrophes. Dots go
# ("B.Tech" -> "BTech"), "&" reads as "and", and brackets and slashes read as spaces ("B.Tech
# (CSE)", "Computer Science/IT"). Anything else, a comma included, drops the whole value, because
# cutting pieces out of a value could leave text its author did not write.
_TERM = re.compile(r"^[A-Za-zऀ-ॣ॰-ॿ\u0b80-\u0bff][A-Za-zऀ-ॣ॰-ॿ\u0b80-\u0bff' -]*$")
_TERM_SPACES = str.maketrans("()/", "   ")
# Instruction-shaped words. A backstop: `_term` is what makes form text inert, and this list only
# stops a seed reading like an order. Word list is ours, not exhaustive.
_INSTRUCTION = re.compile(
    r"\b(?:ignore|disregard|override|bypass|jailbreak|instructions?|prompts?|assistant|"
    r"forget|nirdesh|bhool jao)\b|निर्देश|अनदेखा",
    re.IGNORECASE,
)


# The form asks for no college, yet a degree like "B.Tech from IIT Kanpur" names one. The board
# must never say an institution aloud (it identifies the candidate and invites prestige bias), so a
# value with one is not spoken, and a planned seed that names one is refused. Word list is ours.
_INSTITUTION = re.compile(
    r"\b(?:iit|iits|nit|nits|iiit|iim|iims|aiims|bits|iisc|jnu|du|college|colleges|university|"
    r"universities|univ|institute|institution|vidyalaya|vishwavidyalaya|mahavidyalaya|school|"
    r"academy|campus)\b",
    re.IGNORECASE,
)


def names_an_institution(text: str) -> bool:
    """True when the text names a college, university or institute."""
    return bool(_INSTITUTION.search(text.replace(".", "")))


def seed_violations(
    text: str, segment_id: str = "", form: CandidateForm | None = None,
    max_words: int = MAX_SEED_WORDS,
) -> list[str]:
    """Why a seed question is not acceptable (empty list = acceptable). `max_words` is longer
    only for a plan prompt that asks for richer questions (agenda_plan v4)."""
    problems: list[str] = []
    stripped = text.strip()
    low = stripped.lower()
    if not stripped.endswith("?"):
        problems.append("does not end with a question")
    if count_words(stripped) > max_words:
        problems.append(f"over {max_words} words")
    if _DIGIT.search(stripped):
        problems.append("contains a digit")
    question = stripped.endswith("?") and "." not in stripped and "।" not in stripped
    if _states_a_place_fact(low, question):
        problems.append("states a fact about a place")
    if quiz_question(stripped):
        problems.append("quizzes a number")
    last_sentence = re.split(r"[.।]\s+", low)[-1].strip()
    if CLOSED_STARTS.search(last_sentence) or not any(m in low for m in _OPEN_ENDED):
        problems.append("question is not open-ended")
    if _INSTRUCTION.search(stripped):
        problems.append("reads like an instruction")
    if names_an_institution(stripped):
        problems.append("names a college or institute")
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
    # UPSC-level subject questions (owner, Nov 9: "these questions are very simple"): explain a
    # core idea plainly, apply the subject to a public decision with its cost, defend the subject
    # against a challenge. No fact, figure, quiz or definition is asked for.
    DEGREE: {
        "hi-en": (
            _Seed(
                "{degree} ka koi ek buniyadi siddhant gaon ke ek vyakti ko aap kaise samjhayenge, "
                "aur woh unke liye kyun maayne rakhta hai?",
                "Aapke vishay ka koi ek buniyadi siddhant gaon ke ek vyakti ko aap kaise "
                "samjhayenge, aur woh unke liye kyun maayne rakhta hai?",
            ),
            _Seed(
                "{degree} ki samajh se aap kisi ek sarkari faisle ko kaise behtar banate, aur uski "
                "keemat kaun chukata?",
                "Aapke vishay ki samajh se aap kisi ek sarkari faisle ko kaise behtar banate, aur "
                "uski keemat kaun chukata?",
            ),
            _Seed(
                "Agar koi kahe ki {degree} padhne wale prashasan ke liye sahi nahi hote, to aap "
                "kya jawab denge, aur kyun?",
                "Agar koi kahe ki aapke vishay ke log prashasan ke liye sahi nahi hote, to aap kya "
                "jawab denge, aur kyun?",
            ),
        ),
        "en": (
            _Seed(
                "How would you explain one basic principle of {degree} to a villager, and why does "
                "it matter to them?",
                "How would you explain one basic principle of your subject to a villager, and why "
                "does it matter to them?",
            ),
            _Seed(
                "How could your grasp of {degree} improve one government decision, and who would "
                "bear its cost?",
                "How could your grasp of your subject improve one government decision, and who "
                "would bear its cost?",
            ),
            _Seed(
                "If someone said that people trained in {degree} are unsuited to administration, "
                "how would you answer, and why?",
                "If someone said that people trained in your subject are unsuited to "
                "administration, how would you answer, and why?",
            ),
        ),
        "hi": (
            _Seed(
                "{degree} का कोई एक बुनियादी सिद्धांत आप गाँव के एक व्यक्ति को कैसे समझाएँगे, और वह "
                "उनके लिए क्यों मायने रखता है?",
                "आपके विषय का कोई एक बुनियादी सिद्धांत आप गाँव के एक व्यक्ति को कैसे समझाएँगे, और वह "
                "उनके लिए क्यों मायने रखता है?",
            ),
            _Seed(
                "{degree} की समझ से आप किसी एक सरकारी फ़ैसले को कैसे बेहतर बनाते, और उसकी क़ीमत "
                "कौन चुकाता?",
                "आपके विषय की समझ से आप किसी एक सरकारी फ़ैसले को कैसे बेहतर बनाते, और उसकी क़ीमत "
                "कौन चुकाता?",
            ),
            _Seed(
                "अगर कोई कहे कि {degree} पढ़ने वाले प्रशासन के लिए सही नहीं होते, तो आप क्या जवाब "
                "देंगे, और क्यों?",
                "अगर कोई कहे कि आपके विषय के लोग प्रशासन के लिए सही नहीं होते, तो आप क्या जवाब "
                "देंगे, और क्यों?",
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
    # The candidate still chooses the issue. A sourced briefing (brain/affairs.py) is background
    # for every member, not a seed: a seed may not name a place the form did not give, and the
    # briefing is not the subject of the interview. Nova's knowledge may be stale. No digits.
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

# Tamil + English (Tanglish), kept separate from Hindi/Hinglish so speech and board lines use
# the Tamil locale and script. These templates are deliberately short and open-ended.
_SEEDS[FORM_HR]["ta-en"] = (
    _Seed(
        "நீங்கள் {place} பகுதியைச் சேர்ந்தவர்; {degree} படித்திருக்கிறீர்கள். இந்தப் பயணம் உங்களை "
        "civil services நோக்கி எப்படி அழைத்துச் சென்றது?",
        "உங்கள் application-ஐப் பார்த்தால், civil services-ஐத் தேர்ந்தெடுக்க உங்களைத் "
        "தூண்டியது என்ன, எப்படி?",
    ),
    _Seed(
        "civil services வாய்ப்பு அமையாவிட்டால், சமூகத்திற்காக என்ன செய்ய விரும்புவீர்கள், ஏன்?",
        "civil services வாய்ப்பு அமையாவிட்டால், சமூகத்திற்காக என்ன செய்ய விரும்புவீர்கள், ஏன்?",
    ),
)
_SEEDS[HOME_DISTRICT]["ta-en"] = (
    _Seed(
        "{place} பகுதியில் நீங்கள் நேரில் பார்த்த ஒரு முக்கியமான பிரச்சினை என்ன, அதை எப்படி தீர்ப்பீர்கள்?",
        "உங்கள் பகுதியில் நேரில் பார்த்த ஒரு முக்கியமான பிரச்சினை என்ன, அதை எப்படி தீர்ப்பீர்கள்?",
    ),
    _Seed(
        "{place} பகுதிக்கு ஒரு மாற்றம் கொண்டுவர முடிந்தால் அது என்ன, ஏன்?",
        "உங்கள் பகுதிக்கு ஒரு மாற்றம் கொண்டுவர முடிந்தால் அது என்ன, ஏன்?",
    ),
    _Seed(
        "{place} மக்களுக்கும் அரசு அலுவலகங்களுக்கும் இடையிலான நம்பிக்கையை எப்படி வளர்ப்பீர்கள்?",
        "உங்கள் பகுதி மக்களுக்கும் அரசு அலுவலகங்களுக்கும் இடையிலான நம்பிக்கையை எப்படி வளர்ப்பீர்கள்?",
    ),
)
_SEEDS[DEGREE]["ta-en"] = (
    _Seed(
        "{degree} படிப்பு உங்கள் சிந்தனையை எப்படி மாற்றியது?",
        "உங்கள் படிப்பு உங்கள் சிந்தனையை எப்படி மாற்றியது?",
    ),
    _Seed(
        "{degree} பாடத்தில் உள்ள ஒரு கருத்து சாதாரண மக்களின் வாழ்க்கையுடன் எப்படி தொடர்பு கொள்கிறது?",
        "உங்கள் பாடத்தில் உள்ள ஒரு கருத்து சாதாரண மக்களின் வாழ்க்கையுடன் எப்படி தொடர்பு கொள்கிறது?",
    ),
    _Seed(
        "நிர்வாகத்தில் {degree} பற்றிய உங்கள் புரிதலை எப்படிப் பயன்படுத்துவீர்கள்?",
        "நிர்வாகத்தில் உங்கள் பாடம் பற்றிய புரிதலை எப்படிப் பயன்படுத்துவீர்கள்?",
    ),
)
_SEEDS[INTERESTS]["ta-en"] = (
    _Seed(
        "{hobby} உங்களுக்கு என்ன கற்றுத் தந்தது, அது உங்கள் பணியில் எப்படி உதவும்?",
        "படிப்புக்கு வெளியே உங்களுக்கு உண்மையான மகிழ்ச்சி தரும் செயல் எது, ஏன்?",
    ),
    _Seed(
        "{hobby2} மூலம் உங்களைப் பற்றி என்ன அறிந்தீர்கள், எப்படி?",
        "ஓய்வு நேரத்தில் என்ன செய்ய விரும்புகிறீர்கள், ஏன்?",
    ),
    _Seed(
        "{optional} பாடத்தை optional subject ஆக ஏன் தேர்ந்தெடுத்தீர்கள், அது உங்கள் சிந்தனையை எப்படி மாற்றியது?",
        "உங்கள் degree-க்கு வெளியே எந்தப் பாடம் உங்கள் சிந்தனையை மாற்றியது, எப்படி?",
    ),
)
_SEEDS[CURRENT_AFFAIRS]["ta-en"] = (
    _Seed(
        "சமீபத்தில் எந்த தேசியப் பிரச்சினையை கவனமாகப் பின்தொடர்ந்தீர்கள், ஏன்?",
        "சமீபத்தில் எந்த தேசியப் பிரச்சினையை கவனமாகப் பின்தொடர்ந்தீர்கள், ஏன்?",
    ),
    _Seed(
        "நீங்கள் தேர்ந்தெடுக்கும் ஒரு தேசியப் பிரச்சினையில், உங்களுடன் ஒத்துப்போகாத தரப்பின் "
        "கருத்தை எப்படி மதிப்பீடு செய்கிறீர்கள்?",
        "நீங்கள் தேர்ந்தெடுக்கும் ஒரு தேசியப் பிரச்சினையில், உங்களுடன் ஒத்துப்போகாத தரப்பின் "
        "கருத்தை எப்படி மதிப்பீடு செய்கிறீர்கள்?",
    ),
)

# Asked only when the form lists work experience. The text itself never enters a seed.
_WORK_SEEDS: dict[str, str] = {
    "hi-en": "Aapke kaam ke anubhav ne aapko janseva ke baare mein kya sikhaya, aur use aap "
    "kaise istemal karenge?",
    "en": "What has your work experience taught you about public service, and how would you "
    "use it?",
    "hi": "आपके काम के अनुभव ने आपको जनसेवा के बारे में क्या सिखाया, और उसे आप कैसे काम में लाएँगे?",
}
_WORK_SEEDS["ta-en"] = (
    "உங்கள் work experience பொதுச் சேவையைப் பற்றி என்ன கற்றுத் தந்தது, அதை எப்படி பயன்படுத்துவீர்கள்?"
)

# Hypothetical DM/SP situations, with no communal, party or caste framing, and no real place.
# Chosen by stable hash of the form, so one form always gets the same situation. The last three
# and every twist were added on Nov 7 (owner: "board proposing better scenarios"): each is a real
# dilemma with a constraint, and each has one twist that raises the stakes (`twist_for`).
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
    {  # hospital power in a heatwave
        "hi-en": "Maan lijiye aap DM hain aur bheeshan garmi mein zila aspataal ki bijli baar "
        "baar ja rahi hai, diesel bhi kam hai. Aap sthiti kaise sambhalenge?",
        "en": "Suppose you are a DM in a severe heatwave, the district hospital keeps losing "
        "power, and diesel is short. How would you handle it?",
        "hi": "मान लीजिए आप DM हैं और भीषण गर्मी में ज़िला अस्पताल की बिजली बार-बार जा रही है, डीज़ल "
        "भी कम है। आप स्थिति कैसे संभालेंगे?",
    },
    {  # mid-day meal illness
        "hi-en": "Maan lijiye aap DM hain aur ek gaon mein mid-day meal ke baad kai bachche "
        "beemar pad gaye hain. Pehle ghante mein aap kaise kaam karenge?",
        "en": "Suppose you are a DM and several children in a village fall ill after the "
        "mid-day meal. How would you act in the first hour?",
        "hi": "मान लीजिए आप DM हैं और एक गाँव में मिड-डे मील के बाद कई बच्चे बीमार पड़ गए हैं। "
        "पहले घंटे में आप कैसे काम करेंगे?",
    },
    {  # court order on forest land
        "hi-en": "Maan lijiye aap DM hain aur adalat ne van bhoomi khaali karane ko kaha hai, "
        "par wahan garib parivar barson se rehte hain. Aap kaise aage badhenge?",
        "en": "Suppose you are a DM and a court orders forest land cleared, but poor families "
        "have lived there for years. How would you proceed?",
        "hi": "मान लीजिए आप DM हैं और अदालत ने वन भूमि खाली कराने को कहा है, पर वहाँ गरीब परिवार "
        "बरसों से रहते हैं। आप कैसे आगे बढ़ेंगे?",
    },
)

SITUATIONS[0]["ta-en"] = (
    "நீங்கள் மாவட்ட ஆட்சியர் என்று வைத்துக்கொள்ளுங்கள். வெள்ளத்துக்குப் பிறகு relief supplies "
    "குறைவாக உள்ளன; முதலில் யாருக்கு வழங்குவது என்று எப்படி முடிவு செய்வீர்கள்?"
)
SITUATIONS[1]["ta-en"] = (
    "ஒரு factory-யில் contract workers வேலைநிறுத்தம் செய்கிறார்கள் என்று வைத்துக்கொள்ளுங்கள். "
    "இரு தரப்பினரிடமும் பேசி எப்படி முன்னேறுவீர்கள்?"
)
SITUATIONS[2]["ta-en"] = (
    "இரு கிராமங்களுக்கிடையே தண்ணீர் பகிர்வால் பதற்றம் அதிகரிக்கிறது என்று வைத்துக்கொள்ளுங்கள். "
    "காவல் கண்காணிப்பாளராக நிலைமையை எப்படி கையாள்வீர்கள்?"
)
# UNVERIFIED: Tamil wording below (fluent review).
SITUATIONS[3]["ta-en"] = (
    "நீங்கள் மாவட்ட ஆட்சியர் என்று வைத்துக்கொள்ளுங்கள். கடும் வெப்பத்தில் மாவட்ட மருத்துவமனையில் "
    "மின்சாரம் அடிக்கடி போகிறது, டீசலும் குறைவு. நிலைமையை எப்படி கையாள்வீர்கள்?"
)
SITUATIONS[4]["ta-en"] = (
    "நீங்கள் மாவட்ட ஆட்சியர் என்று வைத்துக்கொள்ளுங்கள். ஒரு கிராமத்தில் மதிய உணவுக்குப் பிறகு பல "
    "குழந்தைகள் நோய்வாய்ப்பட்டுள்ளனர். முதல் ஒரு மணி நேரத்தில் எப்படி செயல்படுவீர்கள்?"
)
SITUATIONS[5]["ta-en"] = (
    "நீங்கள் மாவட்ட ஆட்சியர் என்று வைத்துக்கொள்ளுங்கள். வன நிலத்திலிருந்து ஆக்கிரமிப்பை அகற்ற "
    "நீதிமன்றம் உத்தரவிட்டுள்ளது, ஆனால் அங்கே பல ஏழைக் குடும்பங்கள் பல ஆண்டுகளாக வசிக்கின்றன. "
    "எப்படி முன்னேறுவீர்கள்?"
)

# One twist per situation, same order: asked once, after a real answer to the situation, to see
# how the candidate holds a position when the stakes rise. UNVERIFIED: Tamil wording.
TWISTS: tuple[dict[str, str], ...] = (
    {
        "hi-en": "Ab ek sthaniya neta phone karke kehte hain ki pehle unke samarthakon ke gaon "
        "mein samagri bhejiye. Aap ise kaise sambhalenge?",
        "en": "Now a local politician calls and asks you to send supplies first to the villages "
        "of their supporters. What do you do?",
        "hi": "अब एक स्थानीय नेता फ़ोन करके कहते हैं कि पहले उनके समर्थकों के गाँव में सामग्री भेजिए। "
        "आप इसे कैसे संभालेंगे?",
        "ta-en": "இப்போது ஒரு உள்ளூர் அரசியல்வாதி போன் செய்து, முதலில் தன் ஆதரவாளர்களின் கிராமங்களுக்கு "
        "அனுப்பச் சொல்கிறார். என்ன செய்வீர்கள்?",
    },
    {
        "hi-en": "Ab factory maalik kehte hain ki agar hadtaal kal tak khatam nahi hui, to woh "
        "factory band kar denge aur saikdon log berozgaar ho jayenge. Aap kaise aage badhenge?",
        "en": "Now the owner says that if the strike does not end by tomorrow, the factory will "
        "close and hundreds will lose their jobs. What is your next step?",
        "hi": "अब कारख़ाने के मालिक कहते हैं कि अगर हड़ताल कल तक ख़त्म नहीं हुई, तो वे कारख़ाना बंद "
        "कर देंगे और सैकड़ों लोग बेरोज़गार हो जाएँगे। आप कैसे आगे बढ़ेंगे?",
        "ta-en": "இப்போது நாளைக்குள் வேலைநிறுத்தம் முடியாவிட்டால் factory-ஐ மூடிவிடுவதாக உரிமையாளர் "
        "சொல்கிறார்; நூற்றுக்கணக்கானோர் வேலை இழப்பார்கள். எப்படி முன்னேறுவீர்கள்?",
    },
    {
        "hi-en": "Ab social media par ek bhadkaau video viral ho raha hai aur dono gaon mein "
        "bheed jama ho rahi hai. Aap pehla kadam kaise uthayenge?",
        "en": "Now an inflammatory video is spreading on social media and crowds are gathering "
        "in both villages. What is your first step?",
        "hi": "अब सोशल मीडिया पर एक भड़काऊ वीडियो वायरल हो रहा है और दोनों गाँवों में भीड़ जमा हो रही है। "
        "आप पहला क़दम कैसे उठाएँगे?",
        "ta-en": "இப்போது social media-வில் ஒரு தூண்டும் வீடியோ பரவுகிறது, இரு கிராமங்களிலும் கூட்டம் "
        "கூடுகிறது. உங்கள் முதல் நடவடிக்கை என்ன?",
    },
    {
        "hi-en": "Ab pata chalta hai ki diesel ka theka ek prabhavshali vyakti ke rishtedaar ke "
        "paas hai, aur wahi der kar raha hai. Aap ise kaise sambhalenge?",
        "en": "Now you learn that the diesel contract is held by a relative of an influential "
        "person, and that is the cause of the delay. What do you do?",
        "hi": "अब पता चलता है कि डीज़ल का ठेका एक प्रभावशाली व्यक्ति के रिश्तेदार के पास है, और वही देर "
        "कर रहा है। आप इसे कैसे संभालेंगे?",
        "ta-en": "இப்போது டீசல் contract ஒரு செல்வாக்கான நபரின் உறவினரிடம் இருப்பதும், அவரால்தான் "
        "தாமதம் என்பதும் தெரிகிறது. என்ன செய்வீர்கள்?",
    },
    {
        "hi-en": "Ab media aspataal ke bahar hai aur aapse turant bayaan chahta hai, jabki jaanch "
        "abhi shuru hi hui hai. Aap kya kahenge, aur kyun?",
        "en": "Now the media is outside the hospital and wants a statement at once, while the "
        "inquiry has only just begun. What do you say?",
        "hi": "अब मीडिया अस्पताल के बाहर है और आपसे तुरंत बयान चाहता है, जबकि जाँच अभी शुरू ही हुई है। "
        "आप क्या कहेंगे, और क्यों?",
        "ta-en": "இப்போது ஊடகங்கள் மருத்துவமனைக்கு வெளியே நின்று உடனே அறிக்கை கேட்கின்றன, ஆனால் விசாரணை "
        "இப்போதுதான் தொடங்கியுள்ளது. என்ன சொல்வீர்கள்?",
    },
    {
        "hi-en": "Ab parivaar dharne par baith gaye hain, aur adalat ne agle hafte paalan ki "
        "report maangi hai. Aap ise kaise sambhalenge?",
        "en": "Now the families have begun a sit-in, and the court wants a compliance report next "
        "week. What do you do?",
        "hi": "अब परिवार धरने पर बैठ गए हैं, और अदालत ने अगले हफ़्ते पालन की रिपोर्ट माँगी है। "
        "आप इसे कैसे संभालेंगे?",
        "ta-en": "இப்போது குடும்பங்கள் தர்ணாவில் அமர்ந்துள்ளன, நீதிமன்றம் அடுத்த வாரம் இணக்க அறிக்கை "
        "கேட்கிறது. என்ன செய்வீர்கள்?",
    },
)


def _situation_index(text: str) -> tuple[int, str] | None:
    """(situation index, language mix) when `text` contains one of the situations. Pure."""
    norm = unicodedata.normalize("NFC", text or "")
    for index, situation in enumerate(SITUATIONS):
        for mix, wording in situation.items():
            if unicodedata.normalize("NFC", wording) in norm:
                return index, mix
    return None


def is_situation(text: str) -> bool:
    """True when `text` is (or contains) one of the DM/SP situations."""
    return _situation_index(text) is not None


def twist_for(question: str) -> str | None:
    """The twist for the situation `question` contains, in the same language, or None."""
    found = _situation_index(question)
    if found is None:
        return None
    index, mix = found
    return TWISTS[index].get(mix)


def _term(value: str | None) -> str | None:
    """A form value fit to speak inside a question, or None. See `_TERM`."""
    if not value:
        return None
    spaced = value.translate(_TERM_SPACES).replace("&", " and ").replace(".", "")
    term = " ".join(spaced.split())
    if not _TERM.match(term) or _INSTRUCTION.search(term) or names_an_institution(term):
        return None
    if re.search(r"\bfrom\b", term, re.IGNORECASE):  # "Electronics from <somewhere>"
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
    return {"english": "en", "hindi": "hi", "tanglish": "ta-en"}.get(
        form.interview_language, "hi-en"
    )


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
    "ta-en": "இந்தத் தலைப்பில் உங்கள் அனுபவத்திலிருந்து என்ன சொல்ல விரும்புகிறீர்கள், ஏன்?",
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
