# HR-Briefing (HR-Wissen Weekly)

Wöchentliches HR-Wissen-Weekly per Email, automatisiert über GitHub Actions.
Gebaut nach demselben Muster wie das bestehende `Send-AI-Briefing-Newsletter`
(KI-Briefing) – inkl. aller dort gesammelten Lessons Learned zu Grounding,
Modell-Auswahl und Fehlerbehandlung.

## Wie es funktioniert

1. **Quellen abrufen.** `hr_briefing.py` ruft feste, konfigurierte Quellen
   **wirklich per HTTP ab** (Bundestag, Bundesregierung, BMAS, BMF, BAG,
   BFH, BSG, Bundesrat, EU-Kommission, Haufe, LTO – siehe `SOURCES`).
2. **Einzelmeldungen nachladen.** Von jeder Übersichtsseite wird den
   Links zu den eigentlichen Meldungen gefolgt (`folge_detailseiten`).
   Ohne diesen Schritt steht im Kontext nur eine Liste aus Datum und
   Überschrift – daraus kann das Modell nichts Konkretes schreiben,
   ohne zu erfinden.
3. **Je Kategorie ein eigener LLM-Aufruf** (`meldungen.py`), der
   strukturiertes JSON mit festen Pflichtfeldern liefert – bei Urteilen
   z. B. Gericht, Aktenzeichen, Entscheidungsdatum und Kernaussage, bei
   Gesetzgebungsverfahren Stand, nächster Schritt, geplantes
   Inkrafttreten und Verzögerung.
4. **Feldweise Grounding-Prüfung.** Aktenzeichen und Daten müssen
   wörtlich in einer abgerufenen Quelle stehen, sonst wird das Feld
   entfernt; eine Meldung mit unbekannter URL wird ganz verworfen.
   Beanstandetes steht damit gar nicht erst im Newsletter.
5. **HTML in Python** (`render.py`), nicht im Modell – Formatfehler sind
   dadurch strukturell ausgeschlossen.
6. **Themenradar** (`hot_topics.py`): Dauerthemen werden über Läufe
   hinweg fortgeführt, bis zum Inkrafttreten und eine Karenzzeit darüber
   hinaus (siehe unten).
7. **Versand per Gmail API** von `vdnewsletteranalyse@gmail.com` an
   `REPORT_RECIPIENT_EMAIL` – siehe `send_email_gmail()`. Dasselbe
   OAuth-Token wird auch für das Auslesen der HR-Newsletter aus diesem
   Postfach genutzt (Scopes `gmail.readonly` + `gmail.send`).

Kategorien (aus der ursprünglichen PhiBox-Vorlage übernommen):
Gesetzesvorhaben · BMF-Schreiben · Urteile · Verordnungen ·
Gesetzgebungsverfahren · HR-Digitalisierung.

## Themenradar: Themen über die Woche hinaus

Der Newsletter betrachtete ursprünglich nur einen Wochenausschnitt. Ein
Thema wie die Entgelttransparenzrichtlinie oder die Teilkrankschreibung
war damit genau in der Woche sichtbar, in der eine Pressemitteilung
erschien – und danach nie wieder, obwohl die eigentlich interessante
Zeit erst beginnt: der Weg bis zum Inkrafttreten und die ersten Monate
danach.

`hot_topics.py` schließt diese Lücke, **ohne handgepflegte Themenliste**:

- In jedem abgerufenen Quelltext wird nach Stichtagsformulierungen
  gesucht („tritt am 1. Januar 2027 in Kraft", „bis zum 7. Juni 2026
  umzusetzen", „Übergangsfrist endet am …").
- Im Umfeld der Fundstelle wird der Themenbegriff bestimmt – in der
  Verwaltungssprache fast immer ein langes Kompositum
  („Entgelttransparenzrichtlinie", „Teilkrankschreibung") oder eine
  Gesetzesabkürzung („EntgTranspG").
- Begriff, Stichtag, Belegsatz und Quell-URL wandern nach
  `state/hot_topics.json`. Diese Datei ist das Gedächtnis: Der Workflow
  committet sie nach jedem Lauf zurück ins Repo.
- Ein Thema bleibt aktiv, solange `heute <= Stichtag + Karenzzeit`
  (Standard 120 Tage). Für aktive Themen läuft zusätzlich eine gezielte
  Web-Suche **ohne** die Ein-Monats-Schranke der normalen Suche – sonst
  wäre ein Thema unauffindbar, zu dem seit Wochen nichts Neues erschien.
- Im Newsletter erscheinen die aktiven Themen im Abschnitt „Auf dem
  Radar", mit Countdown und dem Belegsatz aus der Quelle.

**Es wird nichts geraten.** Jeder Stichtag stammt wörtlich aus einem
abgerufenen Text und wird mit Beleg gespeichert. Lässt sich kein Datum
lesen, hat das Thema eben keinen Stichtag und braucht dann mehrere
Erwähnungen, um auf den Radar zu kommen.

`state/hot_topics.json` startet leer und füllt sich ab dem ersten Lauf.
Ein Vorbefüllen aus den bereits versendeten Newsletter-HTMLs war
erprobt, lieferte aber unzuverlässige Zuordnungen: Diese Dateien haben
keine saubere Satzstruktur (Tabellenzellen und Aufzählungen ohne
Satzzeichen), und die Erkennung braucht Sätze. Gegen echte
Pressemitteilungen greift sie deutlich besser.

### Stellschrauben (alle optional, als Umgebungsvariable)

| Variable | Standard | Wirkung |
|---|---|---|
| `HOT_TOPIC_KARENZ_TAGE` | 120 | Wie lange ein Thema nach seinem Stichtag weiterläuft |
| `HOT_TOPIC_VORLAUF_TAGE` | 550 | Wie weit im Voraus ein Stichtag schon auf den Radar kommt |
| `HOT_TOPIC_MAX` | 6 | Maximale Zahl der Themen im Radar-Abschnitt |
| `MAX_DETAILSEITEN` | 4 | Nachgeladene Einzelmeldungen je Übersichtsseite |
| `MIN_CONTEXT_LENGTH` | 32000 | Mindest-Kontextfenster eines zugelassenen Modells |
| `LLM_TIMEOUT` | 180 | Hartes Wanduhr-Timeout je LLM-Anfrage in Sekunden |
| `TEST_KATEGORIE` | Urteile | Welche Kategorie `--mode test` abruft |

## Setup

### 1. Gmail-Token erzeugen (einmalig, lokal)

Lesen *und* Versenden laufen über ein gemeinsames OAuth-Token für
`vdnewsletteranalyse@gmail.com`. Der OAuth-Client (Typ **Desktop-App**)
kann aus dem bestehenden `Newsletter-Analyse`-Projekt in der Google Cloud
Console wiederverwendet werden – nötig ist dort lediglich, dass die
**Gmail API** aktiviert ist.

1. `credentials.json` des OAuth-Clients herunterladen und neben
   `generate_token.py` legen (alternativ den Inhalt in die Variable
   `GMAIL_CREDENTIALS_JSON` exportieren).
2. `pip install -r requirements.txt`
3. `python generate_token.py`
4. Im Browser mit `vdnewsletteranalyse@gmail.com` einloggen und **beide**
   Berechtigungen bestätigen (E-Mails lesen **und** senden). Das Skript
   bricht mit einer Fehlermeldung ab, wenn ein Scope fehlt – dann einfach
   erneut ausführen.
5. Die ausgegebene JSON-Zeile komplett als Secret `GMAIL_TOKEN_JSON`
   hinterlegen (siehe unten).

Wichtig: Steht der OAuth-Zustimmungsbildschirm im Google-Cloud-Projekt
noch auf **"Testing"**, verfällt das Refresh-Token nach 7 Tagen und der
Actions-Lauf schlägt fehl. Für den Dauerbetrieb den Zustimmungsbildschirm
auf **"In Produktion"** setzen; eine Google-Verifizierung ist bei Nutzung
im eigenen Konto nicht nötig (es erscheint nur ein Warnhinweis beim
Login).

### 2. GitHub Secrets anlegen

Unter *Settings → Secrets and variables → Actions*:

| Secret | Beschreibung |
|---|---|
| `GMAIL_CREDENTIALS_JSON` | Inhalt der `credentials.json` des OAuth-Clients (Desktop-App) – kann aus dem `Newsletter-Analyse`-Repo übernommen werden |
| `GMAIL_TOKEN_JSON` | Ausgabe von `generate_token.py` – enthält das Refresh-Token mit **beiden** Scopes (`gmail.readonly` + `gmail.send`) |
| `REPORT_RECIPIENT_EMAIL` | Empfänger-Adresse (z. B. `l.dashoefer@dashoefer.de`) |
| `OPENROUTER_API_KEY` | Kostenloser API-Key von [openrouter.ai](https://openrouter.ai), kann vom KI-Briefing-Repo wiederverwendet werden |
| `FIRECRAWL_API_KEY` | Key von [firecrawl.dev](https://firecrawl.dev) für die Web-Suche (optional – ohne ihn läuft das Briefing nur mit den festen Quellen) |

Die früheren `GRAPH_*`-Secrets (Microsoft-Graph-Versand) werden nicht mehr
gelesen und können gelöscht werden.

### 3. Schreibrecht für den Workflow

Der Workflow schreibt `state/hot_topics.json` ins Repo zurück – das ist
das Gedächtnis des Themenradars. Dafür steht `permissions: contents:
write` in `.github/workflows/hr-briefing.yml`. Zusätzlich muss unter
*Settings → Actions → General → Workflow permissions* **"Read and write
permissions"** gesetzt sein, sonst scheitert der Commit-Schritt mit
einem 403. Ohne Schreibrecht läuft das Briefing trotzdem – der Radar
fängt dann aber jede Woche bei null an.

### 4. Quellen anpassen

Die Liste der abgerufenen Seiten steht im `SOURCES`-Dictionary am Anfang
von `hr_briefing.py`. Behörden-/Presseseiten ändern gelegentlich ihre
Struktur oder URL – wenn eine Quelle im Log dauerhaft als ❌ auftaucht,
einfach die URL in `SOURCES` anpassen. Das Skript bricht dadurch nicht
ab, es lässt die betroffene Quelle nur weg (lieber fehlende als
erfundene Inhalte).

### 5. Manuell testen

Im Tab *Actions* → *HR-Briefing* → *Run workflow*. Dort gibt es zwei
Eingabefelder:

- **mode = weekly** – vollständiger Lauf mit Versand. Dauert rund eine
  halbe Stunde: gut 40 Quellen, dazu die nachgeladenen Einzelmeldungen
  und sieben bis acht LLM-Aufrufe.
- **mode = test** – ruft nur **eine** Kategorie ab (wählbar über
  *test_kategorie*), überspringt Web-Suche und Newsletter-Postfach und
  **versendet nichts**. Kein OneDrive-Upload, und der Themenradar bleibt
  unverändert – ein Ausschnittslauf soll das Gedächtnis nicht
  überschreiben. Das Ergebnis liegt als Actions-Artifact bereit.
  Läuft in wenigen Minuten und ist der Weg, um eine Änderung zu prüfen.

Lokal:

```bash
export GMAIL_CREDENTIALS_JSON="$(cat credentials.json)"
export GMAIL_TOKEN_JSON="$(cat token.json)"
export REPORT_RECIPIENT_EMAIL='du@example.com'
export OPENROUTER_API_KEY='...'
python hr_briefing.py --mode weekly

# schneller Durchlauf ohne Versand, nur eine Kategorie:
TEST_KATEGORIE=Urteile python hr_briefing.py --mode test
```

## Dateien

| Datei | Aufgabe |
|---|---|
| `hr_briefing.py` | Hauptablauf: Quellen, Detailseiten, LLM-Anbindung, Versand |
| `meldungen.py` | Kategorie-Schemata, JSON-Extraktion, feldweise Grounding-Prüfung |
| `render.py` | Baut den HTML-Newsletter aus den geprüften Meldungen |
| `hot_topics.py` | Stichtagserkennung und Themenradar über mehrere Läufe |
| `web_search_sources.py` | Firecrawl-Web-Suche je Kategorie und je Dauerthema |
| `onedrive_upload.py` | Ablage des fertigen Reports in OneDrive |
| `state/hot_topics.json` | Gedächtnis des Themenradars (wird automatisch gepflegt) |

## Bekannte Grenzen

- **Ein vollständiger Lauf dauert rund eine halbe Stunde.** Für ein
  Wochenbriefing ist das unerheblich, beim Entwickeln nicht – dafür gibt
  es `--mode test` (siehe oben). Der größte Zeitfresser sind die
  LLM-Aufrufe: Da je Kategorie einer läuft, sind es sieben bis acht pro
  Ausgabe statt einem. Damit die Fallback-Kette nicht bei jedem Aufruf
  von vorn beginnt, merkt sich `call_openrouter` das zuletzt erfolgreiche
  Modell und überspringt Modelle, die in diesem Lauf schon zweimal
  versagt haben.
- Kostenlose OpenRouter-Modelle können bei Rate-Limits (`429`) einzelne
  Anfragen verzögern; das Skript wartet einmal 20 Sekunden und zieht
  dann zum nächsten Modell weiter, statt lange auf einem Modell zu
  beharren.
- Nur Modelle mit mindestens `MIN_CONTEXT_LENGTH` Token Kontext werden
  zugelassen. Findet sich keins, weicht das Skript auf 16.000 aus, statt
  den Lauf abzubrechen.
- Wenn eine Quelle nicht erreichbar ist, wird sie **nicht** ins Briefing
  aufgenommen – das Skript füllt Lücken nicht mit Vermutungen auf.
  Fehlgeschlagene Quellen werden am Ende des Reports aufgelistet
  (aufklappbar).
- Das Nachladen der Einzelmeldungen arbeitet mit einer Heuristik
  (Linktext ab 30 Zeichen, gleiche Domain, keine Binärdateien). Sie ist
  bewusst konservativ: lieber eine Meldung verpassen als eine
  Rubrikseite in den Kontext holen. PDF-Anlagen – bei BMF-Schreiben der
  Regelfall – werden dabei übersprungen, weil der Textextraktor kein PDF
  liest.
- Manche Behördenseiten laden Inhalte dynamisch per JavaScript nach;
  ein reiner `requests`-Abruf sieht dann ggf. weniger Text als im
  Browser sichtbar ist.
- Gmail hat ein Versandlimit (bei kostenlosen Konten ~500 Empfänger/Tag).
  Für ein Wochenbriefing an wenige Empfänger unkritisch; bei einem
  größeren Verteiler wäre ein echter Mail-Dienst der bessere Weg.

## Geplant, noch nicht enthalten

- PDF-Auswertung für BMF-Schreiben (dort steht der eigentliche Inhalt
  meist im verlinkten PDF, nicht auf der Übersichtsseite).
- GitLab-Migration (reines Kopieren + Workflow-Syntax übersetzen, erst
  nach Stabilisierung auf GitHub).
