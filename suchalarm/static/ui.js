// Small shared pieces of the page: element lookup, icons, links, source names, new-since-last-visit and
// list pages. Read-only use of the app state (app.js).

import { DEFAULT_KIND, HITS, escapeHtml, flatPasses, isFresh, kindOf, raw } from './logic.js';
import { render, state } from './page.js';

export const $ = id => document.getElementById(id);
export const short = {
  ebay: 'EB',
  kleinanzeigen: 'KA',
  vinted: 'VI',
  idealo: 'ID',
  immoscout24: 'IS',
  'kleinanzeigen-wohnung': 'KA',
  'wg-gesucht': 'WG',
  immowelt: 'IW',
  'ohne-makler': 'OM',
  'immobilien-de': 'IM',
  bauverein: 'BV',
  nhw: 'NH',
};
export const sourceName = source => state.sources[source].split(' · ')[0];
export const refreshIcon = raw('<svg viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"/></svg>');
export const clock = ts =>
  new Intl.DateTimeFormat('de-DE', { hour: '2-digit', minute: '2-digit' }).format(new Date(ts * 1000));
// "New since your last visit", per device: offers found after the previous visit
// carry a green dot until they are opened. A reload or a short tab switch is still the same visit.
const SEEN_KEY = 'suchalarm.seen',
  VISIT_GAP = 10 * 60;
export const visit = (() => {
  let stored = {};
  try {
    stored = JSON.parse(localStorage.getItem(SEEN_KEY) || '{}') || {};
  } catch {}
  const now = Date.now() / 1000,
    opened = Array.isArray(stored.opened) ? stored.opened : [];
  if (stored.since && stored.left && now - stored.left < VISIT_GAP) return { since: stored.since, opened }; // same visit
  if (stored.left) return { since: stored.left, opened: [] }; // new visit: everything after the last one is new
  return { since: stored.since || now, opened: stored.since ? opened : [] }; // first visit on this device: nothing is new
})();
const saveVisit = left => {
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify({ since: visit.since, left, opened: visit.opened.slice(-500) }));
  } catch {}
};
saveVisit(null);
document.addEventListener('visibilitychange', () => saveVisit(document.hidden ? Date.now() / 1000 : null));
window.addEventListener('pagehide', () => saveVisit(Date.now() / 1000));
export const isNew = o => isFresh(o, visit.since, visit.opened); // a new search's first result is no news

export const isFlat = s => kindOf(s) === 'wohnung';
// A hit fits its search as the server judged it: flats by their verdict, offers by their price (fits).
export const fits = (o, s) => (isFlat(s) ? flatPasses(o) : Boolean(o.fits));
// A flat on several portals shows once, through its leading listing ("auch bei …").
export const pool = kind => state[HITS[kind]].filter(o => !o.same_as);
// The area of a source; a source the server does not list any more counts as a product source.
export const kindOfSource = source => state.source_kinds[source] || DEFAULT_KIND;
export const markOpened = o => {
  if (!visit.opened.includes(o.url)) {
    visit.opened.push(o.url);
    saveVisit(null);
    setTimeout(render); // the dot goes at once; after the click, so a link still opens from its row
  }
};
export const bookmarkIcon = raw(
  '<svg class="mark" viewBox="0 0 24 24" aria-label="Gemerkt"><path d="M6 3h12v18l-6-4-6 4z"/></svg>',
);

// What each area shows. The page asks the server every few seconds; a refresh that changes nothing leaves the
// area alone, so pictures are not decoded again, hover, focus and a selection stay. An area someone changed by
// hand since (a button disabled while its request runs, a row slid aside) is drawn anew; one changed on purpose
// says so with keepDrawn().
const drawn = new WeakMap(); // element -> { text: the markup it was given, dom: its markup right after }
export function setHtml(element, markup) {
  const text = Array.isArray(markup) ? markup.join('') : String(markup);
  const last = drawn.get(element);
  if (last && last.text === text && last.dom === element.innerHTML) return;
  element.innerHTML = text;
  drawn.set(element, { text, dom: element.innerHTML });
}
// NODE was changed on purpose (a picture that failed to load is gone): the areas around it keep it that way.
export function keepDrawn(node) {
  for (let element = node; element; element = element.parentElement) {
    const last = drawn.get(element);
    if (last) last.dom = element.innerHTML;
  }
}

// Long lists come in pages of 30.
const PAGE_SIZE = 30;
export const pages = { offers: 0, saved: 0 }; // the open page of each list
const chevron = '<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';
export function paged(key, list) {
  const count = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
  const page = (pages[key] = Math.min(pages[key], count - 1)); // the list may shrink under an open page
  setHtml(
    $(`${key}-pager`),
    count > 1
      ? `<button class="round-button" data-page="${key}:-1" aria-label="Vorherige Seite" ${page === 0 ? 'disabled' : ''}>${chevron}</button><span>${page * PAGE_SIZE + 1}–${Math.min((page + 1) * PAGE_SIZE, list.length)} von ${list.length}</span><button class="round-button next" data-page="${key}:1" aria-label="Nächste Seite" ${page === count - 1 ? 'disabled' : ''}>${chevron}</button>`
      : '',
  );
  return list.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
}
// Same origin (stored copies) or https; anything else falls back to the source tile. Both return escaped markup
// (raw), so a link goes into html`…` and plain templates alike without being escaped twice.
export function safeImage(url) {
  try {
    const parsed = new URL(url, document.baseURI);
    return parsed.origin === location.origin || parsed.protocol === 'https:' ? raw(escapeHtml(parsed.href)) : '';
  } catch {
    return '';
  }
}
export function safeLink(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' ? raw(escapeHtml(parsed.href)) : '#';
  } catch {
    return '#';
  }
}
