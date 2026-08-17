"""Content-local, deterministic recognition of Workshop completion intent."""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum


class CompletionUtterance(StrEnum):
    NONE = "NONE"
    AMBIGUOUS = "AMBIGUOUS"
    EXPLICIT = "EXPLICIT"
    AFFIRMATIVE = "AFFIRMATIVE"


def _normalized(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = text.replace("’", "'")
    text = re.sub(r"[^a-z0-9'\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_NEGATED = re.compile(
    r"\b(?:not|never|haven't|hasn't|hadn't|isn't|aren't|wasn't|weren't|don't|doesn't|didn't)\b"
)
_EXPLICIT = (
    re.compile(
        r"\b(?:i|we)\s+(?:have\s+)?(?:completed|finished)\s+(?:the\s+)?"
        r"(?:spec(?:ification)?\s+)?workshop\b"
    ),
    re.compile(
        r"\b(?:i|we)\s+complete\s+(?:the\s+)?(?:spec(?:ification)?\s+)?workshop\b"
    ),
    re.compile(
        r"\b(?:the\s+)?(?:spec(?:ification)?\s+)?workshop\s+is\s+"
        r"(?:complete|completed|finished)\b"
    ),
    re.compile(
        r"\b(?:i am|i'm|we are|we're)\s+done\s+with\s+(?:the\s+)?"
        r"(?:spec(?:ification)?\s+)?workshop\b"
    ),
    re.compile(
        r"\bi\s+(?:have\s+)?defined\s+everything\s+(?:that\s+)?i\s+"
        r"(?:need|needed)(?:\s+to)?\b"
    ),
)
_AMBIGUOUS = (
    re.compile(r"\b(?:i\s+think|i\s+guess|maybe|probably)\b.*\b(?:done|finished|complete)\b"),
    re.compile(r"\b(?:are\s+we|we\s+might\s+be|we\s+could\s+be)\s+(?:done|finished)\b"),
    re.compile(r"\b(?:that\s+should\s+be\s+all|let's\s+wrap\s+up|lets\s+wrap\s+up)\b"),
)
_AFFIRMATIVE = re.compile(
    r"^(?:yes(?:\s+i\s+confirm)?|i\s+confirm|confirmed|correct|that's\s+right|"
    r"finish\s+(?:it|the\s+(?:spec\s+)?workshop)(?:\s+now)?|please\s+finish(?:\s+it)?|"
    r"yes\s+finish(?:\s+it)?)(?:\s+please)?$"
)


def classify_completion_utterance(value: str) -> CompletionUtterance:
    """Classify only explicit local language; never infer intent from semantics."""

    text = _normalized(value)
    if not text or _NEGATED.search(text):
        return CompletionUtterance.NONE
    if any(pattern.search(text) for pattern in _EXPLICIT):
        return CompletionUtterance.EXPLICIT
    if any(pattern.search(text) for pattern in _AMBIGUOUS):
        return CompletionUtterance.AMBIGUOUS
    if _AFFIRMATIVE.fullmatch(text):
        return CompletionUtterance.AFFIRMATIVE
    return CompletionUtterance.NONE
