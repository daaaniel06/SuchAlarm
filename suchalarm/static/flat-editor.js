// The flat search editor: place, warm rent, size, what to leave out and the portals.

import { escapeHtml, splitWords } from './logic.js';
import { $, isFlat } from './ui.js';
import { api, message, refresh, state } from './page.js';

// The flat search editor: place, warm rent, size, what to leave out and the portals.
let flatWords = [],
  flatTraits = new Set(),
  flatDeleteArmed = false,
  picked = null, // the place chosen from the suggestions, while the field still shows its label
  options = [],
  highlight = 0,
  lookup = 0;
const flatBoxes = () => [...document.querySelectorAll('[name=flat-source]')];
const REGIONAL = {
  'wg-gesucht': 'WG-Gesucht nur mit Ortsnamen, ohne PLZ',
  bauverein: 'bauverein nur Darmstadt',
  nhw: 'NHW nur Hessen',
};
// Chips are drawn on opening and on their own clicks only: redrawing them on another field's change
// (which fires as that field loses focus to the chip) would swallow the click.
function renderFlatChips() {
  $('flat-traits').innerHTML = state.choices.traits
    .map(
      t =>
        `<button type="button" class="chip" data-trait="${escapeHtml(t)}" aria-pressed="${flatTraits.has(t)}">ohne ${escapeHtml(t)}</button>`,
    )
    .join('');
  $('flat-word-chips').innerHTML = flatWords
    .map(
      (w, i) =>
        `<button type="button" class="chip" aria-pressed="true" data-flat-word="${i}" aria-label="${escapeHtml(w)} entfernen">${escapeHtml(w)}<svg viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button>`,
    )
    .join('');
  renderFlatDraft();
}
function renderFlatDraft() {
  const boxes = flatBoxes(),
    on = boxes.filter(x => x.checked);
  $('flat-sources-count').textContent = on.length === boxes.length ? 'alle' : `${on.length} von ${boxes.length}`;
  $('flat-toggle-sources').textContent = on.length === boxes.length ? 'Keine' : 'Alle';
  const notes = on.map(x => REGIONAL[x.value]).filter(Boolean);
  $('flat-source-note').textContent = notes.length ? `${notes.join(' · ')}.` : '';
  $('flat-source-note').hidden = !notes.length;
  $('flat-radius-hint').textContent = Number($('flat-radius').value)
    ? 'Entfernung von der Ortsmitte'
    : 'Nur Inserate im Ort selbst';
  const parts = [];
  if (flatWords.length)
    parts.push(flatWords.length === 1 ? '1 Wort ausgeblendet' : `${flatWords.length} Wörter ausgeblendet`);
  if ($('flat-from').value || $('flat-by').value) parts.push('Einzug eingegrenzt');
  if (!$('flat-active').checked) parts.push('pausiert');
  $('flat-more-summary').textContent = parts.join(' · ');
}
// Places while typing (town, district, postcode), as rows right below the field so a phone never clips
// them. One lookup per pause in typing; the server keeps its own small budget for them.
function showPlaces(list) {
  options = list;
  highlight = 0;
  $('flat-place-options').innerHTML = list
    .map(
      (o, i) =>
        `<button type="button" class="field place-option" role="option" aria-selected="${i === highlight}" data-place="${i}"><span>${escapeHtml(o.label)}</span><small>${o.kind === 'plz' ? 'Postleitzahl' : 'Stadt / Kreis'}</small></button>`,
    )
    .join('');
  $('flat-place-options').hidden = !list.length;
  $('flat-place').setAttribute('aria-expanded', String(list.length > 0));
}
function placeHint() {
  const text = $('flat-place').value.trim();
  $('flat-place-hint').hidden =
    Boolean(picked) || text.length < 2 || !$('flat-place').dataset.edited || !$('flat-place-options').hidden;
}
function choosePlace(option) {
  picked = option;
  $('flat-place').value = option.label;
  showPlaces([]);
  placeHint();
}
$('flat-place').addEventListener('input', () => {
  picked = null;
  $('flat-place').dataset.edited = '1';
  const text = $('flat-place').value.trim(),
    ticket = ++lookup;
  showPlaces([]);
  placeHint();
  if (text.length < 3) return;
  setTimeout(async () => {
    if (ticket !== lookup) return; // typing went on
    try {
      const found = await api(`places?q=${encodeURIComponent(text)}`);
      if (ticket === lookup) showPlaces(found);
    } catch {}
    placeHint();
  }, 450);
});
$('flat-place').addEventListener('keydown', e => {
  if (!options.length) return;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    highlight = (highlight + (e.key === 'ArrowDown' ? 1 : options.length - 1)) % options.length;
    document
      .querySelectorAll('.place-option')
      .forEach((el, i) => el.setAttribute('aria-selected', String(i === highlight)));
  }
  if (e.key === 'Enter') {
    e.preventDefault(); // chooses instead of saving
    choosePlace(options[highlight]);
  }
  if (e.key === 'Escape') {
    e.preventDefault(); // closes the list, not the sheet
    showPlaces([]);
    placeHint();
  }
});
$('flat-place').addEventListener('blur', () =>
  setTimeout(() => {
    showPlaces([]);
    placeHint();
  }, 150),
);
$('flat-place-options').addEventListener('mousedown', e => {
  const button = e.target.closest('[data-place]');
  if (!button) return;
  e.preventDefault(); // keeps the focus in the field
  choosePlace(options[Number(button.dataset.place)]);
});

export function openFlatEditor(id) {
  const search = id ? state.searches.find(s => s.id === id) : null,
    f = search?.filters || {};
  $('flat-form').reset();
  $('flat-id').value = id || '';
  $('flat-title').textContent = search ? 'Wohnungssuche bearbeiten' : 'Neue Wohnungssuche';
  $('flat-radius').innerHTML = state.choices.flat_radii
    .map(km => `<option value="${km}">${km ? `${km} km` : 'Nur im Ort'}</option>`)
    .join('');
  const last = state.searches.find(isFlat);
  $('flat-place').value = search?.query || '';
  picked = f.place?.label === search?.query ? f.place : null; // a saved pick keeps its geopath
  delete $('flat-place').dataset.edited;
  ++lookup;
  showPlaces([]);
  placeHint();
  $('flat-place').placeholder = last?.query || 'Darmstadt';
  $('flat-radius').value = String(search?.radius || 0);
  $('flat-rent').value = search ? Math.round(search.max_price / 100) : '';
  $('flat-area').value = f.min_area || '';
  $('flat-rooms').value = f.min_rooms || '';
  $('flat-from').value = f.move_in_from || '';
  $('flat-by').value = f.move_in_by || '';
  $('flat-active').checked = search ? !!search.active : true;
  flatBoxes().forEach(el => (el.checked = search ? search.sources.includes(el.value) : true));
  flatTraits = new Set(f.exclusions || []);
  flatWords = splitWords(search?.excluded);
  $('flat-more').open = false;
  $('flat-delete-wrap').hidden = !search;
  $('flat-delete').textContent = 'Suche löschen';
  flatDeleteArmed = false;
  $('flat-error').hidden = true;
  renderFlatChips();
  $('flat-editor').showModal();
  if (!search) $('flat-place').focus();
}
function addFlatWord() {
  const word = $('flat-word').value.replace(/,/g, ' ').trim();
  if (
    word &&
    !flatWords.some(w => w.toLowerCase() === word.toLowerCase()) &&
    [...flatWords, word].join(', ').length <= 400
  )
    flatWords.push(word);
  $('flat-word').value = '';
  $('flat-add-word').disabled = true;
  renderFlatChips();
}
function flatError(text) {
  $('flat-error').textContent = text;
  $('flat-error').hidden = false;
}
$('flat-form').addEventListener('click', e => {
  const trait = e.target.closest('[data-trait]'),
    word = e.target.closest('[data-flat-word]');
  if (trait) {
    const t = trait.dataset.trait;
    if (!flatTraits.delete(t)) flatTraits.add(t);
    trait.setAttribute('aria-pressed', String(flatTraits.has(t)));
  }
  if (word) {
    flatWords.splice(Number(word.dataset.flatWord), 1);
    renderFlatChips();
  }
});
$('flat-form').addEventListener('change', renderFlatDraft);
$('flat-toggle-sources').addEventListener('click', () => {
  const all = flatBoxes().every(x => x.checked);
  flatBoxes().forEach(x => (x.checked = !all));
  renderFlatDraft();
});
$('flat-word').addEventListener('input', () => ($('flat-add-word').disabled = !$('flat-word').value.trim()));
$('flat-word').addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    e.preventDefault();
    addFlatWord();
  }
});
$('flat-add-word').addEventListener('click', addFlatWord);
$('flat-form').addEventListener('submit', async event => {
  event.preventDefault();
  if ($('flat-word').value.trim()) addFlatWord();
  const sources = flatBoxes()
    .filter(x => x.checked)
    .map(x => x.value);
  const place = $('flat-place').value.trim(),
    rent = Number($('flat-rent').value);
  if (place.length < 2) return flatError('Ort oder Postleitzahl eingeben.');
  if (!(rent > 0)) return flatError('Höchste Warmmiete eingeben.');
  if (!sources.length) return flatError('Wähle mindestens ein Portal.');
  const payload = {
    kind: 'wohnung',
    query: place,
    picked: picked?.label === place ? picked : null,
    max_price: Math.round(rent * 100),
    radius: Number($('flat-radius').value),
    excluded: flatWords.join(', '),
    sources,
    active: $('flat-active').checked,
    filters: {
      min_area: Number($('flat-area').value || 0),
      min_rooms: Number($('flat-rooms').value || 0),
      exclusions: [...flatTraits],
      move_in_from: $('flat-from').value || null,
      move_in_by: $('flat-by').value || null,
    },
  };
  const id = $('flat-id').value;
  $('flat-save').disabled = true;
  try {
    await api(id ? `searches/${id}` : 'searches', { method: id ? 'PUT' : 'POST', body: JSON.stringify(payload) });
    $('flat-editor').close();
    message('Wohnungssuche gespeichert.');
    await refresh();
  } catch (err) {
    flatError(err.message);
  } finally {
    $('flat-save').disabled = false;
  }
});
$('flat-delete').addEventListener('click', async () => {
  if (!flatDeleteArmed) {
    flatDeleteArmed = true;
    $('flat-delete').textContent = 'Endgültig löschen';
    return;
  }
  try {
    await api(`searches/${$('flat-id').value}`, { method: 'DELETE' });
    $('flat-editor').close();
    await refresh();
    message('Suche gelöscht.');
  } catch (err) {
    flatError(err.message);
  }
});
