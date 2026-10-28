# Mitmachen

Schön, dass du helfen willst. Suchalarm ist ein kleines Privatprojekt, deshalb ein paar kurze Regeln.

## Bevor du loslegst

- Für größere Änderungen erst ein [Issue](https://github.com/daaaniel06/SuchAlarm/issues) aufmachen, damit wir kurz abstimmen, ob es passt.
- Gute Einstiege sind mit [`good first issue`](https://github.com/daaaniel06/SuchAlarm/labels/good%20first%20issue) markiert.
- Sicherheitslücken bitte nicht als Issue, sondern wie in [SECURITY.md](SECURITY.md) beschrieben melden.

## Einrichten

Siehe [Schnellstart → Ohne Docker](README.md#ohne-docker) in der README. Danach:

```bash
git config core.hooksPath .githooks   # Checks vor jedem Commit
./scripts/check.sh                    # ruff, mypy, prettier, eslint, alle Tests
./scripts/check.sh --fix              # vorher formatieren
```

## Was einen guten Pull Request ausmacht

- Klein und auf eine Sache beschränkt, mit Test für neues Verhalten.
- `./scripts/check.sh` ist grün.
- Die Regeln in [AGENTS.md](AGENTS.md) gelten auch für Menschen: zum Beispiel läuft jeder Abruf durch die Taktung in `core/cadence.py`, gesperrte Quellen werden als gestört angezeigt und nie als „keine Treffer“, und es gibt keine automatischen Käufe oder Nachrichten an Anbieter.
- Oberfläche: an [DESIGN.md](DESIGN.md) halten, Bedienung auf Deutsch.
- Keine echten Inseratstexte, Namen oder Adressen in Tests; Beispieldaten bitte erfinden.

## Lizenz

Mit einem Beitrag stimmst du zu, dass er unter der [Lizenz des Projekts](LICENSE) (PolyForm Noncommercial 1.0.0) steht.
