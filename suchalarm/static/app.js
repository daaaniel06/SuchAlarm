// The page: loads the state, switches areas and views, renders everything after each refresh and
// handles the clicks and keys that are not part of one area (editors, settings and rows have their own).

import { AREAS, areaUnit, kindOf, mailTarget } from './logic.js';
import { $, fits, pages, pool } from './ui.js';
import {
  api,
  area,
  checkSubmitting,
  message,
  onRender,
  refresh,
  render,
  scheduleRefresh,
  setAppliedTab,
  setArea,
  setCheckSubmitting,
  setClearAsking,
  setRenderLater,
  setShowAll,
  setSort,
  state,
} from './page.js';
import {
  checkStart,
  filter,
  renderAreaSwitch,
  renderFilter,
  renderHead,
  renderOffers,
  renderSaved,
  setCheckStart,
  setFilter,
} from './hits.js';
import { rows } from './rows.js';
import { swipe, wheel } from './swipe.js';
import { renderSearches } from './searches.js';
import { details, loadDetails, openDetails, renderMailStatus, renderSettings } from './settings.js';
import { openEditor } from './editor.js';
import { openFlatEditor } from './flat-editor.js';
import { openSheet, renderSheet } from './sheet.js';

const EDITORS = { wohnung: openFlatEditor, preis: openEditor }; // the search editor of each area

let view = 'offers';
function switchArea(next) {
  if (next === area || !AREAS.includes(next)) return;
  setArea(next);
  setFilter('');
  pages.offers = pages.saved = 0;
  render();
}

function selectView(next) {
  view = next;
  for (const key of ['offers', 'saved', 'searches', 'settings']) $(`${key}-view`).hidden = key !== view;
  $('area-switch').hidden = view === 'settings';
  document.querySelectorAll('nav [data-view]').forEach(el => {
    if (el.dataset.view === view) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });
}

// One render function per area; render() runs them in order after each refresh.
onRender(() => {
  if (swipe?.active || wheel) {
    setRenderLater(true);
    return;
  }
  setRenderLater(false);
  const all = state.searches,
    searchMap = new Map(all.map(s => [s.id, s])),
    byId = id => searchMap.get(id);
  const searches = all.filter(s => kindOf(s) === area);
  const selected = renderFilter(searches, byId),
    selectedIds = new Set(selected.map(s => s.id));
  const offers = pool(area).filter(o => selectedIds.has(o.search_id));
  const passing = offers.filter(o => fits(o, byId(o.search_id)));
  $('offer-count').textContent = passing.length;
  $('offer-unit').textContent = areaUnit(area, passing.length);
  const progress = renderHead(searches, selected, passing);
  scheduleRefresh(progress);
  rows.clear(); // filled again by offerRow for both lists
  renderAreaSwitch(byId);
  renderOffers(searches, selected, offers, passing, byId, progress.busy);
  renderSaved(byId);
  renderSearches(searches);
  renderMailStatus();
  renderSettings(all.filter(s => s.active || s.id === state.running || state.queued.includes(s.id)));
  renderSheet();
});

$('search-filter').addEventListener('change', e => {
  setFilter(e.target.value);
  pages.offers = 0;
  render();
});
$('sort').addEventListener('change', e => {
  setSort(e.target.value);
  pages.offers = 0;
  render();
});
$('tab-match').addEventListener('click', () => {
  setShowAll(false);
  pages.offers = 0;
  render();
});
$('tab-all').addEventListener('click', () => {
  setShowAll(true);
  pages.offers = 0;
  render();
});
for (const [id, applied] of [
  ['tab-saved', false],
  ['tab-applied', true],
])
  $(id).addEventListener('click', () => {
    setAppliedTab(applied);
    pages.saved = 0;
    render();
  });
$('check-all').addEventListener('click', async () => {
  if (checkSubmitting) return;
  setCheckSubmitting(true);
  $('check-all').disabled = true;
  setCheckStart(Date.now() / 1000);
  try {
    const targets = state.searches.filter(s => kindOf(s) === area && (filter ? String(s.id) === filter : s.active));
    const results = [];
    for (const s of targets) results.push(await api(`searches/${s.id}/check`, { method: 'POST', body: '{}' }));
    const retry = Math.max(0, ...results.map(r => r.retry_after || 0));
    message(
      results.some(r => r.started)
        ? 'Prüfung gestartet.'
        : results.some(r => r.already_running)
          ? 'Prüfung läuft bereits.'
          : `Gerade geprüft. In ${retry} Sekunden erneut möglich.`,
    );
    await refresh();
  } catch (err) {
    message(err.message, true);
  } finally {
    setCheckSubmitting(false);
    render();
  }
});
async function setPaused(paused) {
  try {
    await api('settings', { method: 'PUT', body: JSON.stringify({ paused }) });
    await refresh();
  } catch (err) {
    message(err.message, true);
    render();
  }
}
$('resume').addEventListener('click', () => setPaused(false));
$('auto').addEventListener('change', e => setPaused(!e.target.checked));
document.addEventListener('click', async event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.view) selectView(button.dataset.view);
  if (button.dataset.page) {
    const [key, step] = button.dataset.page.split(':');
    pages[key] += Number(step);
    render();
    $(`${key === 'offers' ? 'offer' : 'saved'}-list`).scrollIntoView({ block: 'start', behavior: 'smooth' });
  }
  if (button.dataset.details) {
    const source = button.dataset.details;
    if (openDetails.delete(source)) render();
    else {
      openDetails.add(source);
      delete details[source];
      render();
      await loadDetails(source);
    }
  }
  if (button.dataset.retry) {
    button.disabled = true;
    try {
      const result = await api(`sources/${button.dataset.retry}/retry`, { method: 'POST', body: '{}' });
      message(
        result.started
          ? 'Neuer Versuch gestartet.'
          : result.already_running
            ? 'Prüfung läuft bereits.'
            : result.retry_after
              ? `Gerade geprüft. In ${result.retry_after} Sekunden erneut möglich.`
              : 'Keine aktive Suche nutzt diese Quelle.',
      );
      await refresh();
      if (openDetails.has(button.dataset.retry)) await loadDetails(button.dataset.retry);
    } catch (err) {
      message(err.message, true);
      render();
    }
  }
  if (button.id === 'restore-hidden') {
    try {
      const result = await api('hidden', { method: 'DELETE' });
      await refresh();
      message(`${result.restored} Treffer wieder sichtbar`); // "Treffer" fits one or many, offers or flats
    } catch (err) {
      message(err.message, true);
    }
  }
  if (button.id === 'clear-ask' || button.id === 'clear-cancel') {
    setClearAsking(button.id === 'clear-ask');
    render();
  }
  if (button.id === 'clear-confirm') {
    button.disabled = true;
    try {
      const result = await api('offers', { method: 'DELETE' });
      setClearAsking(false);
      await refresh();
      message(`${result.deleted} Treffer gelöscht. Die Suchen laufen weiter und melden nur wirklich Neues.`);
    } catch (err) {
      message(err.message, true);
      button.disabled = false;
    }
  }
  if (button.dataset.area) switchArea(button.dataset.area);
  if (button.hasAttribute('data-close')) button.closest('dialog')?.close();
  if (button.hasAttribute('data-new')) EDITORS[area]();
  if (button.id === 'show-all') {
    setShowAll(true);
    pages.offers = 0;
    render();
  }
  if (button.dataset.edit) {
    const id = Number(button.dataset.edit);
    EDITORS[kindOf(state.searches.find(s => s.id === id))](id);
  }
  if (button.dataset.check) {
    button.disabled = true;
    setCheckStart(checkStart || Date.now() / 1000);
    try {
      await api(`searches/${button.dataset.check}/check`, { method: 'POST', body: '{}' });
      await refresh();
    } catch (err) {
      message(err.message, true);
      button.disabled = false;
    }
  }
});

// Keys 1 and 2 switch the area, unless typing or a dialog is open.
document.addEventListener('keydown', e => {
  const typing = e.target.closest?.('input, textarea, select, [contenteditable]');
  if (typing || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]')) return;
  const next = AREAS[Number(e.key) - 1];
  if (next) switchArea(next);
});

// A hit's link in the mail (#treffer=search:url) shows its search's hits; a flat opens its sheet.
async function openFromMail() {
  const target = mailTarget(location.hash);
  if (target) history.replaceState(null, '', location.pathname + location.search);
  await refresh();
  const search = target && state?.searches.find(s => s.id === target.searchId);
  if (!search) return;
  switchArea(kindOf(search));
  setFilter(String(search.id));
  pages.offers = 0;
  render();
  const known = state.listings?.some(o => o.url === target.url) || state.saved.some(o => o.url === target.url);
  if (kindOf(search) === 'wohnung' && known) openSheet({ search_id: search.id, url: target.url });
}

selectView('offers');
openFromMail();
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});
