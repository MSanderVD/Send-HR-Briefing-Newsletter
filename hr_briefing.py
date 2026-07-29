"""
HR-Briefing – wöchentliches HR-Wissen-Weekly per Email
Läuft als GitHub Actions Workflow (wöchentlich / manuell).

Gebaut nach demselben Muster wie das bestehende KI-Briefing
(Send-AI-Briefing-Newsletter / ki_briefing.py), inkl. aller dort
gesammelten Lessons Learned:

  1. Feste Quellen (Bundestag, Bundesregierung, BMAS, BMF, BAG, BFH, BSG,
     Bundesrat, Deutsche Rentenversicherung, ...) werden ECHT abgerufen
     (requests + BeautifulSoup) - kein Modellwissen.
  2. Nur der tatsächlich abgerufene Text geht als Kontext an das LLM
     (über OpenRouter, kostenlose Modelle, live abgefragt).
  3. Das LLM darf NUR Fakten aus diesem Kontext verwenden und muss jede
     Meldung mit der Quell-URL versehen. Aktenzeichen/Daten/Gerichte
     dürfen nicht erfunden werden.
  4. Nach der LLM-Antwort läuft ein automatischer Grounding-Check: Alle
     im Report genannten URLs werden gegen die Liste tatsächlich
     abgerufener Quellen geprüft, und alle "Quelle:"-Angaben gegen die
     Namen der konfigurierten Quellen (auf Wort-Ebene, nicht Komplett-
     Name - siehe validate_source_names).
  5. Versand per Microsoft Graph API (Exchange Online) an
     REPORT_RECIPIENT_EMAIL - siehe mail_graph.py für den Auth-/Send-Flow.

Ursprung: PhiBox-Agent "Send Email HR Briefing" (agent-send-email-
hr-briefing.json) - Kategorien, Quellen-Prioritäten, HTML-Template und
Anti-Halluzinations-Regeln stammen aus dieser Vorlage.
"""

import os
import re
import json
import time
import logging
import argparse
import datetime
import threading

import requests
import langdetect
langdetect.DetectorFactory.seed = 0  # deterministische Ergebnisse statt zufälliger Schwankungen
from bs4 import BeautifulSoup

import mail_graph

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; VD-HR-Briefing-Bot/1.0; +internal-use)"
TIMEOUT_SECONDS = 15

# ---------------------------------------------------------------------------
# Quellen-Konfiguration
# ---------------------------------------------------------------------------
# format: (Anzeigename, URL, Format-Typ, Zugriffsbeschreibung)
#
# HINWEIS: Diese Übersichts-/Presseseiten ändern gelegentlich ihre Struktur
# oder URL. Wenn eine Quelle dauerhaft fehlschlägt (siehe Log-Ausgabe beim
# Testlauf), einfach die URL hier anpassen - das Skript bricht dadurch
# nicht ab, es lässt die Quelle nur weg (siehe collect_all_sources).

SOURCES = {
    "Gesetzesvorhaben": [
        ("Bundestag – Textarchiv", "https://www.bundestag.de/dokumente/textarchiv",
         "Nachrichten-Hub", "Chronologische Meldungen zu Plenardebatten/Gesetzentwürfen"),
        ("BMAS – Pressemitteilungen", "https://www.bmas.de/DE/Presse/Pressemitteilungen/pressemitteilungen.html",
         "Pressemitteilungen", "Nach Datum sortiert, neueste oben"),
        ("Bundesregierung – Aktuelles", "https://www.bundesregierung.de/breg-de/aktuelles",
         "Pressemitteilungen", "Nach Datum sortiert"),
    ],
    "BMF-Schreiben": [
        ("BMF – Schreiben Lohnsteuer/Allgemeines",
         "https://www.bundesfinanzministerium.de/Web/DE/Themen/Steuern/Steuerarten/Lohnsteuer/BMF_Schreiben_Allgemeines/bmf_schreiben_allgemeines.html",
         "Verwaltungsanweisungen", "Nach Datum sortiert, mit PDF-Verlinkung"),
        ("BMF – Alle BMF-Schreiben",
         "https://www.bundesfinanzministerium.de/Web/DE/Service/Publikationen/BMF_Schreiben/bmf_schreiben.html",
         "Verwaltungsanweisungen", "Nach Steuerart sortierbar, Datum absteigend"),
    ],
    "Urteile": [
        ("Bundesarbeitsgericht (BAG)", "https://www.bundesarbeitsgericht.de/home-2/",
         "Pressemitteilungen/Entscheidungen", "Neueste Pressemitteilung + Sitzungsergebnisse oben"),
        ("Bundesfinanzhof (BFH)", "https://www.bundesfinanzhof.de/de/presse/pressemitteilungen/",
         "Pressemitteilungen", "Nach Datum sortiert, mit Aktenzeichen"),
        ("Bundessozialgericht (BSG)", "https://www.bsg.bund.de/DE/Presse/Pressemitteilungen/pressemitteilungen_node.html",
         "Pressemitteilungen", "Nach Datum sortiert"),
    ],
    "Verordnungen": [
        ("EU-Kommission – KI-Verordnung Rahmenwerk",
         "https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai",
         "Behördenseite", "Überblick zum EU-KI-Rechtsrahmen (ggf. mit HR-Bezug)"),
        ("Bundesrat – Homepage", "https://www.bundesrat.de/",
         "Behördenseite", "Verweise auf aktuelle Plenarsitzungen/Verordnungen"),
    ],
    "Gesetzgebungsverfahren": [
        ("Bundestag – Textarchiv", "https://www.bundestag.de/dokumente/textarchiv",
         "Nachrichten-Hub", "Berichte zu laufenden Gesetzgebungsverfahren"),
        ("BMAS – Newsroom", "https://www.bmas.de/DE/Service/Presse/presse.html",
         "Newsroom", "Reden/Interviews/Zitate mit Verfahrensbezug"),
    ],
    "HR-Digitalisierung": [
        ("Haufe – Personal", "https://www.haufe.de/personal/",
         "Fachmedium", "News-Übersicht HR/Personalwesen"),
        ("LTO – Arbeitsrecht", "https://www.lto.de/recht/hintergruende/arbeitsrecht/",
         "Fachmedium", "Nachrichten-Hub Arbeitsrecht"),
    ],
}

CATEGORY_ICON = {
    "Gesetzesvorhaben": "📋",
    "BMF-Schreiben": "📄",
    "Urteile": "⚖️",
    "Verordnungen": "🇪🇺",
    "Gesetzgebungsverfahren": "🔄",
    "HR-Digitalisierung": "💻",
}

CATEGORY_COLOR = {
    "Gesetzesvorhaben": "#1a3c6e",
    "BMF-Schreiben": "#0f766e",
    "Urteile": "#9a2d2d",
    "Verordnungen": "#1e5fa8",
    "Gesetzgebungsverfahren": "#a86d00",
    "HR-Digitalisierung": "#5b3a8e",
}

CATEGORY_BG = {
    "Gesetzesvorhaben": "#f0f4fb",
    "BMF-Schreiben": "#effaf8",
    "Urteile": "#fcf3f3",
    "Verordnungen": "#eef5fc",
    "Gesetzgebungsverfahren": "#fdf8ef",
    "HR-Digitalisierung": "#f5f1fb",
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
    inkl. Metadaten (Format, Zugriffsbeschreibung) zurück. Fehlgeschlagene
    Quellen werden NICHT durch Ersatzinhalte aufgefüllt - eine Kategorie
    kann dadurch am Ende leer sein, das ist gewollt (lieber leer als
    erfunden)."""
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


def looks_garbled(text: str) -> str | None:
    """Erkennt typische Ausfallmuster kleiner/schlecht geeigneter Modelle.
    Identisch zur Logik im KI-Briefing (ki_briefing.py) - jede Regel für
    sich reicht, um die Ausgabe zu verwerfen und das nächste Modell zu
    versuchen."""

    # 1) Fremde Schriftsysteme mitten im deutschen Text
    unexpected_scripts = re.findall(
        r'[\u0E00-\u0E7F'    # Thai
        r'\u4E00-\u9FFF'     # CJK (Chinesisch)
        r'\u3040-\u30FF'     # Hiragana/Katakana (Japanisch)
        r'\uAC00-\uD7AF'     # Hangul (Koreanisch)
        r'\u0900-\u097F'     # Devanagari (Hindi)
        r'\u0600-\u06FF'     # Arabisch
        r'\u0590-\u05FF'     # Hebräisch
        r'\u0400-\u04FF'     # Kyrillisch
        r']', text
    )
    if unexpected_scripts:
        sample = "".join(unexpected_scripts[:5])
        return f"Unerwartete Schriftzeichen gefunden (z.B. '{sample}') - vermutlich korrupte Ausgabe"

    # 2) Durchgesickerte interne Platzhalter-/Steuer-Token, z.B. <TASKBODY>
    leaked_tokens = re.findall(r'<\s*[A-Z_]{3,}\s*>', text)
    if leaked_tokens:
        return f"Durchgesickerte Platzhalter-Token gefunden ({leaked_tokens[:3]}) - Modell hat eigenes Prompt-Format nicht sauber ausgefüllt"

    # 3) Echte Sprach-Prüfung, satzweise statt dokumentweise (siehe Lesson
    #    Learned #8 - vermeidet, dass gemischtsprachige Ausgaben durchrutschen)
    plain_text = re.sub(r'<[^>]+>', ' ', text)
    plain_text = re.sub(r'\s+', ' ', plain_text).strip()
    sentences = re.split(r'(?<=[.!?])\s+', plain_text)
    substantial_sentences = [s for s in sentences if len(s) >= 40]

    if len(substantial_sentences) >= 4:
        non_german_count = 0
        checked_count = 0
        for sentence in substantial_sentences:
            try:
                if langdetect.detect(sentence) != "de":
                    non_german_count += 1
                checked_count += 1
            except langdetect.lang_detect_exception.LangDetectException:
                continue  # zu kurz/uneindeutig - zählt weder für noch gegen

        if checked_count >= 4 and (non_german_count / checked_count) > 0.15:
            return (
                f"{non_german_count} von {checked_count} geprüften Sätzen nicht auf "
                "Deutsch erkannt - vermutlich gemischtsprachige Ausgabe"
            )
    elif len(plain_text) > 200:
        try:
            if langdetect.detect(plain_text) != "de":
                return "Spracherkennung meldet nicht 'de' für das Gesamtdokument"
        except langdetect.lang_detect_exception.LangDetectException:
            return "Sprache konnte nicht erkannt werden (evtl. zu wenig zusammenhängender Text)"

    # 4) Struktur-Check: erwartete Bausteine müssen mindestens einmal vorkommen
    if "Quelle" not in text:
        return "Kein einziges 'Quelle:' im Text gefunden - Format-Vorgabe wurde nicht befolgt"
    if "<h2" not in text.lower():
        return "Keine Kategorie-Überschriften (h2) gefunden - Struktur fehlt komplett"

    if len(text) < 500:
        return "Antwort verdächtig kurz für ein vollständiges Briefing"

    return None


def _normalize_for_comparison(s: str) -> str:
    """Reduziert einen String auf Kleinbuchstaben+Ziffern, damit
    typografische Varianten beim Vergleich nicht fälschlich als
    'unbekannt' gelten."""
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _significant_tokens(name: str) -> list[str]:
    """Zerlegt einen Quellennamen in bedeutungstragende Wort-Tokens.
    'Bundesarbeitsgericht (BAG)' -> ['bundesarbeitsgericht', 'bag'].

    Zwei Ausschnitte werden kombiniert:
    - Wörter mit mind. 4 Zeichen (allgemeine Regel, vermeidet Fehltreffer
      durch triviale Kurzwörter).
    - Klammer-Kürzel wie '(BAG)', '(BFH)', '(BSG)' werden UNABHÄNGIG von
      der Länge übernommen (mind. 2 Zeichen), weil im deutschen Arbeits-/
      Steuer-/Sozialrecht 3-Buchstaben-Gerichtskürzel (BAG, BFH, BSG) der
      absolute Normalfall für Quellenangaben sind - eine reine 4-Zeichen-
      Grenze würde genau diese korrekten Kurzzitate fälschlich als
      'verdächtig' einstufen."""
    tokens = re.split(r'[^a-z0-9]+', name.lower())
    long_tokens = [t for t in tokens if len(t) >= 4]

    bracket_matches = re.findall(r'\(([^)]+)\)', name)
    bracket_tokens = [
        t.lower() for t in bracket_matches
        if len(t) >= 2 and re.fullmatch(r'[A-Za-zÄÖÜäöü]+', t)
    ]

    return long_tokens + bracket_tokens


def validate_source_names(html: str, collected: dict) -> list[str]:
    """Prüft, ob jede 'Quelle:'-Angabe im Report zu einem tatsächlich
    konfigurierten Quellennamen passt (Wort-Ebene statt Komplett-Name -
    siehe Lesson Learned #9: Modelle zitieren Quellen oft verkürzt,
    z.B. 'BAG' statt 'Bundesarbeitsgericht (BAG)' - das ist korrekt und
    darf keinen Fehlalarm auslösen)."""
    known_tokens_per_name = [
        _significant_tokens(e["name"])
        for entries in collected.values() for e in entries
        if e["status"] == "ok"
    ]
    suspicious = []
    for match in re.finditer(r'Quelle:\s*([^<\n]{1,80})', html):
        cited = match.group(1).strip()
        cited_normalized = _normalize_for_comparison(cited)
        found = any(
            any(token in cited_normalized for token in tokens)
            for tokens in known_tokens_per_name if tokens
        )
        if not found:
            suspicious.append(cited)
    return suspicious


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


def get_free_models(headers: dict) -> list[str]:
    """Fragt den öffentlichen OpenRouter-Modellkatalog live ab (Lesson
    Learned #2: fest kodierte ':free'-IDs veralten innerhalb weniger
    Wochen). Schließt Utility-/Klassifikationsmodelle (#3) und
    übergroße Modelle >150 Mrd. Parameter (#4) aus. 'reasoning'-
    Varianten werden nachrangig behandelt (#5, neigen zu durchgesickerten
    Platzhaltern)."""
    preferred_patterns = ["gpt-oss-120b", "qwen3", "nemotron", "llama-3.3-70b", "gemma", "gpt-oss-20b"]
    exclude_keywords = [
        "safety", "guard", "moderation", "embed", "rerank", "judge",
        "asr", "tts", "ocr",
    ]
    fallback_static = [
        "openai/gpt-oss-20b:free",
        "meta-llama/llama-3.3-70b-instruct:free",
    ]
    try:
        resp = requests.get(
            "https://openrouter.ai/api/v1/models", headers=headers, timeout=30
        )
        resp.raise_for_status()
        all_models = resp.json().get("data", [])
    except requests.exceptions.RequestException as exc:
        logger.warning(f"Konnte Modell-Katalog nicht abrufen ({exc}) - nutze statische Notliste.")
        return fallback_static

    def estimated_param_billions(model_id: str) -> float:
        matches = re.findall(r'(\d+(?:\.\d+)?)b(?![a-z])', model_id.lower())
        return max((float(n) for n in matches), default=0.0)

    free_models = [
        m for m in all_models
        if m.get("pricing", {}).get("prompt") == "0"
        and m.get("pricing", {}).get("completion") == "0"
        and m.get("id", "").endswith(":free")
        and m.get("context_length", 0) >= 8000
        and not any(kw in m.get("id", "").lower() for kw in exclude_keywords)
        and estimated_param_billions(m.get("id", "")) <= 150
    ]
    if not free_models:
        logger.warning("Kein passendes kostenloses Modell im Katalog gefunden - nutze statische Notliste.")
        return fallback_static

    def sort_key(m):
        model_id = m["id"].lower()
        if "reasoning" in model_id:
            return (2, 0)
        for i, pattern in enumerate(preferred_patterns):
            if pattern in model_id:
                return (0, i)
        return (1, -m.get("context_length", 0))

    free_models.sort(key=sort_key)
    ids = [m["id"] for m in free_models][:6]
    logger.info(f"Aktuell verfügbare kostenlose Modelle (Top 6): {ids}")
    return ids


def _post_with_hard_timeout(url: str, headers: dict, payload: dict, hard_timeout: int = 360):
    """Erzwingt ein echtes Wanduhr-Timeout (Lesson Learned #6: requests'
    eigener timeout-Parameter reicht nicht, manche Server umgehen ihn
    per Keep-Alive). Daemon-Thread statt ThreadPoolExecutor, weil ein
    Executor beim Aufräumen trotzdem auf den langsamen Thread warten
    würde."""
    result: dict = {}

    def worker():
        try:
            result["resp"] = requests.post(
                url, headers=headers, json=payload, timeout=hard_timeout + 15
            )
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=hard_timeout)

    if thread.is_alive():
        raise requests.exceptions.Timeout(
            f"Hartes Timeout nach {hard_timeout}s erzwungen - Server hat nicht "
            "rechtzeitig fertig geantwortet (unabhängig vom Verbindungsstatus)"
        )
    if "error" in result:
        raise result["error"]
    return result["resp"]


def call_openrouter(prompt: str) -> str:
    api_key = os.environ["OPENROUTER_API_KEY"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/",
    }
    models = get_free_models(headers)
    last_error = None
    for model in models:
        logger.info(f"Versuche Modell: {model}")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        for attempt in range(2):
            try:
                resp = _post_with_hard_timeout(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers, payload, hard_timeout=360,
                )
            except requests.exceptions.RequestException as exc:
                last_error = f"{model}: Verbindungsfehler/Timeout - {exc}"
                logger.warning(last_error)
                break
            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                logger.info(f"429 bei {model} - warte {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code in (400, 404, 500, 502, 503):
                last_error = f"{model}: HTTP {resp.status_code} - {resp.text[:300]}"
                logger.warning(f"Modell {model} fehlgeschlagen: {last_error}")
                break
            resp.raise_for_status()
            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
            except (json.JSONDecodeError, requests.exceptions.JSONDecodeError,
                     KeyError, IndexError, TypeError) as exc:
                # Lesson Learned #7: resp.json() kann bei abgeschnittenen
                # Antworten crashen - abfangen statt Skript abstürzen zu lassen.
                last_error = (
                    f"{model}: Antwort nicht auswertbar ({type(exc).__name__}: {exc}) - "
                    f"vermutlich abgeschnittene/kaputte JSON-Antwort. "
                    f"Rohtext-Anfang: {resp.text[:200]!r}"
                )
                logger.warning(last_error)
                break

            garbled_reason = looks_garbled(content)
            if garbled_reason:
                last_error = f"{model}: Ausgabe verworfen - {garbled_reason}"
                logger.warning(last_error)
                break

            logger.info(f"Modell {model} erfolgreich, Ausgabe-Qualitätscheck bestanden")
            return content
    raise ValueError(f"Alle Modelle fehlgeschlagen oder lieferten fehlerhafte Ausgaben. Letzter Fehler: {last_error}")


# ---------------------------------------------------------------------------
# Schritt 3: Newsletter als HTML-Body generieren (Original-Template)
# ---------------------------------------------------------------------------

HTML_TEMPLATE_INSTRUCTIONS = """
Übertrage die recherchierten Inhalte in EXAKT dieses HTML-Grundgerüst
(Platzhalter in eckigen Klammern durch echte, recherchierte Inhalte
ersetzen; Struktur, Tags und Inline-Styles unverändert lassen):

<div style="border-bottom: 4px solid #1a3c6e; padding-bottom: 12px; margin-bottom: 8px;">
  <h1 style="color: #1a3c6e; font-size: 24px; margin: 0;">HR-Wissen Weekly</h1>
  <p style="color: #5a6b80; font-size: 15px; margin: 4px 0 0;">{week_label} &middot; Arbeitsrecht, Lohnsteuer &amp; Sozialversicherung</p>
  <p style="color: #99a3b0; font-size: 12px; margin: 2px 0 0;">Stand: {today_str}</p>
</div>

<p style="font-size: 15px; margin: 18px 0 6px;"><strong>Guten Morgen,</strong></p>
<p style="font-size: 15px; margin: 0 0 24px;">hier kommt das aktuelle HR-Wissen Weekly mit den wichtigsten Entwicklungen für HR, Payroll und Arbeitgeberpraxis.</p>

<div style="background: #f0f4fb; border-left: 4px solid #1a3c6e; padding: 16px 20px; border-radius: 6px; margin-bottom: 28px;">
  <p style="margin: 0 0 10px; font-weight: bold; color: #1a3c6e; font-size: 16px;">Executive Summary</p>
  <p style="margin: 0 0 12px;">[3-5 kurze, vollständige Sätze]</p>
  <p style="margin: 0 0 6px; font-weight: bold;">Was jetzt auf den Radar gehört:</p>
  <ul style="margin: 0; padding-left: 20px;">
    <li>[Punkt 1]</li>
    <li>[Punkt 2]</li>
    <li>[Punkt 3]</li>
  </ul>
</div>

<h2 style="color: #1a3c6e; font-size: 18px; border-bottom: 1px solid #dde3ec; padding-bottom: 6px;">Kurzüberblick</h2>
<table style="width: 100%; border-collapse: collapse; margin-bottom: 28px; font-size: 14px;">
  <thead>
    <tr style="background: #1a3c6e; color: #ffffff;">
      <th style="padding: 9px 12px; text-align: left; border: 1px solid #1a3c6e;">Bereich</th>
      <th style="padding: 9px 12px; text-align: left; border: 1px solid #1a3c6e;">Thema</th>
      <th style="padding: 9px 12px; text-align: left; border: 1px solid #1a3c6e;">Relevanz</th>
      <th style="padding: 9px 12px; text-align: left; border: 1px solid #1a3c6e;">Handlungsbedarf</th>
    </tr>
  </thead>
  <tbody>
    <!-- max. 8-10 Zeilen, eine Zeile je ausgewählter Meldung; abwechselnd
         style="background:#ffffff" und style="background:#f7f9fc" -->
    <tr style="background: #ffffff;">
      <td style="padding: 8px 12px; border: 1px solid #dde3ec;">[Bereich]</td>
      <td style="padding: 8px 12px; border: 1px solid #dde3ec;">[Thema]</td>
      <td style="padding: 8px 12px; border: 1px solid #dde3ec;">[Hoch/Mittel/Niedrig]</td>
      <td style="padding: 8px 12px; border: 1px solid #dde3ec;">[Konkreter Handlungsbedarf]</td>
    </tr>
  </tbody>
</table>

<!-- Für JEDE der sechs Kategorien in dieser Reihenfolge: Gesetzesvorhaben,
     BMF-Schreiben, Urteile, Verordnungen, Gesetzgebungsverfahren,
     HR-Digitalisierung. Icon und Rahmenfarbe je Kategorie siehe unten. -->
<h2 style="color: {cat_color}; font-size: 18px; border-bottom: 1px solid #dde3ec; padding-bottom: 6px;">{cat_icon} {cat_title}</h2>
<div style="margin-bottom: 16px; padding: 14px 18px; background: {cat_bg}; border-left: 3px solid {cat_color}; border-radius: 6px;">
  <p style="margin: 0 0 8px; font-weight: bold; color: {cat_color};">&#9658; [Überschrift der Meldung]</p>
  <p style="margin: 0 0 8px;"><strong>Kurz erklärt:</strong> [2-4 kurze Sätze, bei Urteilen/BMF-Schreiben inkl. Datum und Aktenzeichen]</p>
  <p style="margin: 0 0 4px; font-weight: bold;">Warum das wichtig ist:</p>
  <ul style="margin: 0 0 10px; padding-left: 20px;">
    <li>[konkrete HR/Payroll-Relevanz]</li>
    <li>[konkreter Handlungsbedarf/Prüfpunkt]</li>
  </ul>
  <p style="margin: 0; font-size: 13px; color: #5a6b80;">📎 Quelle: <a href="[exakte URL aus dem Kontext]" style="color: {cat_color};">[Institution/Gericht]</a>[ &middot; Az. [Aktenzeichen] falls vorhanden]</p>
</div>
<!-- Falls keine belastbare Meldung für diese Kategorie im Kontext steht: -->
<p style="margin: 0 0 28px; color: #777; font-style: italic;">Keine belastbare neue Entwicklung im Recherchezeitraum gefunden.</p>

<h2 style="color: #1a3c6e; font-size: 18px; border-bottom: 1px solid #dde3ec; padding-bottom: 6px;">🔭 Ausblick</h2>
<div style="margin-bottom: 24px; padding: 14px 18px; background: #f7f9fc; border-radius: 6px;">
  <p style="margin: 0 0 6px;">Was steht nächste Woche an? Nur belastbar Belegbares aufnehmen:</p>
  <ul style="margin: 0; padding-left: 20px;">
    <li>[bekannte Sitzungen Bundesrat/Bundestag, falls im Kontext belegt]</li>
    <li>[Fristenläufe/Urteile/Veröffentlichungen, falls im Kontext belegt]</li>
  </ul>
  <p style="margin: 8px 0 0; color: #777; font-style: italic;">Falls nichts Belastbares vorliegt: "Für die kommende Woche wurden keine belastbaren konkret terminierten HR-relevanten Ereignisse gefunden."</p>
</div>
"""


def generate_briefing_html(collected: dict, week_label: str, today_str: str) -> str:
    context = build_context_block(collected)
    if not context.strip():
        raise ValueError("Keine einzige Quelle erfolgreich abgerufen - Abbruch.")

    category_style_hints = "\n".join(
        f"- {cat}: Icon '{CATEGORY_ICON[cat]}', Rahmenfarbe {CATEGORY_COLOR[cat]}, "
        f"Hintergrund {CATEGORY_BG[cat]}"
        for cat in SOURCES
    )

    prompt = f"""Du erstellst das "HR-Wissen Weekly" - ein wöchentliches Briefing zu
aktuellen Entwicklungen in Arbeitsrecht, Lohnsteuer und Sozialversicherung
für HR- und Payroll-Verantwortliche in DACH-Unternehmen, für: {week_label}.

━━━ PFLICHTREGELN (keine Ausnahmen) ━━━
REGEL 1 (Inhalt/Grounding): Verwende AUSSCHLIESSLICH Informationen, die
wörtlich im folgenden Kontext stehen. Erfinde KEINE Aktenzeichen, Daten,
Gerichtsbezeichnungen, Verfahrensstände oder Links. Wenn ein Thema im
Kontext nicht ausreichend belegt ist, lass es weg statt zu raten. Lieber
eine Kategorie mit "Keine belastbare neue Entwicklung im Recherchezeitraum
gefunden." belegen als ungesicherte Informationen aufzunehmen.

REGEL 2 (Sprache): Schreibe AUSSCHLIESSLICH auf Deutsch, in vollständigen,
grammatikalisch korrekten Sätzen. Nur lateinische Schriftzeichen. Keine
abgebrochenen Sätze - wenn ein Satz nicht sauber zu Ende geht, die ganze
Meldung weglassen.

REGEL 3 (Quellen): Für JEDE Meldung die exakte Quell-URL aus dem Kontext
angeben (unverändert kopieren, keine Tippfehler, keine erfundenen URLs).

REGEL 4 (Auswahl): Wähle insgesamt 8-15 relevante Meldungen über alle
Kategorien hinweg. Auswahlkriterien: Aktualität, konkrete Relevanz für
HR/Arbeitsrecht/Lohnsteuer/Sozialversicherung/Payroll, belastbare Quelle,
hoher Praxisnutzen für DACH-Unternehmen, keine Dubletten.

REGEL 5 (Ton/Format): Scanbar und professionell - klare Überschriften,
kurze Absätze (max. 2-4 Sätze), keine Bleiwüste, keine werblichen
Formulierungen, keine Emoji-Inflation, kein Hinweis auf einen Anhang.

REGEL 6 (Format-Grenzen): Gib NUR den Inhalt zwischen (exklusive) den
<body>-Tags zurück - kein <!DOCTYPE>, kein <html>, kein <head>, kein
<body>-Tag selbst, kein Markdown, keine Code-Fences.

Kategorien und Styling (in dieser Reihenfolge, jede Kategorie als
eigener H2-Block mit dem jeweiligen Icon und der jeweiligen Rahmen-/
Hintergrundfarbe):
{category_style_hints}

KONTEXT (echt abgerufene Webseiten, einzige zulässige Faktenquelle):
{context}

Nutze exakt dieses HTML-Grundgerüst als Vorlage (Platzhalter in eckigen
Klammern ersetzen, Tags/Inline-Styles unverändert lassen):
{HTML_TEMPLATE_INSTRUCTIONS.format(week_label=week_label, today_str=today_str, cat_icon='[ICON]', cat_color='[FARBE]', cat_bg='[HINTERGRUND]', cat_title='[KATEGORIE-TITEL]')}
"""

    return call_openrouter(prompt)


# ---------------------------------------------------------------------------
# Mailversand: Microsoft Graph API (Exchange Online), siehe mail_graph.py
# ---------------------------------------------------------------------------
# Der eigentliche Versand-Code liegt in mail_graph.py (App-Only-Auth via
# Client Credentials Grant). hr_briefing.py ruft nur noch
# mail_graph.send_email(to, subject, html_body) auf - siehe run().


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------

def run(week_label: str, subject: str):
    recipient = os.environ["REPORT_RECIPIENT_EMAIL"]

    logger.info("Rufe alle HR-Quellen ab...")
    collected = collect_all_sources()

    ok_count = sum(1 for entries in collected.values() for e in entries if e["status"] == "ok")
    error_count = sum(1 for entries in collected.values() for e in entries if e["status"] != "ok")
    logger.info(f"{ok_count} Quellen erfolgreich abgerufen, {error_count} fehlgeschlagen.")

    today_str = datetime.date.today().strftime("%d.%m.%Y")
    body_html = generate_briefing_html(collected, week_label, today_str)
    unknown_urls = validate_output_urls(body_html, collected)
    suspicious_names = validate_source_names(body_html, collected)

    warning_items = []
    if unknown_urls:
        warning_items.append(
            "<strong>Unbekannte URLs</strong> (stammen nicht aus den abgerufenen "
            f"Quellen): <ul>{''.join(f'<li>{u}</li>' for u in unknown_urls)}</ul>"
        )
    if suspicious_names:
        warning_items.append(
            "<strong>Verdächtige Quellenangaben</strong> (passen zu keinem "
            f"konfigurierten Quellennamen): <ul>"
            f"{''.join(f'<li>{n}</li>' for n in suspicious_names)}</ul>"
        )

    warning_banner = ""
    if warning_items:
        warning_banner = (
            "<div style='background:#fff3cd;border:1px solid #ffc107;"
            "padding:12px;margin-bottom:16px;'>"
            "⚠️ Automatischer Grounding-Check hat Auffälligkeiten gefunden - "
            "bitte manuell prüfen, bevor der Report als verlässlich gilt:"
            f"{''.join(warning_items)}</div>"
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
<head><meta charset="utf-8"><title>HR-Wissen Weekly: {week_label}</title></head>
<body style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 720px; margin: 0 auto; color: #222; line-height: 1.6; background: #ffffff;">
{warning_banner}
{body_html}
{failed_sources_note}
<hr style="border: none; border-top: 1px solid #e5e9f0; margin: 28px 0 14px;">
<p style="font-size: 12px; color: #99a3b0; margin: 0 0 8px;">Recherchiert mit KI-Unterstützung &nbsp;|&nbsp; Alle Angaben ohne Gewähr &nbsp;|&nbsp; Automatisch erstellt am {now_str} &middot; {ok_count} Quellen abgerufen, {error_count} fehlgeschlagen</p>
<p style="font-size: 12px; color: #99a3b0; margin: 0;">Hinweis: Diese Zusammenstellung dient der allgemeinen Information und ersetzt keine rechtliche, steuerliche oder sozialversicherungsrechtliche Beratung. Bei konkreten Einzelfragen bitte Fachberatung einbeziehen.</p>
</body>
</html>"""

    # Report IMMER als Datei speichern - unabhängig davon, ob der
    # Mailversand danach klappt.
    output_dir = os.environ.get("OUTPUT_DIR", "output")
    os.makedirs(output_dir, exist_ok=True)
    safe_label = week_label.replace(" ", "_").replace("/", "-")
    output_path = os.path.join(output_dir, f"HR-Briefing_{safe_label}.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_html)
    logger.info(f"Report gespeichert unter: {output_path}")

    try:
        mail_graph.send_email(recipient, subject, full_html)
        logger.info("Mail erfolgreich versendet (Microsoft Graph).")
    except Exception as exc:
        logger.error(f"Mailversand fehlgeschlagen: {exc}")
        logger.error(
            "Der Report wurde trotzdem gespeichert (siehe oben) und steht "
            "als Actions-Artifact zum Download bereit."
        )
        raise
    finally:
        logger.info("Fertig.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["weekly"], default="weekly")
    args = parser.parse_args()

    today = datetime.date.today()
    week = today.isocalendar()[1]
    run(
        week_label=f"KW {week} / {today.strftime('%B %Y')}",
        subject=f"HR-Wissen Weekly – KW {week} / {today.strftime('%B %Y')}",
    )
