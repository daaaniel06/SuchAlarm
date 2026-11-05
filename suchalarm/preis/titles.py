"""Search spellings, title matching and words learned from hidden offers."""

import re
from collections import Counter
from functools import lru_cache
from itertools import pairwise

from ..core.text import excluded_word, folded_words
from .categories import CAR_MAKES, PLATFORMS, PROVIDER_PLATFORMS


def clean_title(source, title):
    """Remove eBay's UI labels at the title boundaries, preserving seller wording."""
    title = title.strip()
    if source == 'ebay':
        title = re.sub(
            r'\s*(?:Wird in neuem Fenster oder Tab geöffnet|Opens in a new window or tab)\s*$', '', title, flags=re.I
        )
        # Old textContent snapshots glued this badge directly to the actual title.
        title = re.sub(r'^(?:Neues Angebot|New Listing)(?=\S)', '', title).strip()
    return title


def platform(search):
    """The platform a search is limited to, or None."""
    chosen = (search.get('filters') or {}).get('platform')
    return chosen if chosen in PLATFORMS else None


MAX_ALTERNATIVES = 2  # each one is a further page in the provider's budget


def alternatives(search):
    """Other spellings the search also looks for ("RE Requiem" besides "Resident Evil Requiem")."""
    return [' '.join(phrase.split()) for phrase in (search.get('alternatives') or '').split(',') if phrase.strip()]


def phrases(source, search):
    """What the provider is searched for, one request each. idealo's new price belongs to the query alone."""
    return [search['query']] if source == 'idealo' else [search['query'], *alternatives(search)]


def terms(search, phrase=None):
    """The words of a search: its query (or PHRASE, one of its other spellings) plus a chosen platform."""
    chosen, phrase = platform(search), phrase or search['query']
    return f'{phrase} {PLATFORMS[chosen]}' if chosen else phrase


def all_terms(search):
    """Every word the search asks for, in any of its spellings."""
    return ' '.join(terms(search, phrase) for phrase in [search['query'], *alternatives(search)])


def platform_ids(source, search):
    """The provider's own platform filter for the search; empty where the platform must be in the text."""
    chosen = platform(search)
    return PROVIDER_PLATFORMS.get(source, {}).get(chosen, ()) if chosen else ()


def wanted_words(source, search, phrase=None):
    """The words a provider is searched for and an offer title must contain. Where the provider filters the
    platform itself, the title need not name it: sellers often leave it out ("Resident Evil Requiem")."""
    phrase = phrase or search['query']
    return phrase if platform_ids(source, search) else terms(search, phrase)


def named_platforms(title):
    """The platforms a title names; "Switch 2" is not also "Switch"."""
    text, named = f' {normalized(title)} ', set()
    for key, label in sorted(PLATFORMS.items(), key=lambda item: -len(item[1])):
        phrase = f' {normalized(label)} '
        if phrase in text:
            named.add(key)
            text = text.replace(phrase, ' ')
    return named


def without_make(search, words):
    """A car search's words without the make, where something else is left: "VW Golf" is also "Golf 7 TSI"."""
    if search.get('category') != 'autos':
        return None
    rest = [word for word in normalized(words).split() if word not in CAR_MAKES]
    return ' '.join(rest) if rest and len(rest) < len(normalized(words).split()) else None


def title_fits(source, title, search):
    """Whether an offer title belongs to the search: all words of one of its spellings, nothing excluded, not
    only other platforms. A provider's platform filter fills up with other platforms once its matches run out."""
    chosen, named = platform(search), named_platforms(title)
    excluded = search.get('excluded', '')
    wanted = [wanted_words(source, search, phrase) for phrase in phrases(source, search)]
    makes = set(normalized(title).split()) & CAR_MAKES
    wanted += [
        rest for words in wanted if (rest := without_make(search, words)) and not makes - set(normalized(words).split())
    ]
    return any(matches(title, words, excluded) for words in wanted) and (chosen is None or not named or chosen in named)


def taught_away(title, search):
    """Whether TITLE carries a word hiding taught the search's category (learning.py), never one of its own words.
    Applied per search after a shared provider result, like the search's own excluded words."""
    own = set(normalized(all_terms(search)).split())
    return any(word not in own for word in set(normalized(title).split()) & set(search.get('learned', ())))


LEARN_FROM = 2  # hidden titles a word needs before it filters on its own


def learned_words(hidden, kept_titles):
    """Words that mark what gets hidden: in at least LEARN_FROM hidden titles and in more hidden than kept ones.
    HIDDEN holds (title, the words its search asked for); those words say what was wanted, never why it was hidden.
    Numbers never count: they are editions, sizes and years, not a kind of junk."""

    def words(title):
        return set(normalized(title).split())

    hidden_counts = Counter(word for title, wanted in hidden for word in words(title) - words(wanted))
    kept_counts = Counter(word for title in kept_titles for word in words(title))
    return sorted(
        word
        for word, count in hidden_counts.items()
        if count >= LEARN_FROM and count > kept_counts[word] and not word.isdigit()
    )


# Words sellers write either way: "Volkswagen" is "VW", "Golf VII" is "Golf 7", "Pedelec" is "E-Bike".
# I, V and X stay letters ("Xbox Series X", "iPhone X"), IX too: "BMW iX" is a car, not a 9.
ROMAN = {'ii': '2', 'iii': '3', 'iv': '4', 'vi': '6', 'vii': '7', 'viii': '8'}
ALIASES = tuple(
    (re.compile(pattern), same)
    for pattern, same in (
        (r'\bvolkswagen\b', 'vw'),
        (r'\bmercedes ?benz\b', 'mercedes'),
        (r'\b(?:e ?bikes?|pedelecs?|elektro ?fahrrad|elektro ?fahrr(?:a|ae)der)\b', 'ebike'),
        (r'\bmountain ?bikes?\b', 'mtb'),
        (r'\be mtb\b', 'ebike mtb'),  # "E-MTB", "E-Mountainbike"
        (r'\broad ?bikes?\b', 'rennrad'),
        *((rf'\b{numeral}\b', value) for numeral, value in ROMAN.items()),
    )
)


@lru_cache(maxsize=4096)  # the same titles and search words come back with every check
def normalized(text):
    # Accents are spelled either way ("Résident Evil", "Hulle"); compare the base letters.
    text = folded_words(text)
    # Sellers write PS5, PS 5 and PlayStation 5 interchangeably.
    text = re.sub(r'\b(?:playstation|ps) ?([1-5])\b', r'ps\1', text)
    text = re.sub(r'\bswitch ?2\b', 'switch 2', text)
    text = re.sub(r'\bxbox ?(series|one)(?: ?([xs]))?\b', lambda m: f'xbox {m[1]} {m[2] or ""}'.strip(), text)
    for pattern, same in ALIASES:
        text = pattern.sub(same, text)
    return text


def _has_all_words(title_words, wanted):
    """Whether every wanted word is in the title, also where one side writes two words as one:
    "Spiderman" is "Spider-Man", "iPhone13" is "iPhone 13"."""
    if Counter(wanted) <= Counter(title_words):
        return True
    present: dict[str, list[int]] = {}
    for index, word in enumerate(title_words):
        present.setdefault(word, []).append(1 << index)
    for index, (first, second) in enumerate(pairwise(title_words)):
        present.setdefault(first + second, []).append(3 << index)

    @lru_cache(maxsize=4096)
    def fits(index, used):
        if index == len(wanted):
            return True
        # Each title occurrence may fill only one word (or one glued pair) of the query.
        for size in (1, 2):
            if index + size > len(wanted):
                continue
            for occurrence in present.get(''.join(wanted[index : index + size]), ()):
                if not used & occurrence and fits(index + size, used | occurrence):
                    return True
        return False

    return fits(0, 0)


def matches(title, query, excluded=''):
    text = normalized(title)
    if not _has_all_words(text.split(), normalized(query).split()):
        return False
    return excluded_word(text, excluded.split(','), normalize=normalized) is None
