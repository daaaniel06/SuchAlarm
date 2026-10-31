# Regeln im Detail

Wie Suchalarm Preise vergleicht, Anbieter abfragt und Mails verschickt. Der Überblick steht im [README](../README.md).

## Suchen und Treffer

- Eine Suche hat Suchbegriff, Preisspanne (optionaler Mindestpreis, Höchstpreis), Quellen und Ausschlusswörter.
- Unter „Weitere Optionen → Auch suchen nach“ stehen bis zu zwei andere Schreibweisen („RE Requiem“). Jede ist ein eigener Seitenabruf bei eBay, Kleinanzeigen und Vinted und wartet auf ihren Platz im Budget; ein Treffer muss die Wörter einer der Schreibweisen enthalten. idealo sucht den Neupreis nur mit dem Suchbegriff.
- Alle Suchwörter müssen im Angebotstitel vorkommen, Akzente zählen nicht („Résident“ = „Resident“). Zwei Wörter dürfen auch zusammengeschrieben sein und umgekehrt („Spiderman“ = „Spider-Man“, „iPhone13“ = „iPhone 13“). Plattform und Edition gehören daher in den Suchbegriff.
- Angebote unter dem Mindestpreis (etwa 3-€-Ramsch) werden komplett ausgeblendet und lösen keine Mail aus. Maßgeblich ist der Vergleichspreis, bei unbekanntem Versand der Artikelpreis.
- „Passend“ richtet sich nach den Preis- und Versandfiltern. Eine aktuelle Quellenstörung verschiebt bereits gespeicherte passende Angebote nicht nach „Alle“; der Quellenstatus bleibt separat sichtbar.
- Über den Treffern steht der idealo-Neupreis als Vergleich. Ein Klick auf einen Treffer öffnet das Angebot beim Anbieter.
- Nach rechts wischen (Finger, Maus oder Zwei-Finger-Geste auf dem Trackpad) merkt ein Angebot, nach links wischen blendet es aus Treffern und Mails aus, mit „Rückgängig“. Ausgeblendete lassen sich unter Einstellungen wiederherstellen.
- Die Merkliste liegt auf dem Server und speichert einen Schnappschuss, damit gemerkte Angebote auch nach dem Verschwinden beim Anbieter sichtbar bleiben.
- Neue Treffer seit dem letzten Besuch tragen pro Gerät einen grünen Punkt, bis sie geöffnet werden; der Tab-Titel zeigt ihre Anzahl.
- Lange Listen kommen in Seiten zu 30. „Alle Angebote löschen“ steht unter Einstellungen.

## Versand und Preisgrenzen

- Versandkosten sind pro Suche ein- oder ausschließbar. Eingeschaltet zählt Artikel + bekannter Versand, ausgeschaltet nur der Artikelpreis.
- Fehlende Versandkosten werden niemals als 0 gewertet.
- eBay, Vinted und Kleinanzeigen erhalten den Preisbereich bereits beim Suchaufruf:
  - Ohne Versand wird der Artikelpreis von Mindestpreis bis unter den Höchstpreis eingegrenzt.
  - Mit Versand wird beim Anbieter nur die obere Artikelpreisgrenze gesetzt; ein günstigerer Artikel kann durch Versand noch in den gewünschten Gesamtpreisbereich fallen.
  - Kleinanzeigen-Grenzen werden nach außen auf ganze Euro gerundet.
- Die genaue Prüfung von Centbeträgen, Versand und Suchwörtern bleibt lokal.
- eBay wird explizit auf Artikelstandort Deutschland begrenzt; erkennbar ausländische Artikelstandorte werden zusätzlich beim Auslesen verworfen. Alte weltweite eBay-Ergebnisse teilen sich keinen Cache mit dieser Suche.
- eBay sortiert nach „Beste Ergebnisse“ und liefert 240 Angebote pro Seite statt 60.
- Kleinanzeigen sortiert nach „Neueste“: Bei Abrufen alle 15 Minuten steht jedes neue Angebot mindestens einmal auf der ersten Seite.
- Vinted nutzt die native Relevanzsortierung, damit billige andere Spiele nicht die erste Seite füllen; passende Treffer sortiert die App anschließend nach Artikel- oder Gesamtpreis.
- idealo bleibt für den unabhängigen Neupreisvergleich ohne Preisgrenze. Bei idealo zählt immer der Gesamtpreis inklusive Versand, unabhängig von „Versand einrechnen“: Gewählt wird der Shop mit dem günstigsten Gesamtpreis, und nur dieser löst einen Alarm aus. Shops mit unbekanntem Versand haben keinen gültigen Gesamtpreis. Zeigt eine Angebotskarte Artikel- und Gesamtpreis inklusive Versand, wird die Differenz als Versandkosten übernommen.
- Unveränderte gespeicherte Seiten können nach einer Parserkorrektur lokal neu ausgewertet werden. Das startet keinen zusätzlichen Anbieterabruf und verändert weder Prüfzeitpunkt noch 12-Stunden-Takt.

## Kategorien und Filter

- Eine neue Suche beginnt mit einer Kategorie (Chips oben im Formular): Videospiele, Elektronik, Möbel, Fahrräder, Autos oder Sonstiges. Die Kategorie füllt Quellen, Lieferart, Umkreis, Verhandlungsbasis und Ausschlusswörter vor; alles bleibt änderbar.
- Ausschlusswörter, „Versand einrechnen“ und „Suche aktiv“ liegen unter „Weitere Optionen“; zugeklappt nennt die Zeile, was darin gesetzt ist.
- Die Kategorie gibt neuen Suchen Ausschlusswörter vor. Bei Videospielen gehört Deko dazu, die Verkäufer als Spiel einstellen (Poster, Affiche, Mauspad, Tapis de souris, Schild, Targa, Flagge, Figur, Sticker …); alles bleibt in der Suche änderbar.
- Ausblenden lehrt die Kategorie: Ein Wort, das in mindestens zwei ausgeblendeten Titeln steht (ohne die Suchwörter ihrer Suche) und dort öfter als in den behaltenen Treffern, filtert ab dem nächsten Abruf alle Suchen dieser Kategorie mit, nie aber ein Wort der eigenen Suche. Wiederherstellen nimmt die Lektion zurück; Titel ausgeblendeter Angebote bleiben gespeichert, auch wenn das Angebot verschwindet (preis.hidden_titles).
- Händler-Signal: Hat ein Verkäufer drei oder mehr passende Inserate in einem Ergebnis, fliegen alle seine Inserate raus. Wer sein eigenes Exemplar verkauft, stellt es einmal ein; Poster, Mauspads und Schilder kommen in Serien. Bisher nur bei Vinted, wo die Verkäufer-ID in den eingebetteten Seitendaten steht (Stand 29.10.2026); eBay-Händler mit echter Ware sollen nicht wegfallen, Kleinanzeigen nennt den Verkäufer nicht auf der Karte.
- Bei eBay (`_sacat`), Kleinanzeigen (`c…`) und Vinted (`catalog[]`) sucht die App nur in der passenden Rubrik (Stand 23.10.2026 live geprüft; Elektronik bei eBay ohne Rubrik, weil es dort keine gemeinsame gibt).
- Videospiele haben eine Plattform, die wie ein Suchwort im Titel stehen muss. Ausnahme Vinted und eBay: Dort filtert der Anbieter selbst nach Plattform (Vinted `video_game_platform_ids[]`, eBay das Artikelmerkmal `Plattform`, Stand 29.10.2026 live geprüft), der Titel muss sie nicht nennen. Für Switch 2 hat eBay noch kein Merkmal, dort zählt weiter der Titel. Ein Titel, der nur andere Plattformen nennt, fliegt überall raus, weil die Anbieter ihre Listen sonst mit anderen Plattformen auffüllen bzw. Verkäufer das Merkmal falsch setzen.
- Autos haben „Erstzulassung ab“ und „Kilometerstand bis“. Beides geht als Filter an Kleinanzeigen, und auf der Karte genannte Werte außerhalb der Grenzen werden zusätzlich lokal verworfen. Fehlende Angaben bleiben drin. Bei eBay gelten diese Filter nicht, das Formular sagt das.
- „Verhandlungsbasis zulassen“ nimmt VB-Angebote mit Preis auf, der genannte Betrag zählt. Ohne diese Option werden VB-Angebote ausgeschlossen, ebenso Auktionen.
- „Zu verschenken“ nimmt Gratis-Angebote von Kleinanzeigen auf. Für sie gilt der Mindestpreis nicht, auch wenn Versand dazukommt (der Höchstpreis zählt weiter), und Kleinanzeigen bekommt dann keine untere Preisgrenze (sonst blendet es sie selbst aus).
- Bestehende Suchen aus der Zeit vor den Kategorien gelten als „Sonstiges“ mit Versand und behalten ihre bisherigen Abfragen. Die Suchliste ist nach Kategorien gruppiert.

### Lieferart

- **Versand:** deutschlandweit.
- **Abholung:** sucht bei eBay und Kleinanzeigen im Umkreis (10–200 km) einer PLZ. Vinted und idealo können das nicht und sind dann gesperrt; Versandkosten spielen keine Rolle. Treffer zeigen Ort und Entfernung.
- **Beides:** liest bei eBay und Kleinanzeigen zwei Seiten: den Umkreis der PLZ und deutschlandweit, wovon nur versendbare Angebote zählen (Kleinanzeigen „Versand möglich“, eBay alles außer „Nur Abholung“). Die zweite Seite wartet auf ihren eigenen Platz im Seitenbudget. Vinted und idealo suchen deutschlandweit mit.
- Bei Abholung und Beides zählt immer der Artikelpreis.
- Kleinanzeigen braucht für die PLZ einmalig eine interne Orts-ID. Dieser Abruf zählt wie ein normaler Seitenaufruf und wird danach dauerhaft gespeichert.

## Abfragen und Taktung

Die App läuft mit einem Uvicorn-Worker. Eine dauerhafte Warteschlange (`scheduler.py`) plant einzelne Aufträge je Anbieter und Suchbegriff statt ganzer Suchen; es gibt kein zusätzliches Suchintervall. Alle Abrufe bleiben seriell, und Reihenfolge, Grenzen und manuell eingereihte Prüfungen überstehen Neustarts.

Zentrale Taktung (`cadence.py`, `cadence.db`):

- Jede Quelle gehört zu einer Taktgruppe (`core/cadence.py`), jeweils plus bis zu 4 Minuten Zufallsversatz:
  - **Portale** (eBay, Kleinanzeigen, Vinted, später ImmoScout24, WG-Gesucht, Immowelt, ohne-makler.net, immobilien.de): alle 15 Minuten, nachts (01–06 Uhr) mindestens stündlich.
  - **Vermieter** (später bauverein, NHW): stündlich, nachts mindestens alle 2 Stunden.
  - **Vergleich** (idealo): alle 12 Stunden, plus bis zu 10 Minuten Versatz.
- Pro Anbieter 40–70 Sekunden zwischen Seitenaufrufen, höchstens 60 pro Stunde, mit 5–10 Sekunden Abstand auch zwischen verschiedenen Anbietern.
- Eine idealo-Produktseite nach der Suchseite zählt als eigener Seitenaufruf und wartet auf den nächsten erlaubten Platz; bereits gelesene Suchseiten werden lokal wiederverwendet.
- Suchen mit gleicher Abfrage teilen sich einen Abruf und ein gemeinsames Ergebnis; Ausschlüsse werden danach lokal angewendet. Nur Abfragen mit demselben Suchbegriff und denselben tatsächlichen Anbieter-Preisgrenzen teilen sich den Cache.
- Wartende Aufträge starten keinen Browser. Länger wartende Aufträge werden vor erneut fälligen bedient.
- Jede Anbieterabfrage hat ein begrenztes Zeitbudget. Bei Fehlern bleiben vorherige Angebote als veraltet sichtbar, lösen aber keine Mail aus. Nach einem Neustart werden fällige Suchen erneut aufgenommen.

### „Jetzt prüfen“

- Startet einen frischen Suchabruf unabhängig vom automatischen Intervall, mit 2–4 Sekunden Abstand zwischen Anbietern und 8–12 Sekunden beim selben Anbieter.
- Hat sofort Vorrang vor noch nicht gestarteten automatischen Aufträgen; ein laufender Seitenabruf wird fertig gelesen.
- Bereits laufende gleiche Abfragen werden geteilt. Mehrfachklicks erzeugen keine weiteren Läufe und verschieben keinen Start. Pro tatsächlicher Abfrage gilt nach Abschluss eine Sperrzeit von 60 Sekunden.
- Anbieter-Sperren und das gemeinsame Stundenbudget bleiben wirksam. Automatische Folgeabrufe halten danach wieder den normalen Abstand ein.
- Nötige Folgeseiten werden wiederverwendet; Vinted-Versand- und Bild-Caches werden nicht unnötig geleert. idealo bleibt automatisch bei 12 Stunden, ein manueller Klick darf bewusst früher aktualisieren.
- Unter Einstellungen lässt sich die automatische Suche global pausieren; „Jetzt prüfen“ funktioniert weiterhin.

## Grenzen der Quellen

- Die erste Ergebnisseite wird nach passenden Titeln ausgewertet; keine Garantie auf das marktweit günstigste Angebot. Anzeigen können während einer Abfrage verschwinden. Preise und Zustand beim Anbieter prüfen; Käuferschutz- und Plattformgebühren sind nicht enthalten.
- eBay-Versandangaben werden auch aus den neuen Angebotskarten gelesen (einschließlich „Gratis Lieferung“). Kostenlose Rücksendungen gelten nicht als kostenloser Versand.
- Bei Vinted sind „inkl.“-Preise Käuferschutz und keine Versandangabe. Der angezeigte Mindestversand („ab“) zählt für Sortierung und Alarm; Oberfläche und Mail nennen „Versand ab“.
- Fehlender Vinted-Versand wird über einzelne Detailabrufe ergänzt: dieselben globalen und Vinted-Seitenbudgets wie bei Suchabfragen, Suchabfragen haben Vorrang, 12 Stunden Cache pro Anzeige, kein Abruf verschwundener oder inaktiver Treffer. Widersprüchliche Beträge bleiben unbekannt.
- Vinted nennt den verbindlichen Versand erst beim Checkout: Bei eingeschaltetem Versandfilter lösen solche Angebote keine Mail aus.
- idealo-Aggregatpreise dienen ausschließlich als Neupreis-Referenz; aus ihnen werden keine fiktiven Einzelangebote erzeugt. Ein Shop-Alarm braucht ein lieferbares Einzelangebot mit bestätigtem EUR-Preis. Bei Suchbegriffen mit mehreren Varianten hängt die Aussagekraft von einem präzisen Suchbegriff ab.
- Der Neupreis über den Treffern ist der günstigste Gesamtpreis inklusive Versand aus der Angebotsliste des Produkts. Kennt idealo bei keinem Shop den Versand, steht stattdessen idealos „ab“-Preis ohne Versand da.

### Sperren und Sicherheitsabfragen

- Anbieter können Sicherheitsabfragen, Einwilligung oder eine Anmeldung verlangen. Dann zeigt die App einen Fehler und versucht es im nächsten Intervall erneut. Sie umgeht keine Captchas.
- Sperrt ein Anbieter mit HTTP 401/403/429 oder einer Sicherheitsprüfung, pausiert er für alle Suchen 30 Minuten, bei weiteren Sperren 1, 2 und 6 Stunden. Die App zeigt „Pause bis …“.
- Diese Einschränkungen sind keine Null-Treffer-Meldungen.
- Beim lokalen Ersttest am 20.10.2026 war Vinted lesbar, eBay und idealo lieferten HTTP 403, und Kleinanzeigen lieferte mit einem breiten Suchbegriff lesbare Angebotskarten (die spezifische Beispielsuche hatte dort keine Ergebnisse). Der Server kann ein anderes Ergebnis haben.

## Mails

- Unter Mails bis zu fünf Empfänger eintragen, Rhythmus wählen (sofort, alle 30 Minuten, täglich; bei „Sofort“ optional Pakete von 2–3 Angeboten, spätestens nach 2 Stunden), Testmail senden und Benachrichtigungen einschalten. Es werden keine Empfänger oder aktiven Suchaufträge vorab angelegt.
- Neue Treffer aller Suchen landen in einem Postausgang und kommen gesammelt in einer Mail (Text und HTML).
- Ein für die Mail vorgemerktes Angebot bleibt bis zum Versand, auch wenn es beim nächsten Abruf nicht mehr auf der ersten Seite steht (neuere Angebote können es nur nach hinten geschoben haben) und auch, wenn die Suche bearbeitet wird; dann zählen beim Versand ihre neuen Filter.
- Der erste lesbare Abruf einer Quelle ist der Startstand und löst keine Mail aus. Gleiche Angebote melden sich erst bei einem weiteren Preisrückgang erneut. Bei Wohnungen gilt das für die Warmmiete: Sinkt sie bei einem bekannten Inserat, auch aus dem Startstand oder einem vorher zu teuren, liest Suchalarm die Detailseite neu und meldet es, wenn es jetzt passt und günstiger ist als zuletzt gemeldet.
- Was bei ausgeschalteten Mails oder pausierten Suchen gefunden wird, gilt als bekannt und wird später nicht nachgeschickt.
- Lehnt der Mailserver eine Mail sicher ab, bleiben die Angebote im Postausgang und werden nach 10 Minuten erneut versucht.
- Ist unklar, ob die Mail angekommen ist (Verbindung während der Übergabe abgebrochen oder Neustart beim Versand), wird sie nicht erneut gesendet: Doppelte Mails zu vermeiden ist wichtiger als eine fehlende. Die Mails-Seite zeigt den Ausgang jeder Mail.

### Shop-Vergleich

- Ist ein lieferbares idealo-Shopangebot günstiger als alle aktuell passenden Gebrauchtangebote derselben Suche, nennt die Mail das samt Ersparnis.
- Verglichen wird nur mit erfolgreichen Gebrauchtabfragen der letzten 3 Stunden; bei eingerechnetem Versand zählen Gebrauchtangebote mit unbekanntem Versand nicht.
- Ein „ab“-Vergleichspreis ist nie ein Shopangebot. Auch Shopangebote lösen nur unter dem Höchstpreis eine Mail aus, und wie alle Angebote erst wieder bei einem weiteren Preisrückgang. Schwankende Gebrauchtpreise allein lösen keine Mail aus.

## Bilder

- Vorschaubilder werden nur von den Bild-CDNs der Anbieter geladen (https, feste Hostliste, keine privaten Adressen), einzeln, pro Anbieter mit 8–15 Sekunden Abstand und höchstens 200 pro Stunde.
- Sie werden auf 320×240 WebP verkleinert und unter `/data/images/` gespeichert. Bilder von Angeboten unter dem Höchstpreis kommen zuerst.
- Die Oberfläche lädt nur diese lokalen Kopien; beim Öffnen gibt es keine externen Bildaufrufe.
- Fehlgeschlagene Bilder werden nach 6 Stunden höchstens dreimal erneut versucht. Nach 30 Tagen ohne Sichtung oder ab 128 MB/2000 Dateien wird aufgeräumt.
- Mails betten die Bilder als JPEG ein (keine Links auf Anbieter), sie bleiben also auch nach der Aufräumung sichtbar. Ein unlesbares Bild fehlt in der Mail, hält sie aber nicht auf. Eine „Sofort“-Mail wartet höchstens 5 Minuten auf noch fehlende Bilder.
