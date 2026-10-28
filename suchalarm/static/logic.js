// Pure helpers without DOM or app state: formatting, the price rules of a search and the swipe maths.
// An ES module: the page's modules and the Node tests import it.

// Formatting
export const euro = cents => new Intl.NumberFormat('de-DE', { style: 'currency', currency: 'EUR' }).format(cents / 100);
export const escapeHtml = text =>
  String(text ?? '').replace(
    /[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
export const thousands = n => new Intl.NumberFormat('de-DE').format(n);
export function ago(ts) {
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  if (m < 1) return 'gerade eben';
  if (m < 60) return `vor ${m} Min.`;
  if (m < 1440) return `vor ${Math.round(m / 60)} Std.`;
  const d = Math.round(m / 1440);
  return d === 1 ? 'vor 1 Tag' : `vor ${d} Tagen`;
}
export function doneText(found, hits, checked) {
  const offers = `${found} ${found === 1 ? 'Angebot' : 'Angebote'}`;
  return {
    found,
    hits,
    checked,
    text: `${found ? `${offers}, davon ${hits} Treffer` : offers} · geprüft ${ago(checked)}`,
  };
}
export const priceInput = cents => (cents ? (cents / 100).toFixed(2).replace(/\.00$/, '') : '');
export const splitWords = text =>
  (text || '')
    .split(',')
    .map(w => w.trim())
    .filter(Boolean);

// The price rules, as on the server (domain.with_shipping, compared_price, in_range).
// idealo shops always count with shipping; other offers as the search says.
export const withShipping = (offer, search) => offer.source === 'idealo' || Boolean(search.include_shipping);
// Vinted's buyer protection: 5 % of the item price plus 0,70 €, as on the server (preis/domain.py).
export const buyerFee = offer =>
  offer.source === 'vinted' && offer.price > 0 ? Math.floor((offer.price * 5 + 50) / 100) + 70 : 0;
export function comparison(offer, search) {
  const item = offer.price + (search.include_fee ? buyerFee(offer) : 0);
  if (!withShipping(offer, search)) return item;
  return offer.shipping === null ? null : item + offer.shipping;
}
// Given away (price 0) counts for a search that wants it, whatever its minimum price.
export const given = (price, search) => price === 0 && Boolean(search.free);
export function match(offer, search) {
  const value = comparison(offer, search);
  return value !== null && (value >= (search.min_price || 0) || given(value, search)) && value < search.max_price;
}
export const priceRange = s =>
  s.min_price ? `${euro(s.min_price)} – ${euro(s.max_price)}` : `Unter ${euro(s.max_price)}`;

// Areas: every search is a flat search ('wohnung') or a product search ('preis'); hits and the watch
// list belong to the area of their search.
export const AREAS = ['wohnung', 'preis'];
export const DEFAULT_KIND = 'preis'; // a search without kind is a product search, as on the server (kinds.py)
export const kindOf = search => (AREAS.includes(search?.kind) ? search.kind : DEFAULT_KIND);
// What the areas call their things; the layouts themselves ask isFlat (ui.js).
export const AREA_TEXT = {
  wohnung: {
    title: 'Wohnungen',
    one: 'Wohnung',
    many: 'Wohnungen',
    search: 'Wohnungssuche',
    noMatch: 'Noch keine passende Wohnung.',
  },
  preis: {
    title: 'Preise',
    one: 'Angebot',
    many: 'Angebote',
    search: 'Preissuche',
    noMatch: 'Noch kein Angebot unter deinem Höchstpreis.',
  },
};
// The key of each area's hit rows in /api/state (HITS in preis/worker.py, wohnung/worker.py).
export const HITS = { wohnung: 'listings', preis: 'offers' };
export function inArea(items, area, searches) {
  const kinds = new Map(searches.map(s => [s.id, kindOf(s)]));
  return items.filter(item => kinds.get(item.search_id) === area);
}
// New since the last visit: found after it, not the first result of a search, not opened yet.
export const isFresh = (item, since, opened) => !item.start && item.first_seen > since && !opened.includes(item.url);
export const newCount = (items, since, opened) =>
  new Set(items.filter(i => isFresh(i, since, opened)).map(i => i.url)).size;
export const areaUnit = (area, count) => (count === 1 ? AREA_TEXT[area].one : AREA_TEXT[area].many);

// Flats: the server judges each listing ("matched", "unclear", "excluded"); only excluded ones are out.
export const flatPasses = listing => listing.status !== 'excluded';
export const decimal = value => String(value).replace('.', ',');
// The district or town of a portal address: street parts (with a house number), postcodes and
// brackets go, the last remaining part stays ("Karlstraße 22, 64283 Darmstadt, Kapellplatzviertel").
export function district(place) {
  const parts = String(place || '')
    .split(',')
    .map(part =>
      part
        .replace(/\(.*?\)/g, '')
        .replace(/\b\d{5}\b/g, '')
        .trim(),
    )
    .filter(part => part && !/\d+\s*[a-z]?$/i.test(part));
  return parts.at(-1) || '';
}
export function flatDetails(listing) {
  return [
    district(listing.place),
    listing.area ? `${decimal(listing.area)} m²` : '',
    listing.rooms ? `${decimal(listing.rooms)} Zi.` : '',
  ]
    .filter(Boolean)
    .join(' · ');
}
// Move-in as the mail says it; a day already past means the flat is free now. TODAY is an ISO day.
export function moveIn(day, today = new Date().toISOString().slice(0, 10)) {
  if (!day) return '';
  return day <= today ? 'sofort frei' : `frei ab ${day.slice(8, 10)}.${day.slice(5, 7)}.${day.slice(0, 4)}`;
}
export const dateText = ts =>
  new Intl.DateTimeFormat('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' }).format(new Date(ts * 1000));
export const flatSummary = s => {
  const f = s.filters || {};
  return [
    `bis ${euro(s.max_price)} warm`,
    f.min_area ? `ab ${decimal(f.min_area)} m²` : '',
    f.min_rooms ? `ab ${decimal(f.min_rooms)} Zi.` : '',
    s.radius ? `${s.radius} km Umkreis` : 'nur im Ort',
  ]
    .filter(Boolean)
    .join(' · ');
};

// Swipe gestures: the rubber band past the limit and the trackpad's after-swing
// The map in a flat's sheet: the stored picture of the searched place shows its middle at
// MAP_ZOOM, slid just enough to keep the flat in view; a flat outside gets an arrow on the frame's edge.
export const MAP_ZOOM = 1.9,
  MAP_MARGIN = 0.1;
const HEADINGS = [
  'nördlich',
  'nordöstlich',
  'östlich',
  'südöstlich',
  'südlich',
  'südwestlich',
  'westlich',
  'nordwestlich',
];
// Position on the picture as a fraction of its width and height (Web Mercator).
export function mapPlace(map, lat, lon) {
  const size = 2 ** map.zoom * 256,
    sin = Math.sin((lat * Math.PI) / 180);
  const x = ((lon + 180) / 360) * size,
    y = (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size;
  return [(x - map.left) / map.width, (y - map.top) / map.height];
}
export function mapCoordinates(map, fx, fy) {
  const size = 2 ** map.zoom * 256;
  const lon = ((map.left + fx * map.width) / size) * 360 - 180;
  const lat = (Math.atan(Math.sinh(Math.PI * (1 - (2 * (map.top + fy * map.height)) / size))) * 180) / Math.PI;
  return [lat, lon];
}
export function kilometres(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const a =
    Math.sin(((lat2 - lat1) * rad) / 2) ** 2 +
    Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(((lon2 - lon1) * rad) / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(a));
}
// Where the frame sits on the picture (background size and position in %), where the dot goes in the
// frame (fractions), or the arrow toward a flat outside it. SPOT is {lat, lon} or null.
export function mapView(map, spot) {
  const [px, py] = spot ? mapPlace(map, spot.lat, spot.lon) : [-1, -1];
  const inside = Boolean(spot) && px >= 0 && px <= 1 && py >= 0 && py <= 1,
    view = 1 / MAP_ZOOM;
  const clamp = (centre, point) => {
    let start = centre - view / 2;
    if (inside) start = Math.min(Math.max(start, point - view + MAP_MARGIN * view), point - MAP_MARGIN * view);
    return Math.min(Math.max(start, 0), 1 - view);
  };
  const vx = clamp((map.centre_x - map.left) / map.width, px),
    vy = clamp((map.centre_y - map.top) / map.height, py);
  const x = (px - vx) / view,
    y = (py - vy) / view;
  const result = {
    size: MAP_ZOOM * 100,
    position: [(vx / (1 - view)) * 100, (vy / (1 - view)) * 100],
    dot: inside ? [x, y] : null,
    arrow: null,
  };
  if (spot && !inside) {
    const dx = (x - 0.5) * map.width,
      dy = (y - 0.5) * map.height;
    const reach = Math.min((0.5 * map.width) / Math.abs(dx || 1e-9), (0.5 * map.height) / Math.abs(dy || 1e-9));
    const angle = (Math.atan2(dy, dx) * 180) / Math.PI;
    const [lat, lon] = mapCoordinates(map, vx + view / 2, vy + view / 2);
    const km = kilometres(lat, lon, spot.lat, spot.lon);
    result.arrow = {
      left: 50 + ((dx * reach) / map.width) * 100,
      top: 50 + ((dy * reach) / map.height) * 100,
      angle,
      text: `Liegt etwa ${km < 10 ? decimal(km.toFixed(1)) : Math.round(km)} km ${HEADINGS[Math.round(((angle + 90 + 360) % 360) / 45) % 8]} der Kartenmitte`,
    };
  }
  return result;
}

export const SWIPE_MAX = 140,
  SWING_EVENTS = 10,
  SWING_SURE_MS = 150;
export function band(dx) {
  const size = Math.abs(dx);
  return Math.sign(dx) * (size < SWIPE_MAX ? size : SWIPE_MAX + (size - SWIPE_MAX) * 0.2);
}
// Index where the trailing after-swing starts, or -1 while it is not certain yet.
export function afterSwing(events) {
  const n = events.length,
    sign = Math.sign(events[n - 1].d);
  let i = n - 1;
  while (i > 0) {
    const a = Math.abs(events[i - 1].d),
      b = Math.abs(events[i].d);
    if (Math.sign(events[i - 1].d) !== sign || b > a + 0.5 || (a > 3 && b < a * 0.6)) break;
    i--;
  }
  const run = events.slice(i),
    first = Math.abs(run[0].d),
    last = Math.abs(run.at(-1).d);
  const steady = run.every((e, k) => !k || e.t - run[k - 1].t < 40); // one event per frame
  // The system's swing fades by a nearly constant factor per frame (integer deltas make single steps
  // jumpy, so the average over the run counts). Slowing fingers drop off faster and less evenly.
  const fade = run.length > 1 && last > 0 ? (last / first) ** (1 / (run.length - 1)) : 0;
  const sure = run.length >= SWING_EVENTS && run.at(-1).t - run[0].t >= SWING_SURE_MS; // same at 60 and 120 Hz
  return sure && first >= 3 && steady && fade >= 0.8 && fade <= 0.99 ? i : -1;
}
