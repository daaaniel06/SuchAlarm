# Changelog

## Unveröffentlicht (Branch claude/bugfixes)

### Mails
- Die Historie unterscheidet „versendet“ (vom Mailserver angenommen), „bekannt“ (Anfangsbestand, Funde bei abgeschalteten Mails, Kopie eines anderen Portals) und „unklar“ (Versand abgebrochen, auch durch Neustart). Unklare Mails werden weiterhin nie doppelt gesendet; ein späterer niedrigerer Preis kommt wieder per Mail.
- Ein Fehler bei einer Quelle hält Mailversand und Backup nicht mehr auf.
- Lehnt der Server eines Empfängers die Mail vorübergehend ab (4xx), geht sie nach 15, 30 und 60 Minuten noch einmal, nur an diese Adresse. Was jeder Empfänger-Server gesagt hat, wird gespeichert.
- Die 10-Minuten-Pause nach einem fehlgeschlagenen Versand gilt nur noch für die Suchart dieser Mail.

### Preise
- Vinted-Versand „ab 2,49 €“ zählt als Untergrenze: App, Mailtext und Betreff („Spiel ab 46,49 €“) schreiben den Preis mit „ab“.
- Vinted: Der Kartentitel zeigt nur noch die Marke; der Titel kommt jetzt aus dem Link. Vorher fiel jede Vinted-Karte durch den Titelabgleich (live: 0 statt 75 Treffer).
- Ein Preis wie „US $20.00 (EUR 18,50)“ wird als 18,50 € gelesen, nicht als 20 €.
- Autos: Kilometerstand und Erstzulassung werden beim Angebot gespeichert. Wird „Erstzulassung ab“ oder „Kilometer bis“ geändert, prüft die nächste Mail wartende Treffer erneut statt sie zu verwerfen. Andere Änderungen, die nur der Anbieter anwendet (Kategorie, Abholung, PLZ, Umkreis, Kraftstoff, Getriebe), verwerfen wartende Treffer; vorher wurden sie trotzdem gemailt.

### Wohnungen
- Ein Kreis („Darmstadt-Dieburg (Kreis)“) ist eine eigene Ortsart. Eine Kreis-Suche ohne Umkreis behält die Wohnungen seiner Gemeinden (vorher alle verworfen). NHW und bauverein werden über den Umriss des Kreises aus OpenStreetMap zugeordnet (einmal abgerufen, dann gespeichert). Die Ortsvorschläge zeigen „Stadt“ oder „Kreis“.
- Kleinanzeigen und Immowelt kennen keine Kreise: Eine Kreis-Suche lässt sie mit „Kennt diesen Ort nicht“ ruhen, statt (wie bisher bei Kleinanzeigen) die Stadt Darmstadt für „Darmstadt-Dieburg“ zu durchsuchen. Bestehende Kreis-Suchen nehmen den Bestand von NHW und bauverein einmal neu als Anfangsbestand auf, ohne Mails.
- Gelöschte Inserate, deren Detailseite im Browser mit 404 oder 410 antwortet, gelten als entfernt und werden nicht mehr gemailt.
- Ortsnamen ohne PLZ werden für die Karte nicht mehr auf das erste Wort gekürzt („Bad Homburg vor der Höhe“ statt „Bad“).

### Für beide
- Ein abgestürzter Suchjob rückt ans Ende der Warteschlange, statt alle anderen Quellen zu blockieren.
- Vorschaubilder haben eindeutige Zustände; nach drei Fehlversuchen aufgegebene Bilder belegen keinen Platz in der Warteschlange mehr, und eine Sperre des Bild-Servers verbraucht keinen Versuch.
- Backups sind jetzt vollständige Generationen in `/data/backups/<Datum>/`: alle Datenbanken samt `cadence.db`, jede per `integrity_check` geprüft, mit `manifest.json`. Wiederherstellung steht in der README.
- Speichern im Frontend folgt einem Muster: Was nach dem Absenden getippt wird, überschreibt keine späte Antwort; Doppelklicks speichern nicht doppelt; ein abgelehntes Speichern behält die Eingabe.
- Neue Tests für die Regeln aus AGENTS.md (tests/test_guarantees.py): Kern ohne Suchart, gebuchte Abrufe, gleiche Anfrage gleicher Cache-Schlüssel, Mail-Historie, gestörte statt leerer Quellen.

## v1.1.0 (2026-10-29)

### Mails
- Der Mailserver lässt sich in der App einrichten (Einstellungen → Mails): Adresse, Passwort, Server, für bekannte Anbieter füllt sich der Server selbst. `SMTP_*` in `compose.yml` geht weiter und hat Vorrang.

### Wohnungen
- WG-Gesucht sucht in jedem Ort in Deutschland, nicht mehr nur in einer festen Städteliste. Kennt WG-Gesucht einen Ort nicht, ruht die Quelle mit Hinweis.
- Duplikate werden auch erkannt, wenn ein Portal nur den Ort und das andere nur den Stadtteil nennt: Die Straße darf nach dem Ort stehen, derselbe lange Titel mit gleicher Fläche zählt als Beweis, und unterschiedlich beschnittene Fotos gelten als gleich.

## v1.0.0 (2026-10-28)

Erste öffentliche Version.

### Wohnungen
- Eine Suche über ImmoScout24, Immowelt, Kleinanzeigen, WG-Gesucht, NHW und bauverein: Ort oder PLZ mit Umkreis, Höchstmiete warm, Mindestfläche und -zimmer, Einzugszeitraum.
- WBS, befristete Angebote, Tauschwohnungen und WG-Zimmer lassen sich ausblenden, dazu eigene Ausschlusswörter.
- Dasselbe Inserat auf mehreren Portalen erscheint nur einmal.
- Warmmiete aus der Detailseite, sonst geschätzt aus der Kaltmiete.

### Preise
- Suchen auf eBay (Sofortkauf), Kleinanzeigen, Vinted und Shops über idealo, mit Versand oder zur Abholung in der Nähe.
- Höchstpreis wahlweise mit Versand; unbekannte Versandkosten lösen keinen Alarm aus.
- idealo-Neupreis als Vergleich.

### Für beide
- Neue Treffer per Sammelmail, alle Portale in einer Liste.
- Merkliste, Ausblenden, Hell- und Dunkelmodus.
- Gemeinsame Taktung aller Abrufe, gestörte Quellen werden als gestört angezeigt.
- Docker-Image mit getarntem Firefox und Xvfb.
