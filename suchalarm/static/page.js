// What every module shares: the loaded state, the view choices, the calls to the server and render().
// app.js owns rendering and registers it with onRender(), so no module has to import app.js.

import { AREAS, createDraft, createSaveQueue } from './logic.js';

export let state,
  area,
  showAll = false,
  appliedTab = false, // the watch list of flats: Gemerkt or Beworben
  sort = 'new',
  checkSubmitting = false,
  clearAsking = false,
  renderLater = false; // a refresh never rebuilds the list under a finger: it waits until the gesture is over
export const setShowAll = value => (showAll = value);
export const setAppliedTab = value => (appliedTab = value);
export const setSort = value => (sort = value);
export const setCheckSubmitting = value => (checkSubmitting = value);
export const setClearAsking = value => (clearAsking = value);
export const setRenderLater = value => (renderLater = value);

// The area (Wohnungen | Preise) holds for hits, watch list and searches at once and stays per device.
const AREA_KEY = 'suchalarm.area';
area = (() => {
  try {
    return localStorage.getItem(AREA_KEY);
  } catch {
    return null;
  }
})();
if (!AREAS.includes(area)) area = AREAS[0];
export function setArea(next) {
  area = next;
  try {
    localStorage.setItem(AREA_KEY, area);
  } catch {}
}

let renderer = () => {};
export const onRender = draw => (renderer = draw);
export const render = () => renderer();

export async function api(path, options = {}) {
  const response = await fetch(new URL(`api/${path}`, document.baseURI), {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw Error('Server nicht erreichbar. Bitte erneut versuchen.');
  }
  if (!response.ok)
    throw Error(typeof data?.detail === 'string' ? data.detail : 'Eingaben prüfen und erneut versuchen.');
  return data;
}

let toastTimer;
export function message(text, error = false, action = null) {
  const toast = document.getElementById('message');
  toast.replaceChildren(text);
  toast.hidden = false;
  toast.classList.toggle('down', error);
  if (action) {
    const button = document.createElement('button');
    button.textContent = action.label;
    button.addEventListener('click', () => {
      toast.hidden = true;
      action.run();
    });
    toast.append(button);
  }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(
    () => {
      toast.hidden = true;
    },
    error || action ? 6000 : 3000,
  );
}

let timer,
  asked = 0,
  shown = 0;
// Polling, tab switches and actions may overlap: an answer older than the one shown is dropped, so
// after "await refresh()" the state is at least as new as that request. STRICT (after a save): an overtaken
// answer asks again, so the caller gets a state from after its request, and a failure is thrown.
export async function refresh({ strict = false } = {}) {
  for (;;) {
    const ticket = ++asked;
    try {
      const next = await api('state', { signal: AbortSignal.timeout(20000) });
      if (ticket >= shown) {
        shown = ticket;
        state = next;
        render();
        return;
      }
    } catch (err) {
      if (ticket >= shown) {
        if (strict) throw err;
        message(err.name === 'TimeoutError' ? 'Server antwortet nicht. Bitte erneut versuchen.' : err.message, true);
        clearTimeout(timer);
        timer = setTimeout(refresh, 15000);
        return;
      }
    }
    if (!strict) return; // overtaken by a newer answer, which is already shown
  }
}
// Poll fast while a check runs, otherwise every 15 seconds; hidden tabs catch up when shown again.
export function scheduleRefresh({ busy, manual, finished }) {
  clearTimeout(timer);
  timer = setTimeout(
    () => {
      if (!document.hidden) refresh();
    },
    busy || manual ? 2000 : finished ? 2300 : 15000,
  );
}

// Capture edits before change/click handlers submit their snapshot. Opening a dialog makes a new
// draft, so a late answer can never close or clear a different editing session.
export function formDraft(element, buttons = '') {
  let draft = createDraft();
  const edit = () => draft.edit();
  element.addEventListener('input', edit, true);
  element.addEventListener('change', edit, true);
  if (buttons)
    element.addEventListener(
      'click',
      e => {
        if (e.target.closest(buttons)) edit();
      },
      true,
    );
  return {
    get current() {
      return draft;
    },
    reset() {
      draft = createDraft();
    },
  };
}
const saves = new Map();
export function saveDraft(draft, resource, write, accept = () => {}) {
  if (!saves.has(resource)) saves.set(resource, createSaveQueue());
  return draft.save(current =>
    saves.get(resource)(async () => {
      let data;
      try {
        data = await write();
      } catch (err) {
        if (current()) throw err;
        return undefined;
      }
      // Saved. Polls started before this write must not bring the old values back; a refresh or render that
      // fails now is reported, but never turns the save into a failure.
      shown = ++asked;
      try {
        await refresh({ strict: true });
      } catch (err) {
        message(err.message, true);
      }
      accept(data, current());
      return data;
    }),
  );
}
export const pauseDraft = createDraft();
