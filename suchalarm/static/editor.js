// The product search editor (category, price, delivery, sources, words).

import { escapeHtml, priceInput, splitWords } from './logic.js';
import { $ } from './ui.js';
import { api, message, refresh, state } from './page.js';
import { DELIVERY_HINTS, PICKUP, PLACEHOLDERS, categoryIcon, categoryOf } from './searches.js';

let words = [],
  deleteArmed = false;

function renderWords() {
  moreSummary();
  $('word-chips').innerHTML = words
    .map(
      (w, i) =>
        `<button type="button" class="chip" aria-pressed="true" data-word="${i}" aria-label="${escapeHtml(w)} entfernen">${escapeHtml(w)}<svg viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button>`,
    )
    .join('');
}
// Words hiding taught the category (preis/learning.py); a tap takes one back for the whole category.
const learnedWords = () => state.learned?.[draft.category] || [];
function renderLearned() {
  const learned = learnedWords();
  $('learned-set').hidden = !learned.length;
  $('learned-chips').innerHTML = learned
    .map(
      w =>
        `<button type="button" class="chip" aria-pressed="true" data-learned="${escapeHtml(w)}" aria-label="${escapeHtml(w)} zurücknehmen">${escapeHtml(w)}<svg viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button>`,
    )
    .join('');
  moreSummary();
}
function addWord() {
  const word = $('word').value.replace(/,/g, ' ').trim();
  if (word && !words.some(w => w.toLowerCase() === word.toLowerCase()) && [...words, word].join(', ').length <= 400)
    words.push(word);
  $('word').value = '';
  $('add-word').disabled = true;
  renderWords();
}
function shippingHint() {
  $('shipping-hint').textContent = $('include-shipping').checked
    ? 'Artikel + Versand unter dem Höchstpreis'
    : 'Nur der Artikelpreis zählt';
}
// The category chips on top preset the form; everything stays editable.
let draft = { category: 'sonstiges', delivery: 'versand' };
// The postcode of the most recent pickup search, so it does not have to be typed again.
const lastPlz = () => state.searches.find(s => s.plz)?.plz || '';
const sourceBoxes = () => [...document.querySelectorAll('[name=source]')];
function renderCategoryChips() {
  $('category-chips').innerHTML = Object.entries(state.choices.categories)
    .map(
      ([key, c]) =>
        `<button type="button" class="chip category-chip" data-category="${key}" aria-pressed="${key === draft.category}" title="${escapeHtml(c.hint)}">${categoryIcon(key)}${escapeHtml(c.label)}</button>`,
    )
    .join('');
}
function chooseCategory(key, presets) {
  const c = state.choices.categories[key],
    before = state.choices.categories[draft.category];
  if (presets) {
    draft.delivery = c.delivery;
    sourceBoxes().forEach(el => (el.checked = c.sources.includes(el.value)));
    $('negotiable').checked = c.negotiable;
    $('free').checked = c.free;
    if (c.radius) $('radius').value = String(c.radius);
    if (PICKUP.includes(c.delivery) && !$('plz').value) $('plz').value = lastPlz();
    // Words the user added themselves stay; an untouched preset list follows the category.
    if (!words.length || words.join(', ') === before.excluded) {
      words = splitWords(c.excluded);
      renderWords();
    }
  }
  draft.category = key;
  renderCategoryChips();
  renderLearned();
  [$('query').placeholder, $('max-price').placeholder] = PLACEHOLDERS[key] || ['', ''];
  document.querySelector('[data-filter=platform]').hidden = !c.filters.includes('platform');
  document.querySelector('[data-filter=vehicle]').hidden = !c.filters.includes('year_from');
  applyDelivery();
}
// Pickup searches run around a postcode; Vinted and idealo cannot do that and step aside.
function applyDelivery() {
  const pickup = draft.delivery === 'abholung',
    nearby = PICKUP.includes(draft.delivery);
  document
    .querySelectorAll('[data-delivery]')
    .forEach(b => b.setAttribute('aria-pressed', String(b.dataset.delivery === draft.delivery)));
  document.querySelectorAll('[data-pickup]').forEach(el => (el.hidden = !nearby));
  $('shipping-field').hidden = nearby; // picked-up offers have no shipping: all compare by item price
  $('delivery-hint').textContent = DELIVERY_HINTS[draft.delivery];
  sourceBoxes().forEach(el => {
    const possible = !pickup || state.choices.pickup_sources.includes(el.value);
    el.disabled = !possible;
    if (!possible) el.checked = false;
    el.closest('.source-chip').classList.toggle('off', !possible);
  });
  sourceCount();
  moreSummary();
}
function sourceNote() {
  const notes = [];
  if (draft.delivery === 'abholung') notes.push('Vinted und idealo können nicht im Umkreis suchen.');
  if (draft.delivery === 'beides') notes.push('Vinted und idealo suchen nur deutschlandweit mit Versand.');
  const vehicle = !document.querySelector('[data-filter=vehicle]').hidden && ($('year-from').value || $('km-to').value);
  if (vehicle && document.querySelector('[name=source][value=ebay]').checked)
    notes.push('Erstzulassung und Kilometer filtert nur Kleinanzeigen, bei eBay gelten sie nicht.');
  $('source-note').textContent = notes.join(' ');
  $('source-note').hidden = !notes.length;
}
// The closed "Weitere Optionen" still says what is set inside.
function moreSummary() {
  const parts = [],
    spellings = splitWords($('alternatives').value).length;
  if (spellings) parts.push(spellings === 1 ? '1 andere Schreibweise' : `${spellings} andere Schreibweisen`);
  if (words.length) parts.push(words.length === 1 ? '1 Wort ausgeblendet' : `${words.length} Wörter ausgeblendet`);
  const learned = learnedWords().length;
  if (learned) parts.push(learned === 1 ? '1 Wort gelernt' : `${learned} Wörter gelernt`);
  if (!$('fee-field').hidden && !$('include-fee').checked) parts.push('ohne Käuferschutz');
  if (!$('active').checked) parts.push('pausiert');
  $('more-summary').textContent = parts.join(' · ');
}
export function openEditor(id) {
  const search = id ? state.searches.find(s => s.id === id) : null,
    f = search?.filters || {};
  $('search-form').reset();
  $('search-id').value = id || '';
  $('editor-title').textContent = search ? 'Suche bearbeiten' : 'Neue Suche';
  $('platform').innerHTML =
    '<option value="">Alle Plattformen</option>' +
    Object.entries(state.choices.platforms)
      .map(([key, label]) => `<option value="${key}">${escapeHtml(label)}</option>`)
      .join('');
  $('radius').innerHTML = state.choices.radii.map(km => `<option value="${km}">${km} km</option>`).join('');
  $('query').value = search?.query || '';
  $('alternatives').value = search?.alternatives || '';
  $('max-price').value = priceInput(search?.max_price);
  $('min-price').value = priceInput(search?.min_price);
  $('include-shipping').checked = search ? !!search.include_shipping : true;
  $('include-fee').checked = search ? !!search.include_fee : true;
  $('active').checked = search ? !!search.active : true;
  $('negotiable').checked = !!search?.negotiable;
  $('free').checked = !!search?.free;
  $('plz').value = search?.plz || '';
  $('radius').value = String(search?.radius || 25);
  $('platform').value = f.platform || '';
  $('year-from').value = f.year_from || '';
  $('km-to').value = f.km_to || '';
  sourceBoxes().forEach(el => (el.checked = search ? search.sources.includes(el.value) : true));
  words = splitWords(search?.excluded);
  $('more').open = false;
  $('delete-wrap').hidden = !search;
  $('delete-search').textContent = 'Suche löschen';
  deleteArmed = false;
  $('form-error').hidden = true;
  shippingHint();
  draft = { category: search ? categoryOf(search) : 'sonstiges', delivery: search?.delivery || 'versand' };
  chooseCategory(draft.category, !search);
  renderWords();
  $('editor').showModal();
  if (!search) $('query').focus();
}
// "Alle/Keine" and the count follow the picked portals; with pickup only those that can search an area count.
function sourceCount() {
  const boxes = sourceBoxes().filter(x => !x.disabled),
    on = boxes.filter(x => x.checked).length,
    limited = boxes.length < sourceBoxes().length;
  sourceNote();
  // Buyer protection only exists on Vinted.
  $('fee-field').hidden = !document.querySelector('[name=source][value=vinted]').checked;
  moreSummary();
  $('sources-count').textContent = limited
    ? `${on} von ${boxes.length} möglichen`
    : on === boxes.length
      ? 'alle'
      : `${on} von ${boxes.length}`;
  $('toggle-sources').textContent = on === boxes.length ? 'Keine' : 'Alle';
}
function formError(text) {
  $('form-error').textContent = text;
  $('form-error').hidden = false;
}

$('include-shipping').addEventListener('change', shippingHint);
$('include-fee').addEventListener('change', moreSummary);
$('active').addEventListener('change', moreSummary);
$('alternatives').addEventListener('input', moreSummary);
$('category-chips').addEventListener('click', e => {
  const chip = e.target.closest('[data-category]');
  // Choosing another category takes its presets; the search term and prices stay.
  if (chip && chip.dataset.category !== draft.category) chooseCategory(chip.dataset.category, true);
});
document.querySelectorAll('[data-delivery]').forEach(b =>
  b.addEventListener('click', () => {
    draft.delivery = b.dataset.delivery;
    if (PICKUP.includes(draft.delivery) && !$('plz').value) $('plz').value = lastPlz();
    // Back from pickup: the category's own sources that pickup had switched off come back.
    if (draft.delivery !== 'abholung')
      sourceBoxes().forEach(el => {
        const restore = !state.choices.pickup_sources.includes(el.value);
        if (restore && state.choices.categories[draft.category].sources.includes(el.value)) el.checked = true;
      });
    applyDelivery();
  }),
);
$('plz').addEventListener('input', () => ($('plz').value = $('plz').value.replace(/\D/g, '').slice(0, 5)));
for (const id of ['year-from', 'km-to']) $(id).addEventListener('input', sourceNote);
document.querySelector('.source-picker').addEventListener('change', sourceCount);
$('toggle-sources').addEventListener('click', () => {
  const boxes = sourceBoxes().filter(x => !x.disabled),
    all = boxes.every(x => x.checked);
  boxes.forEach(x => (x.checked = !all));
  sourceCount();
});
$('close-editor').addEventListener('click', () => $('editor').close());
$('cancel-editor').addEventListener('click', () => $('editor').close());
$('word').addEventListener('input', () => {
  $('add-word').disabled = !$('word').value.trim();
});
$('word').addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    e.preventDefault();
    addWord();
  }
});
$('add-word').addEventListener('click', addWord);
$('word-chips').addEventListener('click', e => {
  const chip = e.target.closest('[data-word]');
  if (chip) {
    words.splice(Number(chip.dataset.word), 1);
    renderWords();
  }
});
$('learned-chips').addEventListener('click', async e => {
  const chip = e.target.closest('[data-learned]');
  if (!chip) return;
  chip.disabled = true;
  try {
    await api('learned/forget', {
      method: 'POST',
      body: JSON.stringify({ category: draft.category, word: chip.dataset.learned }),
    });
    await refresh();
    renderLearned();
    message(`„${chip.dataset.learned}“ blendet nichts mehr aus.`);
  } catch (err) {
    chip.disabled = false;
    formError(err.message);
  }
});

$('search-form').addEventListener('submit', async event => {
  event.preventDefault();
  if ($('word').value.trim()) addWord();
  const sources = [...document.querySelectorAll('[name=source]:checked')].map(x => x.value);
  if ($('query').value.trim().length < 2) return formError('Suchbegriff eingeben.');
  if (!(Number($('max-price').value) > 0)) return formError('Höchstpreis eingeben.');
  if (Number($('min-price').value) >= Number($('max-price').value))
    return formError('Mindestpreis muss unter dem Höchstpreis liegen.');
  const pickup = PICKUP.includes(draft.delivery);
  if (pickup && !/^\d{5}$/.test($('plz').value)) return formError('Für Abholung eine fünfstellige PLZ eingeben.');
  if (!sources.length) return formError('Wähle mindestens eine Quelle.');
  const number = id => ($(id).value ? Math.round(Number($(id).value)) : null);
  const payload = {
    query: $('query').value,
    alternatives: splitWords($('alternatives').value).join(', '),
    min_price: Math.round(Number($('min-price').value || 0) * 100),
    max_price: Math.round(Number($('max-price').value) * 100),
    include_shipping: !pickup && $('include-shipping').checked,
    include_fee: $('include-fee').checked,
    excluded: words.join(', '),
    sources,
    active: $('active').checked,
    category: draft.category,
    delivery: draft.delivery,
    plz: pickup ? $('plz').value : '',
    radius: pickup ? Number($('radius').value) : 0,
    negotiable: $('negotiable').checked,
    free: $('free').checked,
    filters: { platform: $('platform').value || null, year_from: number('year-from'), km_to: number('km-to') },
  };
  const id = $('search-id').value;
  $('save-search').disabled = true;
  try {
    await api(id ? `searches/${id}` : 'searches', { method: id ? 'PUT' : 'POST', body: JSON.stringify(payload) });
    $('editor').close();
    message('Suche gespeichert.');
    await refresh();
  } catch (err) {
    formError(err.message);
  } finally {
    $('save-search').disabled = false;
  }
});
$('delete-search').addEventListener('click', async () => {
  if (!deleteArmed) {
    deleteArmed = true;
    $('delete-search').textContent = 'Endgültig löschen';
    return;
  }
  try {
    await api(`searches/${$('search-id').value}`, { method: 'DELETE' });
    $('editor').close();
    await refresh();
    message('Suche gelöscht.');
  } catch (err) {
    formError(err.message);
  }
});
