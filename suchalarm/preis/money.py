"""EUR amounts and stated outbound shipping costs, in cents."""

import re
from decimal import Decimal, InvalidOperation

MAX_AMOUNT = 10_000_000  # euros; anything above is a parsing accident, not a price


def money(value):
    """Convert a EUR amount into integer cents, never binary floats."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace('\xa0', '').replace(' ', '').replace('€', '').replace('EUR', '')
    if re.fullmatch(r'\d{1,3}(?:\.\d{3})+', text):
        text = text.replace('.', '')
    if ',' in text and '.' in text and text.rfind('.') > text.rfind(','):
        text = text.replace(',', '')
    elif ',' in text:
        text = text.replace('.', '').replace(',', '.')
    try:
        amount = Decimal(text)
        if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT:
            return None
        return int((amount * 100).quantize(Decimal('1')))
    except InvalidOperation:
        return None


_LABEL = r'(?:versand(?:kosten)?|liefer(?:ung|kosten)|shipping|postage|delivery)'
_NUMBER = r'\d+(?:[.,]\d+)*'
_COST = rf'(?:eur\s*{_NUMBER}|{_NUMBER}\s*(?:€|eur\b))'
_FREE = r'(?:gratis|kostenlos(?:e[rmns]?)?|kostenfrei(?:e[rmns]?)?|free)'
_DAYS = r'(?:\d+\s*(?:[-–]\s*\d+)?\s*(?:tage?|days?)\s*)?'
FREE_SHIPPING = re.compile(
    rf'{_FREE}\s*{_DAYS}{_LABEL}|versandkostenfrei|(?<![a-zäöüß]){_LABEL}\s*:?\s*(?:gratis|kostenlos|kostenfrei|free)\b'
)
# Context that makes a mention useless: "kein kostenloser Versand", "ab 4 €", "Rückversand", ranges.
_FREE_NEGATED = re.compile(r'\b(?:kein[er]?|nicht|ohne)\s*$')
_FREE_QUALIFIED = re.compile(r'\s*(?:ab\b|from\b|(?:für\s+)?rück|returns?\b)')
_COST_VAGUE_BEFORE = re.compile(r'(?:\b(?:ca\.?|etwa|bis|kein|ohne|nicht)|[-–])\s*$')
_COST_VAGUE_AFTER = re.compile(r'\s*(?:[-–]|bis\b|to\b|(?:für\s+)?rück|returns?\b)')
_FROM_BEFORE = re.compile(r'\b(?:ab|from)\s*$')


def _around(text, match):
    return text[max(0, match.start() - 25) : match.start()], text[match.end() : match.end() + 35]


def _cents(value):
    value = value.replace('eur', '').replace('€', '').strip()
    return 0 if re.fullmatch(r'0(?:[,.]0+)?', value) else money(value)


def _free_mentions(text):
    """True if free shipping is stated, False if not mentioned, None if a mention is negated or qualified."""
    found = False
    for match in FREE_SHIPPING.finditer(text):
        before, after = _around(text, match)
        if _FREE_NEGATED.search(before) or _FREE_QUALIFIED.match(after):
            return None
        found = True
    return found


def _charges(text, allow_from):
    """Stated shipping amounts in cents, or None if one is vague (a range, "ca.", a return fee, a minimum)."""
    minimum = r'(?:ab\s*)?' if allow_from else ''
    amounts = set()
    for pattern in (
        rf'(?<!rück)(?<!rueck){_LABEL}\s*[:+]?\s*{minimum}(?P<cost>{_COST})',
        rf'(?<![\w.,])(?P<cost>{_COST})\s*(?:·\s*{_DAYS})?{_LABEL}',  # eBay: "+EUR 6,19 · 2-3 Tage Lieferung"
    ):
        for match in re.finditer(pattern, text):
            before, after = _around(text, match)
            if _COST_VAGUE_BEFORE.search(before) or _COST_VAGUE_AFTER.match(after):
                return None
            if not allow_from and _FROM_BEFORE.search(before):
                return None
            amounts.add(_cents(match.group('cost')))
    return amounts


def shipping(text, *, amount_only=False, allow_from=False):
    """Outbound EUR shipping in cents, or None when unknown or ambiguous. Never guesses 0.

    AMOUNT_ONLY: TEXT is a dedicated shipping field, so a bare amount counts. ALLOW_FROM: a
    provider minimum ("ab 2,49 €", Vinted) is accepted as the amount.
    """
    text = re.sub(r'\s+', ' ', str(text).casefold().replace('\xad', '')).strip()
    values = set()
    # A bare amount is valid only in a dedicated shipping field, never in card text.
    if amount_only and re.fullmatch((r'(?:ab\s*)?' if allow_from else '') + _COST, text):
        values.add(_cents(re.sub(r'^ab\s*', '', text)))
    free = _free_mentions(text)
    if free is None:
        return None
    if free:
        values.add(0)
    charges = _charges(text, allow_from)
    if charges is None:
        return None
    values = (values | charges) - {None}
    return values.pop() if len(values) == 1 else None
