// A flat's detail sheet and its application: a click on a flat opens the sheet
// instead of the portal. Applying happens on the portal; the page copies the text and remembers the day.

import { dateText, decimal, district, escapeHtml, euro, mapCoordinates, mapView, moveIn } from './logic.js';
import { $, bookmarkIcon, markOpened, safeImage, safeLink, short, sourceName } from './ui.js';
import { api, message, refresh, state } from './page.js';

let open = null; // { search_id, url } of the flat in the sheet
let copied = false; // the application text went to the portal: now the mark must not be forgotten
let shown = ''; // the sheet's markup: a refresh that changes nothing leaves the sheet alone
const spots = new Map(); // URL -> {lat, lon, approx}, 'pending' or 'none'; looked up once per page load

const closeIcon = '<svg viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg>';
const hideIcon =
  '<svg viewBox="0 0 24 24"><path d="M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c5 0 9 4.5 10 7a12 12 0 0 1-3.2 4.3M6.6 6.6A12 12 0 0 0 2 12c1 2.5 5 7 10 7a9.6 9.6 0 0 0 4.4-1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';

// The flat as it is now: the live listing, or the watch list's snapshot once the portal dropped it.
function current() {
  if (!open) return null;
  const saved = state.saved.find(x => x.url === open.url);
  const live = state.listings.find(o => o.url === open.url && o.search_id === open.search_id);
  if (!live && !saved) return null;
  return { o: live || { ...saved, first_seen: saved.saved_at }, saved, live: Boolean(live) };
}

export function openSheet(o) {
  open = { search_id: o.search_id, url: o.url };
  markOpened(o);
  renderSheet();
  showAtTop($('detail'));
}

// showModal focuses the first button (the bookmark under the photo) and scrolls the sheet down to it,
// which on a phone cuts off the top. The sheet takes the focus itself and starts at its top.
function showAtTop(sheet) {
  sheet.showModal();
  sheet.focus({ preventScroll: true });
  sheet.scrollTop = 0;
}

// Runs after every refresh: the detail page may have arrived, or the flat got saved elsewhere.
export function renderSheet() {
  if (!open) return;
  const flat = current();
  if (!flat) {
    $('detail').close();
    $('application').close();
    return;
  }
  if (flat.live) locate(flat.o);
  const html = detailHtml(flat) + mapHtml(flat);
  if (html !== shown) {
    $('detail-body').innerHTML = shown = html;
    placeMap(flat);
  }
  if ($('application').open) renderApplication(flat);
}

function rentLine(o) {
  if (!o.price) return ['–', 'Miete nicht angegeben'];
  if (o.estimated || o.note === 'geschätzt') return [euro(o.price), 'Geschätzt: Kaltmiete + ca. 3 €/m²'];
  return [euro(o.price), 'Warmmiete pro Monat'];
}

function detailHtml({ o, saved, live }) {
  const [price, priceNote] = rentLine(o),
    move = moveIn(o.available_from),
    image = o.image_url ? safeImage(o.image_url) : '';
  const facts = live
    ? `<dl class="sheet-facts"><div><dt>Fläche</dt><dd>${o.area ? `${decimal(o.area)} m²` : '–'}</dd></div><div><dt>Zimmer</dt><dd>${o.rooms ? decimal(o.rooms) : '–'}</dd></div><div><dt>Kalt</dt><dd>${o.cold_rent ? euro(o.cold_rent * 100) : '–'}</dd></div></dl>`
    : `<p class="sheet-title">${escapeHtml(o.detail || '')}</p>`;
  // "Not checked yet" is what the details note says already.
  const reasons = (o.status === 'matched' ? o.traits : o.reasons || []).filter(
    r => !['available', 'pending', 'failed'].includes(o.details) || r !== 'Beschreibung noch nicht geprüft',
  );
  const twins = (o.twins || [])
    .map(
      t =>
        `<a href="${safeLink(t.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(sourceName(t.source))}</a>`,
    )
    .join(' und ');
  const note =
    o.details === 'pending'
      ? '<div class="details-note"><p><b>Detailseite wird geladen</b>Dauert ein paar Minuten, damit das Portal nicht gedrängt wird.</p></div>'
      : o.details === 'available' || o.details === 'failed'
        ? `<div class="details-note"><p><b>${o.details === 'failed' ? 'Detailseite nicht lesbar' : 'Nur Angaben aus der Trefferliste'}</b>Warmmiete und Beschreibung stehen auf der Detailseite.</p><button class="pill compact" data-sheet="load">${o.details === 'failed' ? 'Erneut' : 'Laden'}</button></div>`
        : '';
  const place = district(o.place) || 'Wohnung';
  return `<div class="sheet-grip" aria-hidden="true"></div>${image ? `<img class="sheet-photo" src="${image}" alt="" decoding="async">` : ''}<div class="sheet-top"><span class="source-mark ${o.source}" aria-hidden="true">${short[o.source]}</span><span class="sheet-source">${escapeHtml(sourceName(o.source))}${live ? ` · ${escapeHtml(new Date(o.first_seen * 1000).toLocaleDateString('de-DE'))}` : ' · nicht mehr gefunden'}</span><span class="sheet-tools"><button class="round-button small tool${saved ? ' on' : ''}" data-sheet="save" aria-pressed="${Boolean(saved)}" aria-label="${saved ? 'Von der Merkliste nehmen' : 'Merken'}" title="${saved ? 'Gemerkt' : 'Merken'}">${bookmarkIcon}</button>${live ? `<button class="round-button small tool" data-sheet="hide" aria-label="Ausblenden" title="Ausblenden">${hideIcon}</button>` : ''}<button class="round-button small" data-sheet="close" aria-label="Schließen">${closeIcon}</button></span></div><h2 id="detail-title" tabindex="-1">${escapeHtml(place)}</h2><p class="sheet-address">${escapeHtml(o.place || '')}</p><p class="sheet-price${o.estimated || o.note === 'geschätzt' ? ' estimate' : ''}">${price}<span>${priceNote}${move ? ` · ${move}` : ''}</span></p>${facts}<p class="sheet-title">${escapeHtml(o.title)}</p>${reasons.length ? `<p class="sheet-reason${o.status === 'matched' ? '' : ' warn'}">${escapeHtml(reasons.join(' · '))}</p>` : ''}${twins ? `<p class="sheet-twins">Auch bei ${twins} <button class="text-button" data-sheet="separate">Nicht dieselbe Wohnung</button></p>` : ''}${saved?.applied_at ? `<p class="sheet-applied">Beworben am ${dateText(saved.applied_at)}</p>` : ''}${note}<div class="sheet-actions"><button class="pill primary" data-sheet="apply">${saved?.applied_at ? 'Bewerbung ansehen' : 'Bewerben'}</button><a class="pill" href="${safeLink(o.url)}" target="_blank" rel="noopener noreferrer">Inserat öffnen</a></div>`;
}

// The flat's position comes from the server: the portal's coordinates, or its address looked up once.
async function locate(o) {
  if (spots.has(o.url)) return;
  spots.set(o.url, 'pending');
  const path = `location?search_id=${o.search_id}&url=${encodeURIComponent(o.url)}`;
  for (let tries = 0; tries < 20 && spots.get(o.url) === 'pending'; tries++) {
    try {
      const found = await api(path);
      if (!found.wait) spots.set(o.url, found);
      else await new Promise(done => setTimeout(done, 1500)); // the OpenStreetMap budget asks to wait
    } catch {
      spots.set(o.url, 'none');
    }
  }
  if (spots.get(o.url) === 'pending') spots.set(o.url, 'none');
  if (open?.url === o.url) renderSheet();
}

// One stored picture of the searched place (the browser keeps it); the flat is a dot on it.
function mapHtml({ o, live }) {
  if (!live) return '';
  const map = state.maps?.[o.search_id];
  if (!map) return '<figure class="map-card"><figcaption role="status">Karte wird vorbereitet …</figcaption></figure>';
  const found = spots.get(o.url),
    spot = typeof found === 'object' ? found : null,
    view = mapView(map, spot);
  const [lat, lon] = spot ? [spot.lat, spot.lon] : mapCoordinates(map, 0.5, 0.5);
  const href = `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=${spot ? 16 : map.zoom}/${lat}/${lon}`;
  const caption =
    found === 'none'
      ? 'Position konnte nicht geladen werden. <button class="text-button" data-sheet="locate">Erneut versuchen</button>'
      : !spot
        ? 'Position wird ermittelt …'
        : view.arrow
          ? view.arrow.text
          : spot.approx
            ? 'Ungefähre Lage – das Inserat nennt keine genaue Adresse'
            : '';
  return `<figure class="map-card"><a class="map-view" href="${safeLink(href)}" target="_blank" rel="noopener noreferrer" aria-label="${spot ? 'Lage auf OpenStreetMap öffnen' : 'Suchbereich auf OpenStreetMap öffnen'}"><span class="map-image" aria-hidden="true"></span>${view.dot ? `<span class="map-pin${spot.approx ? ' approx' : ''}" aria-hidden="true"></span>` : ''}${view.arrow ? '<span class="map-arrow" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="M3 8h9M8.5 4 12.5 8l-4 4"/></svg></span>' : ''}<small class="map-credit">© OpenStreetMap</small></a>${caption ? `<figcaption role="status">${caption}</figcaption>` : ''}</figure>`;
}
// Sizes and positions are set here: the page's CSP allows no inline style attributes.
const EDGE = 22; // px from the frame's edge to the arrow's middle
function placeMap({ o, live }) {
  const map = live && state.maps?.[o.search_id],
    frame = $('detail-body').querySelector('.map-view');
  if (!map || !frame) return;
  const found = spots.get(o.url),
    view = mapView(map, typeof found === 'object' ? found : null);
  frame.style.aspectRatio = `${map.width} / ${map.height}`;
  const image = frame.querySelector('.map-image');
  // Absolute: a relative url() in a CSS variable resolves against the stylesheet, not the page.
  const picture = suffix => `url("${new URL(`api/maps/${map.key}${suffix}.webp`, document.baseURI).href}")`;
  image.style.setProperty('--map-light', picture(''));
  image.style.setProperty('--map-dark', picture('-dark'));
  image.style.backgroundSize = `${view.size}% ${view.size}%`;
  image.style.backgroundPosition = `${view.position[0]}% ${view.position[1]}%`;
  const pin = frame.querySelector('.map-pin');
  if (pin) Object.assign(pin.style, { left: `${view.dot[0] * 100}%`, top: `${view.dot[1] * 100}%` });
  const arrow = frame.querySelector('.map-arrow');
  if (arrow)
    Object.assign(arrow.style, {
      left: `clamp(${EDGE}px, ${view.arrow.left}%, calc(100% - ${EDGE}px))`,
      top: `clamp(${EDGE}px, ${view.arrow.top}%, calc(100% - ${EDGE}px))`,
      transform: `rotate(${view.arrow.angle}deg)`,
    });
}

function renderApplication({ o, saved }) {
  const applied = saved?.applied_at,
    remind = copied && !applied;
  $('application-flat').textContent = o.title;
  $('application-applied').hidden = !applied;
  $('application-applied').textContent = applied ? `Beworben am ${dateText(applied)}` : '';
  $('application-remind').hidden = !remind;
  $('application-open').classList.toggle('primary', !remind);
  $('application-mark').hidden = Boolean(applied) || remind;
  $('application-unmark').hidden = !applied;
  const empty = !$('application-text').value.trim();
  $('application-open').disabled = $('application-copy').disabled = $('application-template').disabled = empty;
}

async function act(path, body, done) {
  try {
    await api(path, { method: 'POST', body: JSON.stringify(body) });
    await refresh();
    if (done) message(done);
  } catch (err) {
    message(err.message, true);
  }
}

$('detail').addEventListener('click', async e => {
  const flat = current(),
    button = e.target.closest('[data-sheet]');
  if (e.target === $('detail')) $('detail').close(); // a click on the backdrop
  if (!flat || !button) return;
  const { o, saved } = flat,
    ref = { search_id: o.search_id, url: o.url };
  const what = button.dataset.sheet;
  if (what === 'close') $('detail').close();
  if (what === 'save')
    await act(
      'saved',
      { ...ref, saved: !saved },
      saved ? 'Aus der Merkliste entfernt' : 'In deiner Merkliste gespeichert',
    );
  if (what === 'hide') {
    $('detail').close();
    await act('hidden', { ...ref, hidden: true }, 'Angebot ausgeblendet');
  }
  if (what === 'load') await act('details', { url: o.url }, 'Detailseite wird geladen');
  if (what === 'separate') await act('separate', ref, 'Als eigene Wohnung getrennt');
  if (what === 'locate') {
    spots.delete(o.url);
    renderSheet();
  }
  if (what === 'apply') {
    copied = false;
    $('application-text').value = state.template || '';
    $('application-feedback').textContent =
      'Der Versand passiert im Portal: Text kopieren, Inserat öffnen, dort einfügen.';
    $('detail').close();
    renderApplication(flat);
    showAtTop($('application'));
  }
});
// On a phone the card is pulled down to close, like in other apps: at its start position it follows the
// finger down (the page would otherwise only bounce) and closes past a threshold, else springs back.
const PULL_CLOSE = 100;
let pull = null; // { y, dy, t } of a drag that began with the card at its start
function pullTo(dy, ease) {
  const card = $('detail-body');
  card.style.transition = ease ? 'transform 0.22s ease-out' : '';
  card.style.transform = dy ? `translateY(${dy}px)` : '';
}
$('detail').addEventListener(
  'touchstart',
  e => {
    pull =
      $('detail').scrollTop <= 0 && e.touches.length === 1 ? { y: e.touches[0].clientY, dy: 0, t: e.timeStamp } : null;
  },
  { passive: true },
);
$('detail').addEventListener(
  'touchmove',
  e => {
    if (!pull) return;
    const dy = e.touches[0].clientY - pull.y;
    if (dy <= 0 && !pull.dy) {
      pull = null; // scrolling up: the sheet slides up as usual
      return;
    }
    e.preventDefault();
    pull.dy = Math.max(0, dy);
    pullTo(pull.dy, false);
  },
  { passive: false },
);
function release() {
  if (!pull) return;
  const { dy, t } = pull;
  pull = null;
  const flick = dy > 40 && dy / Math.max(1, performance.now() - t) > 0.6;
  if (dy > PULL_CLOSE || flick) {
    pullTo(window.innerHeight, true);
    setTimeout(() => $('detail').close(), 200);
  } else pullTo(0, true);
}
$('detail').addEventListener('touchend', release);
$('detail').addEventListener('touchcancel', release);
// A photo that fails to load leaves no empty frame.
$('detail').addEventListener('error', e => e.target.classList?.contains('sheet-photo') && e.target.remove(), true);
$('detail').addEventListener('close', () => {
  pullTo(0, false);
  if (!$('application').open) open = null;
});
$('application').addEventListener('close', () => {
  open = null;
});

// Copying needs a click: the portal opens first, synchronously, so no popup blocker steps in.
async function copy(openPortal) {
  const flat = current(),
    text = $('application-text').value;
  if (!flat || !text.trim()) return;
  if (openPortal && flat.o.url.startsWith('https://')) window.open(flat.o.url, '_blank', 'noopener,noreferrer');
  try {
    await navigator.clipboard.writeText(text);
    $('application-feedback').textContent = openPortal
      ? 'Kopiert. Im Portal einfügen und absenden.'
      : 'Bewerbungstext kopiert.';
  } catch {
    $('application-text').select();
    $('application-feedback').textContent = 'Bitte den markierten Text manuell kopieren (Strg/Cmd+C).';
  }
  copied = true;
  renderApplication(flat);
}
async function mark(applied) {
  const flat = current();
  if (!flat) return;
  await act('applied', { search_id: flat.o.search_id, url: flat.o.url, applied });
  $('application-feedback').textContent = applied ? 'Als beworben markiert und gemerkt.' : 'Markierung entfernt.';
}
$('application-open').addEventListener('click', () => copy(true));
$('application-copy').addEventListener('click', () => copy(false));
$('application-mark').addEventListener('click', () => mark(true));
$('application-remind-mark').addEventListener('click', () => mark(true));
$('application-unmark').addEventListener('click', () => mark(false));
$('application-template').addEventListener('click', async () => {
  try {
    await api('template', { method: 'PUT', body: JSON.stringify({ text: $('application-text').value }) });
    await refresh();
    $('application-feedback').textContent = 'Als Vorlage gespeichert.';
  } catch (err) {
    message(err.message, true);
  }
});
$('application-text').addEventListener('input', () => {
  const flat = current();
  if (flat) renderApplication(flat);
});
