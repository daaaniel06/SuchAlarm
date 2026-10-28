'use strict';
// Unit tests for the frontend's pure helpers: node --test tests/
const test = require('node:test');
const assert = require('node:assert/strict');
const logic = require('../suchalarm/static/logic.js');

const plain = text => text.replace(/ /g, ' ');

test('euro and thousands use German formatting', () => {
  assert.equal(plain(logic.euro(123456)), '1.234,56 €');
  assert.equal(plain(logic.euro(0)), '0,00 €');
  assert.equal(logic.thousands(104000), '104.000');
});

test('escapeHtml neutralises markup in offer titles', () => {
  assert.equal(
    logic.escapeHtml(`<img src=x onerror="a('b')">&`),
    '&lt;img src=x onerror=&quot;a(&#39;b&#39;)&quot;&gt;&amp;',
  );
  assert.equal(logic.escapeHtml(null), '');
});

test('comparison counts unknown shipping only without the shipping switch', () => {
  const offer = { price: 4066, shipping: null };
  assert.equal(logic.comparison(offer, { include_shipping: 0 }), 4066);
  assert.equal(logic.comparison(offer, { include_shipping: 1 }), null);
  assert.equal(logic.comparison({ price: 4066, shipping: 818 }, { include_shipping: 1 }), 4884);
});

test('Vinted counts its buyer protection when the search includes it', () => {
  const offer = { source: 'vinted', price: 4400, shipping: 249 };
  assert.equal(logic.buyerFee(offer), 290);
  assert.equal(logic.comparison(offer, { include_shipping: 0, include_fee: 1 }), 4690);
  assert.equal(logic.comparison(offer, { include_shipping: 1, include_fee: 1 }), 4939);
  assert.equal(logic.comparison(offer, { include_shipping: 0, include_fee: 0 }), 4400);
  assert.equal(logic.comparison({ ...offer, source: 'ebay' }, { include_shipping: 0, include_fee: 1 }), 4400);
});

test('idealo shops always count with shipping', () => {
  const search = { include_shipping: 0, min_price: 0, max_price: 3500 };
  assert.equal(logic.comparison({ source: 'idealo', price: 3000, shipping: 900 }, search), 3900);
  assert.equal(logic.comparison({ source: 'idealo', price: 3000, shipping: null }, search), null);
  assert.equal(logic.comparison({ source: 'ebay', price: 3000, shipping: 900 }, search), 3000);
  assert.equal(logic.match({ source: 'idealo', price: 3000, shipping: 900 }, search), false);
  assert.equal(logic.withShipping({ source: 'idealo' }, search), true);
});

test('match follows the price range of a search', () => {
  const search = { include_shipping: 1, min_price: 2000, max_price: 4500 };
  assert.equal(logic.match({ price: 4066, shipping: null }, search), false); // unknown shipping is never 0
  assert.equal(logic.match({ price: 4066, shipping: 818 }, search), false); // 48,84 € is over the limit
  assert.equal(logic.match({ price: 4066, shipping: 818 }, { ...search, max_price: 5000 }), true);
  assert.equal(logic.match({ price: 4500, shipping: 0 }, search), false); // strictly below the maximum
  assert.equal(logic.match({ price: 300, shipping: 0 }, search), false); // junk below the minimum
});

test('given-away offers pass the minimum only when the search wants them', () => {
  const search = { include_shipping: 0, min_price: 2000, max_price: 4500 };
  assert.equal(logic.match({ price: 0, shipping: null }, search), false);
  assert.equal(logic.match({ price: 0, shipping: null }, { ...search, free: true }), true);
});

test('priceRange and priceInput', () => {
  assert.equal(plain(logic.priceRange({ min_price: 0, max_price: 3500 })), 'Unter 35,00 €');
  assert.equal(plain(logic.priceRange({ min_price: 2000, max_price: 3500 })), '20,00 € – 35,00 €');
  assert.equal(logic.priceInput(3600), '36');
  assert.equal(logic.priceInput(3650), '36.50');
  assert.equal(logic.priceInput(0), '');
});

test('splitWords drops empty entries', () => {
  assert.deepEqual(logic.splitWords(' Hülle, ,nur Cover ,'), ['Hülle', 'nur Cover']);
  assert.deepEqual(logic.splitWords(''), []);
});

test('ago and doneText', t => {
  t.mock.method(Date, 'now', () => 1_000_000_000_000);
  const now = 1_000_000_000;
  assert.equal(logic.ago(now - 10), 'gerade eben');
  assert.equal(logic.ago(now - 5 * 60), 'vor 5 Min.');
  assert.equal(logic.ago(now - 3 * 3600), 'vor 3 Std.');
  assert.equal(logic.ago(now - 86400), 'vor 1 Tag');
  assert.equal(logic.ago(now - 3 * 86400), 'vor 3 Tagen');
  assert.equal(logic.doneText(12, 3, now).text, '12 Angebote, davon 3 Treffer · geprüft gerade eben');
  assert.equal(logic.doneText(0, 0, now).text, '0 Angebote · geprüft gerade eben');
  assert.equal(logic.doneText(1, 1, now).text, '1 Angebot, davon 1 Treffer · geprüft gerade eben');
});

test('band follows the finger up to the limit, then resists', () => {
  assert.equal(logic.band(100), 100);
  assert.equal(logic.band(logic.SWIPE_MAX + 100), logic.SWIPE_MAX + 20);
  assert.equal(logic.band(-(logic.SWIPE_MAX + 100)), -(logic.SWIPE_MAX + 20));
});

test('afterSwing finds a steady fading swing, not slowing fingers', () => {
  const frames = deltas => deltas.map((d, k) => ({ d, t: k * 16 }));
  // Fingers move at a steady speed, then the system swing fades by 0.9 per frame.
  const fingers = [8, 9, 8, 9, 8];
  const swing = Array.from({ length: 12 }, (_, k) => Math.round(40 * 0.9 ** k));
  assert.equal(logic.afterSwing(frames([...fingers, ...swing])), fingers.length);
  // Too short to be sure yet.
  assert.equal(logic.afterSwing(frames([...fingers, ...swing.slice(0, 5)])), -1);
  // Fingers braking hard (halving per frame) are no system swing.
  assert.equal(logic.afterSwing(frames([40, 20, 10, 5, 2, 1, 1, 1, 1, 1, 1, 1])), -1);
});

test('hits and the watch list belong to the area of their search', () => {
  const searches = [{ id: 1, kind: 'wohnung' }, { id: 2, kind: 'preis' }, { id: 3 }];
  const items = [{ search_id: 1 }, { search_id: 2 }, { search_id: 3 }, { search_id: 9 }];
  assert.deepEqual(logic.inArea(items, 'wohnung', searches), [{ search_id: 1 }]);
  assert.deepEqual(logic.inArea(items, 'preis', searches), [{ search_id: 2 }, { search_id: 3 }]); // no kind: a product search
  assert.equal(logic.areaUnit('wohnung', 7), 'Wohnungen');
  assert.equal(logic.areaUnit('preis', 1), 'Angebot');
});

test('new counts each listing once and skips the starting stock and opened ones', () => {
  const since = 100;
  const items = [
    { url: 'a', first_seen: 150 },
    { url: 'a', first_seen: 150 }, // the same flat found by two searches
    { url: 'b', first_seen: 50 },
    { url: 'c', first_seen: 160, start: true },
    { url: 'd', first_seen: 170 },
  ];
  assert.equal(logic.newCount(items, since, []), 2);
  assert.equal(logic.newCount(items, since, ['d']), 1);
});

test('a flat row names district, size and rooms', () => {
  const cases = [
    ['Karlstraße 22, 64283 Darmstadt, Kapellplatzviertel', 'Kapellplatzviertel'],
    ['Hügelstraße 73, Darmstadt, Darmstadt (64283)', 'Darmstadt'],
    ['Gerlachstraße 41, 14480 Potsdam', 'Potsdam'],
    ['Darmstadt, Eichbergstraße 12', 'Darmstadt'],
    ['Seeheimer Straße, Darmstadt', 'Darmstadt'],
    ['', ''],
  ];
  for (const [place, expected] of cases) assert.equal(logic.district(place), expected);
  assert.equal(logic.flatDetails({ place: '14480 Potsdam', area: 62.5, rooms: 2 }), 'Potsdam · 62,5 m² · 2 Zi.');
  assert.equal(logic.flatDetails({ place: '', area: null, rooms: 1.5 }), '1,5 Zi.');
  assert.equal(logic.flatPasses({ status: 'unclear' }), true);
  assert.equal(logic.flatPasses({ status: 'excluded' }), false);
  const summary = logic.flatSummary({ max_price: 130000, radius: 0, filters: { min_area: 40, min_rooms: 2 } });
  assert.equal(plain(summary), 'bis 1.300,00 € warm · ab 40 m² · ab 2 Zi. · nur im Ort');
});

test('move-in reads like the mail and a past day means free now', () => {
  assert.equal(logic.moveIn('2026-12-01', '2026-10-25'), 'frei ab 01.12.2026');
  assert.equal(logic.moveIn('2026-10-01', '2026-10-25'), 'sofort frei');
  assert.equal(logic.moveIn(null), '');
  assert.equal(logic.dateText(Date.UTC(2026, 9, 25, 12) / 1000), '25.10.2026');
});

test('the map keeps a flat in its frame and points to one outside', () => {
  // Darmstadt's stored picture (maps.info for 49.8728, 8.6512)
  const size = 2 ** 13 * 256,
    sin = Math.sin((49.8728 * Math.PI) / 180);
  const cx = ((8.6512 + 180) / 360) * size,
    cy = (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size;
  const map = { zoom: 13, left: cx - 1500, top: cy - 1100, width: 3000, height: 2500, centre_x: cx, centre_y: cy };
  const [fx, fy] = logic.mapPlace(map, 49.8728, 8.6512);
  assert.ok(Math.abs(fx - 0.5) < 1e-6 && Math.abs(fy - 1100 / 2500) < 1e-6);
  const centre = logic.mapView(map, { lat: 49.8728, lon: 8.6512 });
  assert.ok(Math.abs(centre.dot[0] - 0.5) < 1e-6);
  assert.equal(centre.arrow, null);
  const far = logic.mapView(map, { lat: 50.11, lon: 8.68 }); // Frankfurt, north
  assert.equal(far.dot, null);
  assert.match(far.arrow.text, /^Liegt etwa \d+ km nördlich der Kartenmitte$/);
  assert.equal(logic.mapView(map, null).dot, null);
  assert.ok(Math.abs(logic.kilometres(49.8728, 8.6512, 50.1109, 8.6821) - 26.6) < 0.5);
});
