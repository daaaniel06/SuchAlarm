"""Flat searches offline: parsers, filter rules, place checks, storing results and detail pages."""

import asyncio
import json
import os
import re
import tempfile
import time
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from suchalarm import status, store, worker
from suchalarm.core import browser, cadence
from suchalarm.core.browser import Unreadable
from suchalarm.wohnung import catalog, details, domain, outbox, pages, portals, sources
from suchalarm.wohnung import worker as flats
from suchalarm.wohnung.domain import Listing, match, profile

PROFILE = {
    'city': 'Berlin',
    'place': None,
    'radius': 0,
    'maxRent': 1300,
    'minArea': 50,
    'minRooms': 2,
    'exclusions': ['WBS'],
    'excludeWords': [],
    'moveInFrom': None,
    'moveInBy': None,
}
LISTING = Listing(
    'immoscout24',
    'https://www.immobilienscout24.de/expose/123456789',
    'Wohnung',
    place='10115 Berlin',
    rent=1200,
    cold_rent=950,
    area=60,
    rooms=2,
    exclusions_known=True,
)


def flat(**changes):
    return Listing(**{**LISTING.__dict__, **changes})


def search(**changes):
    values = dict(
        id=1,
        kind='wohnung',
        query='Darmstadt',
        max_price=150000,
        excluded='',
        radius=0,
        sources=['immoscout24'],
        filters={'min_area': 0, 'min_rooms': 1, 'exclusions': []},
        active=1,
    )
    return {**values, **changes}


class TextTests(unittest.TestCase):
    def test_trait_detection(self):
        for text, expected in (
            ('Schöne Wohnung, kein WBS erforderlich, unbefristet', []),
            ('Nur mit WBS! Befristet bis 2027', ['WBS', 'Befristet']),
            ('TAUSCHWOHNUNG Weinbergstraße', ['Tauschwohnung']),
            ('Zimmer in netter WG, Mitbewohnerin gesucht', ['WG-Zimmer']),
            ('Zwischenmiete Dezember bis März', ['Befristet']),
            ('Wohnberechtigungsschein ist nicht erforderlich', []),
        ):
            self.assertEqual(domain.detect_traits(text), expected, text)

    def test_german_numbers(self):
        for text, value in (('1.234,50 €', 1234.5), ('55,36 m²', 55.36), ('950 € VB', 950), ('2 Zi.', 2), ('', None)):
            self.assertEqual(domain.number(text), value)

    def test_available_from(self):
        for text, iso in (
            ('15.10.2026', '2026-10-15'),
            ('01.12.26', '2026-12-01'),
            ('September 2026', '2026-09-01'),
            ('10/2026', '2026-10-01'),
            ('nach Vereinbarung', None),
            ('', None),
        ):
            self.assertEqual(domain.available_from(text), iso)
        self.assertEqual(domain.available_from('ab sofort'), date.today().isoformat())

    def test_publication_age_from_the_result_list(self):
        now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
        self.assertEqual(domain.published_at('vor 26 Minuten', now), '2026-09-23T11:34:00+00:00')
        self.assertEqual(domain.published_at('vor einem Tag', now), '2026-09-22T12:00:00+00:00')
        self.assertEqual(domain.published_at('gerade eben', now), '2026-09-23T12:00:00+00:00')
        self.assertIsNone(domain.published_at('Montag', now))

    def test_wanted_ads_are_recognised_by_title(self):
        for title, wanted in (
            ('Suche 2-Zimmer-Wohnung in Potsdam', True),
            ('Junges Paar sucht Wohnung ab Dezember', True),
            ('1-2 Zimmer Wohnung in Schöneberg gesucht', True),
            ('Nachmieter gesucht für 2-Zimmer-Wohnung', False),
            ('Neue Mieter gesucht: 3 Zimmer Altbau', False),
            ('TAUSCHWOHNUNG Suche 3-Zimmer-Wohnung in Potsdam zum Tausch', False),
            ('Helle 2-Zimmer Wohnung mit Balkon', False),
        ):
            self.assertIs(domain.is_wanted_ad(title), wanted, title)


class FilterTests(unittest.TestCase):
    def test_filter_boundaries(self):
        for changes, expected in (
            ({}, 'matched'),
            ({'rent': None}, 'matched'),  # 950 cold + 60 m² × 3 € ≈ 1130 warm
            ({'rent': None, 'cold_rent': 1200}, 'excluded'),  # ≈ 1380 warm
            ({'rent': None, 'cold_rent': None}, 'unclear'),
            ({'rent': 1301}, 'excluded'),
            ({'rent': 1300}, 'matched'),
            ({'area': 49.9}, 'excluded'),
            ({'area': None}, 'unclear'),
            ({'traits': ['WBS']}, 'excluded'),
            ({'traits': ['Befristet']}, 'matched'),
            ({'exclusions_known': False}, 'unclear'),
            ({'title': 'Paar sucht Wohnung'}, 'excluded'),
        ):
            self.assertEqual(match(flat(**changes), PROFILE)[0], expected, changes)

    def test_known_violation_beats_missing_data(self):
        self.assertEqual(match(flat(rent=None, area=None, traits=['WBS']), PROFILE)[0], 'excluded')

    def test_estimated_rent_from_cold_rent(self):
        self.assertEqual(domain.estimated_rent(flat(rent=None)), 1130)
        self.assertEqual(domain.estimated_rent(flat(rent=None, area=None)), 1188)  # +25 %
        self.assertIsNone(domain.estimated_rent(LISTING))  # a stated warm rent is never replaced
        self.assertEqual(
            match(flat(rent=None, cold_rent=1200), PROFILE), ('excluded', ['Warmmiete (geschätzt) zu hoch'])
        )

    def test_only_listings_that_can_still_fit_are_worth_a_detail_page(self):
        could = domain.could_match
        self.assertTrue(could(flat(rent=None, cold_rent=1200), PROFILE))  # the estimate may be too high
        self.assertTrue(could(flat(rent=None, cold_rent=None, area=None), PROFILE))
        self.assertFalse(could(flat(rent=None, cold_rent=1400), PROFILE))  # warm is only higher
        self.assertFalse(could(flat(rent=1400), PROFILE))
        self.assertFalse(could(flat(area=40), PROFILE))
        self.assertFalse(could(flat(traits=['WBS']), PROFILE))
        self.assertFalse(could(flat(title='Paar sucht Wohnung'), PROFILE))
        self.assertFalse(could(flat(title='Möbliert auf Zeit'), {**PROFILE, 'excludeWords': ['möbliert']}))
        self.assertFalse(could(flat(available_from='2027-03-01'), {**PROFILE, 'moveInBy': '2026-12-31'}))

    def test_own_exclusion_words_check_title_and_description(self):
        words = {**PROFILE, 'excludeWords': ['möbliert', 'Wohnen auf Zeit']}
        self.assertEqual(match(flat(text='Schöne Wohnung, voll MÖBLIERT'), words), ('excluded', ['Enthält „möbliert“']))
        self.assertEqual(match(flat(title='Wohnen auf Zeit in Bessungen'), words)[0], 'excluded')
        self.assertEqual(match(flat(exclusions_known=False), words)[0], 'unclear')

    def test_move_in_window(self):
        window = {**PROFILE, 'moveInFrom': '2026-10-01', 'moveInBy': '2026-11-15'}
        for available, expected in (
            (None, 'matched'),
            ('2026-10-01', 'matched'),
            ('2026-12-01', 'excluded'),
            ('2026-09-01', 'excluded'),
        ):
            self.assertEqual(match(flat(available_from=available), window)[0], expected)

    def test_town_only_search_drops_neighbouring_towns(self):
        town = {**PROFILE, 'city': 'Darmstadt', 'exclusions': [], 'minArea': 0, 'minRooms': 1}
        self.assertEqual(match(flat(place='64347 Griesheim'), town), ('excluded', ['Nicht in Darmstadt']))
        self.assertEqual(match(flat(place='64285 Darmstadt-Bessungen, Darmstadt'), town)[0], 'matched')
        self.assertEqual(match(flat(place='64347 Griesheim'), {**town, 'radius': 7})[0], 'matched')
        postcode = {**town, 'place': {'kind': 'plz', 'label': '64283 Darmstadt', 'query': '64283'}}
        self.assertEqual(match(flat(place='64285 Darmstadt'), postcode), ('excluded', ['Nicht in 64283']))

    def test_distance_search_only_drops_flats_clearly_outside(self):
        centre = (49.8728, 8.6512)  # Darmstadt
        near = {**PROFILE, 'city': 'Darmstadt', 'radius': 5, 'exclusions': [], 'minArea': 0, 'minRooms': 1}
        far = flat(place='Darmstadt', lat=49.80, lon=8.60)  # about 8,9 km
        self.assertEqual(match(far, near, centre)[0], 'excluded')
        self.assertEqual(match(flat(place='Darmstadt', lat=49.817, lon=8.645), near, centre)[0], 'matched')  # 6,2 km
        self.assertEqual(match(flat(place='Darmstadt'), near, centre)[0], 'matched')  # position unknown: kept
        self.assertEqual(match(far, near, None)[0], 'matched')  # centre not looked up yet: kept

    def test_a_stored_search_becomes_a_profile(self):
        stored = search(
            max_price=123450,
            excluded='möbliert, Wohnen  auf Zeit,',
            radius=5,
            filters={'min_area': 40, 'min_rooms': 2, 'exclusions': ['WBS', 'Unbekannt'], 'move_in_by': '2026-12-01'},
        )
        p = profile(stored)
        self.assertEqual((p['maxRent'], p['minArea'], p['minRooms'], p['radius']), (1234.5, 40, 2, 5))
        self.assertEqual(
            (p['exclusions'], p['excludeWords'], p['moveInBy']),
            (['WBS'], ['möbliert', 'Wohnen auf Zeit'], '2026-12-01'),
        )

    def test_query_text_is_shared_where_the_portal_ignores_the_filters(self):
        cheap, dear = search(max_price=80000), search(max_price=150000)
        self.assertNotEqual(domain.query_text('immoscout24', cheap), domain.query_text('immoscout24', dear))
        self.assertNotEqual(domain.query_text('wg-gesucht', cheap), domain.query_text('wg-gesucht', dear))
        self.assertNotEqual(domain.query_text('immowelt', cheap), domain.query_text('immowelt', dear))
        self.assertEqual(domain.query_text('nhw', cheap), domain.query_text('nhw', search(query='Wiesbaden')))
        no_wbs = search(filters={'min_area': 0, 'min_rooms': 1, 'exclusions': ['WBS']})
        self.assertNotEqual(domain.query_text('immoscout24', no_wbs), domain.query_text('immoscout24', search()))
        self.assertEqual(domain.query_text('wg-gesucht', no_wbs), domain.query_text('wg-gesucht', search()))

    def test_detail_page_completes_a_card(self):
        card = flat(rent=None, traits=['Tauschwohnung'], exclusions_known=False)
        done = card.with_detail({'rent': 1250.0, 'area': None, 'traits': ['WBS'], 'text': 'Nur mit WBS'})
        self.assertEqual(
            (done.rent, done.area, done.traits, done.exclusions_known), (1250.0, 60, ['Tauschwohnung', 'WBS'], True)
        )


KLEINANZEIGEN_PAGE = """<main id="srchrslt-content"><ul>
<article class="aditem" data-adid="3515855658" data-href="/s-anzeige/helle-2-zimmer/3515855658-203-7965"><div><script type="application/ld+json">{"title":"x 999 €"}</script><img src="https://img.kleinanzeigen.de/api/v1/prod-ads/images/2a/abc?rule=$_2.AUTO" alt="">
<div><span>14480 Potsdam</span><span>(6 km)</span></div><h3><a href="/s-anzeige/helle-2-zimmer/3515855658-203-7965">Helle 2-Zimmer &amp; Balkon</a></h3>
<p>Schön</p><p class="price">1.050 €</p><span>62,5 m²</span><span>2 Zi.</span></div></article>
<article class="aditem" data-adid="3520717913" data-href="/s-anzeige/tauschwohnung/3520717913-203-7963"><h3><a>TAUSCHWOHNUNG Mitte</a></h3><p>700 € VB</p></article>
<article class="aditem" data-adid="3520000001" data-href="/s-anzeige/paar-sucht/3520000001-203-7963"><h3><a>Junges Paar sucht 2-Zimmer-Wohnung</a></h3><p>900 €</p></article>
</ul></main>"""

KLEINANZEIGEN_DETAIL = """<meta property="og:title" content="Helle 2-Zimmer">
<h2 class="boxedarticle--price" id="viewad-price">1.050 €</h2>
<li class="addetailslist--detail">Wohnfläche<span class="addetailslist--detail--value" >62,5 m²</span></li>
<li class="addetailslist--detail">Zimmer<span class="addetailslist--detail--value" >2</span></li>
<li class="addetailslist--detail">Warmmiete<span class="addetailslist--detail--value" >1.290 €</span></li>
<li class="addetailslist--detail">Verfügbar ab<span class="addetailslist--detail--value" >November 2026</span></li>
<p id="viewad-description-text" itemprop="description">Ruhige Lage.<br/>Nur mit WBS.</p>"""

# Layout of 10/2026, shortened from a real result page.
WG_LIST = """<div class="col-sm-6"><a
    id="liste-details-ad-14081263"
    class="wgg-list-card offer_list_item  "
    data-id="14081263"
    href="/wohnungen-in-Darmstadt-Eberstadt.14081263.html"
    aria-label="Schöne 82 m2 Wohnung"
            >
    <div class="top-image-card-wrapper"><img class="top-image-card img-responsive " src="https://img.wg-gesucht.de/media/up/a.small.jpg" alt=""></div>
    <div class="card-content "><div class="horizontal-align-space-between vertical-align-center noprint price-row"><div>
        <span class="card-main-text">1300 &euro;</span><span class="card-subtitle">pro Monat</span></div></div>
        <h2 class="truncate_title noprint"><span class="card-main-text ad-title">Schöne 82 m2 Wohnung</span></h2>
        <div class="card-subtitle ad-subtitle noprint" title="3-Zimmer-Wohnung  · Darmstadt Eberstadt · Odenwaldstr. 46 B"><span>3-Zimmer-Wohnung</span></div>
        <div class="card-subtitle vertical-align-center noprint middle-row"><div><span class="size-value">82 m&sup2;</span></div>
            <div> 01.10.2026 </div><div class="text-right"></div></div>
        <div class="vertical-align-center noprint bottom-card"><span class="card-subtitle">11 Std.</span></div></div>
</a><div class="printonly"><a class="detailansicht" href="/wohnungen-in-Darmstadt-Eberstadt.14081263.html"><b>Schöne 82 m2 Wohnung</b></a></div></div>
<div class="col-sm-6"><a id="liste-details-ad-8832976" class="wgg-list-card offer_list_item  " data-id="8832976" href="/wohnungen-in-Darmstadt-Darmstadt.8832976.html" aria-label="Möbliert auf Zeit">
    <div class="card-content "><div class="price-row"><div><span class="card-main-text">800 &euro;</span></div></div>
        <div class="card-subtitle ad-subtitle noprint" title="1-Zimmer-Wohnung  · Darmstadt Darmstadt · Seeheimer Straße"><span></span></div>
        <div class="card-subtitle vertical-align-center noprint middle-row"><div><span class="size-value">30 m&sup2;</span></div>
            <div> 01.10.2026 - 31.03.2027 </div></div><div class="bottom-card"></div></div>
</a></div>"""

WG_DETAIL = """<h1><span>Schöne 82 m2 Wohnung</span></h1>
<span class="section_panel_detail"> Miete: </span></div><div><span class="section_panel_value"> 1100&euro; </span>
<span class="section_panel_detail"> Nebenkosten: </span></div><div><span class="section_panel_value"> 200&euro; </span>
<span class="section_panel_detail"> Sonstige Kosten: </span></div><div><span class="section_panel_value"> n.a. </span>
<span class="section_panel_detail"> Odenwaldstr. 46 B <br> 64297 Darmstadt Eberstadt </div> <h2> Verfügbarkeit </h2> frei ab:</span></div><div><span class="section_panel_value"> 01.10.2026 </span>
<div id="ad_description_text"><div id="freitext_0"><script>ads()</script> Ruhig, viel Grün. Nur mit WBS.</div></div><div class="panel">"""

BAUVEREIN_MARKERS = {
    'objekte': [
        {'anbieterId': 1, 'lat': '49.8686', 'lon': '8.6566', 'vermarktungsart': 'MIETE_PACHT', 'nutzungsart': 'WOHNEN',
         'adresse': 'Karlstraße 22, 64283 Darmstadt', 'regionaler_zusatz': 'Kapellplatzviertel', 'id': 20068820,
         'verfuegbar_ab': '16.10.2026', 'wohnflaeche': 65.02, 'objekttitel': 'Charmante 2-Zimmer-Wohnung',
         'anzahl_zimmer': 2, 'gesamtpreis': 905, 'titelbild': {'pfad': '325-103-M-1.jpg'}},
        {'anbieterId': 1, 'lat': '49.8234', 'lon': '8.6979', 'vermarktungsart': 'MIETE_PACHT', 'nutzungsart': 'WOHNEN',
         'adresse': 'Bruchmühlenweg 8, 64367 Mühltal', 'id': 20078298, 'verfuegbar_ab': 'sofort', 'wohnflaeche': 77.82,
         'objekttitel': '3-Zimmer in Mühltal', 'anzahl_zimmer': 3, 'gesamtpreis': 1075, 'titelbild': {}},
        {'anbieterId': 1, 'lat': '49.80', 'lon': '8.64', 'vermarktungsart': 'MIETE_PACHT', 'nutzungsart': 'PARKEN',
         'adresse': 'Kirnbergerstraße 3, 64297 Darmstadt', 'id': 17913150, 'objekttitel': 'Garagenbox', 'gesamtpreis': 48},
    ]
}  # fmt: skip


def nhw_card(uid, identifier, title, location, facts):
    spans = ''.join(f'<span data-filtertype="{key}" data-filtervalue="{value}"></span>' for key, value in facts.items())
    return (
        f'<div class="immo--item" data-itemuid="{uid}"><img class="immo--item--image" alt="" src="/fileadmin/_processed_/{uid}.jpg" />'
        f'<span class="h4 immo--item--title cd-primary"><a href="/zuhause-finden/immobilie/{identifier}?cHash=abc">{title}</a></span>'
        f'<em class="immo--item--location small">{location}</em><div class="immo--filterdata">{spans}</div></div>'
    )


NHW_PAGE = (
    "<script>var tmplocation = { uid: 18903, title: 'x', position: { lat: parseFloat('49.8640'), lng: parseFloat('8.6490') }, };</script>"
    '<div class="immo--list">'
    + nhw_card(18903, '5000-S2133-001-102', 'Darmstadt: 2-Zimmer-Mietwohnung', 'Darmstadt, Eichbergstraße 12',
               {'anzahl_zimmer': '2', 'wohnflaeche': '63.06', 'warmmiete': '1451.90', 'nettokaltmiete': '1145.00', 'wbs': '0'})
    + nhw_card(19006, '5000-S3678-002-023', 'Single-Apartment sucht Mieter', 'Wiesbaden, Rudolf-Dyckerhoff-Straße 21',
               {'anzahl_zimmer': '1', 'wohnflaeche': '38.5', 'warmmiete': '610.00', 'nettokaltmiete': '450.00', 'wbs': '1'})
    + '</div>'
)  # fmt: skip


WG_FRANKFURT = {
    'total_items': '2',
    '_embedded': {
        'cities': [
            {'city_id': '41', 'city_name': 'Frankfurt am Main', 'city_and_state': 'Frankfurt am Main (Hessen)'},
            {'city_id': '40', 'city_name': 'Frankfurt (Oder)', 'city_and_state': 'Frankfurt (Oder) (Brandenburg)'},
        ]
    },
}


class WgCityTests(unittest.TestCase):
    def test_the_place_field_gets_the_bare_town(self):
        self.assertEqual(portals.wg_town('64283 Darmstadt, Hessen'), 'Darmstadt')
        self.assertEqual(portals.wg_town('Frankfurt (Oder)'), 'Frankfurt')
        self.assertEqual(
            portals.wg_cities_url('Mörfelden-Walldorf'),
            'https://www.wg-gesucht.de/api/location/cities/names/M%C3%B6rfelden-Walldorf',
        )

    def test_the_town_of_exactly_that_name_wins_over_the_first_suggestion(self):
        self.assertEqual(portals.wg_city(WG_FRANKFURT, 'Frankfurt am Main'), ('Frankfurt-am-Main', 41))
        self.assertEqual(portals.wg_city(WG_FRANKFURT, 'Frankfurt (Oder)'), ('Frankfurt-Oder', 40))
        self.assertEqual(portals.wg_city(WG_FRANKFURT, 'Frankfurt'), ('Frankfurt-am-Main', 41))  # the first

    def test_no_suggestion_is_no_town(self):
        self.assertIsNone(portals.wg_city({'_embedded': {'cities': []}}, 'Xyz'))

    def test_the_slug_is_written_like_the_site_does(self):
        self.assertEqual(portals.wg_slug('Mörfelden-Walldorf'), 'Moerfelden-Walldorf')
        self.assertEqual(portals.wg_slug('Groß-Umstadt'), 'Gross-Umstadt')
        self.assertEqual(portals.wg_slug('Frankfurt (Oder)'), 'Frankfurt-Oder')


class ParserTests(unittest.TestCase):
    def test_kleinanzeigen_search_page_and_detail(self):
        cards = portals.kleinanzeigen_parse(KLEINANZEIGEN_PAGE)
        self.assertEqual(
            [c['url'] for c in cards],
            [
                'https://www.kleinanzeigen.de/s-anzeige/helle-2-zimmer/3515855658-203-7965',
                'https://www.kleinanzeigen.de/s-anzeige/tauschwohnung/3520717913-203-7963',
            ],
        )
        first = cards[0]
        self.assertEqual(
            (first['title'], first['place'], first['coldRent'], first['area'], first['rooms']),
            ('Helle 2-Zimmer & Balkon', '14480 Potsdam', 1050, 62.5, 2),
        )
        self.assertEqual(cards[1]['traits'], ['Tauschwohnung'])
        self.assertTrue(first['imageSource'].endswith('rule=$_72.AUTO'))
        with self.assertRaises(Unreadable):
            portals.kleinanzeigen_parse('<html>Bitte bestätigen Sie, dass Sie kein Roboter sind</html>')
        self.assertEqual(portals.kleinanzeigen_parse('<main id="srchrslt-content">Leider keine Ergebnisse</main>'), [])
        detail = portals.kleinanzeigen_detail(KLEINANZEIGEN_DETAIL)
        self.assertEqual(
            (detail['rent'], detail['coldRent'], detail['traits'], detail['availableFrom']),
            (1290, 1050, ['WBS'], '2026-11-01'),
        )

    def test_kleinanzeigen_search_url(self):
        swaps = {**PROFILE, 'radius': 10, 'minArea': 50, 'minRooms': 2.5, 'exclusions': ['Tauschwohnung']}
        self.assertEqual(
            portals.kleinanzeigen_url(swaps, '3331'),
            'https://www.kleinanzeigen.de/s-wohnung-mieten/anzeige:angebote/sortierung:neuste/preis::1300/'
            'c203l3331r10+wohnung_mieten.qm_d:50%2C+wohnung_mieten.zimmer_d:2.5%2C?pageSize=100',
        )  # no swap_s filter: it would drop every ad that leaves the field empty

    def test_immoscout_search_and_detail(self):
        data = {
            'resultListItems': [
                {
                    'type': 'EXPOSE_RESULT',
                    'item': {
                        'id': '171041588',
                        'title': 'Helle Wohnung',
                        'address': {'line': 'Gerlachstraße 41, 14480 Potsdam'},
                        'pictures': [
                            {'urlScaleAndCrop': 'https://pictures.immobilienscout24.de/x.jpg/%WIDTH%x%HEIGHT%/q'}
                        ],
                        'attributes': [{'value': '748 €'}, {'value': '55,36 m²'}, {'value': '2 Zi.'}],
                    },
                },
                {'type': 'ADVERTISEMENT', 'item': {}},
            ]
        }
        card = portals.immoscout_parse(data)[0]
        self.assertEqual((card['coldRent'], card['area'], card['rooms']), (748, 55.36, 2))
        self.assertEqual(card['imageSource'], 'https://pictures.immobilienscout24.de/x.jpg/480x360/q')
        excluded = {**PROFILE, 'radius': 10, 'exclusions': ['WBS', 'Tauschwohnung']}
        url = portals.immoscout_url(excluded, '/de/x', (52.5, 13.4))
        self.assertIn('haspromotion=false', url)
        self.assertNotIn('exclusioncriteria', url)  # ignored by the portal
        self.assertNotIn('haspromotion', portals.immoscout_url({**PROFILE, 'exclusions': []}, '/de/x', None))
        self.assertIn('geocoordinates=52.50000%3B13.40000%3B10.0', url)
        detail = portals.immoscout_detail(
            {
                'sections': [
                    {'type': 'TOP_ATTRIBUTES', 'attributes': [{'label': 'Kaltmiete', 'text': '747,36 €'}, {'label': 'Warmmiete', 'text': '902,92 €'}]},
                    {'type': 'ATTRIBUTE_LIST', 'attributes': [{'label': 'Bezugsfrei ab:', 'text': '15.10.2026'}]},
                    {'type': 'TEXT_AREA', 'title': 'Sonstiges', 'text': 'Mietvertrag befristet auf 2 Jahre'},
                ]
            }
        )  # fmt: skip
        self.assertEqual(
            (detail['rent'], detail['traits'], detail['availableFrom']), (902.92, ['Befristet'], '2026-10-15')
        )
        parts = lambda *pairs: portals.immoscout_detail(  # noqa: E731
            {'sections': [{'attributes': [{'label': k, 'text': v} for k, v in pairs]}]}
        )
        cold, costs = ('Kaltmiete', '700 €'), ('Nebenkosten', '+150 €')
        self.assertEqual(parts(cold, costs, ('Heizkosten', '+80 €'))['rent'], 930)
        self.assertEqual(parts(cold, costs, ('Heizkosten', 'in Nebenkosten enthalten'))['rent'], 850)
        self.assertIsNone(parts(cold, costs, ('Heizkosten', 'keine Angabe'))['rent'])  # estimated instead
        self.assertIsNone(parts(cold)['rent'])
        with self.assertRaises(Unreadable):
            portals.immoscout_parse({'error': 'x'})

    def test_immowelt_cards(self):
        # Offer contents as Immowelt's list answers them (29.10.2026), with made-up texts.
        offers = json.loads((Path(__file__).parent / 'fixtures' / 'immowelt' / 'list.json').read_text())
        new_build, ground_floor, room = portals.immowelt_cards(offers)  # a project page is no flat
        self.assertEqual(new_build['url'], 'https://www.immowelt.de/expose/11111111-2222-3333-4444-555555555555')
        self.assertEqual(
            (new_build['title'], new_build['place'], new_build['availableFrom']),
            ('Neubau mit Gemeinschaftsgarten', '64285 Darmstadt', '2028-06-01'),
        )
        self.assertEqual(
            (new_build['coldRent'], new_build['rent'], new_build['area'], new_build['rooms']), (335, None, 46, 2)
        )
        self.assertEqual(new_build['traits'], ['WBS'])
        self.assertEqual(new_build['imageSource'], 'https://mms.immowelt.de/b/b/b/b/front.jpg?w=480&h=360')  # no logo
        # The labelled prices count, not rawData.price (here the warm rent).
        self.assertEqual((ground_floor['coldRent'], ground_floor['rent'], ground_floor['area']), (795, 1106, 64.45))
        self.assertEqual(ground_floor['place'], 'Musterweg 15, 64291 Darmstadt, Wixhausen')
        self.assertEqual(
            (room['traits'], room['title'], room['imageSource']), (['WG-Zimmer'], 'Zimmer in der Innenstadt', None)
        )
        self.assertEqual(portals.immowelt_cards([]), [])
        with self.assertRaises(Unreadable):
            portals.immowelt_cards({'error': 'x'})

    def test_immowelt_search_is_newest_first_within_the_limits(self):
        path = portals.immowelt_location(
            'https://www.immowelt.de/suche/mieten/wohnung/hessen/darmstadt-64283/ad08de2508', 'x'
        )
        self.assertEqual(path, '/suche/mieten/wohnung/hessen/darmstadt-64283/ad08de2508')
        with self.assertRaises(RuntimeError):
            portals.immowelt_location('https://www.immowelt.de/suche/darmstadt/wohnungen/mieten', 'x')
        page, request = portals.immowelt_search(
            path, profile(search(max_price=89950, filters={'min_area': 40.5, 'min_rooms': 2, 'exclusions': []}))
        )
        self.assertEqual(
            page,
            'https://www.immowelt.de/classified-search?distributionTypes=Rent&estateTypes=Apartment'
            '&locations=AD08DE2508&priceMax=900&spaceMin=40&numberOfRoomsMin=2',
        )
        self.assertEqual(
            request['criteria'],
            {'distributionTypes': ['Rent'], 'estateTypes': ['Apartment'], 'location': {'placeIds': ['AD08DE2508']},
             'priceMax': 900, 'spaceMin': 40, 'numberOfRoomsMin': 2},
        )  # fmt: skip
        self.assertEqual(request['paging'], {'page': 1, 'size': 100, 'order': 'DateDesc'})
        page, request = portals.immowelt_search(
            path, profile(search(filters={'min_area': 0, 'min_rooms': 0, 'exclusions': []}))
        )
        self.assertNotIn('spaceMin', page)
        self.assertNotIn('numberOfRoomsMin', request['criteria'])
        self.assertNotIn('projectTypes', request['criteria'])
        page, request = portals.immowelt_search(
            path, profile(search(filters={'exclusions': ['WBS', 'Tauschwohnung', 'Befristet', 'WG-Zimmer']}))
        )
        self.assertEqual(request['criteria']['certificateOfEligibilityNeeded'], 'No')
        self.assertEqual(
            request['criteria']['projectTypes'],
            ['Investment', 'Life_Annuity', 'New_Build', 'Projected', 'Resale', 'Stock'],
        )
        self.assertIn('&certificateOfEligibilityNeeded=No&projectTypes=Investment%2CLife_Annuity%2C', page)
        page, request = portals.immowelt_search(path, profile(search()), page=3)
        self.assertTrue(page.endswith('&page=3'))  # its own address, so the page cache keeps the pages apart
        self.assertEqual(request['paging']['page'], 3)

    def test_immowelt_detail(self):
        # The exposé sections of live pages (27.10.2026) as sources.IMMOWELT_EXPOSE reads them.
        def page(name):
            return (Path(__file__).parent / 'fixtures' / 'immowelt' / f'expose_{name}.html').read_text()

        detail = portals.immowelt_detail(page('warm'))
        self.assertEqual(
            (detail['rent'], detail['coldRent'], detail['area'], detail['rooms'], detail['availableFrom']),
            (770, 540, 55, 2, '2026-12-01'),
        )
        self.assertIn('Übernahmekosten', detail['text'])
        # Without a stated warm rent: cold rent + running costs, heating included in them.
        self.assertEqual(portals.immowelt_detail(page('warm').replace('>Warmmiete<', '>Miete<'))['rent'], 770)
        # Immowelt's "geschätzte Warmmiete" is no stated rent; the heating costs are unknown here.
        estimated = portals.immowelt_detail(page('estimated'))
        self.assertEqual((estimated['rent'], estimated['coldRent'], estimated['area']), (None, 750, 74))
        heating = page('estimated').replace(
            '<div class="css-14u1ckg">nicht in Nebenkosten enthalten</div></span>', '</span><div>95&nbsp;€</div>'
        )
        self.assertEqual(portals.immowelt_detail(heating)['rent'], 750 + 240 + 95)
        # An all-in rent ("Monatliche Miete") is the warm rent; the card's values stay for the rest.
        all_in = portals.immowelt_detail(page('all_in'))
        self.assertEqual((all_in['rent'], all_in['coldRent'], all_in['area']), (859, None, 19))
        with self.assertRaises(Unreadable):
            portals.immowelt_detail('<div data-testid="cdp-hardfacts-keyfacts">1 Zimmer</div>')

    def test_ohne_makler_list_and_detail(self):
        # Layout of 31.10.2026 with made-up offers.
        listed, eberstadt = portals.om_parse(fixture('ohne-makler', 'list.html'))  # not the similar ones after it
        self.assertEqual(listed['url'], 'https://www.ohne-makler.net/immobilie/100001/')
        self.assertEqual(
            (listed['title'], listed['place'], listed['coldRent'], listed.get('rent'), listed['area'], listed['rooms']),
            ('Helle 2-Zimmer-Wohnung mit Einbauküche', '64283 Darmstadt', 755, None, 50, 2),
        )
        self.assertEqual(
            listed['imageSource'], 'https://media.ohne-makler.net/rs:fill:488:288/q:90/abc?sig=x&exp=1793321795'
        )
        self.assertEqual(
            (eberstadt['place'], eberstadt['area'], eberstadt['traits']),
            ('64297 Darmstadt (Eberstadt)', 100.5, ['WBS']),
        )
        nothing = fixture('ohne-makler', 'list.html').replace(
            'Hier listen wir 2 Angebote für Wohnungen zur Miete in Darmstadt auf.', ''
        )
        self.assertEqual(portals.om_parse(nothing), [])  # then the page shows only similar offers
        with self.assertRaises(Unreadable):
            portals.om_parse('<html>Wartung</html>')
        detail = portals.om_detail(fixture('ohne-makler', 'detail.html'))
        self.assertEqual(
            (detail['coldRent'], detail['rent'], detail['area'], detail['rooms'], detail['availableFrom']),
            (755, 755 + 205, 50, 2, '2026-12-01'),
        )
        self.assertEqual(detail['place'], 'Musterstraße 21, 64283 Darmstadt')
        self.assertEqual(detail['traits'], ['Befristet'])
        self.assertIn('Bus vor der Tür', detail['text'])
        without_costs = fixture('ohne-makler', 'detail.html').replace('Summe Nebenkosten/Heizkosten', 'Mietsicherheit')
        self.assertIsNone(portals.om_detail(without_costs)['rent'])  # never guessed from the cold rent

    def test_ohne_makler_place_and_address(self):
        frankfurt = [
            {'model': 'CITY', 'id': 2, 'name': 'Frankfurt (Oder)'},
            {'model': 'CITY', 'id': 1, 'name': 'Frankfurt (Main)'},
            {'model': 'REGION', 'id': 3, 'name': 'Frankfurt am Main (Kreis)'},
        ]
        self.assertEqual(portals.om_town(frankfurt, 'Frankfurt am Main'), 'Frankfurt (Main)')
        self.assertEqual(portals.om_town(frankfurt, 'Frankfurt'), 'Frankfurt (Oder)')  # the site's first town
        self.assertIsNone(portals.om_town([], 'Xyz'))
        self.assertEqual(
            portals.om_path('Hessen', 'Bad Homburg (Höhe)'), '/immobilien/wohnung-mieten/hessen/bad-homburg-hohe/'
        )
        self.assertEqual(portals.om_path('Bayern', 'München'), '/immobilien/wohnung-mieten/bayern/munchen/')
        limits = {'maxRent': 899.5, 'minArea': 40.5, 'minRooms': 2, 'radius': 0}
        self.assertEqual(
            portals.om_url('/immobilien/wohnung-mieten/hessen/darmstadt/', limits),
            'https://www.ohne-makler.net/immobilien/wohnung-mieten/hessen/darmstadt/?sort=date_desc&price_max=900&area_min=40&radius=0',
        )
        self.assertEqual(
            portals.om_url('/immobilien/wohnung-mieten/hessen/darmstadt/', {**limits, 'radius': 15, 'minArea': 0}, 2),
            'https://www.ohne-makler.net/immobilien/wohnung-mieten/hessen/darmstadt/?sort=date_desc&price_max=900&radius=25&page=2',
        )
        self.assertEqual(
            portals.om_url('64283', limits),
            'https://www.ohne-makler.net/immobilien/wohnung-mieten/?zip=64283&sort=date_desc&price_max=900&area_min=40',
        )

    def test_immobilien_de_list_and_detail(self):
        # Layout of 31.10.2026 with made-up offers: the schema.org data of the list and of an offer.
        flat, room = portals.id_parse(fixture('immobilien-de', 'list.html'))
        self.assertEqual(flat['url'], 'https://www.immobilien.de/expose/10000001')
        self.assertEqual(
            (flat['title'], flat['place'], flat['coldRent'], flat.get('rent'), flat['area'], flat['rooms']),
            ('Helle 2-Zimmer-Wohnung mit Einbauküche', '64283 Darmstadt', 755, None, 50, 2),
        )
        self.assertEqual(
            flat['imageSource'], 'https://immobilien-api.fly.dev/api/images/openimmo/feed/100001/0-Wohnzimmer.jpg'
        )
        self.assertEqual((room['area'], room['rooms'], room['traits']), (None, None, ['WG-Zimmer']))
        none = '<script type="application/ld+json">{"@graph":[{"@type":"BreadcrumbList"}]}</script>'
        self.assertEqual(portals.id_parse(none + '<p>Keine Ergebnisse für Deine Suche.</p>'), [])
        with self.assertRaises(Unreadable):
            portals.id_parse(none)
        page = fixture('immobilien-de', 'detail.html')
        detail = portals.id_detail(page)
        self.assertEqual(
            (detail['coldRent'], detail['rent'], detail['area'], detail['rooms'], detail['availableFrom']),
            (1750, 2020, 90, 4, '2026-12-01'),
        )
        self.assertEqual(detail['place'], 'Musterweg 10, 60325 Frankfurt am Main')
        self.assertEqual(
            (detail['traits'], detail['text']), (['WBS'], 'Vier Zimmer im ersten Stock. Keine WG, nur mit WBS.')
        )
        unstated = page.replace('>Warmmiete<', '>Gesamt<')
        self.assertEqual(portals.id_detail(unstated)['rent'], 1750 + 270)  # heating included in the running costs
        unknown_heating = unstated.replace('in Warmmiete enthalten', 'keine Angabe')
        self.assertIsNone(portals.id_detail(unknown_heating)['rent'])
        net = page.replace('>Kaltmiete<', '>Nettokaltmiete<')
        self.assertEqual(portals.id_detail(net)['coldRent'], 1750)
        with self.assertRaises(Unreadable):
            portals.id_detail('<html>Exposé nicht gefunden</html>')

    def test_immobilien_de_place_and_address(self):
        frankfurt = {
            'cities': [
                {'name': 'Frankfurt', 'nameAddition': '(Oder)', 'slugToken': 'frankfurt', 'listingCount': 11},
                {'name': 'Frankfurt', 'nameAddition': 'am Main', 'slugToken': 'frankfurt2', 'listingCount': 205},
            ],
            'zips': [],
        }
        self.assertEqual(portals.id_city(frankfurt, 'Frankfurt am Main'), 'frankfurt2')
        self.assertEqual(portals.id_city(frankfurt, 'Frankfurt'), 'frankfurt2')  # the one with most offers
        self.assertEqual(portals.id_city(frankfurt, 'Frankfurt (Oder)'), 'frankfurt')
        self.assertIsNone(portals.id_city({'cities': []}, 'Xyz'))
        self.assertEqual(portals.id_town({'zips': [{'zip': '64283', 'city': 'Darmstadt'}]}), 'Darmstadt')
        self.assertEqual(
            portals.id_url('darmstadt', {'maxRent': 899.5, 'minArea': 40.5, 'minRooms': 1.5}, 2),
            'https://www.immobilien.de/mieten/wohnung/darmstadt/?sort=newest&preis_bis=900&flaeche_von=40&zimmer_von=1&page=2',
        )

    def test_wg_gesucht_list_and_detail(self):
        p = {'maxRent': 700.5, 'minArea': 30.5, 'minRooms': 1.5}
        self.assertEqual(
            portals.wg_url(('Darmstadt', 23), p, page=2),
            'https://www.wg-gesucht.de/wohnungen-in-Darmstadt.23.1+2.1.1.html?rMax=701&sMin=30&rmMin=1&limit=100',
        )
        self.assertEqual(
            portals.wg_url(('Darmstadt', 23), {'maxRent': 700, 'minArea': 0, 'minRooms': 0}),
            'https://www.wg-gesucht.de/wohnungen-in-Darmstadt.23.1+2.1.0.html?rMax=700&limit=100',
        )
        excluded = {'maxRent': 700, 'minArea': 0, 'minRooms': 0, 'exclusions': ['Tauschwohnung', 'Befristet']}
        self.assertEqual(
            portals.wg_url(('Darmstadt', 23), excluded),
            'https://www.wg-gesucht.de/wohnungen-in-Darmstadt.23.1+2.1.0.html?rMax=700&limit=100&exc=2'
            '&offer_filter=1&city_id=23&categories%5B%5D=1&categories%5B%5D=2&rent_types%5B%5D=2',
        )
        cards = portals.wg_parse(WG_LIST)
        self.assertEqual(
            [(c['rent'], c['area'], c['rooms'], c['availableFrom'], c['traits']) for c in cards],
            [(1300, 82, 3, '2026-10-01', []), (800, 30, 1, '2026-10-01', ['Befristet'])],
        )
        self.assertEqual(cards[0]['place'], 'Odenwaldstr. 46 B, Darmstadt, Eberstadt')
        detail = portals.wg_detail(WG_DETAIL)
        self.assertEqual((detail['rent'], detail['coldRent'], detail['traits']), (1300, 1100, ['WBS']))
        self.assertNotIn('ads()', detail['text'])

    def test_bauverein_whole_stock(self):
        cards = portals.bauverein_parse(BAUVEREIN_MARKERS)
        self.assertEqual(
            [c['title'] for c in cards], ['Charmante 2-Zimmer-Wohnung', '3-Zimmer in Mühltal']
        )  # no garage
        self.assertTrue(cards[0]['url'].endswith('#/profile/845981__325-103-M/20068820'))
        self.assertIsNone(cards[1]['url'])  # no picture: the reference number is asked for separately
        self.assertEqual(portals.bauverein_identifier(cards[0]['url']), 20068820)

    def test_nhw_cards(self):
        cards = portals.nhw_parse(NHW_PAGE)
        self.assertEqual((cards[0]['rent'], cards[0]['coldRent'], cards[0]['lat']), (1451.9, 1145, 49.864))
        self.assertEqual(cards[1]['traits'], ['WBS'])  # "sucht Mieter" is a landlord offer
        self.assertEqual(portals.nhw_parse('<div class="immo--list"></div>'), [])
        with self.assertRaises(Unreadable):
            portals.nhw_parse('<html>Wartungsarbeiten</html>')

    def test_renamed_card_markup_falls_back_to_links_and_text(self):
        cards = portals.kleinanzeigen_parse(without_markup(KLEINANZEIGEN_PAGE))
        self.assertEqual(
            [c['url'] for c in cards],
            [
                'https://www.kleinanzeigen.de/s-anzeige/helle-2-zimmer/3515855658-203-7965',
                'https://www.kleinanzeigen.de/s-anzeige/tauschwohnung/3520717913-203-7963',
            ],
        )
        first = cards[0]
        self.assertEqual(
            (first['title'], first['place'], first['coldRent'], first['area'], first['rooms']),
            ('Helle 2-Zimmer & Balkon', '14480 Potsdam', 1050, 62.5, 2),
        )
        self.assertEqual(cards[1]['traits'], ['Tauschwohnung'])

        page = without_markup(WG_LIST) + '<a href="/wohnungen-in-Darmstadt.23.2.1.1.html">Seite 2</a>'
        cards = portals.wg_parse(page)
        self.assertEqual(
            [(c['rent'], c['coldRent'], c['area'], c['rooms'], c['availableFrom']) for c in cards],
            # the shortened second card names its rooms only in an attribute, the live page also in its text
            [(1300, None, 82, 3, '2026-10-01'), (800, None, 30, None, '2026-10-01')],
        )
        self.assertEqual(cards[0]['imageSource'], 'https://img.wg-gesucht.de/media/up/a.small.jpg')

        cards = portals.nhw_parse(without_markup(NHW_PAGE))
        self.assertEqual(cards[0]['url'], 'https://www.nhw.de/zuhause-finden/immobilie/5000-S2133-001-102')
        self.assertEqual(cards[1]['title'], 'Single-Apartment sucht Mieter')  # a landlord's offer stays

    def test_fallback_keeps_one_card_per_offer(self):
        html = (
            '<ul><li><a href="/expose/a"><img src="https://mms.immowelt.de/a.jpg"></a><h3>Altbau</h3>'
            '<p>950 € Warmmiete · 70 m² · 3 Zimmer</p><a href="/expose/a">Details</a></li>'
            '<li><h3>Neubau</h3><p>1.200 €</p><a href="https://www.immowelt.de/expose/b?ref=x">mehr</a></li></ul>'
        )
        first, second = portals.fallback_cards(
            html, r'^(?:https://www\.immowelt\.de)?/expose/[\w-]+$', 'https://www.immowelt.de'
        )
        self.assertEqual(
            (first['url'], first['title'], first['rent'], first['coldRent'], first['area'], first['rooms']),
            ('https://www.immowelt.de/expose/a', 'Altbau', 950, None, 70, 3),
        )
        self.assertEqual(first['imageSource'], 'https://mms.immowelt.de/a.jpg')
        self.assertEqual(
            (second['url'], second['title'], second['coldRent']), ('https://www.immowelt.de/expose/b', 'Neubau', 1200)
        )


def fixture(portal, name):
    return (Path(__file__).parent / 'fixtures' / portal / name).read_text()


def without_markup(html):
    """The page after a redesign: every class, id and test hook renamed away."""
    hooks = r'class|id|data-testid|data-adid|data-itemuid|data-filtertype|data-filtervalue'
    return re.sub(rf'\s(?:{hooks})="[^"]*"', '', html)


class TempData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()
        store.init()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def stored_search(self, **changes):
        values = search(**changes)
        with store.db() as conn:
            cursor = conn.execute(
                'INSERT INTO searches(query,max_price,excluded,sources,radius,filters,kind) VALUES (?,?,?,?,?,?,?)',
                (values['query'], values['max_price'], values['excluded'], json.dumps(values['sources']),
                 values['radius'], json.dumps(values['filters']), 'wohnung'),
            )  # fmt: skip
        return store.search(cursor.lastrowid)


class PlaceTests(TempData):
    def test_whole_stock_is_split_by_place_and_radius(self):
        stock = [
            Listing.from_card('bauverein', c | {'url': c['url'] or 'u'})
            for c in portals.bauverein_parse(BAUVEREIN_MARKERS)
        ]
        darmstadt = self.stored_search(query='Darmstadt', sources=['bauverein'])
        self.assertEqual([x.title for x in sources.select(stock, darmstadt)], ['Charmante 2-Zimmer-Wohnung'])
        cadence.save_location('osm', 'darmstadt', json.dumps([49.8728, 8.6512, 'Hessen']))
        around = self.stored_search(query='Darmstadt', radius=10, sources=['bauverein'])
        self.assertEqual(len(sources.select(stock, around)), 2)  # Mühltal is 6 km from the centre
        postcode = self.stored_search(query='64367', sources=['bauverein'])
        self.assertEqual([x.place for x in sources.select(stock, postcode)], ['Bruchmühlenweg 8, 64367 Mühltal'])

    def test_place_lookups_are_kept(self):
        answer = [{'lat': '49.87', 'lon': '8.65', 'address': {'state': 'Hessen'}}]
        with patch.object(sources.http, 'get_json', new=AsyncMock(return_value=answer)) as get:
            self.assertEqual(asyncio.run(sources.locate('Darmstadt')), (49.87, 8.65, 'Hessen'))
            self.assertEqual(asyncio.run(sources.locate(' darmstadt ')), (49.87, 8.65, 'Hessen'))
        self.assertEqual(get.await_count, 1)


class PageTests(TempData):
    def test_further_pages_reach_each_paged_portal(self):
        from contextlib import asynccontextmanager

        search = self.stored_search(query='Darmstadt')
        cadence.save_location('kleinanzeigen', 'darmstadt', '4868')
        cadence.save_location('immoscout24', 'darmstadt', '/de/hessen/darmstadt')
        cadence.save_location('wg-gesucht', 'darmstadt', '["Darmstadt", 23]')
        cadence.save_location('ohne-makler', 'darmstadt', '/immobilien/wohnung-mieten/hessen/darmstadt/')
        cadence.save_location('immobilien-de', 'darmstadt', 'darmstadt')
        cadence.save_location('immowelt', 'darmstadt', '/suche/mieten/wohnung/hessen/darmstadt-64283/ad08de2508')

        @asynccontextmanager
        async def browser_page(hosts):
            yield object()  # the Playwright page

        visited = []

        async def load_page(page, source, url, *args, **kwargs):
            visited.append(url)
            return {'rows': [], 'html': '', 'text': '', 'total': 0}

        async def get_html(source, url, follow=False):
            visited.append(url)
            return 'keine Angebote'

        async def get_json(source, url, *args, **kwargs):
            visited.append(url)
            return {}

        with (
            patch.object(sources.browser, 'browser_page', browser_page),
            patch.object(sources.browser, 'load_page', load_page),
            patch.object(sources.http, 'get_html', get_html),
            patch.object(sources.http, 'get_json', get_json),
            patch.object(sources.portals, 'immoscout_parse', return_value=[]),
            patch.object(sources.portals, 'om_parse', return_value=[]),
            patch.object(sources.portals, 'id_parse', return_value=[]),
        ):
            for source in sources.PAGED:
                asyncio.run(sources.more(source, search, 2))
        self.assertIn('pagenumber=2&pagesize=100', visited[0])
        self.assertIn('/seite:2/', visited[1])
        self.assertIn('.1+2.1.1.html', visited[2])  # WG-Gesucht counts from 0
        self.assertTrue(all(url.endswith('&page=2') for url in visited[3:]), visited[3:])


class AreaTests(TempData):
    def test_regional_portals_rest_outside_their_area_without_a_request(self):
        postcode = self.stored_search(
            query='64283', filters={'place': {'kind': 'plz', 'label': '64283', 'query': '64283'}}
        )
        potsdam = self.stored_search(query='Potsdam')
        self.assertEqual(
            sources.out_of_area(postcode, 'wg-gesucht'), 'Sucht nur nach Ortsnamen, nicht nach Postleitzahl'
        )
        self.assertIsNone(sources.out_of_area(potsdam, 'wg-gesucht'))  # not looked up yet: asked
        cadence.save_location('wg-gesucht', 'potsdam', sources.UNKNOWN_PLACE)
        self.assertEqual(sources.out_of_area(potsdam, 'wg-gesucht'), 'Kennt diesen Ort nicht')
        self.assertIsNone(sources.out_of_area(potsdam, 'nhw'))  # place unknown yet: asked as before
        cadence.save_location('osm', 'potsdam', json.dumps([52.39, 13.06, 'Brandenburg']))
        self.assertEqual(sources.out_of_area(potsdam, 'nhw'), 'Nur in Hessen')
        self.assertEqual(sources.out_of_area(potsdam, 'bauverein'), 'Nur Darmstadt und Umgebung')
        darmstadt = self.stored_search(query='Darmstadt')
        cadence.save_location('osm', 'darmstadt', json.dumps([49.87, 8.65, 'Hessen']))
        self.assertIsNone(sources.out_of_area(darmstadt, 'bauverein'))
        self.assertIsNone(sources.out_of_area(darmstadt, 'wg-gesucht'))

    def test_a_resting_search_never_stands_in_for_the_shared_stock(self):
        from suchalarm import scheduler

        cadence.save_location('osm', 'potsdam', json.dumps([52.39, 13.06, 'Brandenburg']))
        cadence.save_location('osm', 'darmstadt', json.dumps([49.87, 8.65, 'Hessen']))
        potsdam = self.stored_search(query='Potsdam', sources=['nhw'])
        darmstadt = self.stored_search(query='Darmstadt', sources=['nhw'])
        self.assertNotEqual(cadence.query_key('nhw', potsdam), cadence.query_key('nhw', darmstadt))
        jobs = []
        searches = [potsdam, darmstadt]
        for _ in range(2):
            job, _ = scheduler.plan(searches, False)
            jobs.append([s['id'] for s in job['searches']])
            scheduler.finish(job)
        self.assertIn([darmstadt['id']], jobs)  # Darmstadt has a job of its own

    def test_a_resting_portal_is_not_disturbed(self):
        potsdam = self.stored_search(query='Potsdam', sources=['wg-gesucht'])
        cadence.save_location('wg-gesucht', 'potsdam', sources.UNKNOWN_PLACE)
        fetch = AsyncMock()
        with patch.object(flats.sources, 'fetch', new=fetch):
            done = asyncio.run(worker.check(potsdam))
        fetch.assert_not_called()
        self.assertEqual(done, {'wg-gesucht'})
        with store.db() as conn:
            status = dict(conn.execute('SELECT error, note, count FROM source_status').fetchone())
        self.assertEqual(status, {'error': None, 'note': 'Kennt diesen Ort nicht', 'count': 0})


class WgLookupTests(TempData):
    def setUp(self):
        super().setUp()
        self.visited = []

    async def get_html(self, source, url, follow=False):
        self.visited.append((url, follow))
        return 'keine Angebote'

    def fetch(self, search, answer):
        with (
            patch.object(sources.http, 'get_json', new=AsyncMock(side_effect=answer)) as get_json,
            patch.object(sources.http, 'get_html', self.get_html),
        ):
            for _ in range(2):
                try:
                    asyncio.run(sources.cards('wg-gesucht', search))
                except RuntimeError as exc:
                    self.visited.append(str(exc))
        return get_json

    def test_a_town_is_looked_up_once_and_its_list_follows_in_the_same_fetch(self):
        frankfurt = self.stored_search(query='Frankfurt am Main', sources=['wg-gesucht'])
        get_json = self.fetch(frankfurt, [WG_FRANKFURT])
        self.assertEqual(get_json.await_count, 1)
        self.assertEqual(cadence.location('wg-gesucht', 'frankfurt am main'), '["Frankfurt-am-Main", 41]')
        page = 'https://www.wg-gesucht.de/wohnungen-in-Frankfurt-am-Main.41.1+2.1.0.html?rMax=1500&rmMin=1&limit=100'
        self.assertEqual(self.visited, [(page, True), (page, False)])

    def test_an_unknown_town_is_asked_once_and_then_rests(self):
        unknown = self.stored_search(query='Xyzqwv', sources=['wg-gesucht'])
        get_json = self.fetch(unknown, [sources.http.NotFound('Anbieter antwortet mit HTTP 404.')])
        self.assertEqual(get_json.await_count, 1)
        self.assertEqual(self.visited, ['WG-Gesucht kennt den Ort „Xyzqwv“ nicht.'] * 2)
        self.assertEqual(sources.out_of_area(unknown, 'wg-gesucht'), 'Kennt diesen Ort nicht')


class ListedPlaceTests(TempData):
    """ohne-makler.net and immobilien.de find a town once through their place search."""

    def setUp(self):
        super().setUp()
        self.visited = []

    async def get_html(self, source, url, follow=False):
        self.visited.append((url, follow))
        if 'page=2' in url:
            raise sources.http.NotFound('Anbieter antwortet mit HTTP 404.')
        return '<script type="application/ld+json">{}</script>Keine Ergebnisse für Deine Suche'

    def cards(self, source, search, answers, page=1):
        with (
            patch.object(sources.http, 'get_json', new=AsyncMock(side_effect=answers)) as get_json,
            patch.object(sources.http, 'get_html', self.get_html),
            patch.object(sources, 'locate', new=AsyncMock(return_value=(49.87, 8.65, 'Hessen'))),
        ):
            try:
                return asyncio.run(sources.cards(source, search, page)), get_json
            except RuntimeError as exc:
                return str(exc), get_json

    def test_ohne_makler_builds_the_towns_path_once(self):
        darmstadt = self.stored_search(query='Darmstadt', sources=['ohne-makler'])
        answer = [{'model': 'CITY', 'id': 18150, 'name': 'Darmstadt'}]
        with patch.object(portals, 'om_parse', return_value=[]):
            _, get_json = self.cards('ohne-makler', darmstadt, [answer])
            self.cards('ohne-makler', darmstadt, [])
        self.assertEqual(get_json.await_count, 1)
        page = 'https://www.ohne-makler.net/immobilien/wohnung-mieten/hessen/darmstadt/?sort=date_desc&price_max=1500&radius=0'
        self.assertEqual(self.visited, [(page, True), (page, False)])

    def test_an_unknown_town_rests(self):
        unknown = self.stored_search(query='Xyzqwv', sources=['ohne-makler', 'immobilien-de'])
        self.assertEqual(self.cards('ohne-makler', unknown, [[]])[0], 'ohne-makler.net kennt den Ort „Xyzqwv“ nicht.')
        self.assertEqual(
            self.cards('immobilien-de', unknown, [{'cities': []}])[0], 'immobilien.de kennt den Ort „Xyzqwv“ nicht.'
        )
        self.assertEqual(sources.out_of_area(unknown, 'ohne-makler'), 'Kennt diesen Ort nicht')
        self.assertEqual(sources.out_of_area(unknown, 'immobilien-de'), 'Kennt diesen Ort nicht')

    def test_immobilien_de_finds_a_postcodes_town(self):
        place = {'query': '64283', 'label': '64283 Darmstadt', 'kind': 'plz'}
        filters = {'min_area': 0, 'min_rooms': 1, 'exclusions': [], 'place': place}
        postcode = self.stored_search(query='64283', filters=filters, sources=['immobilien-de'])
        town = {'cities': [{'name': 'Darmstadt', 'slugToken': 'darmstadt'}]}
        cards, get_json = self.cards(
            'immobilien-de', postcode, [{'zips': [{'zip': '64283', 'city': 'Darmstadt'}]}, town]
        )
        self.assertEqual((cards, get_json.await_count), ([], 2))
        self.assertEqual(
            self.visited,
            [('https://www.immobilien.de/mieten/wohnung/darmstadt/?sort=newest&preis_bis=1500&zimmer_von=1', True)],
        )
        self.assertEqual(self.cards('immobilien-de', postcode, [], page=2)[0], [])  # past the last page


class BauvereinTests(TempData):
    def test_a_fetch_that_waits_for_a_reference_continues_where_it_stopped(self):
        answers = []

        async def get_json(source, url, headers=None, data=None, follow=False):
            answers.append(url.rsplit('/', 1)[-1])
            if url.endswith('get-marker'):
                return BAUVEREIN_MARKERS
            if len(answers) == 2:
                raise cadence.Deferred(time.time() + 200, continuation=True)  # the next slot comes later
            return {
                'openimmo': {'anbieter': [{'immobilie': [{'verwaltung_techn': {'objektnr_extern': '607-8275-M'}}]}]}
            }

        with patch.object(sources.http, 'get_json', new=get_json):
            with self.assertRaises(cadence.Deferred):
                asyncio.run(sources._bauverein())
            cards = asyncio.run(sources._bauverein())
            asyncio.run(sources._bauverein())
        self.assertEqual(answers, ['get-marker', 'object-details', 'object-details'])  # no second stock request
        self.assertTrue(cards[1]['url'].endswith('#/profile/845981__607-8275-M/20078298'))


class BauvereinDetailTests(unittest.TestCase):
    def rent(self, **prices):
        offer = {'preise': {'nettokaltmiete': '700', **prices}}
        return portals.bauverein_detail({'openimmo': {'anbieter': [{'immobilie': [offer]}]}})['rent']

    def test_warm_rent_adds_heating_to_the_total(self):
        self.assertEqual(self.rent(warmmiete='990', gesamtmiete='850', heizkosten='90'), 990)
        self.assertEqual(self.rent(gesamtmiete='850', heizkosten='90'), 940)
        self.assertEqual(self.rent(gesamtmiete='850', heizkosten='90', heizkosten_enthalten=True), 850)
        self.assertEqual(self.rent(gesamtmiete='850'), 850)
        self.assertIsNone(self.rent())


class WorkerTests(TempData):
    def card(self, number, **changes):
        return Listing.from_card(
            'immoscout24',
            {'url': f'https://www.immobilienscout24.de/expose/{number}', 'title': f'Wohnung {number}',
             'place': 'Darmstadt', 'coldRent': 900, 'area': 60, 'rooms': 2, **changes},
        )  # fmt: skip

    def run_check(self, found, flat_search):
        with patch.object(flats.sources, 'fetch', new=AsyncMock(return_value=(found, len(found)))):
            return asyncio.run(worker.check(flat_search))

    def due(self):
        with store.db() as conn:
            return [tuple(r) for r in conn.execute('SELECT page, tries FROM more_pages')]

    def test_a_full_page_of_new_flats_is_followed_by_the_next_pages(self):
        flat_search = self.stored_search()
        pages.save_result(
            flat_search, 'immoscout24', [self.card(0)], len([self.card(0)]), time.time()
        )  # the starting stock
        page = lambda start: [self.card(n) for n in range(start, start + pages.FULL_PAGE)]  # noqa: E731
        self.assertEqual(len(pages.save_result(flat_search, 'immoscout24', page(1), len(page(1)), time.time())), 15)
        self.assertEqual(self.due(), [(2, 0)])
        with patch.object(flats.sources, 'more', new=AsyncMock(return_value=(page(100), len(page(100))))) as more:
            asyncio.run(pages.more_step())
            self.assertEqual(more.await_args.args[2], 2)
            self.assertEqual(self.due(), [(3, 0)])
            asyncio.run(pages.more_step())
        self.assertEqual(self.due(), [])  # MAX_PAGE read; the second time page 3 held nothing new
        self.assertEqual(len(catalog.listings(flat_search)), 31)
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM listing_details').fetchone()[0], 30)
        pages.save_result(flat_search, 'immoscout24', page(200), len(page(200)), time.time())
        with patch.object(flats.sources, 'more', new=AsyncMock(side_effect=RuntimeError('weg'))):
            asyncio.run(pages.more_step())
            self.assertEqual(self.due(), [(2, 1)])  # one more try
            asyncio.run(pages.more_step())
        self.assertEqual(self.due(), [])
        pages.save_result(flat_search, 'immoscout24', page(300)[:5], len(page(300)[:5]), time.time())
        self.assertEqual(self.due(), [])  # not a full page

    def test_a_new_search_reads_the_whole_stock_page_by_page(self):
        flat_search = self.stored_search()
        page = lambda start, size=pages.FULL_PAGE: [self.card(n) for n in range(start, start + size)]  # noqa: E731
        self.assertEqual(pages.save_result(flat_search, 'immoscout24', page(0), len(page(0)), time.time()), set())
        self.assertEqual(self.due(), [(2, 0)])
        with patch.object(flats.sources, 'more', new=AsyncMock(return_value=(page(100), len(page(100))))):
            asyncio.run(pages.more_step())
        self.assertEqual(self.due(), [(3, 0)])
        new = pages.save_result(
            flat_search, 'immoscout24', [self.card(500), *page(0)], len([self.card(500), *page(0)]), time.time()
        )
        self.assertEqual(new, {'https://www.immobilienscout24.de/expose/500'})
        self.assertEqual(self.due(), [(3, 0)])  # a regular check does not end the reading of the stock
        with patch.object(flats.sources, 'more', new=AsyncMock(return_value=(page(200, 3), len(page(200, 3))))):
            asyncio.run(pages.more_step())
        self.assertEqual(self.due(), [])  # the last page was not full
        rows = catalog.listings(flat_search)
        self.assertEqual(len(rows), 34)
        self.assertEqual(sorted(r['url'].rsplit('/', 1)[1] for r in rows if not r['start']), ['500'])
        order = [int(r['url'].rsplit('/', 1)[1]) for r in rows]  # newest first, also across the stock's pages
        self.assertEqual(order, [500, *range(15), *range(100, 115), 200, 201, 202])
        with store.db() as conn:
            pending = [r[0] for r in conn.execute('SELECT url FROM listing_details')]
        self.assertEqual(pending, ['https://www.immobilienscout24.de/expose/500'])  # the stock gets none

    def test_reading_the_stock_stops_at_a_page_without_unseen_flats(self):
        flat_search = self.stored_search()
        first = [self.card(n) for n in range(pages.FULL_PAGE)]
        pages.save_result(flat_search, 'immoscout24', first, len(first), time.time())
        with patch.object(
            flats.sources, 'more', new=AsyncMock(return_value=(first, len(first)))
        ):  # the portal ignores the page
            asyncio.run(pages.more_step())
        self.assertEqual(self.due(), [])

    def test_a_page_is_full_by_what_the_portal_listed_not_what_fits_the_place(self):
        flat_search = self.stored_search()
        here = [self.card(n) for n in range(5)]  # the other ten offers of the page lie elsewhere
        pages.save_result(flat_search, 'immoscout24', here, pages.FULL_PAGE, time.time())
        self.assertEqual(self.due(), [(2, 0)])
        with patch.object(flats.sources, 'more', new=AsyncMock(return_value=([], pages.FULL_PAGE))):
            asyncio.run(pages.more_step())
        self.assertEqual(self.due(), [(3, 0)])  # a page of only other places says nothing about the end
        pages.save_result(flat_search, 'immoscout24', here[:1], 1, time.time())
        self.assertEqual(self.due(), [(3, 0)])

    def test_a_burst_page_listing_one_flat_twice_still_counts_as_all_new(self):
        flat_search = self.stored_search()
        pages.save_result(flat_search, 'immoscout24', [self.card(0)], 1, time.time())  # the starting stock
        burst = [self.card(n) for n in range(1, pages.FULL_PAGE)] + [self.card(1)]
        pages.save_result(flat_search, 'immoscout24', burst, len(burst), time.time())
        self.assertEqual(self.due(), [(2, 0)])

    def test_a_changed_portal_limit_reads_the_stock_again_instead_of_mailing_old_flats(self):
        flat_search = self.stored_search()
        pages.save_result(flat_search, 'immoscout24', [self.card(1)], 1, time.time())  # the starting stock
        with store.db() as conn:
            pages.read_stock_again(conn, flat_search['id'], ['immoscout24', 'nhw'])
        self.assertEqual(self.due(), [(1, 0)])  # only paged portals
        older = [self.card(n) for n in range(2, 2 + pages.FULL_PAGE)]  # now within the wider limit
        self.assertEqual(pages.save_result(flat_search, 'immoscout24', older, len(older), time.time()), set())
        self.assertEqual(self.due(), [(2, 0)])
        new = pages.save_result(flat_search, 'immoscout24', [self.card(99), *older], 16, time.time())
        self.assertEqual(new, {'https://www.immobilienscout24.de/expose/99'})
        self.assertEqual(self.due(), [(2, 0)])  # the reading of the stock goes on

    def test_a_failure_after_an_empty_start_does_not_make_the_first_flat_stock(self):
        flat_search = self.stored_search()
        self.assertEqual(pages.save_result(flat_search, 'immoscout24', [], 0, time.time()), set())  # the stock: none
        status.source_error(flat_search, 'immoscout24', time.time(), 'kaputt')
        new = pages.save_result(flat_search, 'immoscout24', [self.card(1)], 1, time.time())
        self.assertEqual(new, {'https://www.immobilienscout24.de/expose/1'})

    def test_first_result_is_the_stock_and_only_new_listings_get_a_detail_page(self):
        flat_search = self.stored_search()
        self.assertEqual(self.run_check([self.card(1), self.card(2)], flat_search), {'immoscout24'})
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM listing_details').fetchone()[0], 0)
        self.run_check([self.card(1), self.card(3), self.card(4, coldRent=5000)], flat_search)
        with store.db() as conn:
            pending = [r[0] for r in conn.execute('SELECT url FROM listing_details')]
        self.assertEqual(pending, ['https://www.immobilienscout24.de/expose/3'])  # 4 is too dear already on the card
        rows = catalog.listings(flat_search)
        self.assertEqual(sorted(r['url'][-1] for r in rows if r['start']), ['1', '2'])
        self.assertEqual(len(rows), 4)  # 2 only moved off the first page: it stays

    def test_whole_stock_drops_let_flats_and_paged_portals_keep_them_a_month(self):
        flat_search = self.stored_search(sources=['immoscout24', 'nhw'])
        landlord = [Listing.from_card('nhw', {'url': f'https://www.nhw.de/zuhause-finden/immobilie/{n}', 'title': 'W',
                                              'place': 'Darmstadt'}) for n in (1, 2)]  # fmt: skip
        pages.save_result(flat_search, 'nhw', landlord, len(landlord), time.time())
        pages.save_result(flat_search, 'nhw', landlord[:1], len(landlord[:1]), time.time())
        pages.save_result(
            flat_search,
            'immoscout24',
            [self.card(1), self.card(2)],
            len([self.card(1), self.card(2)]),
            time.time() - flats.KEEP_LISTINGS - 5,
        )
        pages.save_result(flat_search, 'immoscout24', [self.card(1)], len([self.card(1)]), time.time())
        urls = sorted(r['url'][-1] for r in catalog.listings(flat_search))
        self.assertEqual(urls, ['1', '1', '2'])  # nhw/2 was let, expose/2 is kept for now
        flats.cleanup()
        self.assertEqual(sorted(r['url'][-1] for r in catalog.listings(flat_search)), ['1', '1'])

    def test_detail_page_completes_the_listing(self):
        flat_search = self.stored_search(filters={'min_area': 0, 'min_rooms': 1, 'exclusions': ['WBS']})
        self.run_check([self.card(1)], flat_search)
        self.run_check([self.card(1), self.card(2)], flat_search)
        detail = {'rent': 1100.0, 'traits': ['WBS'], 'text': 'Nur mit WBS'}
        with (
            patch.object(flats.sources, 'detail', new=AsyncMock(return_value=detail)),
            patch.object(flats.cadence, 'ready_at', return_value=0),
        ):
            self.assertEqual(asyncio.run(details.detail_step()), 'https://www.immobilienscout24.de/expose/2')
            self.assertIsNone(asyncio.run(details.detail_step()))  # nothing left
        row = next(r for r in catalog.listings(flat_search) if r['url'].endswith('/2'))
        self.assertEqual((row['rent'], row['status'], row['reasons']), (1100.0, 'excluded', ['WBS']))

    def test_the_starting_stock_is_completed_slowly_when_nothing_else_waits(self):
        flat_search = self.stored_search()
        details._backfilled.clear()
        stock = [self.card(1), self.card(2, coldRent=5000), self.card(3)]
        pages.save_result(flat_search, 'immoscout24', stock, len(stock), time.time() - 60)
        pages.save_result(flat_search, 'immoscout24', [*stock, self.card(4)], len([*stock, self.card(4)]), time.time())
        with patch.object(flats.cadence, 'ready_at', return_value=0):
            self.assertIsNone(details.backfill_step())  # the new listing 4 comes first
            with patch.object(flats.sources, 'detail', new=AsyncMock(return_value={'rent': 1000.0})):
                self.assertTrue(asyncio.run(details.detail_step()).endswith('/4'))
                first = details.backfill_step()
                self.assertIn(first[-1], '13')  # 2 is too dear already on the card
                self.assertIsNone(details.backfill_step())  # one at a time
                self.assertEqual(asyncio.run(details.detail_step()), first)
                self.assertIsNone(details.backfill_step())  # the portal waits BACKFILL_GAP
                second = details.backfill_step(now=time.time() + details.BACKFILL_GAP)
                self.assertEqual({first[-1], second[-1]}, {'1', '3'})
                asyncio.run(details.detail_step())
                self.assertIsNone(details.backfill_step(now=time.time() + 3 * details.BACKFILL_GAP))  # all done
        rows = {r['url'][-1]: r for r in catalog.listings(flat_search)}
        self.assertEqual([(rows[n]['price'], rows[n]['estimated'], rows[n]['start']) for n in '13'],
                         [(100000, False, True)] * 2)  # fmt: skip
        self.assertEqual(rows['2']['details'], 'available')
        outbox.queue_mails()
        with store.db() as conn:
            known = [r[0][-1] for r in conn.execute('SELECT url FROM outbox UNION SELECT url FROM notifications')]
        self.assertEqual(known, ['4'])  # the starting stock stays known, never mailed

    def test_failed_detail_pages_are_retried_then_given_up(self):
        flat_search = self.stored_search()
        self.run_check([self.card(1)], flat_search)
        self.run_check([self.card(1), self.card(2)], flat_search)
        with (
            patch.object(flats.sources, 'detail', new=AsyncMock(side_effect=RuntimeError('weg'))),
            patch.object(flats.cadence, 'ready_at', return_value=0),
        ):
            for _ in range(details.DETAIL_ATTEMPTS):
                asyncio.run(details.detail_step(now=time.time() + 86400))
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT state, attempts FROM listing_details').fetchone()[:], ('failed', 3))

    def test_a_refused_immowelt_expose_skips_only_that_listing(self):
        url = 'https://www.immowelt.de/expose/abc'
        with store.db() as conn:
            conn.execute(
                "INSERT INTO listing_details(url,source,state,created_at) VALUES (?,'immowelt','pending',?)",
                (url, time.time()),
            )
        blocked = AsyncMock(side_effect=browser.Blocked('Anbieter blockiert die Browser-Abfrage (HTTP 403).'))
        now = time.time()
        with patch.object(sources, '_browser_page', new=blocked):
            self.assertEqual(asyncio.run(details.detail_step(now=now)), url)
        self.assertEqual(cadence.ready_at('immowelt', now=now), now)  # Immowelt is not paused
        with store.db() as conn:
            row = conn.execute('SELECT state, attempts, error FROM listing_details').fetchone()
        self.assertEqual((row['state'], row['attempts']), ('pending', 1))
        self.assertIn('HTTP 403', row['error'])

    def test_detail_pages_of_long_gone_listings_are_forgotten(self):
        flat_search = self.stored_search()
        self.run_check([self.card(1)], flat_search)
        self.run_check([self.card(1), self.card(2), self.card(3)], flat_search)
        self.run_check([self.card(1), self.card(3)], flat_search)  # 2 is no longer seen, 3 still listed
        with store.db() as conn:
            conn.execute("UPDATE listings SET last_seen=0 WHERE url LIKE '%/2'")
        self.assertEqual(flats.cleanup(now=time.time()), 1)  # the listing goes, its detail page is too recent
        # A month later nothing was seen again: listings 1 and 3 go, then the detail pages of 2 and 3.
        self.assertEqual(flats.cleanup(now=time.time() + flats.KEEP_DETAILS + 1), 4)
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM listing_details').fetchone()[0], 0)

    def test_a_sudden_empty_page_after_a_stock_is_a_failure(self):
        flat_search = self.stored_search()
        self.run_check([self.card(n) for n in range(6)], flat_search)
        self.run_check([], flat_search)
        with store.db() as conn:
            status = conn.execute('SELECT error, count FROM source_status').fetchone()
        self.assertIn('plötzlich keine Angebote', status['error'])
        self.assertEqual(len(catalog.listings(flat_search)), 6)  # the stock stays visible

    def test_flat_searches_go_through_the_shared_worker_and_scheduler(self):
        from suchalarm import scheduler, worker

        flat_search = self.stored_search()
        with patch.object(flats.sources, 'fetch', new=AsyncMock(return_value=([self.card(1)], 1))):
            asyncio.run(worker.check(flat_search))
        self.assertEqual(len(catalog.listings(flat_search)), 1)
        self.assertEqual(store.searches('wohnung')[0]['id'], flat_search['id'])
        self.assertEqual(store.searches('preis'), [])
        job, _ = scheduler.plan(store.searches(), False)
        self.assertEqual(job['source'], 'immoscout24')


class BrowserExtractTests(unittest.TestCase):
    def test_card_extract_is_a_function_expression_with_the_selector(self):
        script = sources._extract_cards(sources.KLEINANZEIGEN_CARDS)
        self.assertTrue(script.startswith('() => {'))
        self.assertIn('"article[data-adid]"', script)
        self.assertIn(".join('\\n')", script)


class ApiAndMailTests(TempData):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from suchalarm.app import app

        self.client = TestClient(app)

    def create(self, **changes):
        payload = {
            'kind': 'wohnung',
            'query': 'Darmstadt',
            'max_price': 130000,
            'sources': ['immoscout24', 'nhw'],
            'filters': {'min_area': 40, 'min_rooms': 2, 'exclusions': ['WBS']},
            **changes,
        }
        response = self.client.post('/api/searches', json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return store.search(response.json()['id'])

    def card(self, number, **changes):
        return Listing.from_card(
            'immoscout24',
            {'url': f'https://www.immobilienscout24.de/expose/{number}', 'title': f'Wohnung {number}',
             'place': 'Darmstadt', 'rent': 1100, 'area': 60, 'rooms': 2, **changes},
        )  # fmt: skip

    def test_flat_searches_are_created_and_edited_through_the_common_endpoint(self):
        created = self.create()
        self.assertEqual((created['kind'], created['filters']['place']['kind']), ('wohnung', 'city'))
        self.assertEqual(
            self.create(query='64283')['filters']['place'], {'kind': 'plz', 'label': '64283', 'query': '64283'}
        )
        bad = self.client.post(
            '/api/searches', json={'kind': 'wohnung', 'query': 'X', 'max_price': 1, 'sources': ['nhw']}
        )
        self.assertEqual(bad.status_code, 422)
        product = self.client.post('/api/searches', json={'query': 'Spiel', 'max_price': 500, 'sources': ['ebay']})
        self.assertEqual(store.search(product.json()['id'])['kind'], 'preis')  # no kind: a product search as before
        edit = {'kind': 'wohnung', 'query': 'Darmstadt', 'max_price': 140000, 'sources': ['nhw']}
        self.assertEqual(self.client.put(f'/api/searches/{created["id"]}', json=edit).status_code, 200)
        self.assertEqual(store.search(created['id'])['max_price'], 140000)
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM more_pages').fetchone()[0], 0)  # NHW has no pages
        paged = self.create(sources=['immoscout24', 'nhw'])
        self.client.put(f'/api/searches/{paged["id"]}', json={**edit, 'sources': ['immoscout24', 'nhw']})
        with store.db() as conn:
            due = [tuple(r) for r in conn.execute('SELECT source, page, stock FROM more_pages')]
        self.assertEqual(due, [('immoscout24', 1, 1)])  # a higher rent limit: ImmoScout24 lists older flats
        wrong = self.client.put(f'/api/searches/{product.json()["id"]}', json=edit)
        self.assertEqual(wrong.status_code, 422)

    def test_state_lists_flats_and_the_watch_list_keeps_a_snapshot(self):
        flat_search = self.create()
        pages.save_result(flat_search, 'immoscout24', [self.card(1)], len([self.card(1)]), time.time())
        state = self.client.get('/api/state').json()
        row = state['listings'][0]
        self.assertEqual(
            (row['price'], row['status'], row['search_id'], row['kind']),
            (110000, 'unclear', flat_search['id'], 'wohnung'),
        )
        self.assertEqual(state['source_kinds']['nhw'], 'wohnung')
        self.assertEqual(state['sources']['kleinanzeigen-wohnung'], 'Kleinanzeigen')
        saved = self.client.post('/api/saved', json={'search_id': flat_search['id'], 'url': row['url'], 'saved': True})
        self.assertEqual(saved.status_code, 200)
        snapshot = self.client.get('/api/state').json()['saved'][0]
        self.assertEqual(
            (snapshot['price'], snapshot['detail'], snapshot['seller']), (110000, 'Darmstadt · 60 m² · 2 Zi.', '')
        )
        self.client.post('/api/hidden', json={'search_id': flat_search['id'], 'url': row['url'], 'hidden': True})
        self.assertEqual(self.client.get('/api/state').json()['listings'], [])

    def test_applying_keeps_the_flat_saved_and_the_template_is_stored(self):
        flat_search = self.create()
        pages.save_result(flat_search, 'immoscout24', [self.card(1)], len([self.card(1)]), time.time())
        url = self.card(1).url
        body = {'search_id': flat_search['id'], 'url': url, 'applied': True}
        self.assertEqual(self.client.post('/api/applied', json=body).status_code, 200)
        saved = self.client.get('/api/state').json()['saved']
        self.assertEqual((len(saved), saved[0]['url'], saved[0]['applied_at'] > 0), (1, url, True))
        self.client.post('/api/applied', json=dict(body, applied=False))
        self.assertIsNone(self.client.get('/api/state').json()['saved'][0]['applied_at'])  # still saved
        gone = self.client.post('/api/applied', json=dict(body, url='https://www.immobilienscout24.de/expose/9'))
        self.assertEqual(gone.status_code, 404)
        self.assertEqual(self.client.put('/api/template', json={'text': 'Hallo'}).status_code, 200)
        self.assertEqual(self.client.get('/api/state').json()['template'], 'Hallo')

    def test_the_starting_stock_gets_its_detail_page_on_request(self):
        flat_search = self.create()
        pages.save_result(
            flat_search, 'immoscout24', [self.card(1)], len([self.card(1)]), time.time()
        )  # the starting stock
        row = self.client.get('/api/state').json()['listings'][0]
        self.assertEqual(row['details'], 'available')
        self.assertEqual(self.client.post('/api/details', json={'url': row['url']}).status_code, 200)
        self.assertEqual(self.client.get('/api/state').json()['listings'][0]['details'], 'pending')
        with store.db() as conn:
            conn.execute("UPDATE listing_details SET state='done', body='{}'")
        self.client.post('/api/details', json={'url': row['url']})
        self.assertEqual(self.client.get('/api/state').json()['listings'][0]['details'], 'done')  # read once
        missing = self.client.post('/api/details', json={'url': 'https://www.immobilienscout24.de/expose/9'})
        self.assertEqual(missing.status_code, 404)

    def test_a_picked_place_keeps_its_geopath_while_the_field_shows_it(self):
        pick = {'kind': 'city', 'label': 'Darmstadt', 'query': 'Darmstadt', 'geopath': '/de/hessen/darmstadt'}
        self.assertEqual(self.create(picked=pick)['filters']['place']['geopath'], '/de/hessen/darmstadt')
        typed = self.create(query='Darmstadt-Eberstadt', picked=pick)  # changed after picking
        self.assertNotIn('geopath', typed['filters']['place'])
        payload = {'kind': 'wohnung', 'query': 'Darmstadt', 'max_price': 1, 'sources': ['nhw']}
        bad = self.client.post('/api/searches', json=dict(payload, picked=dict(pick, geopath='https://evil')))
        self.assertEqual(bad.status_code, 422)

    def test_place_suggestions_are_cached_and_spaced(self):
        entries = [
            {'entity': {'type': 'city', 'label': 'Darmstadt', 'geopath': {'uri': '/de/hessen/darmstadt?x=1'}}},
            {'entity': {'type': 'postcode', 'label': '64283 Darmstadt - Bessungen'}},
            {'entity': {'type': 'postcode', 'label': '64283 Darmstadt - Mitte'}},
            {'entity': {'type': 'quarter', 'label': 'Bessungen'}},
        ]
        sources._suggestions.clear()
        with patch.object(sources.http, '_download', return_value=json.dumps(entries).encode()) as download:
            first = self.client.get('/api/places', params={'q': 'Darm'}).json()
            self.client.get('/api/places', params={'q': ' darm '})
            self.assertEqual(
                self.client.get('/api/places', params={'q': 'Darmst'}).json(), []
            )  # spaced: ask again later
        self.assertEqual(
            [(x['kind'], x['label'], x['query'], x['geopath']) for x in first],
            [('city', 'Darmstadt', 'Darmstadt', '/de/hessen/darmstadt'), ('plz', '64283 Darmstadt', '64283', None)],
        )
        self.assertEqual(download.call_count, 1)
        self.assertEqual(self.client.get('/api/places', params={'q': 'Da'}).json(), [])

    def test_flat_snapshots_from_before_get_their_own_columns(self):
        flat_search = self.create()
        with store.db() as conn:
            conn.execute(
                'INSERT INTO saved(url,search_id,source,title,price,shipping,seller,condition,saved_at) '
                "VALUES ('u',?,'immoscout24','W',100000,NULL,'Darmstadt · 60 m²','geschätzt',0)",
                (flat_search['id'],),
            )
            conn.execute('PRAGMA user_version=0')  # every migration runs again
        store.init()
        with store.db() as conn:
            row = dict(conn.execute('SELECT seller, condition, detail, note FROM saved').fetchone())
        self.assertEqual(row, {'seller': '', 'condition': '', 'detail': 'Darmstadt · 60 m²', 'note': 'geschätzt'})

    def test_a_flat_without_rent_is_never_given_away_in_the_mail(self):
        from suchalarm import mailer

        search = {'kind': 'wohnung', 'query': 'Darmstadt', 'max_price': 130000}
        flat = {'kind': 'wohnung', 'title': 'Wohnung', 'price': None, 'estimated': False, 'place': 'Darmstadt',
                'area': 50, 'rooms': 2, 'source': 'immowelt', 'url': 'https://www.immowelt.de/expose/x'}  # fmt: skip
        self.assertEqual(mailer.entry_price((search, flat)), 'Miete offen')
        self.assertEqual(mailer.shipping_text((search, flat)), 'Miete im Inserat')
        message = mailer.compose_digest([(search, flat)], ['a@example.com'], 'b')
        self.assertEqual(message['Subject'], 'Wohnung: Wohnung')
        self.assertNotIn('verschenken', message.get_body(('plain',)).get_content())
        self.assertEqual(mailer.entry_price((search, dict(flat, price=101500, estimated=True))), '1.015,00 €')

    def test_clearing_starts_flat_searches_over_silently(self):
        flat_search = self.create()
        pages.save_result(flat_search, 'immoscout24', [self.card(1)], len([self.card(1)]), time.time())
        self.assertEqual(self.client.delete('/api/offers').status_code, 200)
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_status').fetchone()[0], 0)
        pages.save_result(
            flat_search, 'immoscout24', [self.card(1), self.card(2)], len([self.card(1), self.card(2)]), time.time()
        )
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM listing_details').fetchone()[0], 0)  # the stock again

    def test_new_fitting_flats_are_mailed_once_their_check_is_complete(self):
        from suchalarm import mailer, worker

        flat_search = self.create(sources=['immoscout24'], filters={'min_area': 40, 'min_rooms': 2, 'exclusions': []})
        with store.db() as conn:
            store.put_setting(conn, 'recipients', json.dumps(['a@example.com']))
            store.put_setting(conn, 'mail_enabled:wohnung', '1')
        pages.save_result(
            flat_search, 'immoscout24', [self.card(1)], len([self.card(1)]), time.time()
        )  # the starting stock
        pages.save_result(
            flat_search,
            'immoscout24',
            [self.card(1), self.card(2), self.card(3, area=20)],
            len([self.card(1), self.card(2), self.card(3, area=20)]),
            time.time(),
        )
        outbox.queue_mails()
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)  # detail pages pending
            conn.execute("UPDATE listing_details SET state='done', body=?", (json.dumps({'text': 'ruhig'}),))
        outbox.queue_mails()
        with store.db() as conn:
            self.assertEqual([r[0][-1] for r in conn.execute('SELECT url FROM outbox')], ['2'])  # 3 is too small
        with (
            patch.dict(os.environ, {'SMTP_HOST': 'mail', 'SMTP_FROM': 'bot@example.com'}),
            patch.object(mailer, 'deliver') as deliver,
        ):
            asyncio.run(worker.send_digest())
        message = deliver.call_args[0][0]
        self.assertEqual(message['Subject'], 'Wohnung: Wohnung 2 für 1.100,00 € warm')
        html = message.get_body(('html',)).get_content()
        self.assertIn('neue Wohnung', html)
        self.assertIn('Inserat öffnen', html)
        outbox.queue_mails()
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)  # never twice

    def test_a_clear_match_does_not_wait_for_a_paused_detail_page_forever(self):
        flat_search = self.create(sources=['immoscout24'], filters={'min_area': 40, 'min_rooms': 2, 'exclusions': []})
        with store.db() as conn:
            store.put_setting(conn, 'recipients', json.dumps(['a@example.com']))
            store.put_setting(conn, 'mail_enabled:wohnung', '1')
        pages.save_result(
            flat_search, 'immoscout24', [self.card(1)], len([self.card(1)]), time.time()
        )  # the starting stock
        pages.save_result(
            flat_search, 'immoscout24', [self.card(1), self.card(2)], len([self.card(1), self.card(2)]), time.time()
        )
        outbox.queue_mails()
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)  # detail page pending
        outbox.queue_mails(time.time() + outbox.DETAIL_WAIT + 1)
        with store.db() as conn:
            self.assertEqual([r[0][-1] for r in conn.execute('SELECT url FROM outbox')], ['2'])

    def queued(self):
        with store.db() as conn:
            return sorted(r[0][-1] for r in conn.execute('SELECT url FROM outbox'))

    def test_a_known_flat_is_mailed_again_once_its_rent_falls(self):
        flat_search = self.create(sources=['immoscout24'], filters={'min_area': 40, 'min_rooms': 2, 'exclusions': []})
        with store.db() as conn:
            store.put_setting(conn, 'recipients', json.dumps(['a@example.com']))
            store.put_setting(conn, 'mail_enabled:wohnung', '1')
        dear = [self.card(1, rent=1400), self.card(2)]
        pages.save_result(
            flat_search, 'immoscout24', dear[:1], len(dear[:1]), time.time() - 60
        )  # the starting stock, too dear
        pages.save_result(flat_search, 'immoscout24', dear, len(dear), time.time() - 60)
        with store.db() as conn:
            conn.execute("UPDATE listing_details SET state='done', body='{}'")
        outbox.queue_mails()
        self.assertEqual(self.queued(), ['2'])
        with store.db() as conn:  # the mail went out
            conn.execute('INSERT INTO notifications SELECT search_id, url, price, ? FROM outbox', (time.time() - 30,))
            conn.execute('DELETE FROM outbox')
        pages.save_result(flat_search, 'immoscout24', dear, len(dear), time.time())
        outbox.queue_mails()
        self.assertEqual(self.queued(), [])  # the same rent: nothing new
        pages.save_result(
            flat_search,
            'immoscout24',
            [self.card(1, rent=1200), self.card(2, rent=1000)],
            len([self.card(1, rent=1200), self.card(2, rent=1000)]),
            time.time(),
        )
        outbox.queue_mails()
        self.assertEqual(self.queued(), [])  # their detail pages are read first
        with store.db() as conn:
            conn.execute("UPDATE listing_details SET state='done', body='{}'")
        outbox.queue_mails()
        self.assertEqual(self.queued(), ['1', '2'])  # 1 now fits, 2 is cheaper than mailed


def duplicates_close(first, second):
    from suchalarm.wohnung import duplicates

    return duplicates.same_photo({'photo_hash': first}, {'photo_hash': second})


class DuplicateTests(unittest.TestCase):
    def flat(self, url, source, seen, **facts):
        values = {'area': 62.0, 'rooms': 2.0, 'rent': 1000.0, 'cold_rent': 800.0, 'available_from': '2026-11-01',
                  'place': 'Heinrichstraße 12, 64283 Darmstadt', 'lat': None, 'lon': None}  # fmt: skip
        return {'url': url, 'source': source, 'first_seen': seen, **values, **facts}

    def test_the_same_flat_on_two_portals_is_one_group(self):
        from suchalarm.wohnung import duplicates

        a = self.flat('a', 'immoscout24', 100)
        b = self.flat('b', 'immowelt', 200, place='Heinrichstr. 12, 64283 Darmstadt', area=62.5, cold_rent=810.0)
        self.assertEqual(duplicates.groups([b, a]), {'a': 'a', 'b': 'a'})

    def test_an_immobilien_de_copy_of_an_ohne_makler_offer_is_one_flat(self):
        from suchalarm.wohnung import duplicates

        # Many immobilien.de offers come from ohne-makler.net; both detail pages name the street.
        a = self.flat('a', 'ohne-makler', 100, place='Musterstraße 21, 64283 Darmstadt', rent=960.0, cold_rent=755.0)
        b = self.flat('b', 'immobilien-de', 200, place='Musterstraße 21, 64283 Darmstadt', rent=960.0, cold_rent=755.0)
        self.assertEqual(duplicates.groups([a, b]), {'a': 'a', 'b': 'a'})

    def test_an_identical_photo_counts_as_the_same_place(self):
        from suchalarm.wohnung import duplicates

        a = self.flat('a', 'immoscout24', 100, place='Bessungen', photo_hash='f0f0f0f0f0f0f0f0')
        b = self.flat('b', 'kleinanzeigen-wohnung', 200, place='64285 Darmstadt', photo_hash='f0f0f0f0f0f0f0f1')
        self.assertEqual(duplicates.groups([a, b]), {'a': 'a', 'b': 'a'})
        other = dict(b, photo_hash='0f0f0f0f0f0f0f0f')  # another photo
        self.assertEqual(duplicates.groups([a, other]), {})
        self.assertEqual(duplicates.groups([a, dict(b, area=70.0)]), {})  # the photo alone is not enough

    def test_a_landlords_stock_picture_proves_nothing(self):
        from suchalarm.wohnung import duplicates

        photo = {'photo_hash': 'f0f0f0f0f0f0f0f0'}
        a = self.flat('a', 'immoscout24', 100, place='Darmstadt', **photo)
        b = self.flat('b', 'immowelt', 200, place='Darmstadt', **photo)
        self.assertEqual(duplicates.groups([a, b]), {'a': 'a', 'b': 'a'})
        # Vonovia: the same picture on a second flat of the same portal, or on a flat with other facts.
        same_portal = self.flat('c', 'immoscout24', 10_000_000, place='Darmstadt', **photo)
        other_flat = self.flat('c', 'wg-gesucht', 300, place='Darmstadt', area=48.0, **photo)
        for third in (same_portal, other_flat):
            self.assertEqual(duplicates.groups([a, b, third]), {}, third['source'])
        # Different postcodes contradict the photo.
        apart = dict(b, place='64289 Darmstadt')
        self.assertEqual(duplicates.groups([dict(a, place='64283 Darmstadt'), apart]), {})
        # A street and house number still link flats with a stock picture.
        street = {'place': 'Heinrichstraße 12, 64283 Darmstadt'}
        self.assertEqual(duplicates.groups([a | street, b | street, other_flat]), {'a': 'a', 'b': 'a'})

    def test_photo_fingerprints_survive_resizing_and_skip_placeholders(self):
        import io

        from PIL import Image

        from suchalarm.core import images

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'DATA_DIR': folder}):
            images.root().mkdir()
            picture = Image.new('RGB', (480, 360))
            picture.putdata([((x * 7) % 256, (y * 3) % 256, (x + y) % 256) for y in range(360) for x in range(480)])
            names = {}
            for label, size, quality in (('big', (480, 360), 90), ('small', (240, 180), 60)):
                out = io.BytesIO()
                picture.resize(size).save(out, 'WEBP', quality=quality)
                names[label] = f'{label}.webp'
                (images.root() / names[label]).write_bytes(out.getvalue())
            Image.new('RGB', (320, 240), (200, 200, 200)).save(images.root() / 'grey.webp')
            big, small = images._fingerprint('big.webp'), images._fingerprint('small.webp')
            self.assertTrue(duplicates_close(big, small))
            self.assertIsNone(images._fingerprint('grey.webp'))  # a uniform placeholder proves nothing
            self.assertIsNone(images._fingerprint('missing.webp'))
            # One portal shows the 3:2 original, the other its centred 4:3 crop.
            import math

            wide = Image.new('L', (540, 360))
            wide.putdata([int(128 + 90 * math.sin(x / 37) * math.cos(y / 23) + 30 * math.sin((x + y) / 61))
                          for y in range(360) for x in range(540)])  # fmt: skip
            wide.save(images.root() / 'wide.webp', quality=90)
            wide.crop((30, 0, 510, 360)).resize((480, 360)).save(images.root() / 'crop.webp', quality=70)
            self.assertTrue(duplicates_close(images._fingerprint('wide.webp'), images._fingerprint('crop.webp')))
            images._fingerprint.cache_clear()

    def test_doubt_keeps_flats_apart(self):
        from suchalarm.wohnung import duplicates

        a = self.flat('a', 'immoscout24', 100)
        for other in (
            self.flat('b', 'immoscout24', 200),  # same portal: two flats
            self.flat('b', 'immowelt', 100 + duplicates.ADDRESS_WINDOW + 1),  # too far apart
            self.flat('b', 'immowelt', 200, area=70.0),
            self.flat('b', 'immowelt', 200, cold_rent=900.0),
            self.flat('b', 'immowelt', 200, place='Darmstadt'),  # no house: no evidence
            self.flat('b', 'immowelt', 200, available_from='2027-01-01'),
            self.flat('b', 'immowelt', 200, area=None),
        ):
            self.assertEqual(duplicates.groups([a, other]), {}, other)

    def test_the_address_links_flats_listed_days_apart_the_photo_only_hours(self):
        from suchalarm.wohnung import duplicates

        a = self.flat('a', 'immoscout24', 100)
        later = self.flat('b', 'immowelt', 100 + 4 * 86400)
        self.assertEqual(duplicates.groups([a, later]), {'a': 'a', 'b': 'a'})
        photo = {'place': 'Darmstadt', 'photo_hash': 'f0f0f0f0f0f0f0f0'}
        self.assertEqual(duplicates.groups([a | photo, later | photo]), {})
        # A portal listing the same flat again days later is no new build: the twin still links.
        repost = self.flat('c', 'immoscout24', 100 + 2 * 86400)
        self.assertEqual(duplicates.groups([a, repost, later]), {'a': 'a', 'b': 'a'})

    def test_half_rooms_and_cold_against_warm_rent_agree_at_one_address(self):
        from suchalarm.wohnung import duplicates

        a = self.flat('a', 'immoscout24', 100, rooms=2.5, rent=None, cold_rent=800.0)
        b = self.flat('b', 'kleinanzeigen-wohnung', 200, rent=1010.0, cold_rent=None)
        self.assertEqual(duplicates.groups([a, b]), {'a': 'a', 'b': 'a'})
        for other in (dict(b, rent=1400.0), dict(b, rent=700.0), dict(b, rooms=3.5)):
            self.assertEqual(duplicates.groups([a, other]), {}, other)
        # Without the address the photo needs comparable rents.
        photo = {'place': 'Darmstadt', 'photo_hash': 'f0f0f0f0f0f0f0f0'}
        self.assertEqual(duplicates.groups([a | photo, b | photo]), {})

    def test_listings_the_user_separated_stay_apart(self):
        from suchalarm.wohnung import duplicates

        a, b = self.flat('a', 'immoscout24', 100), self.flat('b', 'immowelt', 200)
        c = self.flat('c', 'wg-gesucht', 300)
        self.assertEqual(duplicates.groups([a, b, c], {frozenset(('a', 'b'))}), {'a': 'a', 'c': 'a'})

    def test_identical_units_of_a_new_build_are_never_linked(self):
        from suchalarm.wohnung import duplicates

        first, second = self.flat('a', 'immoscout24', 100), self.flat('b', 'immoscout24', 110)
        landlord = self.flat('c', 'bauverein', 120)
        self.assertEqual(duplicates.groups([first, second, landlord]), {})

    def test_exact_spot_counts_only_with_a_house_number(self):
        from suchalarm.wohnung import duplicates

        a = self.flat('a', 'nhw', 100, place='Eichbergstraße 12, Darmstadt', lat=49.864, lon=8.649)
        b = self.flat('b', 'immoscout24', 200, place='Eichberg Str 12a, 64285 Darmstadt', lat=49.8641, lon=8.6491)
        self.assertEqual(duplicates.groups([a, b]), {'a': 'a', 'b': 'a'})
        self.assertEqual(duplicates.street_key('Darmstadt, Eichbergstraße 12'), 'eichbergstr12|')  # NHW: town first
        self.assertEqual(duplicates.street_key('64285 Darmstadt, Bessungen'), None)
        # A postcode only one side names does not contradict the street.
        nhw = self.flat('a', 'nhw', 100, place='Darmstadt, Heinrichstraße 12')
        self.assertEqual(duplicates.groups([nhw, self.flat('b', 'immoscout24', 4 * 86400)]), {'a': 'a', 'b': 'a'})
        other_code = self.flat('b', 'immoscout24', 200, place='Heinrichstraße 12, 64289 Darmstadt')
        self.assertEqual(duplicates.groups([self.flat('a', 'nhw', 100), other_code]), {})

    def test_the_landlords_own_title_links_town_and_district(self):
        from suchalarm.wohnung import duplicates

        title = 'Kurzfristig frei: 3-Zimmer-Wohnung mit Balkon'
        a = self.flat('a', 'nhw', 100, place='Darmstadt', title=title, area=65.68)
        b = self.flat('b', 'immoscout24', 5000, place='Bessungen', title=title + ' ', area=65.68)
        self.assertEqual(duplicates.groups([a, b]), {'a': 'a', 'b': 'a'})
        for other in (
            dict(b, title='3-Zimmer-Wohnung'),  # too short to tell
            dict(b, title='Kurzfristig frei: 3-Zimmer-Wohnung mit Garten'),
            dict(b, area=66.2),  # identical title, another unit
            dict(b, first_seen=100 + duplicates.WINDOW + 1),
            dict(b, place='64289 Darmstadt') | {'title': title},
        ):
            self.assertEqual(duplicates.groups([dict(a, place='64285 Darmstadt'), other]), {}, other)


class DuplicateMailTests(ApiAndMailTests):
    def test_a_flat_on_two_portals_is_listed_and_mailed_once(self):
        flat_search = self.create(
            sources=['immoscout24', 'immowelt'], filters={'min_area': 40, 'min_rooms': 2, 'exclusions': []}
        )
        with store.db() as conn:
            store.put_setting(conn, 'recipients', json.dumps(['a@example.com']))
            store.put_setting(conn, 'mail_enabled:wohnung', '1')
        place = {'place': 'Heinrichstraße 12, 64283 Darmstadt'}
        pages.save_result(flat_search, 'immoscout24', [], 0, time.time())
        pages.save_result(flat_search, 'immowelt', [], 0, time.time())
        pages.save_result(
            flat_search, 'immoscout24', [self.card(1, **place)], len([self.card(1, **place)]), time.time()
        )
        copy = Listing.from_card('immowelt', {'url': 'https://www.immowelt.de/expose/x', 'title': 'Wohnung zur Miete',
                                              'rent': 1100, 'area': 60, 'rooms': 2, **place})  # fmt: skip
        pages.save_result(flat_search, 'immowelt', [copy], len([copy]), time.time() + 60)
        rows = catalog.listings(flat_search)
        leader = next(r for r in rows if r['source'] == 'immoscout24')
        self.assertEqual((leader['also'], leader['same_as']), (['immowelt'], None))
        self.assertEqual(leader['twins'][0]['source'], 'immowelt')
        self.assertEqual(next(r for r in rows if r['source'] == 'immowelt')['same_as'], leader['url'])
        with store.db() as conn:
            conn.execute("UPDATE listing_details SET state='done', body='{}'")
        outbox.queue_mails()
        with store.db() as conn:
            self.assertEqual([r[0] for r in conn.execute('SELECT url FROM outbox')], [leader['url']])

    def test_a_copy_queued_before_its_twin_was_recognised_is_not_mailed(self):
        flat_search = self.create(
            sources=['immoscout24', 'immowelt'], filters={'min_area': 40, 'min_rooms': 2, 'exclusions': []}
        )
        with store.db() as conn:
            store.put_setting(conn, 'recipients', json.dumps(['a@example.com']))
            store.put_setting(conn, 'mail_enabled:wohnung', '1')
        place = {'place': 'Heinrichstraße 12, 64283 Darmstadt'}
        pages.save_result(flat_search, 'immoscout24', [], 0, time.time())
        pages.save_result(flat_search, 'immowelt', [], 0, time.time())
        pages.save_result(
            flat_search, 'immoscout24', [self.card(1, **place)], len([self.card(1, **place)]), time.time()
        )
        copy = Listing.from_card('immowelt', {'url': 'https://www.immowelt.de/expose/x', 'title': 'Wohnung zur Miete',
                                              'rent': 1100, 'area': 60, 'rooms': 2, **place})  # fmt: skip
        pages.save_result(flat_search, 'immowelt', [copy], len([copy]), time.time() + 60)
        with store.db() as conn:
            conn.execute("UPDATE listing_details SET state='done', body='{}'")
            for url in (copy.url, self.card(1).url):  # both queued while the twin was not known yet
                conn.execute(
                    'INSERT INTO outbox(search_id,url,price,created_at) VALUES (?,?,0,0)', (flat_search['id'], url)
                )
            entries = outbox.take_pending(conn)
        self.assertEqual([offer['url'] for _, offer, _ in entries], [self.card(1).url])

    def test_nicht_dieselbe_wohnung_separates_the_listings(self):
        flat_search = self.create(
            sources=['immoscout24', 'immowelt'], filters={'min_area': 40, 'min_rooms': 2, 'exclusions': []}
        )
        place = {'place': 'Heinrichstraße 12, 64283 Darmstadt'}
        pages.save_result(
            flat_search, 'immoscout24', [self.card(1, **place)], len([self.card(1, **place)]), time.time()
        )
        copy = Listing.from_card('immowelt', {'url': 'https://www.immowelt.de/expose/x', 'title': 'Wohnung zur Miete',
                                              'rent': 1100, 'area': 60, 'rooms': 2, **place})  # fmt: skip
        pages.save_result(flat_search, 'immowelt', [copy], len([copy]), time.time() + 60)
        leader = self.card(1).url
        self.assertEqual(next(r for r in catalog.listings(flat_search) if r['url'] == copy.url)['same_as'], leader)
        response = self.client.post('/api/separate', json={'search_id': flat_search['id'], 'url': leader})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(r['same_as'] is None and not r['also'] for r in catalog.listings(flat_search)))
        missing = self.client.post(
            '/api/separate', json={'search_id': flat_search['id'], 'url': 'https://nowhere.example'}
        )
        self.assertEqual(missing.status_code, 404)

    def test_the_hit_list_shows_the_twin_still_listed(self):
        flat_search = self.create(
            sources=['immoscout24', 'immowelt'], filters={'min_area': 40, 'min_rooms': 2, 'exclusions': []}
        )
        place = {'place': 'Heinrichstraße 12, 64283 Darmstadt'}
        old = time.time() - 3 * 86400
        pages.save_result(flat_search, 'immoscout24', [self.card(1, **place)], len([self.card(1, **place)]), old)
        copy = Listing.from_card('immowelt', {'url': 'https://www.immowelt.de/expose/x', 'title': 'Wohnung zur Miete',
                                              'rent': 1100, 'area': 60, 'rooms': 2, **place})  # fmt: skip
        pages.save_result(flat_search, 'immowelt', [copy], len([copy]), time.time())
        rows = {r['url']: r for r in catalog.listings(flat_search)}
        first, shown = rows[self.card(1).url], rows[copy.url]
        self.assertEqual((first['same_as'], first['copy']), (copy.url, False))  # gone from the portal
        self.assertEqual((shown['same_as'], shown['copy'], shown['also']), (None, True, ['immoscout24']))


class FlatMailCardTests(unittest.TestCase):
    def test_the_card_names_the_district_and_offers_every_portal(self):
        from suchalarm import mailer

        search = {'kind': 'wohnung', 'query': 'Darmstadt', 'max_price': 130000}
        flat = {'url': 'https://a.example/1', 'title': 'Helle Wohnung', 'place': 'Heinrichstraße 12, 64283 Darmstadt',
                'price': 98000, 'estimated': True, 'area': 62, 'rooms': 2, 'source': 'immoscout24',
                'twins': [{'source': 'immowelt', 'url': 'https://b.example/2', 'name': 'Immowelt'}]}  # fmt: skip
        message = mailer.compose_digest([(search, flat)], ['a@example.com'], 'b1')
        html = message.get_body(('html',)).get_content()
        for part in (
            '>Darmstadt<',
            'Heinrichstraße 12, 64283',
            '≈ 980,00 €',
            'Bei ImmoScout24 öffnen',
            'Bei Immowelt öffnen',
        ):
            self.assertIn(part, html)
        self.assertIn('Auch bei Immowelt: https://b.example/2', message.get_body(('plain',)).get_content())


if __name__ == '__main__':
    unittest.main()
