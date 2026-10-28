# Sicherheit

## Lücken melden

Sicherheitslücken bitte **nicht** als öffentliches Issue melden, sondern über [„Report a vulnerability“](https://github.com/daaaniel06/suchalarm/security/advisories/new) (GitHub Private Vulnerability Reporting). Hilfreich sind eine kurze Beschreibung, betroffene Datei oder Endpunkt und Schritte zum Nachvollziehen.

Suchalarm ist ein privates Projekt ohne festen Support. Ich antworte so bald wie möglich, kann aber keine Fristen zusagen.

## Was im Rahmen liegt

Suchalarm ist für den Betrieb im privaten Netz gebaut und hat bewusst **keine Benutzeranmeldung** (siehe README). Dass jemand mit Zugriff auf die Oberfläche Einstellungen ändern kann, ist deshalb keine Lücke.

Interessant sind dagegen zum Beispiel:

- Daten von Anbieterseiten, die im Browser Skripte ausführen (XSS) oder die Content-Security-Policy umgehen
- Anfragen fremder Websites, die trotz Origin-Prüfung Daten ändern (CSRF)
- Wege, über den Browser oder den Bild-Abruf Adressen im privaten Netz zu erreichen (SSRF)
- SMTP-Zugangsdaten oder andere Secrets, die über die API oder in Fehlermeldungen sichtbar werden
