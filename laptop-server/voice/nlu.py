"""
Rule-based intent parser. No LLM anywhere in this path (architectural decision #7).

Handles four command shapes, tried in priority order:

  1. AUTO       — "auto mode", "automatic", "अपने आप"
  2. ABSOLUTE   — "set to 60 percent", "2000 lux", "saath pratishat"
  3. MODE       — matched against the *live* modes config, so a mode the user added
                  from the dashboard a second ago is recognised without a restart
  4. RELATIVE   — a direction word (brighter/dimmer) plus an optional magnitude word
                  (a little / a lot), where the magnitude maps to a configurable step

Order matters. "Set office mode to 60%" contains both a mode name and a number, and the
explicit number is the more specific instruction, so ABSOLUTE is tried first. Likewise
AUTO outranks everything because it is an escape hatch the user should always be able
to reach.

Vocabulary covers English, Hindi, Marathi and the romanised Hinglish that people
actually speak, in both Devanagari and Latin script. The tables are plain data — adding
a dialect word is a one-line change, not a code change.
"""

import re
import unicodedata

# --- INTENT KINDS ---
AUTO = "auto"
ABSOLUTE = "absolute"
MODE = "mode"
RELATIVE = "relative"
UNKNOWN = "unknown"

# --- LANGUAGE TAGS (drive TTS voice selection) ---
EN, HI, MR, HINGLISH = "en", "hi", "mr", "hinglish"

# --- VOCABULARY ---------------------------------------------------------------
# Every list is matched against whitespace-normalised, punctuation-stripped text.

AUTO_WORDS = [
    "auto", "automatic", "auto mode", "automatically", "self", "sensor mode",
    "ऑटो", "स्वचालित", "अपने आप", "आपोआप", "खुद",
    "apne aap", "aapoaap", "khud", "swachalit",
]

BRIGHTER_WORDS = [
    # English
    "brighter", "bright", "brighten", "lighter", "increase", "increment", "up",
    "raise", "more light", "more", "higher", "boost", "turn up",
    # Hindi / Hinglish (Latin)
    "badhao", "badha", "badhaao", "tez", "tej", "zyada", "jyada", "roshni", "ujala",
    "chamkao", "upar",
    # Marathi (Latin)
    "vadhav", "vadhava", "jasta", "jast", "mothe", "prakash",
    # Devanagari
    "बढ़ा", "बढ़ाओ", "तेज", "ज्यादा", "रोशनी", "उजाला", "चमका", "ऊपर",
    "वाढव", "जास्त", "प्रकाश", "मोठा",
]

DIMMER_WORDS = [
    # English
    "dimmer", "dim", "darker", "dark", "decrease", "down", "lower", "reduce",
    "less light", "less", "softer", "turn down",
    # Hindi / Hinglish (Latin)
    "kam", "kamkaro", "kam karo", "ghata", "ghatao", "halka", "dhima", "neeche",
    "kami", "andhera",
    # Marathi (Latin)
    "kami kar", "kamee", "halke", "khali", "andhar",
    # Devanagari
    "कम", "घटा", "घटाओ", "हल्का", "धीमा", "नीचे", "अंधेरा",
    "कमी", "हळू", "खाली", "अंधार",
]

# Magnitude buckets map to the configurable step sizes in settings.json.
SLIGHT_WORDS = [
    "a little", "little", "a bit", "bit", "slightly", "slight", "somewhat",
    "thoda", "thoda sa", "thodasa", "zara", "jara", "kinchit",
    "थोड़ा", "थोड़ा सा", "ज़रा", "जरा", "किंचित",
    "थोडा", "थोडं", "जरासा",
]
LARGE_WORDS = [
    "a lot", "lot", "lots", "way", "much", "very", "a great deal", "significantly",
    "heavily", "fully", "maximum", "max",
    "bahut", "bohot", "bahut zyada", "kaafi", "kafi",
    "बहुत", "काफ़ी", "काफी", "बहुत ज्यादा",
    "khup", "khoop", "खूप", "खूप जास्त",
]

# Number words, for transcripts that spell quantities out.
NUMBER_WORDS = {
    "zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    "half": 50, "full": 100, "quarter": 25,
    "das": 10, "bees": 20, "tees": 30, "chalis": 40, "pachas": 50, "saath": 60,
    "sattar": 70, "assi": 80, "nabbe": 90, "sau": 100, "aadha": 50, "poora": 100,
    "दस": 10, "बीस": 20, "तीस": 30, "चालीस": 40, "पचास": 50, "साठ": 60,
    "सत्तर": 70, "अस्सी": 80, "नब्बे": 90, "सौ": 100, "आधा": 50, "पूरा": 100,
    "शंभर": 100, "अर्धा": 50,
}

PERCENT_WORDS = ["percent", "%", "pratishat", "percentage", "प्रतिशत", "टक्के", "takke"]
LUX_WORDS = ["lux", "lx", "लक्स"]

# Language detection: words that are distinctively Marathi rather than Hindi.
MARATHI_MARKERS = [
    "वाढव", "कमी कर", "खूप", "जास्त", "प्रकाश", "आहे", "कर", "थोडं", "टक्के",
    "vadhav", "khup", "jasta", "kami kar", "aahe", "karaa", "takke",
]

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_PUNCT = re.compile(r"[^\w\s%ऀ-ॿ]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalise(text):
    """Lowercase, strip punctuation, collapse whitespace. Devanagari passes through."""
    text = unicodedata.normalize("NFC", (text or "").strip().lower())
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def detect_language(text):
    """Pick the TTS voice family. Cheap and deterministic — no model involved."""
    lowered = normalise(text)
    has_devanagari = bool(DEVANAGARI.search(lowered))
    is_marathi = any(marker in lowered for marker in MARATHI_MARKERS)

    if is_marathi:
        return MR
    if has_devanagari:
        return HI
    # Latin script carrying Indic vocabulary is Hinglish, which is spoken with the
    # Hindi voice but is worth distinguishing for the confirmation phrasing.
    if _contains(lowered, _HINGLISH_MARKERS):
        return HINGLISH
    return EN


# Words that appear in the direction/magnitude tables but are ordinary English, so
# their presence must not by itself mark a sentence as Hinglish.
_ENGLISH_ONLY = {
    "brighter", "bright", "brighten", "lighter", "increase", "up", "raise", "more",
    "higher", "boost", "dimmer", "dim", "darker", "dark", "decrease", "down", "lower",
    "reduce", "less", "softer", "little", "bit", "slightly", "slight", "lot", "lots",
    "way", "much", "very", "max", "maximum", "fully", "heavily", "increment",
    "somewhat", "significantly",
    # Plain English quantity and unit words, which say nothing about language.
    "percent", "percentage", "%", "lux", "lx",
    "zero", "ten", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety", "hundred", "half", "full", "quarter",
    # Plain English auto vocabulary.
    "auto", "automatic", "auto mode", "automatically", "self", "sensor mode",
}

# Articles and particles that carry no language signal on their own.
_FILLER = {"a", "an", "the", "of", "great", "deal", "light", "mode", "turn", "it", "to"}

_HINGLISH_MARKERS = None  # populated after the vocabulary tables are defined


def _build_hinglish_markers():
    """Every romanised Indic term across the vocabulary tables.

    Built by subtraction rather than by hand: take all the words the parser knows,
    drop anything in Devanagari (already handled by script detection) and anything
    that is ordinary English, and what remains is romanised Hindi/Marathi.
    """
    pool = (AUTO_WORDS + BRIGHTER_WORDS + DIMMER_WORDS + SLIGHT_WORDS + LARGE_WORDS
            + PERCENT_WORDS + LUX_WORDS + MARATHI_MARKERS + list(NUMBER_WORDS))
    def is_english(entry):
        # A multi-word entry is English when every one of its tokens is — this catches
        # "a little" and "turn down" without having to list each phrase by hand.
        return all(tok in _ENGLISH_ONLY or tok in _FILLER
                   for tok in entry.split())

    return sorted({w for w in pool
                   if not DEVANAGARI.search(w) and not is_english(w)},
                  key=len, reverse=True)


def _contains(text, words):
    """True if any vocabulary entry appears as a whole word / phrase in `text`."""
    for word in words:
        if " " in word:
            if word in text:
                return True
        elif re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text):
            return True
    return False


def _first_match(text, words):
    """Longest matching entry, so 'bahut zyada' beats 'bahut'."""
    best = None
    for word in sorted(words, key=len, reverse=True):
        if " " in word:
            if word in text:
                return word
        elif re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text):
            best = best or word
    return best


_HINGLISH_MARKERS = _build_hinglish_markers()


class Intent:
    """The parsed result. `kind` selects which of the other fields carry meaning."""

    def __init__(self, kind, language=EN, transcript="", mode=None,
                 percent=None, lux=None, direction=0, magnitude=None, delta=None,
                 note=""):
        self.kind = kind
        self.language = language
        self.transcript = transcript
        self.mode = mode              # the matched mode dict, for MODE
        self.percent = percent        # 0-100, for ABSOLUTE
        self.lux = lux                # absolute lux request, for ABSOLUTE
        self.direction = direction    # +1 brighter / -1 dimmer, for RELATIVE
        self.magnitude = magnitude    # "slight" | "normal" | "large"
        self.delta = delta            # signed output-fraction change, for RELATIVE
        self.note = note

    def as_dict(self):
        return {
            "kind": self.kind, "language": self.language,
            "transcript": self.transcript,
            "mode": self.mode, "percent": self.percent, "lux": self.lux,
            "direction": self.direction, "magnitude": self.magnitude,
            "delta": self.delta, "note": self.note,
        }

    def __repr__(self):
        return f"<Intent {self.kind} {self.as_dict()}>"


def _parse_quantity(text):
    """Extract an explicit quantity and its unit. Returns (value, unit) or (None, None).

    Unit is "percent", "lux", or None when the number was bare — a bare number is
    treated as a percentage, since "set it to sixty" almost always means 60%.
    """
    digits = re.findall(r"\d+(?:\.\d+)?", text)
    value = float(digits[0]) if digits else None

    if value is None:
        # Spelled-out numbers, longest first so "bahut zyada" style compounds and
        # multi-word entries win over their prefixes.
        for word in sorted(NUMBER_WORDS, key=len, reverse=True):
            if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text):
                value = float(NUMBER_WORDS[word])
                break

    if value is None:
        return None, None

    if _contains(text, LUX_WORDS):
        return value, "lux"
    if _contains(text, PERCENT_WORDS):
        return value, "percent"
    # A bare number above 100 cannot be a percentage, so read it as lux.
    return value, ("lux" if value > 100 else None)


def _match_mode(text, modes):
    """Find the best matching mode from the live config.

    Scored by how much of the mode's name the transcript actually contains, so a
    two-word mode like "Movie Night" is not matched by "night" alone when a mode
    literally called "Night" also exists.
    """
    best, best_score = None, 0.0
    for mode in modes:
        name = normalise(mode.get("name", ""))
        if not name:
            continue
        tokens = [t for t in name.split() if t]
        if not tokens:
            continue
        hits = sum(1 for t in tokens
                   if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text))
        if not hits:
            continue
        # Full-name substring match is a stronger signal than scattered token hits.
        score = hits / len(tokens) + (0.5 if name in text else 0.0)
        if score > best_score:
            best, best_score = mode, score
    # Require the whole mode name to be present, not just one token of it.
    return best if best_score >= 1.0 else None


def parse(transcript, modes, settings):
    """Turn a transcript into an Intent.

    `modes` is the live list from ConfigStore, so newly added modes are matchable
    immediately. `settings` supplies the magnitude step sizes, which are configurable
    from the dashboard rather than hardcoded (architectural decision #7).
    """
    raw = transcript or ""
    text = normalise(raw)
    language = detect_language(raw)

    if not text:
        return Intent(UNKNOWN, language, raw, note="empty transcript")

    # 1. AUTO — always reachable, outranks everything else.
    if _contains(text, AUTO_WORDS):
        return Intent(AUTO, language, raw, note="auto keyword")

    # 2. ABSOLUTE — an explicit quantity is the most specific thing a user can say.
    value, unit = _parse_quantity(text)
    if value is not None and unit == "lux":
        return Intent(ABSOLUTE, language, raw, lux=value, note="explicit lux")
    if value is not None and unit == "percent":
        return Intent(ABSOLUTE, language, raw, percent=min(100.0, max(0.0, value)),
                      note="explicit percent")

    direction = 0
    if _contains(text, BRIGHTER_WORDS):
        direction = 1
    if _contains(text, DIMMER_WORDS):
        # If both appear, the later word in the sentence is the operative one.
        if direction == 1:
            bright_at = min((text.find(w) for w in BRIGHTER_WORDS
                             if w in text and text.find(w) >= 0), default=10**6)
            dim_at = min((text.find(w) for w in DIMMER_WORDS
                          if w in text and text.find(w) >= 0), default=10**6)
            direction = -1 if dim_at > bright_at else 1
        else:
            direction = -1

    # A bare number with no unit and no direction word is an absolute percentage:
    # "set it to sixty".
    if value is not None and unit is None and direction == 0:
        return Intent(ABSOLUTE, language, raw, percent=min(100.0, max(0.0, value)),
                      note="bare number read as percent")

    # 3. MODE — checked before relative, since a mode name is unambiguous.
    mode = _match_mode(text, modes)
    if mode is not None:
        return Intent(MODE, language, raw, mode=mode, note=f"matched mode {mode['id']}")

    # 4. RELATIVE — direction plus magnitude, resolved against configurable steps.
    if direction != 0:
        steps = settings.get("magnitude_steps", {}) or {}
        if _contains(text, LARGE_WORDS):
            magnitude = "large"
        elif _contains(text, SLIGHT_WORDS):
            magnitude = "slight"
        else:
            magnitude = "normal"

        # A bare number alongside a direction word is the step size itself:
        # "brighter by 15".
        if value is not None and unit is None:
            step_percent = value
            magnitude = "explicit"
        else:
            step_percent = float(steps.get(magnitude, 25))

        delta = direction * (step_percent / 100.0)
        return Intent(RELATIVE, language, raw, direction=direction,
                      magnitude=magnitude, delta=delta,
                      note=f"{magnitude} step of {step_percent:g}%")

    return Intent(UNKNOWN, language, raw, note="no rule matched")
