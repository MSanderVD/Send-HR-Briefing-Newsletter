# HR-Briefing (HR-Wissen Weekly)

Wöchentliches HR-Wissen-Weekly per Email, automatisiert über GitHub Actions.
Gebaut nach demselben Muster wie das bestehende `Send-AI-Briefing-Newsletter`
(KI-Briefing) – inkl. aller dort gesammelten Lessons Learned zu Grounding,
Modell-Auswahl und Fehlerbehandlung.

## Wie es funktioniert

1. `hr_briefing.py` ruft feste, konfigurierte Quellen **wirklich per HTTP ab**
   (Bundestag, Bundesregierung, BMAS, BMF, BAG, BFH, BSG, Bundesrat, EU-
   Kommission, Haufe, LTO – siehe `SOURCES`-Dictionary im Skript).
2. Nur der tatsächlich abgerufene Text wird als Kontext an ein LLM
   (über OpenRouter, kostenlose Modelle, live abgefragt) geschickt. Das
   Modell darf keine Aktenzeichen, Daten, Gerichte oder Links erfinden,
   die nicht im Kontext stehen.
3. Nach der LLM-Antwort läuft ein automatischer Grounding-Check: alle im
   Report genannten URLs werden gegen die Liste tatsächlich abgerufener
   Quellen geprüft, und jede "Quelle:"-Angabe gegen die konfigurierten
   Quellennamen (auf Wort-Ebene – verkürzte Zitate wie "BAG" statt
   "Bundesarbeitsgericht (BAG)" sind korrekt und lösen keinen Fehlalarm aus).
4. Versand per **Gmail API** von `vdnewsletteranalyse@gmail.com` an
   `REPORT_RECIPIENT_EMAIL` – siehe `send_email_gmail()` in
   `hr_briefing.py`. Dasselbe OAuth-Token wird auch für das Auslesen der
   HR-Newsletter aus diesem Postfach genutzt (Scopes `gmail.readonly` +
   `gmail.send`).

Kategorien (aus der ursprünglichen PhiBox-Vorlage übernommen):
Gesetzesvorhaben · BMF-Schreiben · Urteile · Verordnungen ·
Gesetzgebungsverfahren · HR-Digitalisierung.

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

Die früheren `GRAPH_*`-Secrets (Microsoft-Graph-Versand) werden nicht mehr
gelesen und können gelöscht werden.

### 3. Quellen anpassen

Die Liste der abgerufenen Seiten steht im `SOURCES`-Dictionary am Anfang
von `hr_briefing.py`. Behörden-/Presseseiten ändern gelegentlich ihre
Struktur oder URL – wenn eine Quelle im Log dauerhaft als ❌ auftaucht,
einfach die URL in `SOURCES` anpassen. Das Skript bricht dadurch nicht
ab, es lässt die betroffene Quelle nur weg (lieber fehlende als
erfundene Inhalte).

### 4. Manuell testen

Im Tab *Actions* → *HR-Briefing* → *Run workflow* auslösen, oder lokal:

```bash
export GMAIL_CREDENTIALS_JSON="$(cat credentials.json)"
export GMAIL_TOKEN_JSON="$(cat token.json)"
export REPORT_RECIPIENT_EMAIL='du@example.com'
export OPENROUTER_API_KEY='...'
python hr_briefing.py --mode weekly
```

## Bekannte Grenzen

- Kostenlose OpenRouter-Modelle können bei Rate-Limits (`429`) einzelne
  Anfragen verzögern; das Skript versucht automatisch mehrere Modelle
  nacheinander.
- Wenn eine Quelle nicht erreichbar ist, wird sie **nicht** ins Briefing
  aufgenommen – das Skript füllt Lücken nicht mit Vermutungen auf.
  Fehlgeschlagene Quellen werden am Ende des Reports aufgelistet
  (aufklappbar).
- Manche Behördenseiten laden Inhalte dynamisch per JavaScript nach;
  ein reiner `requests`-Abruf sieht dann ggf. weniger Text als im
  Browser sichtbar ist. Die `SOURCES`-Einträge wurden bewusst auf
  möglichst textlastige, serverseitig gerenderte Übersichtsseiten
  (Pressemitteilungslisten, Schreiben-Verzeichnisse) ausgerichtet.
- Die konfigurierten URLs wurden per Web-Recherche ermittelt, aber
  **nicht** aus dieser Sandbox heraus per `requests` gegen die
  Zielserver getestet (die Sandbox hat keinen Netzwerkzugriff auf
  Behördendomains) – ein erster Testlauf über *Run workflow* sollte
  vor der ersten produktiven Woche gemacht werden.
- Der Gmail-Versand wurde nicht aus der Sandbox heraus getestet (kein
  Netzwerkzugriff auf `googleapis.com`) – beim ersten Testlauf das Log
  prüfen. Häufigste Fehlerquellen:
  - `403 insufficient authentication scopes` → `GMAIL_TOKEN_JSON` wurde
    ohne `gmail.send` erzeugt, `generate_token.py` erneut ausführen.
  - `invalid_grant` → Refresh-Token abgelaufen (OAuth-App noch im
    Testing-Modus, 7-Tage-Limit) oder Zugriff im Google-Konto entzogen.
- Gmail hat ein Versandlimit (bei kostenlosen Konten ~500 Empfänger/Tag).
  Für ein Wochenbriefing an wenige Empfänger unkritisch; bei einem
  größeren Verteiler wäre ein echter Mail-Dienst der bessere Weg.

## Geplant, noch nicht enthalten

- GitLab-Migration (reines Kopieren + Workflow-Syntax übersetzen, erst
  nach Stabilisierung auf GitHub).
