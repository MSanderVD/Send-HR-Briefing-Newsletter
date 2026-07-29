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
4. Versand per **Microsoft Graph API** (Exchange Online / M365,
   `dashoefer.onmicrosoft.com`) an `REPORT_RECIPIENT_EMAIL` – siehe
   `mail_graph.py`. Kein Gmail mehr im Einsatz.

Kategorien (aus der ursprünglichen PhiBox-Vorlage übernommen):
Gesetzesvorhaben · BMF-Schreiben · Urteile · Verordnungen ·
Gesetzgebungsverfahren · HR-Digitalisierung.

## Setup

### 1. Azure-AD-App-Registrierung anlegen (einmalig, durch einen Admin)

Mailversand läuft über Microsoft Graph (App-Only-Auth), nicht mehr über
Gmail. Details und Hintergrund stehen ausführlich in `mail_graph.py`,
kurz zusammengefasst:

1. **Azure Portal → Azure Active Directory → App registrations →
   New registration** (z. B. Name "HR-Briefing-Mailer")
2. **API permissions → Add a permission → Microsoft Graph → Application
   permissions → `Mail.Send`**
3. **"Grant admin consent for `dashoefer`"** klicken – zwingend nötig,
   da es sich um eine Application- (nicht Delegated-)Permission handelt.
   Ohne diesen Schritt schlägt der Versand mit HTTP 403 fehl.
4. **Certificates & secrets → New client secret** → Wert sofort
   kopieren (wird nur einmal angezeigt).
5. Von der Overview-Seite: **Application (client) ID** und
   **Directory (tenant) ID** kopieren.
6. Empfohlen (Sicherheit): Per PowerShell (`ExchangeOnlineManagement`-
   Modul) eine **Application Access Policy** einrichten, damit die App
   nur das eine Absender-Postfach (z. B. `ki@dashoefer.onmicrosoft.com`)
   ansprechen darf – `Mail.Send` als Application-Permission erlaubt
   sonst standardmäßig Versand "as any user" im gesamten Tenant.

### 2. GitHub Secrets anlegen

Unter *Settings → Secrets and variables → Actions*:

| Secret | Beschreibung |
|---|---|
| `GRAPH_TENANT_ID` | Directory (tenant) ID oder Domain, z. B. `dashoefer.onmicrosoft.com` |
| `GRAPH_CLIENT_ID` | Application (client) ID der App-Registrierung |
| `GRAPH_CLIENT_SECRET` | Client-Secret der App-Registrierung |
| `GRAPH_SENDER_UPN` | Absender-Postfach, z. B. `ki@dashoefer.onmicrosoft.com` |
| `REPORT_RECIPIENT_EMAIL` | Empfänger-Adresse (z. B. `l.dashoefer@dashoefer.de`) |
| `OPENROUTER_API_KEY` | Kostenloser API-Key von [openrouter.ai](https://openrouter.ai), kann vom KI-Briefing-Repo wiederverwendet werden |

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
export GRAPH_TENANT_ID='dashoefer.onmicrosoft.com'
export GRAPH_CLIENT_ID='...'
export GRAPH_CLIENT_SECRET='...'
export GRAPH_SENDER_UPN='ki@dashoefer.onmicrosoft.com'
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
- `mail_graph.py` wurde nicht gegen einen echten Tenant getestet (aus
  der Sandbox heraus kein Netzwerkzugriff auf `login.microsoftonline.com`/
  `graph.microsoft.com`) – Token-Request- und sendMail-Logik folgen der
  offiziellen Microsoft-Graph-Dokumentation, sollten aber beim ersten
  Testlauf genau im Log geprüft werden (häufigste Fehlerquelle: fehlender
  Admin Consent → HTTP 403).

## Geplant, noch nicht enthalten

- Kombination mit dem `Newsletter-Analyse`-Repo (Gmail-Newsletter mit
  HR-Keyword-Vorfilter) – als zweites Modul vorgesehen, sobald dieses
  Kern-Skript stabil läuft.
- GitLab-Migration (reines Kopieren + Workflow-Syntax übersetzen, erst
  nach Stabilisierung auf GitHub).
