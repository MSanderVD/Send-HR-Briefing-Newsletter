"""
KI-Briefing – wöchentliches KI-News-Briefing per Email
Läuft als GitHub Actions Workflow (wöchentlich / manuell).

Ablauf:
  1. Feste Quellen (Vendor-Release-Notes, EU-Regulierung, Themen-Hubs)
     werden ECHT abgerufen (requests + BeautifulSoup) - kein Modellwissen.
  2. Nur der tatsächlich abgerufene Text geht als Kontext an das LLM
     (über OpenRouter, gleiche kostenlose Modelle wie im
     Newsletter-Analyse-Repo).
  3. Das LLM darf NUR Fakten aus diesem Kontext verwenden und muss jede
     Meldung mit der Quell-URL versehen.
  4. Nach der LLM-Antwort wird automatisch geprüft, ob alle im Text
     genannten URLs auch tatsächlich zu den abgerufenen Quellen gehören
     (Grounding-Check). Unbekannte URLs werden im Report sichtbar
     markiert, nicht stillschweigend akzeptiert.
  5. Versand per Gmail (gleiches Konto/Setup wie Newsletter-Analyse).

Warum dieser Aufbau: In einer früheren Version (PhiBox-Skill) wurden
reale URLs mit erfundenen oder falsch zugeordneten Fakten verknüpft.
Das passiert hier nicht mehr, weil das LLM ausschließlich mit bereits
abgerufenem Text arbeitet und die Ausgabe automatisch gegengeprüft wird.
"""

import os
import re
import json
import time
import base64
import logging
import argparse
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; VD-KI-Briefing-Bot/1.0; +internal-use)"
TIMEOUT_SECONDS = 15

# ---------------------------------------------------------------------------
# Quellen-Konfiguration
# ---------------------------------------------------------------------------
# format: (Anzeigename, URL, Format-Typ, Zugriffsbeschreibung)

SOURCES = {
    "Modelle & Forschung / Chatbot-Updates": [
        ("ChatGPT / OpenAI", "https://help.openai.com/en/articles/6825453-chatgpt-release-notes",
         "Release Notes", "Chronologische Liste, neueste Einträge oben"),
        ("Claude / Anthropic", "https://www.anthropic.com/news",
         "Newsroom", "Neueste Meldungen oben auf der Seite"),
        ("Gemini / Google", "https://gemini.google/release-notes/",
         "Release Notes", "Nach Datum sortiert"),
        ("Perplexity", "https://www.perplexity.ai/changelog",
         "Changelog", "Nach Version/Datum sortiert"),
        ("Copilot / Microsoft", "https://learn.microsoft.com/en-us/microsoft-365/copilot/release-notes",
         "Release Notes", "Nach Monat gruppiert"),
        ("Mistral", "https://mistral.ai/news/",
         "Newsroom", "Neueste Meldungen oben"),
    ],
    "Regulierung & Recht": [
        ("EUR-Lex – AI Act Volltext", "https://eur-lex.europa.eu/legal-content/DE/TXT/?uri=CELEX:32024R1689",
         "Gesetzestext", "Volltext der EU-KI-Verordnung"),
        ("EU-Kommission – AI Act Rahmenwerk", "https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai",
         "Behördenseite", "Überblicksseite zum Rechtsrahmen"),
        ("Bundesregierung – KI-Verordnung", "https://www.bundesregierung.de/breg-de/aktuelles/umsetzung-ki-verordnung-2406638",
         "Pressemitteilung", "Meldung zur nationalen Umsetzung"),
    ],
    "Trends / Agenten / Gesellschaft": [
        ("CNBC – Technology", "https://www.cnbc.com/technology/",
         "Nachrichten-Hub", "Artikelliste, neueste oben"),
        ("TechCrunch – AI", "https://techcrunch.com/category/artificial-intelligence/",
         "Nachrichten-Hub", "Artikelliste, neueste oben"),
        ("UN News", "https://news.un.org/en/",
         "Nachrichten-Hub", "Artikelliste aller Themen"),
        ("IT-Boltwise", "https://www.it-boltwise.de/",
         "Nachrichten-Hub", "Deutschsprachige KI-News-Übersicht"),
    ],
    "Marktdaten / Rangliste": [
        ("First Page Sage", "https://firstpagesage.com/reports/top-generative-ai-chatbots/",
         "Marktreport", "Tabelle mit Marktanteilen"),
        ("LLM-Stats", "https://llm-stats.com/",
         "Benchmark-Übersicht", "Sortierbare Modell-Tabelle"),
    ],
}

CATEGORY_ICON = {
    "Modelle & Forschung / Chatbot-Updates": "🔬",
    "Regulierung & Recht": "⚖️",
    "Trends / Agenten / Gesellschaft": "🌍",
    "Marktdaten / Rangliste": "🏆",
}


# ---------------------------------------------------------------------------
# Schritt 1: Echtes Abrufen der Quellen
# ---------------------------------------------------------------------------

def fetch_and_extract(url: str, max_chars: int = 4000) -> dict:
    """Ruft eine URL wirklich ab und liefert bereinigten Text zurück.
    status == 'error' bedeutet: NICHT verwenden, keine Ersatzinhalte."""
    result = {
        "url": url, "status": "error", "http_status": None,
        "title": None, "text": "", "error": None,
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
        result["http_status"] = resp.status_code
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()

        title_tag = soup.find("title")
        result["title"] = title_tag.get_text(strip=True) if title_tag else None
        result["text"] = soup.get_text(separator="\n", strip=True)[:max_chars]
        result["status"] = "ok"
    except requests.exceptions.RequestException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def collect_all_sources() -> dict:
    """Ruft alle konfigurierten Quellen ab. Gibt strukturierte Ergebnisse
    inkl. Metadaten (Format, Zugriffsbeschreibung) zurück."""
    collected = {}
    for category, entries in SOURCES.items():
        collected[category] = []
        for name, url, fmt, access_hint in entries:
            fetch_result = fetch_and_extract(url)
            entry = {
                "name": name, "url": url, "format": fmt,
                "access_hint": access_hint, **fetch_result,
            }
            status_note = "✅" if entry["status"] == "ok" else f"❌ {entry['error']}"
            logger.info(f"[{category}] {name}: {status_note}")
            collected[category].append(entry)
    return collected


# ---------------------------------------------------------------------------
# Schritt 2: LLM-Aufruf - NUR mit abgerufenem Text als Kontext
# ---------------------------------------------------------------------------

def build_context_block(collected: dict) -> str:
    """Baut den Kontext-Block für den Prompt - nur erfolgreich abgerufene
    Quellen, mit klarer URL-Zuordnung pro Absatz."""
    blocks = []
    for category, entries in collected.items():
        ok_entries = [e for e in entries if e["status"] == "ok" and e["text"]]
        if not ok_entries:
            continue
        blocks.append(f"\n=== KATEGORIE: {category} ===\n")
        for e in ok_entries:
            blocks.append(
                f"--- QUELLE: {e['name']} | URL: {e['url']} | "
                f"Format: {e['format']} ---\n{e['text'][:3000]}\n"
            )
    return "\n".join(blocks)


def call_openrouter(prompt: str) -> str:
    api_key = os.environ["OPENROUTER_API_KEY"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/",
    }
    models = [
        "openai/gpt-oss-120b:free",
        "openai/gpt-oss-20b:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "google/gemini-2.0-flash-exp:free",
    ]
    last_error = None
    for model in models:
        logger.info(f"Versuche Modell: {model}")
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        for attempt in range(2):
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload, timeout=180,
            )
            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                logger.info(f"429 bei {model} - warte {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code in (400, 404):
                last_error = resp.status_code
                break
            resp.raise_for_status()
            data = resp.json()
            if "choices" not in data:
                last_error = data
                break
            logger.info(f"Modell {model} erfolgreich")
            return data["choices"][0]["message"]["content"]
    raise ValueError(f"Alle Modelle fehlgeschlagen. Letzter Fehler: {last_error}")


def generate_briefing_html(collected: dict, week_label: str) -> str:
    context = build_context_block(collected)
    if not context.strip():
        raise ValueError("Keine einzige Quelle erfolgreich abgerufen - Abbruch.")

    prompt = f"""Du erstellst ein KI-News-Briefing für DACH-B2B-Professionals
(HR, Management, Recht, Compliance) für: {week_label}.

REGEL (unbedingt einhalten): Verwende AUSSCHLIESSLICH Informationen, die
wörtlich im folgenden Kontext stehen. Erfinde KEINE Zahlen, Produktnamen,
Fristen oder Ereignisse. Wenn ein Thema im Kontext nicht ausreichend
belegt ist, lass es weg statt zu raten.

Für JEDE Meldung: gib die exakte Quell-URL aus dem Kontext an (kopiere
sie unverändert). Erfinde keine URLs.

KONTEXT (echt abgerufene Webseiten):
{context}

Erstelle einen strukturierten HTML-Report (nur <body>-Inhalt, kein
Markdown) mit:
1. Executive Summary (3-5 Sätze)
2. Pro Kategorie (nur wenn Meldungen vorhanden): Überschrift (h2),
   darunter je Meldung: Überschrift (h3), 2-4 Sätze Zusammenfassung,
   "Relevanz:" (1 Satz für DACH-Professionals), und
   "Quelle: [Name] (Format) – <a href='URL'>URL</a>"
Verwende einfaches HTML mit inline-Styles. Hintergrund weiß,
Überschriften dunkelblau (#1a3a5c)."""

    return call_openrouter(prompt)


# ---------------------------------------------------------------------------
# Schritt 3: Grounding-Check - stammen alle genannten URLs aus dem Cache?
# ---------------------------------------------------------------------------

def validate_output_urls(html: str, collected: dict) -> list[str]:
    """Extrahiert alle URLs aus der LLM-Antwort und prüft sie gegen die
    Liste tatsächlich abgerufener Quellen. Gibt eine Liste unbekannter
    (potenziell erfundener) URLs zurück."""
    known_urls = {
        e["url"] for entries in collected.values() for e in entries
    }
    found_urls = set(re.findall(r'href=[\'"]?(https?://[^\'" >]+)', html))
    unknown = sorted(u for u in found_urls if u not in known_urls)
    if unknown:
        logger.warning(f"{len(unknown)} unbekannte URL(s) in der LLM-Antwort gefunden: {unknown}")
    return unknown


# ---------------------------------------------------------------------------
# Gmail-Versand (identisches Setup wie Newsletter-Analyse-Repo)
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds_data = json.loads(os.environ["GMAIL_TOKEN_JSON"])
    client_info = json.loads(os.environ["GMAIL_CREDENTIALS_JSON"])["installed"]
    creds = Credentials(
        token=creds_data.get("token"),
        refresh_token=creds_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_info["client_id"],
        client_secret=client_info["client_secret"],
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    return build("gmail", "v1", credentials=creds)


def send_email(service, to: str, subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    logger.info(f"Email gesendet an {to}")


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------

def run(week_label: str, subject: str):
    recipient = os.environ["REPORT_RECIPIENT_EMAIL"]

    logger.info("Rufe alle Quellen ab...")
    collected = collect_all_sources()

    ok_count = sum(1 for entries in collected.values() for e in entries if e["status"] == "ok")
    error_count = sum(1 for entries in collected.values() for e in entries if e["status"] != "ok")
    logger.info(f"{ok_count} Quellen erfolgreich abgerufen, {error_count} fehlgeschlagen.")

    body_html = generate_briefing_html(collected, week_label)
    unknown_urls = validate_output_urls(body_html, collected)

    warning_banner = ""
    if unknown_urls:
        items = "".join(f"<li>{u}</li>" for u in unknown_urls)
        warning_banner = (
            "<div style='background:#fff3cd;border:1px solid #ffc107;"
            "padding:12px;margin-bottom:16px;'>"
            "⚠️ Automatischer Grounding-Check: Folgende URLs im Report "
            "stammen NICHT aus den abgerufenen Quellen und sollten manuell "
            f"geprüft werden:<ul>{items}</ul></div>"
        )

    failed_sources_note = ""
    if error_count:
        failed_items = "".join(
            f"<li>{e['name']} ({e['url']}): {e['error']}</li>"
            for entries in collected.values() for e in entries if e["status"] != "ok"
        )
        failed_sources_note = (
            f"<details><summary>{error_count} Quelle(n) nicht erreichbar "
            f"(nicht ins Briefing eingeflossen)</summary><ul>{failed_items}"
            f"</ul></details>"
        )

    now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    full_html = f"""<!DOCTYPE html>
<html lang="de">
<head><meta charset="utf-8"><title>KI-Briefing: {week_label}</title></head>
<body style="font-family:Arial,sans-serif;max-width:800px;margin:40px auto;padding:0 20px;">
{warning_banner}
<h1 style="color:#1a3a5c;">KI-Briefing: {week_label}</h1>
{body_html}
{failed_sources_note}
<div style="margin-top:40px;font-size:12px;color:#888;border-top:1px solid #ddd;padding-top:12px;">
Automatisch erstellt am {now_str} · {ok_count} Quellen abgerufen, {error_count} fehlgeschlagen ·
Alle Angaben ohne Gewähr
</div>
</body>
</html>"""

    service = get_gmail_service()
    send_email(service, recipient, subject, full_html)
    logger.info("Fertig.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["weekly"], default="weekly")
    args = parser.parse_args()

    today = datetime.date.today()
    week = today.isocalendar()[1]
    run(
        week_label=f"KW {week} / {today.strftime('%B %Y')}",
        subject=f"🤖 KI-Briefing KW {week} – {today.strftime('%d.%m.%Y')}",
    )
