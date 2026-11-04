// One row for hits and the watch list, both kinds: picture, title, details, price; saving and hiding.

import { ago, euro, flatDetails, html, moveIn, raw } from './logic.js';
import { bookmarkIcon, fits, isFlat, isNew, redraw, safeImage, safeLink, short, sourceName } from './ui.js';
import { api, message, refresh, render, state } from './page.js';

export const rows = new Map(); // filled by offerRow for both lists; a swipe finds its row here
const popped = new Set(); // URLs whose green dot already popped in: a refresh redraws it without the animation
const tick = raw('<svg viewBox="0 0 24 24"><path d="m5 12 5 5 9-10"/></svg>');
const dayMonth = ts =>
  new Intl.DateTimeFormat('de-DE', { day: '2-digit', month: '2-digit' }).format(new Date(ts * 1000));

// The picture fills the tile; without one (or if it fails to load) the source's tile shows.
// An applied flat carries a green check on its picture.
function thumb(o, applied) {
  const src = o.image_url ? safeImage(o.image_url) : '';
  const picture = src && html`<img src="${src}" alt="" loading="lazy" decoding="async" /><em>${short[o.source]}</em>`;
  return html`<span class="thumb ${o.source}${src ? ' photo' : ''}" aria-hidden="true"
    ><b>${short[o.source]}</b>${picture}${applied && html`<i class="applied-check">${tick}</i>`}</span
  >`;
}
document.addEventListener(
  'error',
  e => {
    const img = e.target;
    if (img.tagName === 'IMG' && img.parentElement?.classList.contains('thumb')) {
      img.parentElement.classList.remove('photo');
      img.nextElementSibling?.remove();
      img.remove();
    }
  },
  true,
);
function note(o) {
  if (o.price === 0) return 'Zu verschenken';
  return o.shipping === null
    ? 'Versand unbekannt'
    : o.source === 'vinted'
      ? `Versand ab ${euro(o.shipping)}`
      : o.shipping === 0
        ? 'Versand frei'
        : o.with_shipping
          ? `inkl. ${euro(o.shipping)} Versand`
          : `+ ${euro(o.shipping)} Versand`;
}
// One row for hits and the watch list. A click opens the offer at the provider. Swiping right
// saves it (or takes it off the watch list); swiping left hides a hit (on the watch list: removes it).
const eyeOff = raw(
  '<svg viewBox="0 0 24 24"><path d="M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c5 0 9 4.5 10 7a12 12 0 0 1-3.2 4.3M6.6 6.6A12 12 0 0 0 2 12c1 2.5 5 7 10 7a9.6 9.6 0 0 0 4.4-1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>',
);
// What a flat row says under its price; a watch-list snapshot keeps "geschätzt" in its note.
function flatNote(o) {
  return o.price === null || o.price === undefined || o.price === 0 ? 'Miete fehlt' : 'warm';
}
const estimated = o => Boolean(o.estimated || o.note === 'geschätzt'); // the list shows it as "≈ 850 €" in grey
// When the portal published the flat; a search's first result without that date has no age worth showing.
function flatAge(o) {
  const published = o.published_at ? Date.parse(o.published_at) / 1000 : NaN;
  return Number.isFinite(published) ? ago(published) : o.start ? '' : ago(o.first_seen);
}
// Parts of a line, separated by " · "; empty ones are left out.
const line = parts => parts.filter(Boolean).reduce((out, part, i) => (i ? html`${out} · ${part}` : html`${part}`), '');

// What a row says at its foot: a small green source tag, then when and why; an applied flat still found says
// when it was applied for instead. Flats are read on the portals' first page: one that moved down stays a month
// and says when it was last seen.
function rowFoot(o, flat, gone, applied) {
  if (applied && !gone) return html`<span class="asset-foot applied">Beworben am ${dayMonth(applied)}</span>`;
  // Flats that do not clearly fit say why, so "Alle" explains itself.
  const why = [
    flat && o.also?.length && `auch bei ${o.also.map(sourceName).join(', ')}`,
    flat && o.reasons && o.status !== 'matched' && o.reasons.join(', '),
  ];
  const unseen = flat && o.last_seen && o.last_seen < Date.now() / 1000 - 86400;
  const when = flat ? [flatAge(o), moveIn(o.available_from)] : [ago(o.first_seen)];
  const text = gone
    ? html`<span class="stale">nicht mehr gefunden</span>`
    : unseen
      ? line([html`<span class="stale">zuletzt gesehen ${ago(o.last_seen)}</span>`, ...why])
      : line([...when, ...why]);
  return html`<span class="asset-foot"><b class="source-tag">${sourceName(o.source)}</b><span>${text}</span></span>`;
}

function rowPrice(o, flat, value) {
  if (flat) return value ? `${estimated(o) ? '≈ ' : ''}${euro(value)}` : '–';
  return (value ?? o.price) === 0 ? 'Gratis' : euro(value ?? o.price);
}

// The green dot pops in once; later refreshes draw it still.
function newDot(o, gone) {
  if (gone || !isNew(o)) return '';
  const pop = !popped.has(o.url);
  popped.add(o.url);
  return html`<i class="new-dot${pop ? ' pop' : ''}" role="img" aria-label="Neu seit deinem letzten Besuch"></i>`;
}

export function offerRow(o, s, gone = false, list = 'offers') {
  // The same offer can sit in both lists; each list keeps its own key so a swipe acts on the list it happened in.
  const flat = isFlat(s),
    key = `${list}|${o.search_id}|${o.url}`,
    snapshot = state.saved.find(x => x.url === o.url),
    saved = Boolean(snapshot),
    applied = snapshot?.applied_at;
  rows.set(key, { o, s, gone, saved, list });
  const detail = flat
    ? o.kind
      ? flatDetails(o)
      : o.detail
    : [o.seller, o.condition].filter(Boolean).join(' · ') || sourceName(o.source);
  const mark = applied ? html`<span class="badge applied">Beworben</span>` : saved && bookmarkIcon;
  const actions = html`<span class="swipe-action save" aria-hidden="true"
      >${bookmarkIcon}<span>${saved ? 'Entfernen' : 'Merken'}</span></span
    ><span class="swipe-action hide" aria-hidden="true"
      ><span>${list === 'saved' ? 'Entfernen' : 'Ausblenden'}</span>${list === 'saved' ? bookmarkIcon : eyeOff}</span
    >`;
  const main = html`<span class="asset-main"
    ><strong>${newDot(o, gone)}<span class="name">${o.title}</span>${mark}</strong
    ><span>${detail || sourceName(o.source)}</span></span
  >`;
  const value = html`<span class="asset-value${!gone && fits(o, s) ? '' : ' over'}"><strong${flat && estimated(o) ? raw(' class="estimate"') : ''}>${rowPrice(o, flat, flat ? o.price : o.compared)}</strong><span>${flat ? flatNote(o) : note(o)}</span></span>`;
  return html`<li class="swipe${saved ? ' saved' : ''}${list === 'saved' ? ' in-list' : ''}">
    ${actions}<a
      class="asset-row"
      href="${safeLink(o.url)}"
      rel="noopener noreferrer"
      draggable="false"
      data-offer="${key}"
      >${thumb(o, applied)}${main}${value}${rowFoot(o, flat, gone, applied)}</a
    >
  </li>`;
}
async function setSaved(o, saved) {
  try {
    await api('saved', { method: 'POST', body: JSON.stringify({ search_id: o.search_id, url: o.url, saved }) });
    await refresh();
    message(saved ? 'In deiner Merkliste gespeichert' : 'Aus der Merkliste entfernt');
  } catch (err) {
    message(err.message, true);
  }
}
async function setHidden(o, hidden) {
  try {
    await api('hidden', { method: 'POST', body: JSON.stringify({ search_id: o.search_id, url: o.url, hidden }) });
    await refresh();
    if (hidden) message('Angebot ausgeblendet', false, { label: 'Rückgängig', run: () => setHidden(o, false) });
  } catch (err) {
    message(err.message, true);
    redraw(); // the row slid out by hand: draw it back
    render();
  }
}
// Runs once the row has settled: hiding slides the row out and folds it away first.
export function swiped(key, li, row, toRight) {
  const entry = rows.get(key);
  if (!entry) return;
  if (entry.list === 'saved' || toRight) {
    settle(li, row, () => setSaved(entry.o, !entry.saved));
    return;
  }
  row.style.transform = 'translateX(-105%)';
  setTimeout(() => {
    li.style.height = `${li.offsetHeight}px`;
    requestAnimationFrame(() => li.classList.add('leaving'));
    setTimeout(() => setHidden(entry.o, true), 220);
  }, 200);
}
// The row glides back with the action colour still showing, then the action runs.
export function settle(li, row, then) {
  row.style.transform = '';
  setTimeout(() => {
    li.classList.remove('armed', 'to-right', 'to-left');
    then?.();
  }, 260);
}
