"""Rule-based item detail extraction.

extract(text, today) pulls structured fields out of a plain-language message:
category, item name, colour, brand, campus location, date, time and (for found items)
where the item is now. Anything it can't find is left as None, and the agent asks for it.

Also used by the agent tools to clean up sloppy arguments from a small local LLM
(e.g. lost_date="yesterday" or location="library").
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, time, timedelta

from app.campus import LOCATION_ALIASES, LOCATIONS

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
# (phrase, category, item_name). When several match, the EARLIEST phrase in the
# message wins, then the longest ("laptop bag with red keychain" -> laptop bag).
ITEM_PHRASES: list[tuple[str, str, str]] = [
    ("laptop bag", "bag", "laptop bag"), ("laptop sleeve", "bag", "laptop sleeve"),
    ("backpack", "bag", "backpack"), ("college bag", "bag", "bag"), ("school bag", "bag", "bag"),
    ("sling bag", "bag", "sling bag"), ("handbag", "bag", "handbag"), ("tote bag", "bag", "tote bag"),
    ("pouch", "bag", "pouch"), ("bag", "bag", "bag"),
    ("water bottle", "bottle", "water bottle"), ("steel bottle", "bottle", "steel bottle"),
    ("flask", "bottle", "flask"), ("sipper", "bottle", "sipper"), ("tumbler", "bottle", "tumbler"),
    ("bottle", "bottle", "bottle"),
    ("mobile phone", "electronics", "phone"), ("smartphone", "electronics", "phone"),
    ("iphone", "electronics", "phone"), ("phone", "electronics", "phone"),
    ("mobile", "electronics", "phone"),
    ("laptop charger", "electronics", "laptop charger"), ("phone charger", "electronics", "phone charger"),
    ("charger", "electronics", "charger"), ("power bank", "electronics", "power bank"),
    ("powerbank", "electronics", "power bank"), ("laptop", "electronics", "laptop"),
    ("tablet", "electronics", "tablet"), ("ipad", "electronics", "tablet"),
    ("scientific calculator", "electronics", "calculator"), ("calculator", "electronics", "calculator"),
    ("earbuds case", "electronics", "earbuds"), ("earbuds", "electronics", "earbuds"),
    ("airdopes", "electronics", "earbuds"), ("airpods", "electronics", "earbuds"),
    ("earphones", "electronics", "earphones"), ("headphones", "electronics", "headphones"),
    ("headset", "electronics", "headphones"), ("smartwatch", "electronics", "smartwatch"),
    ("smart watch", "electronics", "smartwatch"), ("pendrive", "electronics", "pendrive"),
    ("pen drive", "electronics", "pendrive"), ("usb drive", "electronics", "pendrive"),
    ("mouse", "electronics", "mouse"),
    ("college id card", "id_card", "college ID card"), ("college id", "id_card", "college ID card"),
    ("id card", "id_card", "ID card"), ("identity card", "id_card", "ID card"),
    ("i-card", "id_card", "ID card"), ("icard", "id_card", "ID card"),
    ("bus pass", "id_card", "bus pass"), ("library card", "id_card", "library card"),
    ("card holder", "wallet", "card holder"), ("wallet", "wallet", "wallet"), ("purse", "wallet", "purse"),
    ("key bunch", "keys", "key bunch"), ("bunch of keys", "keys", "key bunch"),
    ("keys", "keys", "keys"), ("key", "keys", "key"),
    ("textbook", "book", "textbook"), ("text book", "book", "textbook"),
    ("notebook", "book", "notebook"), ("lab manual", "book", "lab manual"),
    ("journal", "book", "journal"), ("book", "book", "book"),
    ("hooded jacket", "clothing", "hooded jacket"), ("jacket", "clothing", "jacket"),
    ("hoodie", "clothing", "hoodie"), ("sweatshirt", "clothing", "sweatshirt"),
    ("sweater", "clothing", "sweater"), ("t-shirt", "clothing", "t-shirt"), ("shirt", "clothing", "shirt"),
    ("jersey", "clothing", "jersey"), ("raincoat", "clothing", "raincoat"), ("coat", "clothing", "coat"),
    ("scarf", "clothing", "scarf"), ("cap", "clothing", "cap"), ("hat", "clothing", "hat"),
    ("shoes", "clothing", "shoes"), ("slippers", "clothing", "slippers"),
    ("wrist watch", "jewellery", "watch"), ("wristwatch", "jewellery", "watch"),
    ("watch", "jewellery", "watch"), ("bracelet", "jewellery", "bracelet"),
    ("necklace", "jewellery", "necklace"), ("earrings", "jewellery", "earrings"),
    ("earring", "jewellery", "earring"), ("pendant", "jewellery", "pendant"),
    ("bangle", "jewellery", "bangle"), ("ring", "jewellery", "ring"), ("chain", "jewellery", "chain"),
    ("geometry box", "stationery", "geometry box"), ("compass box", "stationery", "geometry box"),
    ("pencil box", "stationery", "pencil box"), ("drawing kit", "stationery", "drawing kit"),
    ("mini drafter", "stationery", "drawing kit"), ("mini-drafter", "stationery", "drawing kit"),
    ("pencil", "stationery", "pencil"), ("pen", "stationery", "pen"),
    ("stapler", "stationery", "stapler"), ("ruler", "stationery", "ruler"),
    ("umbrella", "other", "umbrella"), ("sunglasses", "other", "sunglasses"),
    ("spectacles", "other", "spectacles"), ("specs", "other", "spectacles"),
    ("glasses", "other", "spectacles"), ("lunch box", "other", "lunch box"),
    ("tiffin", "other", "lunch box"), ("helmet", "other", "helmet"),
]

COLORS: list[str] = [
    "navy blue", "dark blue", "light blue", "sky blue", "royal blue", "dark green", "light green",
    "dark grey", "light grey", "dark gray", "light gray", "off white", "off-white",
    "black", "white", "grey", "gray", "silver", "blue", "navy", "red", "maroon", "pink", "green",
    "yellow", "orange", "purple", "violet", "brown", "beige", "tan", "golden", "gold", "cream",
    "transparent", "multicolour", "multicolor",
]

BRANDS: list[str] = [
    "HP", "Dell", "Lenovo", "Asus", "Acer", "Apple", "Samsung", "Redmi", "Xiaomi", "OnePlus",
    "Realme", "Vivo", "Oppo", "Motorola", "Nokia", "boAt", "JBL", "Sony", "Casio", "Milton",
    "Cello", "Borosil", "Tupperware", "Wildcraft", "Skybags", "American Tourister", "Safari",
    "Puma", "Nike", "Adidas", "Reebok", "Titan", "Fastrack", "Fossil", "Sonata", "Parker",
    "Camlin", "Classmate", "Reynolds", "Lava", "Noise", "Fire-Boltt",
]

# "gave it to the admin office" describes WHERE THE ITEM IS NOW, not where it was found,
# so we cut these phrases out before looking for the location.
_HOLDING_SPAN_RE = re.compile(
    r"(gave|given|handed|submitted|deposited|kept|left|dropped)\s+(it\s+|them\s+)?(over\s+)?"
    r"(to|at|in|with)\s+(the\s+)?(admin(istration)?\s+office|admin|office|security( cabin| office)?|guard)"
)
# Lenient rules: used when the user is ANSWERING "where is the item now?"
_HOLDING_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("admin_office", re.compile(r"admin(istration)? office|\boffice\b|\badmin\b")),
    ("security", re.compile(r"\bsecurity\b|\bguard\b|main gate")),
    ("finder", re.compile(
        r"\bwith me\b|\bi have it\b|\bi('ve| have)? ?kept it\b|\bkeeping it\b|\bwith myself\b"
        r"|\bi still have\b|\bmy (room|bag|desk|hostel)\b|\bi have (them|the)\b|^(me|myself|mine|i do)$"
    )),
]
# Strict rule for "with me" inside a longer message ("found a wallet, it's with me")
_FINDER_STRICT_RE = re.compile(r"\bwith me\b|\bi have it\b|\bi('ve| have)? kept it\b|\bi still have it\b")
# Location after a preposition is preferred: "ID card, 2nd year CSE, lost IN the canteen"
_PREPOSITION_RE = r"(?:in|at|near|outside|inside|from|behind|opposite|around|by|on)\s+(?:the\s+)?"

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9,
    "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}

_SKIP_RE = re.compile(r"^(skip|no|none|nothing|nope|nahi|na|don'?t know|dont know|not sure|no idea|-)\b")
_SENSITIVE_DIGITS_RE = re.compile(r"(?:\d[ -]?){9,}\d")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _phrase_re(phrase: str) -> re.Pattern[str]:
    # Like \b...\b but also works for phrases containing '&' or '-'.
    return re.compile(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])")


def _earliest_longest(text: str, phrases: list[str]) -> tuple[str, int] | None:
    best: tuple[int, int, str] | None = None  # (start, -length, phrase)
    for phrase in phrases:
        m = _phrase_re(phrase.lower()).search(text)
        if m:
            cand = (m.start(), -len(phrase), phrase)
            if best is None or cand < best:
                best = cand
    return (best[2], best[0]) if best else None


def is_skip(text: str) -> bool:
    return bool(_SKIP_RE.match(text.strip().lower()))


def mask_sensitive(text: str) -> str:
    """Hide long digit runs (phone / ID / card numbers): 10+ digits."""
    return _SENSITIVE_DIGITS_RE.sub("[number hidden]", text)


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------
def match_item(text: str) -> tuple[str, str] | None:
    """Return (category, item_name) or None."""
    t = text.lower()
    lookup = {p: (c, n) for p, c, n in ITEM_PHRASES}
    hit = _earliest_longest(t, list(lookup))
    return lookup[hit[0]] if hit else None


def match_color(text: str) -> str | None:
    hit = _earliest_longest(text.lower(), COLORS)
    if not hit:
        return None
    c = hit[0].replace("gray", "grey").replace("off-white", "off white").replace("multicolor", "multicolour")
    return c


def match_brand(text: str) -> str | None:
    hit = _earliest_longest(text.lower(), [b.lower() for b in BRANDS])
    if not hit:
        return None
    return next(b for b in BRANDS if b.lower() == hit[0])


def match_location(text: str) -> str | None:
    """Canonical campus location mentioned in the text, or None."""
    t = _HOLDING_SPAN_RE.sub(" ", text.lower())
    alias_to_loc: dict[str, str] = {}
    for loc in LOCATIONS:
        alias_to_loc[loc.lower()] = loc
        for alias in LOCATION_ALIASES.get(loc, []):
            alias_to_loc[alias] = loc
    # 1) prefer a place that follows a preposition ("in the canteen")
    best: tuple[int, int, str] | None = None
    for alias, loc in alias_to_loc.items():
        m = re.search(r"\b" + _PREPOSITION_RE + r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", t)
        if m:
            cand = (m.start(), -len(alias), loc)
            if best is None or cand < best:
                best = cand
    if best:
        return best[2]
    # 2) otherwise the earliest place mentioned at all ("library" as a one-word answer)
    hit = _earliest_longest(t, list(alias_to_loc))
    return alias_to_loc[hit[0]] if hit else None


def parse_holding(text: str, answer: bool = False) -> str | None:
    """Where a FOUND item is now: finder / admin_office / security.

    answer=True  -> the user is replying to "where is it now?", so be lenient ("office", "me").
    answer=False -> a general message; only clear phrases count ("handed it to security").
    """
    t = text.lower().strip()
    if not answer:
        span = _HOLDING_SPAN_RE.search(t)
        if span:
            return parse_holding(span.group(0), answer=True)
        return "finder" if _FINDER_STRICT_RE.search(t) else None
    for value, pattern in _HOLDING_RULES:
        if pattern.search(t):
            return value
    return None


def _past(d: date, today: date) -> date | None:
    return d if d <= today else None


def parse_date(text: str, today: date) -> date | None:
    """Explicit or relative date in the past (or today). Future dates return None."""
    t = text.lower()

    m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", t)  # 2026-09-28
    if m:
        try:
            return _past(date(int(m.group(1)), int(m.group(2)), int(m.group(3))), today)
        except ValueError:
            pass

    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", t)  # 28/09, 28-09-2026 (day first)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            d = date(year, int(m.group(2)), int(m.group(1)))
            if not m.group(3) and d > today:
                d = d.replace(year=d.year - 1)
            return _past(d, today)
        except ValueError:
            pass

    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({_MONTH_ALT})\b\.?(?:,?\s*(20\d{{2}}))?", t) \
        or re.search(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(20\d{{2}}))?", t)
    if m:
        a, b, y = m.group(1), m.group(2), m.group(3)
        day, month = (int(a), _MONTHS[b]) if a.isdigit() else (int(b), _MONTHS[a])
        try:
            d = date(int(y) if y else today.year, month, day)
            if not y and d > today:
                d = d.replace(year=d.year - 1)
            return _past(d, today)
        except ValueError:
            pass

    if re.search(r"\bday before yesterday\b|\bparso\b", t):
        return today - timedelta(days=2)
    if re.search(r"\byesterday\b|\bkal\b|\blast night\b|\byday\b", t):
        return today - timedelta(days=1)
    if re.search(r"\btoday\b|\baaj\b|\bthis (morning|afternoon|evening)\b|\btonight\b|\bjust now\b"
                 r"|\b(an|one|few|\d+) hours? ago\b|\bearlier today\b", t):
        return today

    m = re.search(r"\b(\d+|a|an|one|two|three|four|five|six|seven)\s+days?\s+ago\b", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORDS[m.group(1)]
        return today - timedelta(days=n)
    if re.search(r"\blast week\b|\ba week ago\b", t):
        return today - timedelta(days=7)

    m = re.search(r"\b(last\s+|on\s+|this\s+(?:past\s+)?)?(" + "|".join(_WEEKDAYS) + r")\b", t)
    if m:
        target = _WEEKDAYS.index(m.group(2))
        back = (today.weekday() - target) % 7
        if back == 0 and (m.group(1) or "").strip() == "last":
            back = 7
        return today - timedelta(days=back)
    return None


def parse_time(text: str) -> time | None:
    t = text.lower()
    m = re.search(r"\b(\d{1,2})(?::|\.)(\d{2})\s*(am|pm|a\.m\.|p\.m\.)", t) \
        or re.search(r"\b(\d{1,2}):(\d{2})\b()", t)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm and ampm.startswith("p") and hour < 12:
            hour += 12
        if ampm and ampm.startswith("a") and hour == 12:
            hour = 0
        if hour < 24 and minute < 60:
            return time(hour, minute)
    m = re.search(r"\b(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.)", t)
    if m:
        hour = int(m.group(1))
        if m.group(2).startswith("p") and hour < 12:
            hour += 12
        if m.group(2).startswith("a") and hour == 12:
            hour = 0
        if hour < 24:
            return time(hour, 0)
    for words, value in ((r"\bnoon\b|\bmidday\b|\blunch\b", time(13, 0)),
                         (r"\bmorning\b", time(9, 0)), (r"\bafternoon\b", time(14, 0)),
                         (r"\bevening\b", time(18, 0)), (r"\bnight\b|\btonight\b", time(21, 0))):
        if re.search(words, t):
            return value
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
@dataclass
class Extraction:
    category: str | None = None
    item_name: str | None = None
    color: str | None = None
    brand: str | None = None
    location: str | None = None
    event_date: date | None = None
    event_time: time | None = None
    current_holding: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def extract(text: str, today: date) -> Extraction:
    item = match_item(text)
    return Extraction(
        category=item[0] if item else None,
        item_name=item[1] if item else None,
        color=match_color(text),
        brand=match_brand(text),
        location=match_location(text),
        event_date=parse_date(text, today),
        event_time=parse_time(text),
        current_holding=parse_holding(text, answer=False),
    )
