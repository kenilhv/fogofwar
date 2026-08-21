"""Entity-mention extraction -- v1, deliberately simple and swappable.

This is a regex-based proper-noun heuristic (consecutive capitalized words),
not an NLP pipeline. It exists to get a real graph structure end to end
without a hard dependency on an LLM call or a heavy NER model before the
core bitemporal mechanism (the actual research claim) is even testable.

It is wrong in the normal ways this kind of heuristic is wrong: it misses
lowercase references ("the auth service"), it over-triggers on sentence-
initial capitals, and it can't resolve "Sam" / "the intern" / "@soham" to
one entity (that's Parallax's problem, not this one, and deliberately out
of scope here). Swap `extract_entities` for an LLM-based or spaCy-based
extractor without touching anything downstream -- callers only depend on
the (name: str) contract below.
"""

from __future__ import annotations

import re

_PROPER_NOUN_RUN = re.compile(r"\b([A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*){0,3})\b")

_STOPWORDS_SENTENCE_START = {
    "I",
    "The",
    "A",
    "An",
    "This",
    "That",
    "It",
    "We",
    "You",
    "They",
    "He",
    "She",
    # question/auxiliary words that are only capitalized because they
    # happen to open a sentence -- LongMemEval questions are interrogative,
    # so these show up constantly leading straight into a real entity
    # ("Does Sam Ratnaparkhi own..."), and left untreated they get pulled
    # into the same capitalized run as the entity that follows them.
    "Does",
    "Do",
    "Did",
    "Is",
    "Was",
    "Were",
    "Are",
    "Can",
    "Could",
    "Will",
    "Would",
    "Should",
    "Has",
    "Have",
    "Had",
    "Who",
    "What",
    "Where",
    "When",
    "Why",
    "How",
}


def extract_entities(text: str) -> list[str]:
    """Returns deduplicated candidate entity names mentioned in `text`."""
    candidates: list[str] = []
    seen: set[str] = set()
    for match in _PROPER_NOUN_RUN.finditer(text):
        phrase = match.group(1).strip()
        words = phrase.split(" ")
        # Strip a leading sentence-starter word even from a multi-word run
        # ("Does Sam Ratnaparkhi" -> "Sam Ratnaparkhi"), not just a lone
        # single-word match -- the regex can't tell "capitalized because
        # it's a real name" from "capitalized because it opens a sentence"
        # on its own.
        if words[0] in _STOPWORDS_SENTENCE_START:
            words = words[1:]
        if not words:
            continue
        phrase = " ".join(words)
        if phrase not in seen:
            seen.add(phrase)
            candidates.append(phrase)
    return candidates
