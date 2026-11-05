"""Offer prices, comparison rules and selection of matching offers."""

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, replace

from .titles import clean_title, taught_away, title_fits


def lowest_item_price(source, search, compared):
    """The lowest item price whose price before shipping (item + buyer protection) reaches COMPARED."""
    return bisect_left(range(compared), compared, key=lambda price: price + search_fee(source, price, search))


def portal_price_bounds(source, search):
    """Conservative item-price window; never drop a valid total-price match because of shipping."""
    if source == 'idealo':
        return 0, None  # the independent new-price reference must survive even above the alarm limit
    # Buyer protection counts on top of the item price, so the window shifts down to the item prices that fit.
    # Shipping is unknown until read, so with shipping the item itself may cost anything below the maximum.
    minimum = 0 if search.get('include_shipping') else lowest_item_price(source, search, search.get('min_price', 0))
    maximum = (
        lowest_item_price(source, search, search['max_price']) - 1 if search.get('max_price') is not None else None
    )
    if source == 'kleinanzeigen':
        # "Zu verschenken" has no price: any lower bound would hide it at the provider.
        if search.get('free'):
            minimum = 0
        # Kleinanzeigen accepts whole euros: widen outwards, then enforce exact cents locally.
        minimum = minimum // 100 * 100
        maximum = ((maximum + 99) // 100) * 100 if maximum is not None else None
    return minimum, maximum


def select_offers(offers, search):
    """One destination per result; choose its cheapest shop using this search's price basis."""
    result: dict[str, Offer] = {}

    def rank(offer):
        value = offer.compared_price(search)
        return (
            value is None,
            value if value is not None else offer.price,
            offer.price,
            offer.shipping if offer.shipping is not None else float('inf'),
        )

    for offer in offers:
        offer = replace(offer, title=clean_title(offer.source, offer.title))
        if not title_fits(offer.source, offer.title, search) or taught_away(offer.title, search):
            continue
        if offer.url not in result or rank(offer) < rank(result[offer.url]):
            result[offer.url] = offer
    dealers = mass_sellers(result.values())
    return [offer for offer in result.values() if (offer.source, offer.owner) not in dealers]


DEALER_FROM = 3  # matching listings of one seller in one result that mark a merchandise dealer


def mass_sellers(offers):
    """(source, owner) of sellers with DEALER_FROM or more matching listings in one result. Someone selling their
    own copy lists it once; posters, mouse pads and signs come in series under the product's name."""
    counts = Counter((offer.source, offer.owner) for offer in offers if offer.owner)
    return {seller for seller, count in counts.items() if count >= DEALER_FROM}


def with_shipping(source, search):
    """Whether an offer compares with shipping: idealo shops always by their full price, others as the search says."""
    return source == 'idealo' or bool(search.get('include_shipping'))


def buyer_fee(source, price):
    """Vinted's buyer protection on an item: 5 % of the price plus 0,70 €. Checked 28.10.2026, the "inkl."
    price on Vinted's cards says the same (44,00 € -> 46,90 €). Other providers add nothing."""
    return (price * 5 + 50) // 100 + 70 if source == 'vinted' and price > 0 else 0


def search_fee(source, price, search):
    """The buyer protection a search adds to an item price: only on Vinted and only while it includes it."""
    return buyer_fee(source, price) if search.get('include_fee') else 0


def compared_price(price, shipping, include_shipping, fee=0):
    """The price a search compares: item price (+ buyer protection FEE), or that + known shipping.
    Unknown shipping is never 0."""
    if not include_shipping:
        return price + fee
    return None if shipping is None else price + fee + shipping


@dataclass(frozen=True)
class Offer:
    source: str
    url: str
    title: str
    price: int
    shipping: int | None = None
    seller: str = ''
    condition: str = ''
    owner: str = ''  # the provider's ID of the seller, only for the dealer signal; not kept with the hits, never shown

    @classmethod
    def from_row(cls, row):
        """An offer from a stored row (offers or saved table)."""
        return cls(
            row['source'], row['url'], row['title'], row['price'], row['shipping'], row['seller'], row['condition']
        )

    def compared_price(self, search):
        return compared_price(
            self.price, self.shipping, with_shipping(self.source, search), search_fee(self.source, self.price, search)
        )


def free(price, search):
    """A given-away offer (price 0) in a search that wants them; the minimum price does not apply."""
    return price == 0 and bool(search.get('free'))


def in_range(price, search, given_away=False):
    """The price range of a search: from min_price (junk such as 3 € listings stays out) up to below max_price.
    GIVEN_AWAY (see free) skips the minimum: shipping alone does not make an offer junk."""
    return price is not None and (given_away or search.get('min_price', 0) <= price) and price < search['max_price']


def too_cheap(offer, search):
    # Without a known shipping price the item price alone decides; a 3 € listing is junk either way.
    if free(offer.price, search):
        return False
    price = offer.compared_price(search)
    return (price if price is not None else offer.price) < search.get('min_price', 0)


def eligible(offer, search):
    return in_range(offer.compared_price(search), search, free(offer.price, search))
