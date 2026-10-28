# Suchalarm

- Zuerst git status und git diff; Symbolsuche vor Datei-Reads.
- Produktcode liegt hier; der eigene Betrieb (Compose, Proxy, Deploy) liegt außerhalb dieses Repos, compose.example.yml ist nur ein Beispiel.
- suchalarm/core/ kennt keine Suchart und importiert nichts aus preis/ (oder später wohnung/). Quellen melden sich über cadence.register() und images.register() an; ein neues Fachmodul in suchalarm/__init__.py eintragen und sein worker-Modul (dieselben Schritte wie preis/worker.py, Liste in kinds.py) in kinds.KINDS. app.py, mailer.py und worker.py verzweigen nicht nach Suchart, sie fragen KINDS. Tabellen einer Suchart gehören in deren eigene Datenbank-Datei (cadence.transaction).
- Anbieterseiten standardmäßig über das getarnte Firefox aus invisible_playwright (core/browser.py, startet lokal im Container mit Xvfb; läuft nicht unter macOS). Direkter HTTP-Abruf ist erlaubt, wenn er ohne Mehraufwand funktioniert, etwa Vorschaubilder von Bild-CDNs (core/images.py) oder ImmoScout24 über die App-Schnittstelle (die Website sperrt den Browser). Jeder Abruf, egal wie, läuft durch die Taktung in core/cadence.py (Budgetarten page, image, osm für OpenStreetMap, suggest für Ortsvorschläge).
- Docker Compose, Proxy, persistente Volumes und Deploy des eigenen Betriebs nicht hier pflegen.
- DESIGN.md ist verbindlich. Keine neue visuelle Identität erfinden.
- Bei eingeschlossenem Versand sind unbekannte Kosten kein gültiger Gesamtpreis.
- Erfolgreicher SMTP-Versand muss der Benachrichtigungs-Historie vorausgehen.
- Gesperrte Quellen als gestört anzeigen, niemals als erfolgreiche Null-Treffer-Suche.
- Keine Käufe oder Nachrichten an Verkäufer.
- Tests: .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v, Frontend: npm test. Browser-Test (Extraktions-JS) benötigt Playwright Chromium aus requirements-dev.txt; die App selbst nutzt invisible_playwright.
- Vor jedem Commit: ./scripts/check.sh (--fix formatiert vorher): ruff, mypy, prettier, eslint, alle Tests. Der Deploy-Test prüft ruff und mypy.
- Frontend sind ES-Module ohne Build: reine Logik in suchalarm/static/logic.js (Tests in tests/logic.test.js), geteilter Zustand, Serveraufrufe und render() in page.js (Setter statt Zuweisung), Rendern und Klicks in app.js, das nur importiert und von niemandem importiert wird, je Bereich ein Modul (hits, rows, swipe, searches, settings, editor, flat-editor, sheet für Detailblatt und Bewerbung, steps für den Quellenstatus), Kleinteile in ui.js. Eine Variable ändert nur ihr eigenes Modul (ESLint no-import-assign); exportiert wird nur, was ein anderes Modul importiert.
- Abhängigkeiten in requirements.in ändern, dann pip-compile; requirements.txt nie von Hand.
