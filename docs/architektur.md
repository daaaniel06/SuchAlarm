# Architektur

Eine FastAPI-App mit einem Hintergrund-Worker auf derselben Event-Loop. Handler und Worker wechseln sich nur an `await`-Stellen ab, deshalb braucht SQLite keine weiteren Sperren.

```text
Browser (static/)  ──  app.py (API)  ──  store.py ── suchalarm.db    Suchen, Merkliste, Mails
                                                  ├─ preis.db        Angebote, idealo-Neupreis
                                                  └─ wohnung.db      Inserate, Detailseiten
                           │
                       worker.py  ── scheduler.py         Warteschlange je Anbieter und Abfrage
                           │      ── core/cadence.py ─── cadence.db       Taktung, Seiten- und Abfrage-Cache
                           │      ── preis/sources.py ── core/browser.py ── getarntes Firefox
                           │      ── preis/delivery.py ─ delivery.db      Vinted-Versand nachtragen
                           │      ── mailer.py ───────── SMTP             Sammelmail
                       core/images.py ────────────────── images/cache.db  Vorschaubilder
```

## Kern und Fachmodule

`suchalarm/core/` importiert nichts aus den Fachmodulen und hat keine Tabellen einer Suchart. Jedes Fachmodul meldet seine Quellen an: `cadence.register()` (Takt, Abfrage-Schlüssel, Ergebnistyp, lokale Filter) und `images.register()` (erlaubte Bild-Hosts). `suchalarm/__init__.py` lädt alle Fachmodule, deshalb sind die Quellen angemeldet, egal welches Modul zuerst importiert wird. Orts-IDs der Anbieter liegen je Quelle in `cadence.db` (`places`); der Vinted-Versand hat seine eigene `delivery.db`. Jede Suche trägt ihre Art in `searches.kind` (`preis` oder `wohnung`).

`worker.py` ist der gemeinsame Ablauf (Quellen prüfen, Pausen und Fehler zeigen, Mails, Backup). Was eine Suchart anders macht, liefert ihr eigenes `preis/worker.py` bzw. `wohnung/worker.py` über dieselben Schritte:

| Zweck | Schritte |
| --- | --- |
| Prüfen | `prepare`, `rests`, `fetch`, `save`, `finish` |
| Mail | `take_pending`, `explain` |
| Schleife | `idle`, `queue_mails`, `cleanup` |
| API | `hits`, `bookmark`, `samples`, `clear`, `reset`, `search_url`, `extras` |
| Editor | `MODEL` (Anfrage-Schema der Suche), `CHOICES` (was der Editor anbietet) |

Das Verzeichnis der Sucharten ist `kinds.py`. Eigene Endpunkte einer Suchart (`wohnung/api.py`) hängt `app.py` über `kinds.ROUTERS` ein; `app.py`, `models.py` und `mailer.py` fragen nur `kinds.KINDS` und verzweigen nie nach Suchart. Wie ein Treffer in der Mail aussieht, steht in `preis/mail.py` bzw. `wohnung/mail.py`; `mailer.py` baut nur das gemeinsame Gerüst.

## Datenhaltung

Die Tabellen einer Suchart liegen in ihrer eigenen Datei (`preis/tables.py` → `preis.db`, `wohnung/tables.py` → `wohnung.db`), angemeldet mit `store.register()`. `store.db()` hängt sie an jede Verbindung an (`ATTACH`), so verbinden Abfragen sie über einfache Tabellennamen mit den gemeinsamen Tabellen; ein TEMP-Trigger löscht ihre Zeilen mit der Suche, wie es `ON DELETE CASCADE` täte. `app.py` schreibt kein SQL selbst, es ruft benannte Funktionen in `store.py`. Das Backup sichert alle Dateien.

## Wohnungssuche

- `wohnung/pages.py` speichert Ergebnisseiten und liest Folgeseiten. Ist die erste Seite eines Portals voll und ganz neu, liest der Worker bis zu zwei weitere Seiten (`more_pages`).
- `wohnung/details.py` liest Detailseiten, `wohnung/outbox.py` füllt die Mail-Warteschlange, `wohnung/catalog.py` liest die gespeicherten Inserate samt Urteil und Duplikaten.
- `wohnung/portals/` hat ein Modul je Portal.
- `wohnung/duplicates.py` erkennt Duplikate über Portale hinweg, auch am gleichen Foto; Standardbilder eines Vermieters (etwa Vonovia) zählen dabei nicht.
- Immowelt-Exposés liest der Browser wie die anderen Detailseiten (nur Eckdaten, Mietkosten, Beschreibung, Merkmale); Immowelts „geschätzte Warmmiete“ zählt nicht als angegebene Miete. Die Ergebnisliste fragt der Browser auf Immowelts eigener Ergebnisseite bei Immowelts JSON-Suche ab (wie Fredy): die neuesten 30 Inserate, schon nach Kaltmiete, Fläche und Zimmern vorgefiltert, dann ihre Inhalte in einer zweiten Anfrage. Die Kaltmiete-Grenze ist die Warmmiete-Grenze der Suche, die genaue Prüfung bleibt lokal.
- ohne-makler.net und immobilien.de werden direkt per HTTP gelesen, neueste zuerst. Den Ort finden beide einmal über ihre eigene Ortssuche (ohne-makler braucht dazu noch das Bundesland aus OpenStreetMap, immobilien.de liefert ein eigenes Kürzel wie „frankfurt2“). Bei ohne-makler zählen nur die Karten vor dem Block „Ähnliche Angebote“, immobilien.de liest Liste und Detailseite aus ihren schema.org-Daten. Eine Warmmiete gilt nur, wenn sie genannt ist oder aus Kaltmiete und allen Nebenkosten folgt.

Ein Klick auf eine Wohnung öffnet ihr Detailblatt (`static/sheet.js`): Fakten, Bewerbung mit Vorlage und „beworben“ (Merkliste „Beworben“), Detailseite auf Wunsch und eine Karte. Die Karte ist ein gespeichertes Bild des Suchorts aus OpenStreetMap-Kacheln (`wohnung/maps.py`), das der Worker in freien Momenten baut; Adressen ohne Koordinaten sucht Nominatim beim Öffnen. OpenStreetMap und die Ortsvorschläge beim Tippen (ImmoScout24) haben in `core/cadence.py` eigene kleine Budgets (`osm`, `suggest`) neben Seiten und Bildern.

## Module

| Modul | Aufgabe |
| --- | --- |
| `core/browser.py` | Geschützte Seite im getarnten Firefox (invisible_playwright), gebuchter Seitenabruf, Erkennen von Sperren und unlesbaren Seiten. |
| `core/cadence.py` | Taktung je Anbieter, geteilte Abfragen, Seiten- und Ergebnis-Cache. |
| `core/images.py` | Vorschaubilder laden und ausliefern. |
| `preis/domain.py`, `wohnung/domain.py` | Fachregeln (Preise, Versand, Titelvergleich, Kategorien bzw. Miete, Filter). Reine Funktionen ohne Datenbank. |
| `preis/sources.py`, `preis/extract.js` | Anbieter-URLs und Auslesen der Angebotskarten. |
| `wohnung/sources.py`, `wohnung/portals/` | Portal-URLs und Auslesen der Inserate. |
| `models.py` | Validierung der API-Eingaben (Pydantic). |
| `static/*.js` | Oberfläche als ES-Module ohne Build: `page.js` (geteilter Zustand, Serveraufrufe, `render()`), `app.js` (Rendern, Klicks), `hits.js`, `rows.js`, `swipe.js`, `searches.js`, `settings.js`, `editor.js`, `flat-editor.js`, `sheet.js` (Detailblatt und Bewerbung), `steps.js` (Quellenstatus), `ui.js`; `logic.js` enthält die testbaren reinen Funktionen. |
