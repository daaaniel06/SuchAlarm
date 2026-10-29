import json
import re
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote, urlencode

from ..core import browser, cadence, images
from ..core.browser import EMPTY, Unreadable, check_response
from . import delivery
from .domain import (
    HOSTS,
    Offer,
    around,
    canonical,
    clean_title,
    money,
    phrases,
    platform_ids,
    portal_price_bounds,
    section,
    shipping,
    title_fits,
    vehicle_fits,
    wanted_words,
)

# A function expression for page.evaluate; a formatter's trailing semicolon would make it a statement.
EXTRACT = Path(__file__).with_name('extract.js').read_text().strip().removesuffix(';')


def search_url(source, query, search=None):
    search = search or {}
    minimum, maximum = portal_price_bounds(source, search)
    category, place = section(source, search), around(source, search)

    def euros(cents):
        return f'{cents // 100}.{cents % 100:02d}'

    if source == 'ebay':
        # Best Match keeps the searched game on page one; price order fills it with
        # cheap unrelated titles. The app sorts matching offers by price itself.
        # 240 cards per page (eBay's largest page) instead of 60: one visit sees four times as many offers.
        params = {
            '_nkw': query,
            '_ipg': '240',
            'LH_BIN': '1',
            '_sop': '12',
            'LH_ItemCondition': '2750|3000|4000|5000|6000',
            'LH_LocatedIn': '1',
            '_salic': '77',
        }
        if category:
            params['_sacat'] = category
        if platforms := platform_ids(source, search):
            # eBay's own filter links encode the value twice ("Sony%2520PlayStation%25205").
            params['Plattform'] = quote('|'.join(platforms), safe='')
        if place:
            # Around a postcode instead of all of Germany; the cards then say "… km von <PLZ>".
            del params['LH_LocatedIn'], params['_salic']
            params.update({'LH_PrefLoc': '99', '_stpos': place[0], '_sadis': str(place[1]), '_fspt': '1'})
        if minimum:
            params['_udlo'] = euros(minimum)
        if maximum is not None:
            params['_udhi'] = euros(maximum)
        return 'https://www.ebay.de/sch/i.html?' + urlencode(params)
    if source == 'kleinanzeigen':
        price = (
            f'preis:{minimum // 100 if minimum else ""}:{maximum // 100 if maximum is not None else ""}/'
            if minimum or maximum is not None
            else ''
        )
        slug = quote(query.replace(' ', '-'), safe='')
        # Newest first: checked every 15 minutes, every new offer is on page one at least once.
        return f'https://www.kleinanzeigen.de/s-{price}{slug}/k0{kleinanzeigen_code(category, place, search)}?sortingField=SORTING_DATE'
    if source == 'vinted':
        # Native relevance prevents cheap unrelated games from filling the first page.
        # The app sorts the matching offers by item/total price after extraction.
        params = {'search_text': query, 'currency': 'EUR'}
        if category:
            params['catalog[]'] = category
        if minimum:
            params['price_from'] = euros(minimum)
        if maximum is not None:
            params['price_to'] = euros(maximum)
        platforms = platform_ids(source, search)
        if platforms:
            params['video_game_platform_ids[]'] = platforms
        return 'https://www.vinted.de/catalog?' + urlencode(params, doseq=True)
    return 'https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?' + urlencode({'q': query})


def kleinanzeigen_code(category, place, search):
    """The part after "k0": section (c…), location ID and radius (l…r…), car limits."""
    code = f'c{category}' if category else ''
    if place:
        location = cadence.location('kleinanzeigen', place[0])
        # Without a known location ID the search would silently run nationwide.
        if location is None:
            raise LookupError(place[0])
        code += f'l{location}r{place[1]}'
    filters = search.get('filters') or {}
    if category == CARS and (filters.get('year_from') or filters.get('km_to')):
        code += f'+autos.ez_i:{filters.get("year_from") or ""}%2C+autos.km_i:%2C{filters.get("km_to") or ""}'
    return code


def row_shipping(row, price, source):
    amount = row.get('shippingAmount')
    field = row.get('shipping', '')
    explicit = (
        (0 if str(amount) in ('0', '0.0', '0.00') else money(amount))
        if amount is not None
        else shipping(field, amount_only=True, allow_from=source == 'vinted')
    )
    if source == 'ebay' and explicit is None and amount is None and not field:
        # New eBay cards no longer expose a dedicated shipping class. Older cached
        # snapshots still carry the label in textContent; exclude title/item price.
        text = row.get('shippingText')
        if text is None:
            text = row.get('text', '')
            for value in (row.get('title', ''), row.get('price', '')):
                if value:
                    text = text.replace(str(value), '', 1)
        explicit = shipping(text)
    # Vinted's higher "inkl." price is buyer protection, never shipping.
    # Only an explicitly labelled/structured shipping field is usable there.
    if source != 'idealo':
        return explicit
    # idealo displays a total per shop rather than a separate shipping price.
    # Subtract only an unambiguous confirmed total from that same offer card.
    totals = {
        money(value)
        for value in re.findall(
            r'(\d[\d.,\s\xa0]*?)\s*€\s*(?:inkl\.?|inklusive)\s+Versand(?:kosten)?\b', row.get('text', ''), re.I
        )
    }
    totals.discard(None)
    if len(totals) == 1:
        total = totals.pop()
        derived = total - price
        return derived if derived >= 0 and (explicit is None or explicit == derived) else None
    return explicit


def ebay_in_germany(row):
    """Ignore explicitly foreign search-card locations, including international recommendations."""
    location = row.get('location', '').strip()
    if location:
        # eBay's location field normally says "aus <country>". Unknown city-only labels stay valid.
        country = re.sub(r'^(?:aus|from|Artikelstandort\s*:)\s*', '', location, flags=re.I).strip()
        if re.search(r'\b(?:Deutschland|Germany)\b', country, re.I):
            return True
        if re.match(r'^(?:aus|from|Artikelstandort\s*:)', location, re.I):
            return False
    # Old cached cards have no separate location field, but concatenate shipping and origin.
    foreign = r'USA|Vereinigte[nr]? Staaten(?: von Amerika)?|United States(?: of America)?|Kanada|Canada|China|Hongkong|Hong Kong|Japan|Großbritannien|United Kingdom|Frankreich|France|Italien|Italy|Spanien|Spain|Polen|Poland|Österreich|Austria|Schweiz|Switzerland|Niederlande|Netherlands|Belgien|Belgium|Australien|Australia'
    if location and re.fullmatch(foreign, location, re.I):
        return False
    origins = re.findall(r'(?:Lieferung|Versand)\s*aus\s+([^\n]+)', row.get('text', ''), re.I)
    return all(re.match(r'(?:Deutschland|Germany)', origin.strip(), re.I) for origin in origins)


MARKUP_SAMPLE, MARKUP_USABLE_SHARE = 5, 0.2  # at least 5 cards, under 20 % usable


def markup_changed(source, rows):
    """Cards were found, but hardly any still carries title, link and price."""
    usable = sum(
        1
        for r in rows
        if clean_title(source, r.get('title', ''))
        and canonical(source, r.get('url', ''))
        and re.search(r'\d', str(r.get('price', '')))
    )
    return len(rows) >= MARKUP_SAMPLE and usable < len(rows) * MARKUP_USABLE_SHARE


MAX_TITLE, MAX_SELLER, MAX_CONDITION = 400, 200, 100
CARS = '216'  # Kleinanzeigen's car section; its cards carry mileage and first registration
PRICE_RANGE = re.compile(r'\b(?:bis|to)\b|\s[-–]\s', re.I)  # "20 bis 30 €": no single price
# Reserved, sold or auction listings never count as a buyable offer; negotiable ("VB") ones only if the search allows them.
UNAVAILABLE = re.compile(
    r'reserviert|verkauft|beendet|sold|nicht lieferbar|nicht verfügbar|ausverkauft|auktion|gebote|\bgesuch\b', re.I
)
NEGOTIABLE = re.compile(r'\bVB\b')
# A DOM shop row without a structured EUR price only counts with a visible delivery promise.
DELIVERABLE = re.compile(
    r'lieferbar|auf lager|lieferzeit|werktage|lieferung\s*:\s*bis\b|lieferung\s+(?:heute|morgen)', re.I
)
EURO_AMOUNT = re.compile(r'(\d(?:[\d.,\s\xa0]*\d)?)')


GIVEN_AWAY = re.compile(r'zu verschenken', re.I)


def row_price(row, search=None):
    """Item price in cents: a structured EUR price, or a DOM price explicitly marked as euros.
    "Zu verschenken" is 0, but only for a search that wants such offers."""
    if search and search.get('free') and GIVEN_AWAY.search(str(row.get('price', ''))):
        return 0
    if row.get('currency') == 'EUR':
        return money(row['price'])
    text = str(row.get('price', ''))
    match = EURO_AMOUNT.search(text) if re.search(r'€|\bEUR\b', text) else None
    return money(match.group(1)) if match else None


def usable(source, row, title, url, search):
    """Whether a card is a single, buyable offer that matches the search."""
    if not title or not url or not title_fits(source, title, search):
        return False
    if source == 'ebay' and not ebay_in_germany(row):
        return False
    price_text, text = str(row.get('price', '')), row.get('text', '').replace('\xad', '')
    if PRICE_RANGE.search(price_text) or UNAVAILABLE.search(price_text + ' ' + text):
        return False
    if not search.get('negotiable') and NEGOTIABLE.search(price_text + ' ' + text):
        return False
    if source == 'kleinanzeigen' and section(source, search) == CARS and not vehicle_fits(text, search):
        return False
    return source != 'idealo' or row.get('currency') == 'EUR' or bool(DELIVERABLE.search(text))


def place_of(source, row):
    """Seller and condition labels; a pickup search also shows where the offer is and how far."""
    seller, condition = row.get('seller', '').strip(), row.get('condition', '').strip()
    text = ' '.join(row.get('text', '').split())
    if source == 'kleinanzeigen' and not seller:
        # The card starts with "<PLZ> <Ort>(<n> km)" once a search runs around a postcode.
        found = re.search(r'(\d{5}) ([^\d(]+?)\s*\((\d+) km\)', text)
        if found:
            seller = f'{found.group(2).strip()} · {found.group(3)} km'
    if source == 'ebay':
        found = re.search(r'(\d+(?:,\d+)?) km von \d{5}', text)
        if found:
            condition = ' · '.join(filter(None, (condition, f'{found.group(1)} km entfernt')))
    return seller, condition


def parse_rows(source, data, search):
    """Offers from extracted cards, one per listing (per shop offer on idealo), the cheapest kept."""
    result: dict[object, Offer] = {}
    for row in data:
        title = clean_title(source, row.get('title', ''))
        url = canonical(source, row.get('url', ''))
        price = row_price(row, search) if usable(source, row, title, url, search) else None
        if price is None:
            continue
        seller, condition = place_of(source, row)
        offer = Offer(
            source,
            url,
            title[:MAX_TITLE],
            price,
            row_shipping(row, price, source),
            seller[:MAX_SELLER],
            condition[:MAX_CONDITION],
            str(row.get('owner', '')),
        )
        # Keep idealo's shop alternatives: the shared cache serves searches with both shipping settings.
        key = (url, price, offer.shipping, offer.seller) if source == 'idealo' else url
        total = offer.price + (offer.shipping or 0)
        if key not in result or total < result[key].price + (result[key].shipping or 0):
            result[key] = offer
    return list(result.values())


def _readable(source, data, follow):
    if source == 'vinted' and follow:
        return data.get('vintedDetail') and data.get('vintedShipping')  # a detail page is read for its shipping
    return data['rows'] or data['structured'] or data['links'] or EMPTY.search(data['text'])


def _keep_unready(source, data, follow):
    return source == 'vinted' and follow and data.get('vintedDetail')  # an item without a shipping line: unknown


async def load_page(page, source, url, follow=False):
    """Extracted content of one provider page: from the page cache, or one booked visit.

    FOLLOW marks a follow-up page of the same visit (idealo product, Vinted item).
    """
    return await browser.load_page(
        page,
        source,
        url,
        follow,
        extract=EXTRACT,
        readable=lambda data, follow: _readable(source, data, follow),
        keep_unready=lambda data, follow: _keep_unready(source, data, follow),
    )


LOCATION_URL = 'https://www.kleinanzeigen.de/s-ort-empfehlungen.json?'


async def find_location(page, postcode):
    """Kleinanzeigen's location ID for a postcode: one page visit within the normal budget, then stored for good."""
    try:
        cadence.book('kleinanzeigen', 'page')
    except cadence.Deferred as exc:
        raise cadence.Deferred(exc.until, continuation=True) from None
    response = await page.goto(LOCATION_URL + urlencode({'query': postcode}), timeout=browser.PAGE_TIMEOUT_MS)
    check_response(response)
    try:
        places = json.loads(await response.text())  # Firefox shows JSON in its viewer, not as body text
    except ValueError:
        raise Unreadable('Ortssuche von Kleinanzeigen nicht lesbar. Später erneut versuchen.') from None
    # {"_0": "Deutschland", "_9668": "10115 Mitte", …}: the first place with exactly this postcode.
    found = next(
        (
            key[1:]
            for key, label in places.items()
            if key[1:].isdigit() and key != '_0' and str(label).startswith(postcode + ' ')
        ),
        None,
    )
    if found is None:
        raise RuntimeError(f'PLZ {postcode} bei Kleinanzeigen nicht gefunden.')
    cadence.save_location('kleinanzeigen', postcode, found)
    return found


def idealo_product(data, search):
    """The first matching product page with a valid idealo address; its offer list holds the shops."""
    return next(
        (
            item
            for item in data['links']
            if title_fits('idealo', item['title'], search) and canonical('idealo', item['url'])
        ),
        None,
    )


def _references(pages, search):
    """idealo's "ab" prices of matching products: a new-price yardstick, never an offer."""
    return [
        r
        for page in pages
        for r in page['structured']
        if r.get('reference') and r.get('currency') == 'EUR' and title_fits('idealo', r['title'], search)
    ]


def _cheapest_reference(rows):
    priced = [(money(r['price']), r) for r in rows if money(r['price'])]
    if not priced:
        return None
    value, row = min(priced, key=lambda pair: pair[0])
    return {'price': value, 'title': row['title'], 'url': canonical('idealo', row['url']), 'shipping_included': False}


def _cheapest_total(offers):
    """The cheapest shop offer with known shipping, as a total: what buying it really costs."""
    known = [o for o in offers if o.shipping is not None]
    if not known:
        return None
    best = min(known, key=lambda o: o.price + o.shipping)
    return {'price': best.price + best.shipping, 'title': best.title, 'url': best.url, 'shipping_included': True}


def idealo_result(data, detail, product, search):
    """Shop offers and the new-price reference from the search page and the product's offer list."""
    pages = [data] if detail is None else [data, detail]
    actual = [dict(r) for page in pages for r in page['structured'] if not r.get('reference')]
    if detail is not None:
        # Offer cards of the product page carry no own link or title; they belong to the product.
        # Only the enclosing card has delivery and total; nested price blocks have no title.
        image = detail.get('image') or product.get('image')
        actual += [
            dict(row, url=product['url'], title=product['title'], image=image or row.get('image'))
            for row in detail['rows']
            if row.get('title')
        ]
    offers = parse_rows('idealo', actual, search) if actual else []
    reference = _cheapest_total(offers) or _cheapest_reference(_references(pages, search))
    if not reference and not actual and not EMPTY.search(data['text']):
        # The page itself was readable (load_page checked): an unmatched search term must not pause idealo for every search.
        raise RuntimeError('idealo: keine eindeutig passende Variante gefunden. Suchbegriff präziser fassen.')
    images.remember('idealo', pictures('idealo', offers, actual))
    return offers, reference


def repair_cached(source, search, value, observed_at):
    if (search.get('delivery') == 'beides' and around(source, search)) or len(phrases(source, search)) > 1:
        return None  # several pages per result; not worth reassembling from the page cache
    if source not in ('idealo', 'ebay', 'vinted'):
        return value
    try:
        data = cadence.historical_page(source, search_url(source, wanted_words(source, search), search), observed_at)
    except LookupError:
        return None
    if data is None:
        return None
    if source != 'idealo':
        offers = (
            delivery.enrich(parse_rows(source, data['rows'], search))
            if source == 'vinted'
            else parse_rows(source, data['rows'], search)
        )
        return {'offers': [asdict(o) for o in offers], 'reference': value.get('reference')}
    product = idealo_product(data, search)
    if product is None:
        return None
    detail = cadence.historical_page(source, product['url'], observed_at)
    if detail is None:
        return None
    offers, reference = idealo_result(data, detail, product, search)
    return {'offers': [asdict(o) for o in offers], 'reference': reference}


@cadence.shared_search(cache_version=5, cached_parser=repair_cached)
async def fetch(source, search):
    """(offers, idealo reference) of one provider query; the decorator shares, caches and times it."""
    return await _visit(source, search)


async def _visit(source, search):
    cadence.note(stage='Browser verbinden')
    async with browser_page(source) as page:
        place = around(source, search)
        if source == 'kleinanzeigen' and place and cadence.location('kleinanzeigen', place[0]) is None:
            await find_location(page, place[0])
        if source == 'idealo':
            data = await load_page(page, source, search_url(source, wanted_words(source, search), search))
            product = idealo_product(data, search)
            detail = await load_page(page, source, product['url'], follow=True) if product else None
            return idealo_result(data, detail, product, search)
        rows = []
        for number, phrase in enumerate(phrases(source, search)):
            # Every further spelling is a page of its own and waits for its slot in the budget.
            rows += await _phrase_rows(page, source, search, phrase, follow=number > 0)
        offers = parse_rows(source, _unique(rows), search)
        if source == 'vinted':
            offers = delivery.enrich(offers)
        images.remember(source, pictures(source, offers, rows))
        return offers, None  # the new-price reference comes from idealo only


async def _phrase_rows(page, source, search, phrase, follow):
    """The offer cards for one spelling of the search."""
    data = await load_page(page, source, search_url(source, wanted_words(source, search, phrase), search), follow)
    rows = listing_rows(source, data)
    if search.get('delivery') == 'beides' and around(source, search):
        # The area around the postcode is read above; from all of Germany only what can be shipped.
        nationwide = dict(search, delivery='versand')
        wide = await load_page(
            page, source, search_url(source, wanted_words(source, nationwide, phrase), nationwide), follow=True
        )
        rows += [row for row in listing_rows(source, wide) if shippable(source, row)]
    return rows


def _unique(rows):
    """Each card once, where the area, all of Germany or two spellings list the same offer."""
    seen, unique = set(), []
    for row in rows:
        if row.get('url') not in seen:
            seen.add(row.get('url'))
            unique.append(row)
    return unique


def pictures(source, offers, rows):
    """Offer URL -> picture of its card, for the offers that came out of ROWS."""
    wanted = {o.url for o in offers}
    found = {}
    for row in rows:
        url = canonical(source, row.get('url', ''))
        if url in wanted and row.get('image'):
            found[url] = row['image']
    return found


def listing_rows(source, data):
    """The offer cards of a result page; an unreadable or silently changed page is an error, never 0 hits."""
    if not data['rows'] and not EMPTY.search(data['text']):
        raise Unreadable('Angebotsliste nicht lesbar. Anbieteraufbau oder Anmeldung prüfen.')
    if markup_changed(source, data['rows']):
        raise RuntimeError('Seitenaufbau des Anbieters geändert: Angebotskarten ohne Titel, Link oder Preis.')
    return list(data['rows'])


def shippable(source, row):
    """Whether a card from the nationwide search can be sent: Kleinanzeigen says "Versand möglich"
    (or names a cost); on eBay everything except "Nur Abholung"."""
    text = ' '.join(row.get('text', '').split())
    if source == 'kleinanzeigen':
        return 'Versand möglich' in text or bool(row.get('shipping', '').strip())
    return not re.search(r'\bnur abholung\b', text, re.I)


def browser_page(source):
    """A fresh, guarded page in the remote browser that may only navigate to SOURCE's own hosts."""
    return browser.browser_page(HOSTS[source])


async def fetch_shipping(url):
    """One Vinted detail visit, using the same browser rules and page budget as searches."""
    async with browser_page('vinted') as page:
        data = await load_page(page, 'vinted', url, follow=True)
    return shipping(data.get('vintedShipping', ''), allow_from=True, amount_only=True)
