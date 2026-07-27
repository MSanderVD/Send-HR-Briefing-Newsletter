# KI-Briefing

Wöchentliches KI-News-Briefing per Email, automatisiert über GitHub Actions.

## Wie es funktioniert

1. `ki_briefing.py` ruft feste, konfigurierte Quellen **wirklich per HTTP ab**
   (Vendor-Release-Notes, EU-Regulierungsseiten, Themen-Hubs, Marktreports) –
   siehe `SOURCES`-Dictionary im Skript.
2. Nur der tatsächlich abgerufene Text wird als Kontext an ein LLM
   (über OpenRouter, kostenlose Modelle) geschickt. Das Modell darf keine
   Fakten erfinden, die nicht im Kontext stehen, und muss jede Meldung mit
   der exakten Quell-URL versehen.
3. Nach der LLM-Antwort läuft ein **automatischer Grounding-Check**: Alle im
   Report genannten URLs werden gegen die Liste tatsächlich abgerufener
   Quellen geprüft. Unbekannte URLs erscheinen als gelbe Warnbox oben im
   Report statt unbemerkt durchzugehen.
4. Versand per Gmail an `REPORT_RECIPIENT_EMAIL`.

## Warum dieser Aufbau

In einer früheren Version wurde ein Skill so betrieben, dass ein Modell frei
im Web recherchierte. Dabei kam es vor, dass eine echte, existierende URL mit
einer erfundenen oder falsch zugeordneten Detailaussage verknüpft wurde
(z. B. ein Wirtschafts-Newsartikel als Beleg für eine technische
Protokoll-Ankündigung, die darin gar nicht vorkam). Dieser Aufbau verhindert
das strukturell: Das Modell bekommt nie die Möglichkeit, Inhalte zu URLs zu
erfinden, die es nicht selbst abgerufen hat – und falls doch, fängt der
Grounding-Check es ab.

## Setup

### 1. GitHub Secrets anlegen

Unter *Settings → Secrets and variables → Actions* folgende Secrets anlegen:

| Secret | Beschreibung |
|---|---|
| `GMAIL_CREDENTIALS_JSON` | Inhalt der Google OAuth `credentials.json` (Desktop-App) |
| `GMAIL_TOKEN_JSON` | Erzeugt via `generate_token.py` (siehe unten) |
| `REPORT_RECIPIENT_EMAIL` | Empfänger-Adresse für das Briefing |
| `OPENROUTER_API_KEY` | Kostenloser API-Key von [openrouter.ai](https://openrouter.ai) |

**Hinweis:** Falls ihr bereits ein `Newsletter-Analyse`-Repo mit denselben
Secrets betreibt, könnt ihr `GMAIL_CREDENTIALS_JSON` und `GMAIL_TOKEN_JSON`
wiederverwenden (gleicher Google-Account), solange der Scope
`gmail.send` im Token enthalten ist.

### 2. Gmail-Token erzeugen (falls noch nicht vorhanden)

Lokal ausführen (nicht in GitHub Actions):

```bash
pip install google-auth-oauthlib google-api-python-client
python generate_token.py
```

Das Skript druckt den Wert für `GMAIL_TOKEN_JSON` aus.

### 3. Quellen anpassen

Die Liste der abgerufenen Seiten steht im `SOURCES`-Dictionary am Anfang von
`ki_briefing.py`. Neue Quelle hinzufügen: Tupel
`(Anzeigename, URL, Format-Typ, Zugriffsbeschreibung)` in die passende
Kategorie einfügen.

### 4. Manuell testen

Im Tab *Actions* → *KI-Briefing* → *Run workflow* auslösen, oder lokal:

```bash
export GMAIL_CREDENTIALS_JSON='...'
export GMAIL_TOKEN_JSON='...'
export REPORT_RECIPIENT_EMAIL='du@example.com'
export OPENROUTER_API_KEY='...'
python ki_briefing.py --mode weekly
```

## Bekannte Grenzen

- Kostenlose OpenRouter-Modelle können bei Rate-Limits (`429`) einzelne
  Anfragen verzögern; das Skript versucht automatisch mehrere Modelle
  nacheinander.
- Wenn eine Quelle nicht erreichbar ist (Fehler, Timeout, Blockade durch die
  Zielseite), wird sie **nicht** ins Briefing aufgenommen – das Skript füllt
  Lücken nicht mit Vermutungen auf. Fehlgeschlagene Quellen werden am Ende
  des Reports aufgelistet (aufklappbar).
- Manche Zielseiten laden Inhalte dynamisch per JavaScript nach; ein reiner
  `requests`-Abruf sieht dann ggf. weniger Text als im Browser sichtbar ist.
