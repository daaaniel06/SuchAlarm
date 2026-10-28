// The head of the hit list (count, status line, "Jetzt prüfen"), the hits and the watch list.

import { AREA_TEXT, HITS, ago, comparison, escapeHtml, euro, kindOf, newCount } from './logic.js';
import { $, fits, kindOfSource, paged, pool, safeLink, short, sourceName, visit } from './ui.js';
import { pill, sourceSteps } from './steps.js';
import { appliedTab, area, checkSubmitting, showAll, sort, state } from './page.js';
import { offerRow } from './rows.js';
import { categoryOf, searchName, usedCategories } from './searches.js';

export let filter = '',
  checkStart = 0,
  wasBusy = false,
  finishedUntil = 0;
const manualSources = new Set();
// Only this module assigns them; the page sets them through these.
export function setFilter(value) {
  filter = value;
}
export function setCheckStart(value) {
  checkStart = value;
}

// The other area's pill says when something new waits there.
function passingIn(kind, byId) {
  return pool(kind).filter(o => byId(o.search_id) && kindOf(byId(o.search_id)) === kind && fits(o, byId(o.search_id)));
}
export function renderAreaSwitch(byId) {
  let unseen = 0;
  document.querySelectorAll('[data-area]').forEach(button => {
    const kind = button.dataset.area,
      fresh = newCount(passingIn(kind, byId), visit.since, visit.opened);
    unseen += fresh;
    button.setAttribute('aria-pressed', String(kind === area));
    button.querySelector('.area-new').textContent = kind !== area && fresh ? `${fresh} neu` : '';
  });
  document.title = unseen ? `(${unseen}) Suchalarm` : 'Suchalarm';
}

// The search filter; returns the searches it selects.
export function renderFilter(searches, byId) {
  if (filter && !byId(Number(filter))) filter = '';
  // Rebuilt only when the searches change: replacing the options closes an open dropdown and drops focus.
  const option = s => `<option value="${s.id}">${escapeHtml(searchName(s))}</option>`;
  const categories = area === 'preis' ? usedCategories(searches) : [];
  const filterOptions =
    '<option value="">Alle Suchen</option>' +
    (categories.length > 1
      ? categories
          .map(
            key =>
              `<optgroup label="${escapeHtml(state.choices.categories[key].label)}">${searches
                .filter(s => categoryOf(s) === key)
                .map(option)
                .join('')}</optgroup>`,
          )
          .join('')
      : searches.map(option).join(''));
  if ($('search-filter').dataset.options !== filterOptions) {
    $('search-filter').innerHTML = filterOptions;
    $('search-filter').dataset.options = filterOptions;
  }
  $('search-filter').value = filter;
  $('filter-wrap').hidden = searches.length < 2;
  $('filter-wrap').classList.toggle('active', Boolean(filter)); // on phones the round filter button shows a chosen search
  return searches.filter(s => !filter || String(s.id) === filter);
}

export function renderHead(searches, selected, passing) {
  // Head: what is new today, then the state of the sources; "Jetzt prüfen" turns into one step per source.
  // Like the Wohnungsbot: progress only for a manual check ("Jetzt prüfen"), automatic runs stay quiet.
  // A source in a block pause is not waited for, otherwise its queued job would hide the button until the pause ends.
  const blocked = job =>
    (state.schedule || []).some(x => x.search_id === job.search_id && x.source === job.source && x.state === 'blocked');
  const busy = Boolean(state.running),
    jobs = (state.queued_jobs || []).filter(job => !blocked(job)),
    manual = jobs.length > 0;
  if (manual && !wasBusy) manualSources.clear();
  jobs.forEach(job => manualSources.add(job.source));
  if (!manual && wasBusy) {
    finishedUntil = Date.now() + 2200;
    checkStart = 0;
  }
  wasBusy = manual;
  const finished = !manual && Date.now() < finishedUntil;
  const active = searches.filter(s => s.active),
    checking = searches.filter(s => s.active || s.id === state.running || state.queued.includes(s.id));
  const steps = sourceSteps(checking),
    failed = steps.filter(x => x.step === 'failed' && x.failures >= 2); // a single failure shows only under Quellen
  const today = new Date().setHours(0, 0, 0, 0) / 1000,
    fresh = passing.filter(o => !o.start && o.first_seen >= today).length; // a search's first result is stock, not news
  const checked = state.statuses.filter(p => active.some(s => s.id === p.search_id)).map(p => p.checked_at);
  const detail = !searches.length
    ? ''
    : busy
      ? ' · wird geprüft'
      : failed.length
        ? `<span class="down"> · ${failed.length === 1 ? `${escapeHtml(sourceName(failed[0].source))} gestört` : `${failed.length} Quellen gestört`}</span>`
        : state.queued.length
          ? ' · Prüfung startet'
          : state.paused || !active.length
            ? ' · pausiert'
            : !checked.length
              ? ' · erster Abruf gleich'
              : ` · geprüft ${ago(Math.max(...checked))}`;
  $('summary').innerHTML = (fresh ? `<b class="up">+${fresh} heute</b>` : '<b>Heute nichts Neues</b>') + detail;
  // Like the Wohnungsbot: the sources' progress replaces the "Jetzt prüfen" icon, below the summary.
  // With a search chosen in the filter, the icon checks only that one (even if paused).
  const targets = filter ? selected : active,
    label = filter && selected[0] ? `${searchName(selected[0])} jetzt prüfen` : 'Jetzt prüfen';
  $('check-all').hidden = !targets.length || manual || finished; // the progress takes its place
  $('check-all').setAttribute('aria-label', label);
  $('check-all').title = label;
  $('check-all').disabled = checkSubmitting || manual || finished;
  // One step per manually checked source: pending while still queued, then done; a disturbed source keeps its "!".
  const manualSteps = Object.keys(state.sources)
    .filter(source => manualSources.has(source))
    .map(source => {
      const disturbed = steps.find(x => x.source === source && x.step === 'failed');
      return (
        disturbed || {
          source,
          step: jobs.some(job => job.source === source) ? 'pending' : 'done',
          text: jobs.some(job => job.source === source) ? 'Wird geprüft' : 'Geprüft',
        }
      );
    });
  $('progress').hidden = !(manual || finished) || !manualSteps.length;
  $('progress').classList.toggle('complete', finished);
  $('progress').innerHTML = manualSteps.map(x => pill(x, short[x.source])).join(''); // badge labels keep it on the heading row
  $('progress').setAttribute(
    'aria-label',
    finished
      ? 'Alle Quellen geprüft'
      : `${manualSteps.filter(x => x.step !== 'pending').length} von ${manualSteps.length} Quellen geprüft`,
  );
  $('paused-note').hidden = !state.paused || !searches.length;
  return { busy, manual, finished, checking };
}

export function renderOffers(searches, selected, offers, passing, byId, busy) {
  // The idealo new price as the yardstick, right above the offers.
  // Only when it is clear which search it belongs to: one search, or the list filtered to one.
  const ref = area === 'preis' && selected.length === 1 ? selected[0].reference : null;
  $('references').innerHTML = ref
    ? `<a class="reference" href="${safeLink(ref.url)}" rel="noopener noreferrer">idealo-Neupreis ${ref.shipping_included ? '' : 'ab '}<b>${euro(ref.price)}</b>${ref.shipping_included ? ' inkl. Versand' : ''} <span aria-hidden="true">↗</span></a>`
    : '';
  $('tab-match').textContent = `Passend ${passing.length}`;
  $('tab-all').textContent = `Alle ${offers.length}`;
  $('tab-match').setAttribute('aria-pressed', String(!showAll));
  $('tab-all').setAttribute('aria-pressed', String(showAll));
  const price = o => (area === 'wohnung' ? o.price || Infinity : (comparison(o, byId(o.search_id)) ?? Infinity));
  const shown = (showAll ? offers : passing).sort((a, b) =>
    sort === 'new' ? b.first_seen - a.first_seen : price(a) - price(b),
  );
  $('offer-list').innerHTML = shown.length
    ? paged('offers', shown)
        .map(o => offerRow(o, byId(o.search_id)))
        .join('')
    : emptyOffers(searches.length > 0, busy, offers.length > 0);
  if (!shown.length) paged('offers', []);
}

// Why the hit list is empty, and the one step that helps.
function emptyOffers(anySearch, busy, anyOffer) {
  const text = AREA_TEXT[area];
  let reason,
    button = '';
  if (!anySearch) {
    reason = `Noch keine ${text.search} angelegt.`;
    button = '<button class="text-button" data-new>Suche anlegen</button>';
  } else if (busy) reason = 'Die Suche läuft. Treffer erscheinen hier automatisch.';
  else if (showAll) reason = `Noch keine ${text.many} gefunden.`;
  else {
    reason = text.noMatch;
    if (anyOffer) button = `<button class="text-button" id="show-all">Alle ${text.many} zeigen</button>`;
  }
  return `<li class="asset-empty"><p>${reason}</p>${button}</li>`;
}

export function renderSaved(byId) {
  // The watch list of the area: the current hit while it is still found, otherwise the saved snapshot.
  const kindOfSaved = x => (byId(x.search_id) ? kindOf(byId(x.search_id)) : kindOfSource(x.source));
  const all = state.saved.filter(x => kindOfSaved(x) === area);
  // Flats split into saved and applied for, like the Wohnungsbot; offers have one list.
  const flats = area === 'wohnung',
    applied = all.filter(x => x.applied_at),
    saved = flats ? all.filter(x => Boolean(x.applied_at) === appliedTab) : all;
  $('saved-tabs').hidden = !flats;
  $('tab-saved').textContent = `Gemerkt ${all.length - applied.length}`;
  $('tab-applied').textContent = `Beworben ${applied.length}`;
  $('tab-saved').setAttribute('aria-pressed', String(!appliedTab));
  $('tab-applied').setAttribute('aria-pressed', String(appliedTab));
  const anySearch = { query: '', kind: area, include_shipping: 1, include_fee: 1, min_price: 0, max_price: Infinity };
  $('saved-count').textContent = all.length ? all.length : '';
  $('saved-list').innerHTML = saved.length
    ? paged('saved', saved)
        .map(x => {
          const live = state[HITS[area]].find(o => o.url === x.url && o.search_id === x.search_id);
          return offerRow(
            live || { ...x, first_seen: x.saved_at, last_seen: 0 },
            byId(x.search_id) || anySearch,
            !live,
            'saved',
          );
        })
        .join('')
    : (paged('saved', []),
      `<li class="asset-empty"><p>${flats && appliedTab ? 'Noch keine Bewerbungen markiert.' : 'Noch nichts gemerkt.'}</p><button class="text-button" data-view="offers">Zu den Treffern</button></li>`);
}
