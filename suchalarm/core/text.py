"""Whole-word exclusions shared by search kinds; a directly preceding negation cancels a mention."""

import re
import unicodedata
from collections.abc import Callable, Iterable

NEGATION = r'(?:kein|keine|keinen|keiner|ohne|nicht|nie)'


def folded_words(text: str) -> str:
    """Case- and accent-insensitive words, with punctuation and whitespace treated as separators."""
    text = ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))
    return re.sub(r'[^\w]+', ' ', text).strip()


def excluded_word(text: str, words: Iterable[str], *, normalize: Callable[[str], str] = folded_words) -> str | None:
    """The first configured word/phrase with an unnegated whole-word mention, preserving its original spelling."""
    padded = f' {normalize(text)} '
    for word in words:
        term = normalize(word)
        if term and any(
            not re.search(rf' {NEGATION} $', padded[: match.start() + 1])
            for match in re.finditer(rf'(?= {re.escape(term)} )', padded)
        ):
            return word
    return None
