"""Rule-based intent recognition (used by the fallback engine and the confirmation guard).

detect_intent(text) returns one of:
    report_lost, report_found, search, check_status, find_matches, item_details,
    self_found, greeting, help, cancel, yes, no, other

It is keyword/regex based (English + common Hinglish). It is deterministic: the same
message always gives the same intent, which makes it easy to test and to explain.
"""

from __future__ import annotations

import re

ITEM_CODE_RE = re.compile(r"\b([LF])-?(\d{1,4})\b", re.IGNORECASE)

_YES_WORDS = {
    "yes", "y", "yeah", "yea", "yep", "yup", "ok", "okay", "k", "sure", "confirm", "confirmed",
    "correct", "right", "haan", "han", "ha", "haa", "ji", "save", "done", "perfect",
}
_YES_PHRASES = {
    "go ahead", "save it", "please save", "yes please", "looks good", "that's right",
    "thats right", "theek hai", "thik hai", "haan ji", "ha ji", "all good", "yes save it",
}
_NO_FIRST_WORDS = {"no", "n", "nope", "nah", "nahi", "nahin", "na", "wrong", "incorrect", "dont", "don't"}
_HEDGE_WORDS = {"but", "change", "wrong", "not", "edit", "instead", "except", "actually", "wait"}

_CANCEL_RE = re.compile(r"^(cancel|stop|reset|start over|never ?mind|forget it|chhodo|rehne do)\b")

_STATUS_RE = re.compile(
    r"\bmy (reports?|items?|complaints?|posts?)\b|\bwhat (did|have) i (report|post)|\bstatus\b"
    r"|\bmy (lost|found) (reports?|items?)\b"
)
_MATCH_RE = re.compile(r"\bmatch(es|ing|ed)?\b|\bany (news|update)s?\b")
_DETAILS_RE = re.compile(r"\b(details?|show|tell me about|about|info|information|what is)\b")
_SELF_FOUND_RE = re.compile(r"\b(i )?found (it|my|mine)\b.*\b(myself|own|back)\b|\bgot it back\b")
_FOUND_RE = re.compile(
    r"\b(i|we)\s+(have\s+|just\s+|had\s+)?(found|picked up|picked|got hold of)\b"
    r"|\bfound (a|an|this|these|some|one|someone'?s?|somebody'?s?)\b"
    r"|\b(mila|mili|mile|mil gaya|mil gayi)\b"
    r"|\bsomeone (left|forgot)\b|\bturn(ed|ing)? in\b|\bhand(ed|ing)? (it )?over\b"
    r"|\breport (a |an )?found\b|\blying (unattended|around|there)\b"
)
_LOST_RE = re.compile(
    r"\blost\b|\bmissing\b|\bmisplaced\b|\bcan'?t find\b|\bcannot find\b|\bcould ?n'?t find\b"
    r"|\bkho\b|\bkhoya\b|\bkhoyi\b|\bgum ho\b|\bleft (my|it)\b|\bforgot my\b|\breport (a |an )?lost\b"
)
_SEARCH_RE = re.compile(
    r"\b(has|have|did) (any|some)(one|body) (found|seen|picked|turned)\b"
    r"|\bany(one|body)? (found|seen)\b|\bsearch\b|\blook(ing)? for\b|\bfind (a|an|my)\b"
    r"|\bshow (me )?(all |the )?(found|lost)\b|\bis there (a|an|any)\b|\blist (found|lost)\b"
    r"|\bwas (a|an|any) .* found\b"
)
_ASKS_RE = re.compile(
    r"\b(has|have|did) (any|some)(one|body)\b|\bany(one|body)? (found|seen)\b|\bis there (a|an|any)\b"
    r"|\bwas (a|an|any) .* found\b"
)
_GREETING_RE = re.compile(r"^(hi+|hello|hey|namaste|namaskar|good (morning|afternoon|evening))\b")
_HELP_RE = re.compile(r"\b(help|what can you do|how does (this|it) work|how to use|lost and found work)\b")


def _clean(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"[^\w\s'&-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def is_negative(text: str) -> bool:
    t = _clean(text)
    if not t:
        return False
    words = t.split()
    return words[0] in _NO_FIRST_WORDS or "not correct" in t or "not right" in t


def is_affirmative(text: str) -> bool:
    """True only for a short, plain 'yes' (used to guard report creation)."""
    t = _clean(text)
    if not t or len(t.split()) > 6 or is_negative(text):
        return False
    words = set(t.split())
    if words & _HEDGE_WORDS:  # "ok but change the location" is NOT a yes
        return False
    if t in _YES_WORDS or t in _YES_PHRASES:
        return True
    return re.match(
        r"^(yes|yeah|yep|yup|haan|han|ha|ok|okay|sure|confirm|correct|right|ji|theek|thik|save|go ahead|done)\b",
        t,
    ) is not None


def find_item_code(text: str) -> str | None:
    """'l-1', 'L0001', 'F-0017' → 'L-0001' / 'F-0017'."""
    m = ITEM_CODE_RE.search(text)
    if not m:
        return None
    return f"{m.group(1).upper()}-{int(m.group(2)):04d}"


def detect_intent(text: str) -> str:
    t = _clean(text)
    if not t:
        return "other"
    if _CANCEL_RE.match(t):
        return "cancel"

    code = find_item_code(text)
    if _STATUS_RE.search(t):
        return "check_status"
    if _MATCH_RE.search(t):
        return "find_matches"
    if code and (_DETAILS_RE.search(t) or len(t.split()) <= 2):
        return "item_details"
    if _HELP_RE.search(t):
        return "help"
    if _SELF_FOUND_RE.search(t):
        return "self_found"

    lost = bool(_LOST_RE.search(t))
    found = bool(_FOUND_RE.search(t))
    asks = bool(_ASKS_RE.search(t))  # "has anyone found a calculator?" is a search, not a found report
    if lost:
        return "report_lost"          # "I lost my wallet, has anyone found it?" → report it, then match
    if asks:
        return "search"
    if found:
        return "report_found"
    if _SEARCH_RE.search(t):
        return "search"
    if _GREETING_RE.match(t):
        return "greeting"
    if is_affirmative(text):
        return "yes"
    if is_negative(text):
        return "no"
    return "other"
