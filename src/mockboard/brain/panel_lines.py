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


def _once(table_id: str, hi_en: str, en: str, hi: str, ta_en: str) -> LineSet:
    """A line said at most once in a session: one wording per mix."""
    return LineSet(
        table_id, {"hi-en": (hi_en,), "en": (en,), "hi": (hi,), "ta-en": (ta_en,)}
    )


# The ritual before the first question.
RITUAL_LINES = _once(
    "ritual",
    "Namaste. Please be seated.",
    "Good day. Please be seated.",
    "नमस्ते। कृपया बैठिए।",
    "வணக்கம். அமருங்கள், தயவுசெய்து.",
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
        "ta-en": (
            "நன்றி. {name}, தொடருங்கள்.",
            "நன்றி. இப்போது {name} பேசலாம்.",
            "நன்றி. {name}, நீங்கள் தொடங்கலாம்.",
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
        "ta-en": (
            "நிச்சயமாக, பரவாயில்லை.",
            "சரி, இதை இங்கேயே நிறுத்திக்கொள்வோம்.",
            "கண்டிப்பாக, வேறு தலைப்புக்குச் செல்வோம்.",
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
        "ta-en": (
            "பரவாயில்லை, இதை விட்டுவிடலாம்.",
            "சரி, இந்தக் கேள்வியை இங்கே நிறுத்துவோம்.",
            "நீங்கள் விரும்பினால் அடுத்த கேள்விக்குச் செல்வோம்.",
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
        "ta-en": (
            "நிச்சயமாக, கேள்வியை மீண்டும் கேளுங்கள்.",
            "சரி, இன்னொரு முறை கேட்கிறேன்.",
            "கண்டிப்பாக, மீண்டும் கேட்கிறேன்.",
        ),
    },
)
# A request for a moment to think (`think_time`, owner's request Nov 7): the whole line. The same
# question stands, and the next turn is a long one. UNVERIFIED: Tamil wording (fluent review).
# An answer that calls a community inferior or wants it driven out (listener.v7 `prejudice`,
# the owner's live sitting, Nov 9). The member says calmly what the Constitution promises every
# citizen, names no community, quotes nothing back, and asks one question on the candidate's own
# duty (`D-VALUES`). A board does not let such a remark pass as "one-sided". No article number:
# board lines carry no digits. UNVERIFIED: Tamil wording.
VALUES_LINES = LineSet(
    "values",
    {
        "hi-en": (
            "Ek baat saaf kar doon. Samvidhan har nagrik ko, chahe uska dharm, jaati, ling ya "
            "kshetra kuch bhi ho, barabar adhikar aur samman deta hai.",
            "Yahan ek cheez yaad rakhiye. Ek lok sevak har samuday ke liye barabar hota hai, "
            "kyunki Samvidhan sabko ek jaisa samman deta hai.",
        ),
        "en": (
            "Let me be clear on one point. The Constitution gives every citizen equal rights and "
            "equal dignity, whatever their religion, caste, gender or region.",
            "Keep one thing in mind here. A civil servant serves every community equally, because "
            "the Constitution gives everyone the same dignity.",
        ),
        "hi": (
            "एक बात साफ़ कर दूँ। संविधान हर नागरिक को, चाहे उसका धर्म, जाति, लिंग या क्षेत्र कुछ भी हो, "
            "बराबर अधिकार और सम्मान देता है।",
            "यहाँ एक बात याद रखिए। एक लोक सेवक हर समुदाय के लिए बराबर होता है, क्योंकि संविधान सबको एक "
            "जैसा सम्मान देता है।",
        ),
        "ta-en": (
            "ஒன்றைத் தெளிவாகச் சொல்கிறேன். மதம், சாதி, பாலினம், பகுதி எதுவாக இருந்தாலும், "
            "அரசியலமைப்பு ஒவ்வொரு குடிமகனுக்கும் சம உரிமையும் சம மரியாதையும் தருகிறது.",
            "இதை நினைவில் கொள்ளுங்கள். அரசியலமைப்பு எல்லோருக்கும் ஒரே மரியாதை தருவதால், ஒரு அரசு "
            "அதிகாரி எல்லா சமூகங்களுக்கும் சமமாகப் பணியாற்றுகிறார்.",
        ),
    },
)
# The question after `VALUES_LINES`, same wording index.
VALUES_QUESTION_LINES = LineSet(
    "values_question",
    {
        "hi-en": (
            "Ek civil servant ke roop mein, aap har samuday ka bharosa kaise jeetenge, khaas kar "
            "unka jinke baare mein aapne abhi baat ki?",
            "Agar aapke zile mein do samudayon ke beech tanav ho, to aap dono ka bharosa kaise "
            "banayenge?",
        ),
        "en": (
            "As a civil servant, how would you earn the trust of every community, including the "
            "one you just spoke about?",
            "If two communities in your district were in tension, how would you earn the trust of "
            "both?",
        ),
        "hi": (
            "एक लोक सेवक के रूप में, आप हर समुदाय का भरोसा कैसे जीतेंगे, ख़ास कर उनका जिनके बारे में "
            "आपने अभी बात की?",
            "अगर आपके ज़िले में दो समुदायों के बीच तनाव हो, तो आप दोनों का भरोसा कैसे बनाएँगे?",
        ),
        "ta-en": (
            "ஒரு அரசு அதிகாரியாக, நீங்கள் இப்போது பேசிய சமூகம் உட்பட, ஒவ்வொரு சமூகத்தின் "
            "நம்பிக்கையையும் எப்படிப் பெறுவீர்கள்?",
            "உங்கள் மாவட்டத்தில் இரண்டு சமூகங்களுக்கு இடையே பதற்றம் இருந்தால், இருவரின் "
            "நம்பிக்கையையும் எப்படிப் பெறுவீர்கள்?",
        ),
    },
)
# The last answer of a segment hedged ("shayad... pakka nahi pata"), and the segment's budget is
# spent, so no follow-up can call it out. The member who heard it says this before the hand-off
# (owner, Nov 9: "the board should be blunt"; the MCP run that day went from "I would accept a
# transfer" to "shayad police bula lunga" and the board said nothing). UNVERIFIED: Tamil wording.
HEDGE_PRESS_LINES = LineSet(
    "hedge_press",
    {
        "hi-en": (
            "Aap is par khud aashwast nahi lage. Ise aur taiyaar karke aaiyega.",
            "Is jawab mein yakeen ki kami thi. Is par aur sochiyega.",
        ),
        "en": (
            "You did not sound sure of that yourself. Prepare it further.",
            "That answer lacked conviction. Think it through again.",
        ),
        "hi": (
            "आप इस पर ख़ुद आश्वस्त नहीं लगे। इसे और तैयार करके आइएगा।",
            "इस जवाब में यक़ीन की कमी थी। इस पर और सोचिएगा।",
        ),
        "ta-en": (
            "இதில் நீங்களே உறுதியாக இல்லை போல் தெரிகிறது. இதை இன்னும் தயார் செய்யுங்கள்.",
            "இந்தப் பதிலில் நம்பிக்கை குறைவாக இருந்தது. இதைப் பற்றி மீண்டும் யோசியுங்கள்.",
        ),
    },
)
# A turn that was only a greeting ("hello sir"): the member greets back, then the same question
# follows verbatim (`D-GREET`, owner's live sitting, Nov 9). UNVERIFIED: Tamil wording.
GREETING_BACK_LINES = LineSet(
    "greeting_back",
    {
        "hi-en": ("Namaste. Aaiye, shuru karte hain.", "Ji, namaste. Sawal yeh tha."),
        "en": ("Good day to you. Let us begin.", "Hello. The question was this."),
        "hi": ("नमस्ते। आइए, शुरू करते हैं।", "जी, नमस्ते। प्रश्न यह था।"),
        "ta-en": ("வணக்கம். தொடங்கலாம்.", "வணக்கம். கேள்வி இதுதான்."),
    },
)
THINK_LINES = LineSet(
    "think",
    {
        "hi-en": (
            "Zaroor, aaram se sochiye. Jab taiyaar hon, boliye.",
            "Ji, samay lijiye. Hum sun rahe hain.",
            "Koi jaldi nahi. Soch kar boliye.",
        ),
        "en": (
            "Of course, take a moment. Begin when you are ready.",
            "Certainly, take your time. We are listening.",
            "No hurry. Answer when you are ready.",
        ),
        "hi": (
            "ज़रूर, आराम से सोचिए। जब तैयार हों, बोलिए।",
            "जी, समय लीजिए। हम सुन रहे हैं।",
            "कोई जल्दी नहीं। सोचकर बोलिए।",
        ),
        "ta-en": (
            "நிச்சயமாக, யோசித்துச் சொல்லுங்கள்.",
            "சரி, நேரம் எடுத்துக்கொள்ளுங்கள். நாங்கள் கேட்கிறோம்.",
            "அவசரம் இல்லை. தயாரானதும் சொல்லுங்கள்.",
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
        "ta-en": (
            "நேர்காணலை இங்கே முடிக்க விரும்புகிறீர்களா?",
            "நேர்காணலை இப்போதே முடிக்கலாமா?",
            "நாம் இங்கே நேர்காணலை முடிக்கலாமா?",
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
        "ta-en": (
            "சரி, தொடரலாம்.",
            "நிச்சயமாக, தொடர்ந்து பேசலாம்.",
            "சரி, அடுத்ததாகப் பேசுவோம்.",
        ),
    },
)
# A garbled turn, or one that was only noise: the panel asks for it again (Ruling 37: ask
# generically, never echo the transcript). Not gendered: three members are women, so the Hindi and
# Hinglish first wordings say "your words did not reach me clearly" ("sunai nahi di", agreeing with
# "baat") instead of `actor.CLARIFY_LINES`' "sun nahi paaya", which is masculine. English and
# Tamil keep `actor.CLARIFY_LINES`, which are not gendered.
CLARIFY_PANEL_LINES = LineSet(
    "clarify",
    {
        "hi-en": (
            "Maaf kijiye, aapki baat theek se sunai nahi di. Kya aap phir se bata sakte hain?",
            "Kshama kijiye, aawaz saaf nahi aayi. Kya aap dobara bata sakte hain?",
            "Maaf kijiye, yeh mujhe theek se sunai nahi diya. Ek baar phir kahiye?",
        ),
        "en": (
            CLARIFY_LINES["en"],
            "Pardon me, that was not clear to us. Would you say it once more?",
            "I am sorry, the last part did not come through. Could you repeat it?",
        ),
        "hi": (
            "माफ़ कीजिए, आपकी बात ठीक से सुनाई नहीं दी। क्या आप फिर से बता सकते हैं?",
            "क्षमा कीजिए, आवाज़ साफ़ नहीं आई। क्या आप दोबारा बता सकते हैं?",
            "माफ़ कीजिए, यह मुझे ठीक से सुनाई नहीं दिया। एक बार फिर कहिए?",
        ),
        "ta-en": (
            CLARIFY_LINES["ta-en"],
            "மன்னிக்கவும், குரல் தெளிவாக இல்லை. இன்னொரு முறை சொல்ல முடியுமா?",
            "கடைசிப் பகுதி தெளிவாகக் கேட்கவில்லை. மீண்டும் சொல்ல முடியுமா?",
        ),
    },
)
# A clear slang or an abuse. One sentence, then the question continues. Not a lecture.
REGISTER_LINES = LineSet(
    "register",
    {
        "hi-en": (
            "Kya aap apne senior se isi tarah baat karenge?",
            "Board ke saamne yeh shabd theek nahi baithte. Seedhe point par aaiye.",
            "Isi bhaasha mein aap ek senior officer se baat karenge?",
        ),
        "en": (
            "Is that how you would speak to a senior?",
            "Those words do not belong in front of a board. Please come to the point.",
            "Would you use that language with a senior officer?",
        ),
        "hi": (
            "क्या आप अपने सीनियर से इसी तरह बात करेंगे?",
            "बोर्ड के सामने ये शब्द ठीक नहीं बैठते। सीधे बिंदु पर आइए।",
            "इसी भाषा में आप एक सीनियर अधिकारी से बात करेंगे?",
        ),
        "ta-en": (
            "உங்கள் senior officer-ிடம் இப்படித்தான் பேசுவீர்களா?",
            "Board முன்னால் இந்த வார்த்தைகள் பொருத்தமாக இல்லை. நேராகக் கருத்துக்கு வாருங்கள்.",
            "ஒரு senior officer-ிடம் இதே மொழியைப் பயன்படுத்துவீர்களா?",
        ),
    },
)
# The Chairperson's normal close, when the agenda is done.
CLOSE_LINES = _once(
    "close",
    "Thank you, that will be all.",
    "Thank you, that will be all.",
    "धन्यवाद, बस इतना ही।",
    "நன்றி. இத்துடன் முடித்துக்கொள்கிறோம்.",
)
# After a confirmed `end` request.
END_CLOSE_LINES = _once(
    "end_close",
    "Theek hai, aapke samay ke liye dhanyavaad. Thank you, that will be all.",
    "Very well, thank you for your time. That will be all.",
    "ठीक है, आपके समय के लिए धन्यवाद। बस इतना ही।",
    "சரி, உங்கள் நேரத்திற்கு நன்றி. இத்துடன் முடித்துக்கொள்கிறோம்.",
)
# Ruling 47: the spoken close when the interview's time runs out.
WRAP_UP_CLOSE_LINES = _once(
    "wrap_up_close",
    "Hamara samay yahin poora hota hai. Aapka dhanyavaad. Thank you, that will be all.",
    "Our time ends here. Thank you, that will be all.",
    "हमारा समय यहीं पूरा होता है। आपका धन्यवाद। बस इतना ही।",
    "நமது நேரம் இத்துடன் முடிகிறது. நன்றி, இத்துடன் முடித்துக்கொள்கிறோம்.",
)
# Ruling 49: the courteous close at a test user's answer cap, truthful about why it ends.
CAP_CLOSE_LINES = _once(
    "cap_close",
    "Is abhyaas interview ki tay seema yahin poori hoti hai. Aapke samay ke liye "
    "dhanyavaad. Thank you, that will be all.",
    "This practice interview has reached its set limit. Thank you for your time. "
    "That will be all.",
    "इस अभ्यास साक्षात्कार की तय सीमा यहीं पूरी होती है। आपके समय के लिए धन्यवाद। बस इतना ही।",
    "இந்த practice interview-க்கான வரம்பு இங்கே நிறைவடைகிறது. உங்கள் நேரத்திற்கு நன்றி. "
    "இத்துடன் முடிக்கிறோம்.",
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
    REGISTER_LINES,
    CLOSE_LINES,
    END_CLOSE_LINES,
    WRAP_UP_CLOSE_LINES,
    CAP_CLOSE_LINES,
    # Nov 9: the lines added after the owner's live sittings (Rounds 55-57).
    THINK_LINES,
    GREETING_BACK_LINES,
    VALUES_LINES,
    VALUES_QUESTION_LINES,
    HEDGE_PRESS_LINES,
)


def line_for(table: Mapping[str, str], mix: str, uses: int = 0) -> str:
    """The table's line for this mix, Hinglish when the mix is unknown (as `CLARIFY_LINES`).
    `uses` is how often a `LineSet` was used before, which picks the wording (module docstring)."""
    if isinstance(table, LineSet):
        return table.wording(mix, uses)
    return table.get(mix, table["hi-en"])


def handoff_line(name: str, mix: str, uses: int = 0) -> str:
    return line_for(HANDOFF_LINES, mix, uses).format(name=name)
