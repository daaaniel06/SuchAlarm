// The searches page and what describes a search (name, category, delivery, tags).

import { AREA_TEXT, escapeHtml, euro, flatSummary, priceRange, splitWords, thousands } from './logic.js';
import { $, isFlat, refreshIcon, safeLink, sourceName } from './ui.js';
import { pill, sourceStep } from './steps.js';
import { area, state } from './page.js';

// Categories: one icon each, drawn like the navigation icons.
const CATEGORY_ICONS = {
  videospiele:
    '<path d="M6 11h4M8 9v4M15 12h.01M18 10h.01"/><path d="M17.3 5H6.7a4 4 0 0 0-3.9 3.2l-1 5.2A3 3 0 0 0 6.7 16l1.8-2h7l1.8 2a3 3 0 0 0 4.9-2.6l-1-5.2A4 4 0 0 0 17.3 5Z"/>',
  elektronik: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M2 20h20"/>',
  moebel:
    '<path d="M4 11V8a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v3"/><path d="M2 13a2 2 0 0 1 4 0v2h12v-2a2 2 0 0 1 4 0v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2Z"/><path d="M5 19v2M19 19v2"/>',
  fahrraeder:
    '<circle cx="5.5" cy="16.5" r="3.5"/><circle cx="18.5" cy="16.5" r="3.5"/><path d="M15 5h2.5l1 11.5M5.5 16.5 9 9h8.5M9 9l3 7.5"/>',
  autos:
    '<path d="M5 17H3v-5l2.2-5h13.6l2.2 5v5h-2M3 12h18M9.5 17h5"/><circle cx="7.5" cy="17" r="2"/><circle cx="16.5" cy="17" r="2"/>',
  sonstiges: '<path d="M20 13 11 22 2 13V2h11l7 7a3 3 0 0 1 0 4Z"/><circle cx="7" cy="7" r="1"/>',
};
// Example query and maximum price per category.
export const PLACEHOLDERS = {
  videospiele: ['Resident Evil Requiem', 35],
  elektronik: ['iPhone 15 128 GB', 400],
  moebel: ['Ecksofa', 150],
  fahrraeder: ['Cube Rennrad', 500],
  autos: ['VW Golf', 8000],
  sonstiges: ['Lego Millennium Falcon', 50],
};
export const categoryIcon = key =>
  `<svg viewBox="0 0 24 24" aria-hidden="true">${CATEGORY_ICONS[key] || CATEGORY_ICONS.sonstiges}</svg>`;
export const categoryOf = s => (state.choices.categories[s.category] ? s.category : 'sonstiges');
export const searchName = s =>
  isFlat(s) ? s.query : s.filters?.platform ? `${s.query} · ${state.choices.platforms[s.filters.platform]}` : s.query;
const deliveryText = s =>
  s.delivery === 'abholung'
    ? `Abholung · ${s.radius} km um ${s.plz}`
    : s.delivery === 'beides'
      ? `Abholung ${s.radius} km um ${s.plz} oder Versand`
      : s.include_shipping
        ? 'inkl. Versand'
        : 'ohne Versand';
// Searches with pickup show postcode and radius; "beides" also takes shipped offers from all of Germany.
export const PICKUP = ['abholung', 'beides'];
export const DELIVERY_HINTS = {
  versand: 'Deutschlandweit mit Versand',
  abholung: 'Nur Angebote im Umkreis deiner PLZ',
  beides: 'Im Umkreis abholen oder deutschlandweit mit Versand',
};
// What sets a search apart besides price and delivery, as small tags.
function searchTags(s) {
  const f = s.filters || {},
    tags = [];
  if (f.platform) tags.push(state.choices.platforms[f.platform]);
  if (f.year_from) tags.push(`EZ ab ${f.year_from}`);
  if (f.km_to) tags.push(`bis ${thousands(f.km_to)} km`);
  if (f.fuel) tags.push(state.choices.fuels[f.fuel]);
  if (f.gearbox) tags.push(state.choices.gearboxes[f.gearbox]);
  if (f.bike_type) tags.push(state.choices.bike_types[f.bike_type]);
  if (s.negotiable) tags.push('inkl. VB');
  if (s.free) tags.push('Verschenken');
  return tags;
}
// Categories in their fixed order, only those with searches.
export const usedCategories = searches =>
  Object.keys(state.choices.categories).filter(key => searches.some(s => categoryOf(s) === key));

function searchItem(s) {
  const running = state.running === s.id,
    queued = state.queued.includes(s.id),
    label = running ? 'Wird geprüft' : queued ? 'Eingereiht' : 'Jetzt prüfen';
  const problems = s.sources.map(x => ({ source: x, ...sourceStep(s, x) })).filter(x => x.step === 'failed');
  const reference = s.reference
    ? `<a class="search-ref" href="${safeLink(s.reference.url)}" target="_blank" rel="noopener noreferrer">idealo-Neupreis ${s.reference.shipping_included ? `${euro(s.reference.price)} inkl. Versand` : `ab ${euro(s.reference.price)}`}</a>`
    : '';
  const tags = (isFlat(s) ? flatTags(s) : searchTags(s)).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('');
  const summary = isFlat(s) ? flatSummary(s) : `${priceRange(s)} · ${deliveryText(s)}`;
  return `<li class="search-item${s.active ? '' : ' paused'}"><button class="search-main" data-edit="${s.id}" ${running ? 'disabled' : ''}><strong>${escapeHtml(searchName(s))}</strong><span>${escapeHtml(summary)} · ${s.active ? 'automatisch eingeplant' : 'pausiert'}</span></button><button class="round-button small" data-check="${s.id}" ${running || queued ? 'disabled' : ''} aria-label="${label}" title="${label}">${refreshIcon}</button>${tags ? `<span class="search-tags">${tags}</span>` : ''}<span class="search-sources">${s.sources.map(x => pill({ source: x, ...sourceStep(s, x) })).join('')}</span>${problems.map(p => `<small class="search-error">${escapeHtml(sourceName(p.source))}: ${escapeHtml(p.text)}</small>`).join('')}${reference}</li>`;
}
// What sets a flat search apart: the excluded kinds of offers, words and the move-in window.
function flatTags(s) {
  const f = s.filters || {},
    listed = splitWords(s.excluded),
    tags = (f.exclusions || []).map(t => `ohne ${t}`);
  if (listed.length) tags.push(listed.length === 1 ? `ohne „${listed[0]}“` : `${listed.length} Wörter ausgeblendet`);
  if (f.move_in_from) tags.push(`frei ab ${new Date(f.move_in_from).toLocaleDateString('de-DE')}`);
  if (f.move_in_by) tags.push(`spätestens ${new Date(f.move_in_by).toLocaleDateString('de-DE')}`);
  return tags;
}
// One group per category, in the fixed category order; flat searches are one group.
export function renderSearches(searches) {
  const label = `Neue ${AREA_TEXT[area].search}`;
  $('new-search').setAttribute('aria-label', label);
  $('new-search').title = label;
  if (area === 'wohnung') {
    $('search-list').innerHTML = searches.length
      ? `<section class="search-group"><ul class="group">${searches.map(searchItem).join('')}</ul></section>`
      : '<ul class="group"><li class="asset-empty"><p>Noch keine Wohnungssuche.</p><button class="text-button" data-new>Wohnungssuche anlegen</button></li></ul>';
    return;
  }
  $('search-list').innerHTML = searches.length
    ? usedCategories(searches)
        .map(key => {
          const group = searches.filter(s => categoryOf(s) === key);
          return `<section class="search-group"><h2 class="group-title category-title">${categoryIcon(key)}${escapeHtml(state.choices.categories[key].label)}<span>${group.length}</span></h2><ul class="group">${group.map(searchItem).join('')}</ul></section>`;
        })
        .join('')
    : '<ul class="group"><li class="asset-empty"><p>Noch keine Preissuche.</p><button class="text-button" data-new>Preissuche anlegen</button></li></ul>';
}
