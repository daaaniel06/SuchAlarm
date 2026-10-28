// Settings: automatic search on top, below it the tabs Mails, Quellen (with details) and Daten.

import { AREAS, AREA_TEXT, ago, escapeHtml } from './logic.js';
import { $, clock, kindOfSource, safeLink } from './ui.js';
import { STEP_LABEL, nextFetch, pill, sourceSteps } from './steps.js';
import { api, clearAsking, message, refresh, render, state } from './page.js';
import { searchName } from './searches.js';

// "Details" under Quellen: which sources are open, and what the server last said about them.
export const openDetails = new Set(),
  details = {};
// The open tab; Mails first, since that is what is changed most.
let tab = 'mail';

let mail = null; // the mail settings as shown; every change is saved right away
let saving = Promise.resolve();

export function renderMailStatus() {
  if (!$('mail-form').dataset.saving) {
    mail = { recipients: [...state.mail.recipients] };
    for (const kind of AREAS) mail[kind] = { ...state.mail[kind] };
    renderMail();
  }
  $('test-mail').disabled = !state.mail.configured || !state.mail.recipients.length;
  const mailError = !state.mail.configured
    ? 'Mailserver fehlt. SMTP-Zugangsdaten in der Server-Konfiguration hinterlegen.'
    : state.mail.error;
  $('mail-error').hidden = !mailError;
  $('mail-error').textContent = mailError || '';
  const pending = AREAS.reduce((sum, kind) => sum + (state.mail[kind].enabled ? state.mail.pending[kind] : 0), 0);
  $('mail-pending').hidden = !pending;
  $('mail-pending').textContent =
    `${pending} ${pending === 1 ? 'Treffer wartet' : 'Treffer warten'} auf die nächste Mail.`;
  const outcome = log =>
    log.status === 'sent'
      ? ago(log.sent_at)
      : log.status === 'failed'
        ? `<span class="down">Fehlgeschlagen · ${escapeHtml(log.error || '')}</span>`
        : log.status === 'unknown'
          ? `<span class="down">Unklar, ob angekommen · ${ago(log.created_at)}</span>`
          : 'Wird gesendet …';
  $('mail-log').innerHTML = state.mail_log.length
    ? state.mail_log
        .map(
          log =>
            `<li class="field"><span><b>${escapeHtml(log.subject)}</b><small>${outcome(log)}</small></span><span class="count">${log.count}</span></li>`,
        )
        .join('')
    : '<li class="field"><span><small>Noch keine Mails verschickt.</small></span></li>';
}

export function renderSettings(checking) {
  $('hidden-text').textContent = state.hidden
    ? `${state.hidden} ausgeblendet · nach links gewischt`
    : 'Keine · nach links wischen blendet einen Treffer aus';
  $('restore-hidden').disabled = !state.hidden;
  // Destructive, so it asks once more in place before anything is deleted.
  $('clear-field').classList.toggle('confirming', clearAsking);
  $('clear-text').textContent = clearAsking
    ? 'Auch die Merkliste. Nicht rückgängig zu machen.'
    : `${state.offers.length + state.listings.length} gespeichert · Suchen und Einstellungen bleiben`;
  $('clear-actions').innerHTML = clearAsking
    ? '<button class="pill compact" id="clear-cancel">Abbrechen</button><button class="pill compact danger" id="clear-confirm">Endgültig löschen</button>'
    : `<button class="pill compact text-danger" id="clear-ask" ${state.offers.length || state.listings.length || state.saved.length ? '' : 'disabled'}>Löschen</button>`;

  document
    .querySelectorAll('[data-settings-panel]')
    .forEach(panel => (panel.hidden = panel.dataset.settingsPanel !== tab));
  document
    .querySelectorAll('[data-settings-tab]')
    .forEach(b => b.setAttribute('aria-pressed', String(b.dataset.settingsTab === tab)));

  $('auto').checked = !state.paused;
  $('auto-text').textContent = state.paused
    ? 'Pausiert · „Jetzt prüfen“ geht weiterhin'
    : 'Anbieter werden automatisch mit Abstand abgefragt';
  let failedAll = 0;
  $('source-list').innerHTML = AREAS.map((kind, i) => {
    const steps = sourceSteps(checking, true).filter(x => kindOfSource(x.source) === kind),
      failed = steps.filter(x => x.step === 'failed').length,
      count = `${steps.length} ${steps.length === 1 ? 'Quelle' : 'Quellen'}${failed ? ` · <span class="down">${failed} gestört</span>` : ''}`;
    failedAll += failed;
    return `<h2 class="group-title${i ? '' : ' first'}">${AREA_TEXT[kind].title}<small>${count}</small></h2><div class="group form-group">${steps.map(sourceField).join('')}</div>`;
  }).join('');
  // A disturbed source shows on its tab, so it is not missed while another tab is open.
  document.querySelector('[data-settings-tab="sources"]').classList.toggle('alert', failedAll > 0);
}
$('settings-tabs').addEventListener('click', e => {
  const b = e.target.closest('[data-settings-tab]');
  if (!b) return;
  tab = b.dataset.settingsTab;
  render();
});
function sourceField(x) {
  const next = nextFetch(x.source),
    text = x.step === 'done' && next < Infinity ? `${x.text} · nächster Abruf ${clock(next)}` : x.text;
  // A disturbed source can be retried by hand: that lifts a provider pause and checks it once more.
  const queued = (state.queued_jobs || []).some(job => job.source === x.source),
    action =
      x.step === 'failed'
        ? `<button class="pill compact" data-retry="${x.source}" ${queued ? 'disabled' : ''}>${queued ? 'Eingereiht' : 'Neu versuchen'}</button>`
        : pill(x, STEP_LABEL[x.step]);
  const open = openDetails.has(x.source),
    toggle = `<button class="text-button" data-details="${x.source}" aria-expanded="${open}">${open ? 'Weniger' : 'Details'}</button>`;
  return `<div class="field source-field ${x.step}"><span><b>${escapeHtml(state.sources[x.source])}</b><small>${escapeHtml(text)}</small></span><span class="confirm-actions">${toggle}${action}</span></div>${open ? detailsPanel(x.source) : ''}`;
}

// Why a source fails, so it can be checked without opening the server: the provider's pause and
// budget, and per search the last attempts, the error and what the page showed at that moment.
function detailsPanel(source) {
  const d = details[source];
  if (!d) return '<div class="source-details"><p>Lädt …</p></div>';
  const now = Date.now() / 1000,
    p = d.provider;
  const provider = [
    p.blocked_until ? `Anbieter pausiert bis ${clock(p.blocked_until)}` : 'Keine Anbieter-Pause',
    p.strikes ? `Sperr-Stufe ${p.strikes} von 4` : '',
    `${p.pages_last_hour} von ${p.pages_per_hour} Seitenaufrufen in der letzten Stunde`,
    p.blocked_until
      ? ''
      : p.ready_at > now
        ? `nächster erlaubter Seitenaufruf ${clock(p.ready_at)}`
        : 'Seitenaufruf jetzt erlaubt',
  ].filter(Boolean);
  const searches = d.searches.map(entry => {
    const search = state.searches.find(s => s.id === entry.search_id),
      q = entry.query,
      f = entry.failure,
      info = f?.detail,
      failures = entry.status?.failures || 0;
    const facts = [
      failures ? `${failures} ${failures === 1 ? 'Fehlversuch' : 'Fehlversuche'} in Folge` : '',
      q?.at ? `letzter Erfolg ${ago(q.at)}` : 'noch kein erfolgreicher Abruf',
      q?.next_at > now ? `nächster Versuch ${clock(q.next_at)}` : '',
    ].filter(Boolean);
    let html = `<h4>${escapeHtml(search ? searchName(search) : 'Suche')}</h4><p>${escapeHtml(facts.join(' · '))}</p>`;
    if (f) {
      html += `<p class="down">${escapeHtml(f.error)} (${clock(f.at)})</p>`;
      if (info) {
        const page = [
          info.stage ? `Schritt: ${info.stage}` : '',
          info.status ? `HTTP ${info.status}` : '',
          info.cards != null ? `${info.cards} Angebotskarten erkannt` : '',
          info.error ? `Fehlerart: ${info.error}` : '',
        ].filter(Boolean);
        html += `<p>${escapeHtml(page.join(' · '))}</p>`;
        if (info.title) html += `<p>Seitentitel „${escapeHtml(info.title)}“</p>`;
        if (info.text) html += `<blockquote>${escapeHtml(info.text)}</blockquote>`;
      }
    } else html += '<p>Kein Fehler gespeichert.</p>';
    const link = info?.url || entry.url;
    if (link)
      html += `<a class="text-button" href="${safeLink(link)}" target="_blank" rel="noopener noreferrer">${info?.url ? 'Fehlgeschlagene Seite selbst öffnen' : 'Suchseite selbst öffnen'} ↗</a>`;
    return `<section>${html}</section>`;
  });
  return `<div class="source-details"><section><h4>Anbieter</h4><p>${escapeHtml(provider.join(' · '))}</p></section>${searches.join('') || '<p>Keine aktive Suche nutzt diese Quelle.</p>'}</div>`;
}
export async function loadDetails(source) {
  try {
    details[source] = await api(`sources/${source}/details`);
  } catch (err) {
    message(err.message, true);
  }
  render();
}

// Each change is saved at once; refreshes leave the settings alone while a save is underway.
function renderMail() {
  document.querySelectorAll('[data-mail-area]').forEach(group => {
    const settings = mail[group.dataset.mailArea],
      part = name => group.querySelector(`[data-mail="${name}"]`);
    part('enabled').checked = settings.enabled;
    part('frequency').value = settings.frequency;
    part('frequency-field').hidden = !settings.enabled;
    part('batch-field').hidden = settings.frequency !== 'Sofort' || !settings.enabled;
    group
      .querySelectorAll('[data-batch]')
      .forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.batch) === settings.batch_size)));
    part('batch-text').textContent =
      settings.batch_size === 1
        ? 'Jeder passende Treffer sofort'
        : `Sobald ${settings.batch_size} zusammen sind, spätestens nach 2 Std.`;
  });
  $('recipient-items').innerHTML = mail.recipients
    .map(
      (address, i) =>
        `<div class="field"><span>${escapeHtml(address)}</span><button type="button" class="round-button small" data-remove="${i}" aria-label="${escapeHtml(address)} entfernen"><svg viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></div>`,
    )
    .join('');
  $('recipient-add').placeholder = mail.recipients.length ? 'Weitere Mailadresse' : 'Mailadresse hinzufügen';
  $('recipient-add').disabled = mail.recipients.length >= 5;
}
// Saves in order, one at a time; on failure the server's settings come back.
function mailChanged() {
  const body = JSON.stringify({ recipients: mail.recipients, wohnung: mail.wohnung, preis: mail.preis });
  $('mail-form').dataset.saving = '1';
  renderMail();
  saving = saving.then(async () => {
    try {
      await api('mail', { method: 'PUT', body });
    } catch (err) {
      message(err.message, true);
    }
  });
  const mine = saving;
  mine.then(async () => {
    if (saving !== mine) return;
    delete $('mail-form').dataset.saving;
    await refresh();
  });
}
function addRecipient() {
  const address = $('recipient-add').value.trim();
  if (!/^[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+$/.test(address)) {
    message('Gültige Mailadresse eingeben.', true);
    return;
  }
  if (!mail.recipients.some(a => a.toLowerCase() === address.toLowerCase())) mail.recipients.push(address);
  $('recipient-add').value = '';
  $('add-recipient').disabled = true;
  mailChanged();
}

// One handler for both areas' mail settings.
$('mail-form').addEventListener('change', e => {
  const group = e.target.closest('[data-mail-area]');
  if (!group) return;
  const settings = mail[group.dataset.mailArea];
  if (e.target.dataset.mail === 'enabled') settings.enabled = e.target.checked;
  if (e.target.dataset.mail === 'frequency') settings.frequency = e.target.value;
  mailChanged();
});
$('mail-form').addEventListener('click', e => {
  const b = e.target.closest('[data-batch]'),
    group = b?.closest('[data-mail-area]');
  if (group) {
    mail[group.dataset.mailArea].batch_size = Number(b.dataset.batch);
    mailChanged();
  }
});
$('recipient-items').addEventListener('click', e => {
  const b = e.target.closest('[data-remove]');
  if (b) {
    mail.recipients.splice(Number(b.dataset.remove), 1);
    mailChanged();
  }
});
$('recipient-add').addEventListener('input', () => {
  $('add-recipient').disabled = !$('recipient-add').value.trim();
});
$('recipient-add').addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    e.preventDefault();
    addRecipient();
  }
});
$('add-recipient').addEventListener('click', addRecipient);
$('mail-form').addEventListener('submit', e => e.preventDefault());
$('test-mail').addEventListener('click', async () => {
  $('test-mail').disabled = true;
  try {
    await api('mail/test', { method: 'POST', body: '{}' });
    message('Testmail verschickt.');
  } catch (err) {
    message(err.message, true);
  } finally {
    $('test-mail').disabled = false;
  }
});
