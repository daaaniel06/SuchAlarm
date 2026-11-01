---
name: Suchalarm
description: Ruhige Trefferlisten für Wohnungen und Preise
colors:
  light-bg: "#fff"
  light-ink: "#0a0a0a"
  light-secondary: "#636369"
  light-muted: "#707077"
  light-hair: "#ececef"
  light-fill: "#f4f4f5"
  light-fill2: "#e9e9ec"
  light-surface: "#fff"
  light-green: "#09703b"
  light-red: "#c5221f"
  light-amber: "#9a5200"
  light-blue-bg: "#e7efff"
  light-blue: "#1a47b8"
  light-green-bg: "#e4f4e9"
  light-purple-bg: "#efeafc"
  light-purple: "#5b3cc4"
  light-teal-bg: "#e2f4f4"
  light-teal: "#0f6b6b"
  dark-bg: "#000"
  dark-ink: "#f5f5f7"
  dark-secondary: "#a1a1a8"
  dark-muted: "#a1a1a8"
  dark-hair: "#3a3a3e"
  dark-fill: "#1c1c1e"
  dark-fill2: "#2c2c2e"
  dark-surface: "#161618"
  dark-green: "#34c77b"
  dark-red: "#ff6b61"
  dark-amber: "#f5b35c"
  dark-blue-bg: "#16243f"
  dark-blue: "#8fb2ff"
  dark-green-bg: "#13291c"
  dark-purple-bg: "#221a3d"
  dark-purple: "#b6a2ff"
  dark-teal-bg: "#0f2a2a"
  dark-teal: "#6fd6d0"
typography:
  display:
    fontFamily: "Geist, ui-sans-serif, system-ui, sans-serif"
    fontSize: "clamp(52px, 7vw, 72px)"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-.04em"
  headline:
    fontFamily: "Geist, ui-sans-serif, system-ui, sans-serif"
    fontSize: "38px"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "-.035em"
  title:
    fontFamily: "Geist, ui-sans-serif, system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 650
    letterSpacing: "-.02em"
  body:
    fontFamily: "Geist, ui-sans-serif, system-ui, sans-serif"
    fontSize: "15px"
    lineHeight: 1.5
  label:
    fontFamily: "Geist, ui-sans-serif, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 600
rounded:
  "nav": "14px"
  "row": "16px"
  "group": "20px"
  "sheet": "24px"
  "dialog": "28px"
  "pill": "999px"
spacing:
  "4": "4px"
  "6": "6px"
  "8": "8px"
  "12": "12px"
  "16": "16px"
  "20": "20px"
  "24": "24px"
  "28": "28px"
  "32": "32px"
  "48": "48px"
  "56": "56px"
components:
  button-primary:
    backgroundColor: "{colors.light-ink}"
    textColor: "{colors.light-bg}"
    rounded: "{rounded.pill}"
    padding: "10px 20px"
  button-secondary:
    backgroundColor: "{colors.light-fill}"
    textColor: "{colors.light-ink}"
    rounded: "{rounded.pill}"
    padding: "10px 20px"
  field-group:
    backgroundColor: "{colors.light-fill}"
    rounded: "{rounded.group}"
    padding: "0 16px"
  navigation:
    rounded: "{rounded.nav}"
    padding: "13px 16px"
  source-chip:
    backgroundColor: "{colors.light-fill}"
    rounded: "{rounded.pill}"
    padding: "8px 12px"
  offer-row:
    rounded: "{rounded.row}"
    padding: "16px 12px"
  switch:
    rounded: "{rounded.pill}"
    width: "48px"
    height: "28px"
  dialog:
    backgroundColor: "{colors.light-surface}"
    rounded: "{rounded.dialog}"
    padding: "28px"
---
# Design System: Suchalarm

## Overview

**Creative North Star: "Das persönliche Preis-Depot"**

Suchalarm hat eine klare visuelle Welt: Geist, Weiß und Schwarz, ruhige Positionslisten und große Zähler. Die Oberfläche wirkt persönlich, sachlich und leicht. Preise, Quellen und Aktualität erhalten Vorrang vor dekorativen Flächen.

Die Darstellung folgt automatisch dem hellen oder dunklen Systemmodus. Flächen werden durch Tonwerte und Abstände gegliedert; Farbe trägt fachliche Bedeutung. Desktop-Seitenleiste und mobile Bodennavigation bilden dieselbe ruhige Orientierung.

**Key Characteristics:**
- Monochrome Grundfläche mit semantischen Farbsignalen.
- Kompakte, offene Listen und großzügige Abschnittsabstände.
- Große Kennzahlen, tabellarische Beträge und lokal geladene Geist.

Die Gestaltung ist aus dem implementierten CSS dokumentiert. Das Frontmatter enthält reale helle und dunkle Werte; die laufende Anwendung verwendet dafür dieselben semantischen CSS-Variablen mit automatischem Moduswechsel.

## Colors

Primär sind Tinte und Grundfläche: Sie invertieren zwischen Hell und Dunkel. Sekundärtext und gedämpfter Text staffeln Informationen; Haarlinien trennen Gruppen sparsam. Füllung und zweite Füllung tragen Bedienelemente und Hover. Die eigene Oberfläche hebt Dialoge und ausgewählte Segmente tonal ab.

**The Meaning-Only Rule.** Farbe erscheint nur, wenn sie etwas bedeutet – neu, passend, gestört, prüfen, Quelle. Keine Akzentfarbe zur Zierde.

**The Semantic Color Rule.** Grün markiert passende Preise, neue Treffer, Beworbenes, das kleine Anbieter-Etikett vorn in der Fußzeile jeder Listenzeile und aktive Schalter; Bernstein markiert unsichere oder veraltete Daten, Rot Fehler und gefährliche Aktionen. Quellenfarben bleiben auf Quellenkennzeichen begrenzt.

Blau kennzeichnet eBay, Grün Kleinanzeigen, Türkis Vinted und Violett idealo. Jede Quellenfarbe besitzt einen passenden Hintergrund. Diese Kennzeichen sind ergänzend beschriftet; Farbe allein trägt keine Quellenerkennung.

## Typography

Geist Variable wird lokal als WOFF2 geladen, mit System-Sans als Fallback. Die Rollen im Frontmatter beschreiben Desktop-Größen. Auf Mobilgeräten wird die Hauptüberschrift kleiner (32px); Listenbezeichnungen sinken auf (14px). Kleine Hilfstexte liegen bei (12px), Begleittexte bei (13px). Preise stehen mit mittlerem Gewicht (650), Listentitel mit (600).

**The Price Alignment Rule.** Beträge und Kennzahlen verwenden tabellarische Ziffern; Angebotspreise stehen rechts und bleiben als zusammenhängender Betrag lesbar.

## Layout

Die Desktop-Hülle kombiniert eine feste Seitenleiste (244px) mit einem flexiblen Hauptbereich. Der Hauptbereich hat eine maximale Außenbreite (816px) und seitliche Innenabstände (48px); daraus ergeben sich maximal (720px) nutzbare Inhaltsbreite. Die Seitenleiste bleibt am oberen Fensterrand stehen. Listen verwenden Quelle, flexiblen Titel und Preis als drei Spalten.

Bei maximal (819px) wird die Navigation unten fixiert. Der Hauptbereich erhält seitlich (24px), unten (112px) und maximal (720px) Außenbreite. Bei maximal (380px) sinkt der Seitenabstand auf (16px). Die Bodennavigation berücksichtigt den sicheren Bildschirmrand. Mobile Angebotszeilen sind dichter; ihre Quellenzeichen schrumpfen von (44px) auf (40px), auf sehr schmalen Geräten auf (32px).

**The One Navigation Rule.** Ab dem mobilen Umbruch ersetzt die Bodennavigation die Seitenleiste. Reservierter Platz unter dem Inhalt hält die letzten Aktionen erreichbar.

## Elevation & Depth

Es gibt keine Box-Schatten. Tonale Füllungen, wenige Haarlinien und Abstand übernehmen die Gliederung. Der Dialog erhält einen abgedunkelten Hintergrund (rgb(10 10 10 / .4)); er erzeugt räumliche Priorität ohne Schlagschatten.

**The Quiet List Rule.** Angebote und Suchen bleiben offene Zeilen ohne Schatten und ohne dauerhafte Kartenrahmen. Hover hebt die Zeile durch einen neutralen Tonwert hervor.

## Shapes

Pills tragen Aktionen, Filter und Quellenoptionen. Listenzeilen sind weich gerundet; Feldgruppen fassen zusammengehörige Eingaben als größere ruhige Fläche. Quellenzeichen sind kreisförmig. Desktop-Dialoge sind vollständig gerundet; mobile Dialoge schließen als unteres Sheet an den Bildschirmrand an und runden nur die oberen Ecken. Radien folgen dem Frontmatter.

## Components

Primäre Buttons invertieren Tinte und Grundfläche; sekundäre Buttons nutzen neutrale Füllung. Beide haben eine Mindesthöhe (44px), wechseln beim Hover den Tonwert und reagieren beim Drücken mit einer kleinen Skalierung (.98). Deaktivierte Aktionen sind halbtransparent und zeigen einen Wartecursor.

Ein Icon steht rechts vom Text, nur wo es das Ziel zeigt: ↗ für eine fremde Seite („Inserat öffnen“, „Kopieren & öffnen“), zwei Blätter für die Zwischenablage. Kurze Aktionen innerhalb einer Zeile (Hinzufügen, Testmail, Entfernen) sind runde Icon-Buttons; ihr Name steht in `aria-label` und `title`.

Feldgruppen nutzen Innenabstand und Haarlinien zwischen den Zeilen statt einzelner Eingaberahmen. Zeilen, die an einem Schalter hängen (Rhythmus, Treffer pro Mail), folgen ihm eingerückt und ohne Haarlinie; was dasselbe Ziel betrifft, steht in einer Gruppe, etwa Mailserver und Empfänger. Labels bleiben sichtbar. Eingaben verwenden mindestens (16px) Schriftgröße. Ein globaler Fokus mit (2px) Kontur und (4px) Abstand hält interaktive Elemente erkennbar.

**The One Primary Rule.** Genau ein primärer Button pro Ansicht („Speichern“, „Kopieren & öffnen“); alles andere ist sekundär oder ein Textbutton darunter.

Eingelegte Bedienelemente in grauen Gruppen (Pills, Segmente, Schalter) nutzen `--inset`, nie den Seitengrund.

**The One-Decision Rule.** Eine offene Pflichtwahl ersetzt die Aktionsleiste; was von ihr abhängt, erscheint erst danach, mit einer kleinen Zeile „Über X · Ändern“ darüber. Nie Wahl und abhängige Buttons gleichzeitig zeigen, nie deaktivierte Buttons als Hinweis auf eine fehlende Wahl. Beispiel: die Portalwahl bei doppelten Inseraten.

Ein Name pro Ziel, überall gleich: Treffer, Merkliste, Suchen, Einstellungen. Die Navigation zeigt auf Desktop den aktiven Eintrag mit Füllung und stärkerer Textfarbe. Mobil stehen Icon und kurzer Text übereinander; die aktive Textfarbe trägt die Auswahl. Segmente zeigen das gewählte Element auf einer eigenen Oberfläche innerhalb der neutralen Pill.

**The No Label Heading Rule.** Feldgruppen bekommen keine Überschrift, wenn ihre Zeilen sich selbst erklären („Mails zu Wohnungen“, „ohne WBS“, „Erstzulassung ab“). Abstand trennt Gruppen. Eine Überschrift bleibt nur, wo sie Gleichnamiges unterscheidet, etwa Kleinanzeigen unter Wohnungen und unter Preisen.

**The Shipping Context Rule.** Der Versand-Schalter gehört in dieselbe Feldgruppe wie Suchbegriff und Höchstpreis. Die Erläuterung darunter beschreibt die Auswirkung auf den Alarm.

**The New Dot Rule.** Ein Treffer trägt ein Treffer einen kleinen grünen Punkt vor dem Titel, wenn er nach dem letzten Besuch auf diesem Gerät gefunden wurde. Ein Besuch endet erst nach 10 Minuten Abwesenheit; Neuladen oder ein kurzer Tabwechsel behalten die Punkte. Beim ersten Besuch auf einem Gerät ist nichts neu, der erste Abruf einer Suche ist Bestand. Öffnen nimmt den Punkt sofort weg. Der Punkt springt einmal auf, spätere Aktualisierungen zeichnen ihn ruhig. Die Anzahl steht im Tab-Titel, die des anderen Bereichs als „3 neu“ an dessen Pill.

**The Row Foot Rule.** Unter jeder Zeile steht vorn ein kleines grünes Anbieter-Etikett, dahinter grau das Alter (bei Wohnungen das Veröffentlichungsdatum des Portals, sonst der Fund) und „frei ab …“. Eine geschätzte Warmmiete steht als „≈ 850 €“ in Zweitgrau, sie ist keine Warnung. Beworbene Wohnungen zeigen einen grünen Haken am Bild, ein grünes „Beworben“ neben dem Titel und grün „Beworben am …“ als Fußzeile.

**The Source Picker Rule.** Im Suchdialog steht jede Quelle als eigene Zeile einer Feldgruppe: Quellenzeichen, Name, kurze Beschreibung und rechts ein runder Haken, gewählt grün. Neue Suchen starten mit allen Quellen; abgewählte Zeilen werden grau. „Alle/Keine“ schaltet die Gruppe auf einmal.

Schalter haben einen verschiebbaren runden Griff, aktive Schalter werden grün. Der Suchdialog wird mobil zum unteren Sheet; seine abschließenden Aktionen bleiben beim Scrollen sichtbar. Hintergrundwechsel dauern (.16s ease), der Schaltergriff (.18s ease). Bei reduzierter Bewegung entfallen Übergänge.

## Do's and Don'ts

### Do:
- Do dieselben semantischen Rollen in beiden Systemmodi erhalten.
- Do Angebote als ruhige Zeilen mit erkennbarem Quellenzeichen und rechtsbündigem Preis darstellen.
- Do sichtbare Fokusmarkierungen und reduzierte Bewegung erhalten.
- Do Versand je Suche unmittelbar beim Höchstpreis anbieten.
- Do eine große Zahl pro Übersicht setzen und alles andere ruhig halten.
- Do Aktionen ins Detailblatt legen; die Liste zeigt nur, was neu und passend ist.
- Do jede neue Farbe als Token mit Hell- und Dunkelwert anlegen.
- Do Ausblenden und Löschen rückgängig machbar halten oder vorher in place bestätigen lassen.

### Don't:
- Don't zusätzliche dekorative Akzentfarben oder Verlaufshintergründe einführen.
- Don't offene Angebotslisten durch schwebende Karten oder Schattenraster ersetzen.
- Don't mobile Inhalte hinter der Bodennavigation verstecken.
- Don't Kicker, Eyebrows oder Abschnittsüberschriften über selbsterklärende Zeilen setzen.
- Don't Punktmatrix, Neon oder andere „edgy“ Effekte einsetzen.
