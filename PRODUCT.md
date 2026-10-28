# Suchalarm
<!-- impeccable:product-schema 1 -->
## Platform
web
## Stack
Python-Webapp (FastAPI, SQLite, ES-Module ohne Build), Repository daaaniel06/suchalarm. Führt Preisalarm und Wohnungsbot in einer App zusammen, umschaltbar zwischen den Bereichen „Wohnungen“ und „Preise“.
## Users
Einzelpersonen, die Suchalarm selbst im privaten Netz betreiben und darüber Wohnungen sowie günstige neue und gebrauchte Produkte suchen.
## Product Purpose
Gespeicherte Suchen regelmäßig über ein getarntes Firefox (und App-Schnittstellen, wo es ohne Mehraufwand geht) prüfen und neue Wohnungen oder Angebote unter dem persönlichen Höchstpreis per Mail melden.
## Capabilities and Constraints
Quellen Wohnungen: ImmoScout24, Kleinanzeigen, Immowelt, WG-Gesucht, NHW, bauverein. Quellen Preise: eBay Sofortkauf, Kleinanzeigen, Vinted und Shops über idealo; idealo dient zugleich als Neupreis-Vergleich. Pro Suche sind Versandkosten ein- oder ausschließbar. Bei eingeschlossenem Versand dürfen unbekannte Kosten keine Preisalarme auslösen. Daten persistent in SQLite; Betrieb als Docker-Image hinter einem Reverse-Proxy (Beispiel in compose.example.yml). Keine automatischen Käufe, Bewerbungen oder Nachrichten an Anbieter.
## Brand Commitments
Wohnungsbot-Design verbindlich: Geist, ruhige Positionslisten, große Zähler, Pills, Hell/Dunkel aus semantischen Tokens. Deutsche Bedienung.
## Open Decisions
Keine Empfängeradresse vorgegeben: in den Mail-Einstellungen konfigurierbar. Standardintervall 60 Minuten als Implementierungsannahme, pro Suche änderbar.
