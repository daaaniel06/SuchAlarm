# Changelog

## Unveröffentlicht (Branch claude/bugfixes)

### Mails
- Die Historie unterscheidet „versendet“ (vom Mailserver angenommen), „bekannt“ (Anfangsbestand, Funde bei abgeschalteten Mails, Kopie eines anderen Portals) und „unklar“ (Versand abgebrochen, auch durch Neustart). Unklare Mails werden weiterhin nie doppelt gesendet; ein späterer niedrigerer Preis kommt wieder per Mail.
- Ein Fehler bei einer Quelle hält Mailversand und Backup nicht mehr auf.

### Preise
- Vinted-Versand „ab 2,49 €“ zählt als Untergrenze: App, Mailtext und Betreff schreiben den Preis mit „ab“.
- Ein Preis wie „US $20.00 (EUR 18,50)“ wird als 18,50 € gelesen, nicht als 20 €.
- Autos: Kilometerstand und Erstzulassung werden beim Angebot gespeichert. Wird „Erstzulassung ab“ oder „Kilometer bis“ geändert, prüft die nächste Mail wartende Treffer erneut statt sie zu verwerfen. Andere Änderungen, die nur der Anbieter anwendet (Kategorie, Abholung, PLZ, Umkreis, Kraftstoff, Getriebe), verwerfen wartende Treffer; vorher wurden sie trotzdem gemailt.

### Wohnungen
- Ein Kreis („Darmstadt-Dieburg (Kreis)“) ist eine eigene Ortsart. Eine Kreis-Suche ohne Umkreis behält die Wohnungen seiner Gemeinden (vorher alle verworfen); den Gesamtbestand von NHW und bauverein kann sie nicht zuordnen und nimmt ihn nicht. Die Ortsvorschläge zeigen „Stadt“ oder „Kreis“.
- Gelöschte Inserate, deren Detailseite im Browser mit 404 antwortet, gelten als entfernt und werden nicht mehr gemailt.
- Ortsnamen ohne PLZ werden für die Karte nicht mehr auf das erste Wort gekürzt („Bad Homburg vor der Höhe“ statt „Bad“).

### Für beide
- Ein abgestürzter Suchjob rückt ans Ende der Warteschlange, statt alle anderen Quellen zu blockieren.
- Vorschaubilder haben eindeutige Zustände; nach drei Fehlversuchen aufgegebene Bilder belegen keinen Platz in der Warteschlange mehr, und eine Sperre des Bild-Servers verbraucht keinen Versuch.
- Das tägliche Backup sichert alle Datenbankdateien ohne Schreibzugriff dazwischen.
- Ein während des Speicherns neu getipptes Mail-Passwort wird nicht mehr gelöscht.
- Neue Tests für die Regeln aus AGENTS.md (tests/test_guarantees.py).

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
