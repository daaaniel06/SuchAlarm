// The state of a source for a search (checking, disturbed, waiting, done) and its pill.

import { doneText, escapeHtml, kindOf } from './logic.js';
import { clock, fits, pool, sourceName } from './ui.js';
import { state } from './page.js';

const tick = '<svg viewBox="0 0 24 24"><path d="m5 12 5 5 9-10"/></svg>';
export const STEP_LABEL = { done: 'Aktiv', pending: 'Prüft', failed: 'Gestört', waiting: 'Wartet', inactive: 'Aus' };

// The next request the cadence allows for a source, or 0.
export const nextFetch = source =>
  Math.min(
    ...(state.schedule || []).filter(x => x.source === source && x.next_at > Date.now() / 1000).map(x => x.next_at),
    Infinity,
  );
// One state per source and search: pending while it is being checked, otherwise its last result.
export function sourceStep(search, source) {
  const status = state.statuses.find(p => p.search_id === search.id && p.source === source);
  const schedule = (state.schedule || []).find(p => p.search_id === search.id && p.source === source);
  if (state.running === search.id && state.running_source === source)
    return { step: 'pending', text: 'Wird gerade geprüft' };
  if (status?.note) return { step: 'inactive', text: status.note }; // a regional portal outside its area
  // failures: attempts in a row that failed; a pause counts as at least one.
  if (status?.error) return { step: 'failed', text: status.error, failures: status.failures || 1 };
  if (schedule?.state === 'blocked')
    return {
      step: 'failed',
      text: `Anbieter pausiert bis ${clock(schedule.next_at)}`,
      failures: status?.failures || 1,
    };
  if (
    (state.queued_jobs || []).some(job => job.search_id === search.id && job.source === source) &&
    schedule &&
    schedule.next_at > Date.now() / 1000
  )
    return { step: 'waiting', text: `Eingereiht · frühestens ${clock(schedule.next_at)}` };
  if (!status)
    return {
      step: 'waiting',
      text:
        schedule && schedule.next_at > Date.now() / 1000
          ? `Erster Abruf eingeplant · frühestens ${clock(schedule.next_at)}`
          : 'Erster Abruf läuft gleich',
    };
  // Hits are the offers of the last fetch that pass the price and shipping filters.
  const hits = pool(kindOf(search)).filter(
    o => o.search_id === search.id && o.source === source && fits(o, search),
  ).length;
  return { step: 'done', ...doneText(status.count, hits, status.checked_at) };
}
// Across searches: a source is only fine if it is fine for every search that uses it.
// When every search is done, the counts add up and the oldest check is shown.
export function sourceSteps(searches, all = false) {
  const order = ['pending', 'failed', 'waiting', 'done', 'inactive'];
  return Object.keys(state.sources).flatMap(source => {
    const steps = searches.filter(s => s.sources.includes(source)).map(s => sourceStep(s, source));
    if (!steps.length) return all ? [{ source, step: 'inactive', text: 'In keiner aktiven Suche' }] : [];
    const worst = steps.sort(
      (a, b) => order.indexOf(a.step) - order.indexOf(b.step) || (b.failures || 0) - (a.failures || 0),
    )[0];
    // Searches for which a regional portal rests do not count towards its result.
    const done = steps.filter(x => x.step === 'done');
    if (worst.step !== 'done' || done.length === 1) return [{ source, ...(done[0] || worst) }];
    const sum = key => done.reduce((total, x) => total + x[key], 0);
    return [{ source, step: 'done', ...doneText(sum('found'), sum('hits'), Math.min(...done.map(x => x.checked))) }];
  });
}
export const pill = ({ source, step, text }, label = sourceName(source)) =>
  `<span class="source-step ${step}" title="${escapeHtml(sourceName(source))}: ${escapeHtml(text)}"><i aria-hidden="true">${step === 'done' ? tick : step === 'failed' ? '!' : ''}</i>${escapeHtml(label)}</span>`;
