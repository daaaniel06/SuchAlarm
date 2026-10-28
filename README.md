<h1 align="center">Suchalarm</h1>

<p align="center">
  <b>Dein eigener Alarm für neue Wohnungen und gute Preise.</b><br>
  Läuft auf deinem Server, prüft die Portale im Hintergrund und meldet sich per Mail.
</p>

<p align="center">
  <img alt="Python 3.13" src="https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-SQLite-009688?logo=fastapi&logoColor=white">
  <img alt="Lizenz MIT" src="https://img.shields.io/badge/Lizenz-MIT-blue">
</p>

<p align="center">
  <img src="docs/hero.png" alt="Suchalarm: Trefferlisten für Wohnungen und Preise" width="100%">
</p>

- 🔎 **Wohnungen oder alles andere:** Möbel, Videospiele, Kleidung, Fahrräder, Autos, mit Versand oder zur Abholung in deiner Nähe.
- 🔔 **Neue Treffer kommen per Mail**, alle Portale in einer Liste.
- 💶 **Nur was ins Budget passt**, gerechnet mit Versand bzw. Warmmiete.

<p align="center">
  <a href="#schnellstart"><b>Schnellstart</b></a> ·
  <a href="#was-es-kann"><b>Funktionen</b></a> ·
  <a href="#quellen"><b>Quellen</b></a> ·
  <a href="docs/architektur.md"><b>Architektur</b></a>
</p>

## Was es kann

Suchalarm vereint zwei Bereiche in einer App, umschaltbar oben in der Oberfläche.

### Wohnungen

- **Eine Suche, alle Portale:** Ort oder PLZ mit Umkreis, Höchstmiete warm, Mindestfläche und -zimmer, Einzugszeitraum.
- **Störendes raus:** WBS, befristet, Tauschwohnung und WG-Zimmer ausblendbar, dazu eigene Ausschlusswörter.
- **Duplikate erkannt:** dasselbe Inserat auf mehreren Portalen erscheint einmal, auch wenn nur das Foto gleich ist.
- **Warmmiete im Blick:** angegebene Warmmiete aus der Detailseite; fehlt sie, schätzt Suchalarm sie aus der Kaltmiete. Immowelts eigene Schätzung zählt nicht.
- **Detailblatt:** Fakten, Karte, Bewerbung mit Vorlage und Merkliste „Beworben“.

### Preise

- **Für alles Mögliche:** Kategorien für Videospiele, Elektronik, Möbel, Fahrräder und Autos, dazu „Sonstiges“ für alles andere wie Kleidung oder Spielzeug.
- **Versand, Abholung oder beides:** Pro Suche wählbar. Bei Abholung sucht Suchalarm auf eBay und Kleinanzeigen im Umkreis deiner PLZ (10–200 km) und zeigt Ort und Entfernung. Bei Möbeln und Fahrrädern ist das voreingestellt.
- **Ein Preis, der stimmt:** Versand pro Suche ein- oder ausschließbar; unbekannter Versand zählt nie als 0. Bei Vinted ist der Käuferschutz eingerechnet.
- **Mehrere Quellen, eine Liste:** eBay, Kleinanzeigen und Vinted, dazu der idealo-Neupreis als Vergleich.
- **Kategorien mit eigenen Filtern:** Plattform bei Videospielen, Erstzulassung und Kilometer bei Autos, VB und „Zu verschenken“.

### Für beide

- **Mails ohne Flut:** gesammelt im gewählten Rhythmus, mit Bildern; ein Angebot kommt erst bei einem weiteren Preisrückgang wieder.
- **Ehrlicher Status:** gesperrte oder gestörte Quellen werden als solche angezeigt, nie als „keine Treffer“.
- **Mobil bedienbar:** helles und dunkles Design, Wischgesten in der Trefferliste.

Die genauen Regeln (Preisgrenzen, Taktung, Grenzen der Quellen, Mails, Bilder) stehen in [docs/regeln.md](docs/regeln.md).

## Quellen

| Bereich | Quelle | Abruf |
| --- | --- | --- |
| Wohnungen | ImmoScout24 | App-Schnittstelle |
| Wohnungen | Kleinanzeigen, Immowelt | Browser |
| Wohnungen | WG-Gesucht, NHW, bauverein | direkt |
| Preise | eBay (Sofortkauf, gebraucht, Standort Deutschland) | Browser |
| Preise | Kleinanzeigen, Vinted | Browser |
| Preise | idealo (Neupreis-Vergleich) | Browser |

Seiten laufen standardmäßig über ein getarntes Firefox. Direkte Abrufe gibt es nur dort, wo sie ohne Mehraufwand gehen, etwa bei Vorschaubildern und App-Schnittstellen. Jeder Abruf läuft durch dieselbe zentrale Taktung.

**Regionale Grenzen:** ImmoScout24, Kleinanzeigen und Immowelt funktionieren bundesweit. WG-Gesucht ist nur für Darmstadt und Umgebung eingerichtet, NHW und bauverein haben ohnehin nur Bestand in der Region. Andere Städte bei WG-Gesucht brauchen einen Eintrag in `suchalarm/wohnung/portals/wg.py`.

## Schnellstart

**Voraussetzungen:** Python 3.13, Node.js 20+ und Linux oder Windows. Anbieterseiten lädt ein getarntes Firefox über [invisible_playwright](https://github.com/feder-cr/invisible_playwright); unter Linux braucht es Xvfb, unter macOS läuft es nur im Docker-Image.

```bash
git clone https://github.com/daaaniel06/suchalarm.git
cd suchalarm

python3.13 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/invisible-playwright fetch   # einmalig, lädt die Browser-Engine (~250 MB)
npm install

DATA_DIR=./data .venv/bin/uvicorn suchalarm.app:app --host 127.0.0.1 --port 8000
```

Danach läuft die App unter <http://127.0.0.1:8000>. Ohne Browser startet sie trotzdem; Abrufe schlagen dann fehl und werden später erneut versucht.

> [!WARNING]
> Suchalarm hat **keine Benutzeranmeldung**. Wer die Oberfläche erreicht, kann Suchen, Empfänger und Einstellungen ändern. Die App gehört deshalb nur an `127.0.0.1` oder in ein privates Netz (VPN, Tailnet), nie ungeschützt ins Internet. Wer sie trotzdem von außen erreichbar machen will, braucht davor einen Reverse-Proxy mit Anmeldung.

### Mit Docker

```bash
docker build -t suchalarm .
docker run -p 127.0.0.1:8000:8000 -v suchalarm-data:/data suchalarm
```

Das Image bringt das getarnte Firefox samt Xvfb mit und läuft als Benutzer mit UID 10001.

### Mit Docker Compose

[`compose.example.yml`](compose.example.yml) ist ein vollständiges Beispiel mit Datenvolume und Mailversand:

```bash
cp compose.example.yml compose.yml   # SMTP_* und APP_URL eintragen
docker compose up -d --build
```

## Konfiguration

Alles Weitere (Empfänger, Mail-Rhythmus, Quellen) stellt man in der Oberfläche ein und es liegt in SQLite.

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `DATA_DIR` | `/data` | Datenverzeichnis für Datenbanken, Bilder und Backups |
| `APP_URL` | – | Link zur App in den Mails |
| `SMTP_HOST`, `SMTP_PORT` | –, `587` | Mailserver |
| `SMTP_SECURITY` | `starttls` | `starttls` oder `ssl` |
| `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | – | Anmeldung und Absender |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Adressen von Reverse-Proxys, deren `X-Forwarded-*`-Header gelten (Uvicorn), etwa das Docker-Netz des Proxys |

Secrets werden nie über die API ausgeliefert.

## Entwicklung

```bash
./scripts/check.sh                    # ruff, mypy, prettier, eslint, alle Tests
./scripts/check.sh --fix              # vorher formatieren
git config core.hooksPath .githooks   # check.sh vor jedem Commit
```

Dieselben Checks (ohne Frontend und Browser-Test) laufen beim Deploy in der Test-Stage des Dockerfiles.

| Aufgabe | Befehl |
| --- | --- |
| Python-Tests | `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` |
| Frontend-Tests | `npm test` (reine Funktionen aus `static/logic.js`, `node --test`) |
| Browser-Test | braucht einmalig `.venv/bin/playwright install chromium` |
| Abhängigkeiten | in `requirements.in` ändern, dann `.venv/bin/pip-compile --strip-extras --no-emit-index-url requirements.in` |

Weitere Dokumente:

- [AGENTS.md](AGENTS.md): Regeln für Änderungen am Code
- [DESIGN.md](DESIGN.md): Designsystem
- [docs/regeln.md](docs/regeln.md): Fachregeln im Detail
- [docs/architektur.md](docs/architektur.md): Aufbau im Detail

## Architektur

Eine FastAPI-App mit einem Hintergrund-Worker auf derselben Event-Loop, Daten in SQLite, Oberfläche als ES-Module ohne Build-Schritt.

```text
suchalarm/
├── core/       Taktung, Browser, HTTP, Bilder – kennt keine Suchart
├── preis/      Fachmodul Preise: Quellen, Regeln, Tabellen, Mail
├── wohnung/    Fachmodul Wohnungen: Portale, Details, Duplikate, Karten, Mail
├── static/     Oberfläche (HTML, CSS, JS)
├── kinds.py    Verzeichnis der Sucharten
├── worker.py   gemeinsamer Ablauf: prüfen, speichern, Mails, Backup
├── app.py      API
└── store.py    gemeinsame Datenbank
```

Der Kern importiert nichts aus den Fachmodulen. Jede Suchart liefert dieselben Schritte (`prepare`, `fetch`, `save`, `take_pending` …), eigene Tabellen in einer eigenen Datenbankdatei und ihr eigenes Mail-Layout. Eine neue Suchart braucht deshalb keine Änderung an API, Worker oder Mailer. Details stehen in [docs/architektur.md](docs/architektur.md).

## Betrieb

Gedacht ist Suchalarm für einen eigenen Server im privaten Netz, hinter einem Reverse-Proxy wie Caddy.

- **Deploy:** Vor jedem neuen Image die Test-Stage bauen (`docker build --target test .` und starten: ruff, mypy, Tests ohne Netzwerk), erst danach das Image und den Dienst aktualisieren. Images mit dem Commit-SHA taggen, dann ist ein Rollback nur ein Tag-Wechsel.
- **Rollback:** auf einen vorhandenen SHA-Tag zurückgehen; vor Schemaänderungen Daten sichern.
- **Backups:** täglich über die SQLite-Backup-API nach `/data/backups/`, sieben Tage aufbewahrt. Gegen Datenträgerverlust zusätzlich extern sichern.
- **Zugriff:** keine Benutzeranmeldung (siehe Warnung im Schnellstart). Schreibende Anfragen fremder Origins werden abgewiesen. Nur der Proxy sollte einen Port veröffentlichen; `FORWARDED_ALLOW_IPS` auf dessen Adresse setzen.

## Grundsätze

- **Rücksicht auf die Anbieter:** zentrale Taktung je Anbieter, gemeinsame Abfragen für gleiche Suchen, Pause bei Sperren, kein Lösen von CAPTCHAs.
- **Nur beobachten:** Suchalarm kauft nichts, bewirbt sich nicht selbst und schreibt keinen Anbietern.
- **Eine Mail gilt erst als gesendet, wenn der Mailserver sie angenommen hat.**

## Hinweis zur Nutzung

Suchalarm ist ein privates Projekt für den eigenen Gebrauch. Es steht in keiner Verbindung zu den genannten Anbietern. Viele von ihnen untersagen in ihren Nutzungsbedingungen den automatisierten Abruf ihrer Seiten. Das getarnte Firefox und die App-Schnittstelle umgehen dabei bewusst die Erkennung automatisierter Zugriffe, auch wenn keine CAPTCHAs gelöst werden. Ob und wie du Suchalarm einsetzt, liegt in deiner Verantwortung: Nutzungsbedingungen der Anbieter beachten, die Taktung nicht verschärfen und Treffer nicht weiterverbreiten. Ohne Gewähr; Anbieter können ihre Seiten jederzeit ändern oder Zugriffe sperren.

## Lizenz

[MIT](LICENSE)
