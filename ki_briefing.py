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
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
import langdetect
langdetect.DetectorFactory.seed = 0  # deterministische Ergebnisse statt zufälliger Schwankungen
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


def looks_garbled(text: str) -> str | None:
    """Erkennt typische Ausfallmuster kleiner/schlecht geeigneter Modelle.
    Deckt mehrere unabhängige Fehlerklassen ab, die in der Praxis
    beobachtet wurden - jede für sich reicht, um die Ausgabe zu verwerfen
    und stattdessen das nächste Modell zu versuchen."""

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

    # 2) Durchgesickerte interne Platzhalter-/Steuer-Token, z.B. <TASKBODY>,
    #    <THINK>, <ANSWER> - kommen von manchen 'reasoning'-Modellen, die
    #    ein eigenes Prompt-Template erwarten und dessen Marker versehentlich
    #    mit ausgeben, wenn das erwartete Format fehlt.
    leaked_tokens = re.findall(r'<\s*[A-Z_]{3,}\s*>', text)
    if leaked_tokens:
        return f"Durchgesickerte Platzhalter-Token gefunden ({leaked_tokens[:3]}) - Modell hat eigenes Prompt-Format nicht sauber ausgefüllt"

    # 3) Echte Sprach-Prüfung: erwarten deutschen Text. Frühere Version
    #    prüfte das GESAMTE Dokument als einen Block - das übersieht
    #    gemischtsprachige Ausgaben, bei denen z.B. Executive Summary und
    #    'Relevanz:'-Sätze deutsch sind, die eigentlichen Meldungstexte
    #    aber englisch (beobachtet: Gesamtdokument kippt dann trotzdem
    #    auf 'de', weil genug deutsche Füllsätze vorhanden sind). Deshalb
    #    jetzt satzweise: jeder ausreichend lange Satz wird einzeln
    #    geprüft, ein Anteil nicht-deutscher Sätze über der Schwelle
    #    gilt als gemischtsprachig und wird verworfen.
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
                "Deutsch erkannt - vermutlich gemischtsprachige Ausgabe "
                "(z.B. Meldungstexte englisch, nur Rahmensätze deutsch)"
            )
    elif len(plain_text) > 200:
        # Zu wenige einzeln prüfbare Sätze (z.B. sehr kurzer Report) -
        # Rückfall auf Gesamtdokument-Prüfung als besser als nichts.
        try:
            if langdetect.detect(plain_text) != "de":
                return "Spracherkennung meldet nicht 'de' für das Gesamtdokument"
        except langdetect.lang_detect_exception.LangDetectException:
            return "Sprache konnte nicht erkannt werden (evtl. zu wenig zusammenhängender Text)"

    # 4) Struktur-Check: erwartete Bausteine (Quellenangaben, Kategorien)
    #    müssen mindestens einmal vorkommen - sonst wurde das angeforderte
    #    Format schlicht ignoriert (z.B. nur Fließtext ohne Meldungen).
    if "Quelle:" not in text and "Quelle :" not in text:
        return "Kein einziges 'Quelle:' im Text gefunden - Format-Vorgabe wurde nicht befolgt"
    if "<h2" not in text.lower() and "<h3" not in text.lower():
        return "Keine Kategorie-/Meldungs-Überschriften (h2/h3) gefunden - Struktur fehlt komplett"

    if len(text) < 500:
        return "Antwort verdächtig kurz für ein vollständiges Briefing"

    return None


def _normalize_for_comparison(s: str) -> str:
    """Reduziert einen String auf Kleinbuchstaben+Ziffern, damit
    typografische Varianten (z.B. U+2011 non-breaking hyphen statt '-',
    unterschiedliche Groß-/Kleinschreibung oder Leerzeichen) beim
    Vergleich nicht fälschlich als 'unbekannt' gelten."""
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _significant_tokens(name: str) -> list[str]:
    """Zerlegt einen Quellennamen in bedeutungstragende Wort-Tokens
    (mind. 4 Zeichen, damit z.B. 'AI' oder 'EU' allein nicht zu
    Fehltreffern führen). 'ChatGPT / OpenAI' -> ['chatgpt', 'openai']."""
    tokens = re.split(r'[^a-z0-9]+', name.lower())
    return [t for t in tokens if len(t) >= 4]


def validate_source_names(html: str, collected: dict) -> list[str]:
    """Prüft, ob jede 'Quelle:'-Angabe im Report zu einem tatsächlich
    konfigurierten Quellennamen passt. Fängt Fälle ab, in denen das
    Modell sich einen plausibel klingenden, aber erfundenen Quellennamen
    ausdenkt (z.B. 'ChatUIView' statt eines echten Namens aus SOURCES).

    Vergleicht auf Wort-Ebene statt auf Komplett-Namen-Ebene: Modelle
    zitieren Quellen oft verkürzt (z.B. 'OpenAI (Release Notes)' statt
    dem vollen konfigurierten Namen 'ChatGPT / OpenAI') - das ist
    inhaltlich korrekt und darf keinen Fehlalarm auslösen. Ein Treffer
    reicht: mindestens ein bedeutungstragendes Wort aus dem
    konfigurierten Namen muss im Zitat vorkommen."""
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


def get_free_models(headers: dict) -> list[str]:
    """Fragt den öffentlichen OpenRouter-Modellkatalog live ab und gibt
    eine priorisierte Liste aktuell kostenloser Modell-IDs zurück.
    Löst das Grundproblem, dass fest kodierte ':free'-Modell-IDs
    innerhalb weniger Wochen ungültig werden (siehe Log vom 27.07.:
    3 von 4 fest kodierten Modellen waren bereits 404).

    Zwei Ausschlusskriterien (siehe Log vom 28.07.):
    - Utility-/Klassifikationsmodelle (Safety, Guard, Moderation, Embed,
      Rerank) sind keine Text-Generierungsmodelle und liefern keine
      brauchbare Briefing-Ausgabe.
    - Übergroße Modelle (>150 Mrd. Parameter laut Namenskonvention, z.B.
      '...-550b-...') neigen bei kostenlosen Endpunkten zu Timeouts von
      mehreren Minuten (beobachtet: nemotron-3-ultra-550b -> 524 nach
      ~5 Minuten). Werden deshalb komplett ausgeschlossen statt nur
      nachrangig behandelt.

    Bevorzugt darüber hinaus größere/etablierte Modellfamilien (bessere
    Textqualität, weniger Sprachvermischung), erkennbar an bekannten
    Namensmustern - fällt aber auf jedes verfügbare Gratis-Modell
    zurück, falls keines davon passt."""
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
        """Extrahiert die größte '<Zahl>b'-Angabe aus der Modell-ID als
        grobe Schätzung der Parameterzahl (z.B. 'nemotron-3-ultra-550b-a55b'
        -> 550). Liefert 0, wenn keine Zahl gefunden wird (dann nicht
        ausgeschlossen, da unbekannt != riesig)."""
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
        # 'reasoning'-Varianten erwarten oft ein eigenes Prompt-Template
        # und neigen zu durchgesickerten Platzhaltern (siehe looks_garbled) -
        # deshalb grundsätzlich zuletzt versuchen, unabhängig von der Familie.
        if "reasoning" in model_id:
            return (2, 0)
        for i, pattern in enumerate(preferred_patterns):
            if pattern in model_id:
                return (0, i)
        return (1, -m.get("context_length", 0))  # unbekannte Modelle: größerer Kontext zuerst

    free_models.sort(key=sort_key)
    ids = [m["id"] for m in free_models][:6]
    logger.info(f"Aktuell verfügbare kostenlose Modelle (Top 6): {ids}")
    return ids


def _post_with_hard_timeout(url: str, headers: dict, payload: dict, hard_timeout: int = 360):
    """Erzwingt ein echtes Wanduhr-Timeout. requests' eigener 'timeout'-
    Parameter greift nur, wenn zwischen zwei empfangenen Datenpaketen zu
    lange nichts kommt - manche Gratis-Modelle senden aber gerade genug
    Keep-Alive-Daten, um das zu umgehen (beobachtet: >6 Minuten trotz
    timeout=90). Nutzt bewusst einen Daemon-Thread statt
    ThreadPoolExecutor: Ein Executor würde beim Aufräumen (__exit__)
    trotzdem auf den langsamen Thread warten und damit den Timeout
    wirkungslos machen. Der Daemon-Thread läuft im Hintergrund einfach
    weiter (verworfen), sobald wir aufgeben - ohne den Hauptablauf
    aufzuhalten."""
    result: dict = {}

    def worker():
        try:
            result["resp"] = requests.post(
                url, headers=headers, json=payload, timeout=hard_timeout + 15
            )
        except Exception as exc:  # wird im Hauptthread erneut ausgewertet
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
            "temperature": 0.2,  # niedriger = weniger zufällige Wort-/Sprach-Einschübe
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
                break  # nächstes Modell versuchen, statt abzustürzen
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
                last_error = (
                    f"{model}: Antwort nicht auswertbar ({type(exc).__name__}: {exc}) - "
                    f"vermutlich abgeschnittene/kaputte JSON-Antwort. "
                    f"Rohtext-Anfang: {resp.text[:200]!r}"
                )
                logger.warning(last_error)
                break  # nächstes Modell versuchen, statt abzustürzen

            garbled_reason = looks_garbled(content)
            if garbled_reason:
                last_error = f"{model}: Ausgabe verworfen - {garbled_reason}"
                logger.warning(last_error)
                break  # nächstes Modell versuchen statt kaputten Text zu verwenden

            logger.info(f"Modell {model} erfolgreich, Ausgabe-Qualitätscheck bestanden")
            return content
    raise ValueError(f"Alle Modelle fehlgeschlagen oder lieferten fehlerhafte Ausgaben. Letzter Fehler: {last_error}")


def generate_briefing_html(collected: dict, week_label: str) -> str:
    context = build_context_block(collected)
    if not context.strip():
        raise ValueError("Keine einzige Quelle erfolgreich abgerufen - Abbruch.")

    prompt = f"""Du erstellst ein KI-News-Briefing für DACH-B2B-Professionals
(HR, Management, Recht, Compliance) für: {week_label}.

REGEL 1 (Inhalt): Verwende AUSSCHLIESSLICH Informationen, die wörtlich im
folgenden Kontext stehen. Erfinde KEINE Zahlen, Produktnamen, Fristen oder
Ereignisse. Wenn ein Thema im Kontext nicht ausreichend belegt ist, lass
es weg statt zu raten.

REGEL 2 (Sprache): Schreibe AUSSCHLIESSLICH auf Deutsch, in vollständigen,
grammatikalisch korrekten Sätzen. Verwende NUR lateinische Schriftzeichen
(keine Thai-, chinesischen, japanischen oder koreanischen Zeichen, auch
nicht einzelne). Baue keine unvollständigen oder abgebrochenen Sätze ein -
wenn du einen Satz nicht sauber zu Ende formulieren kannst, lass die
gesamte Meldung weg.

REGEL 3 (Quellen): Für JEDE Meldung: gib die exakte Quell-URL aus dem
Kontext an (kopiere sie unverändert, ohne Tippfehler). Erfinde keine URLs.

REGEL 4 (Format): Beginne DIREKT mit der Executive Summary. Füge KEINE
eigene Titelüberschrift (kein <h1>) hinzu - das übernimmt die aufrufende
Anwendung bereits.

KONTEXT (echt abgerufene Webseiten):
{context}

Erstelle einen strukturierten HTML-Report (nur <body>-Inhalt, kein
Markdown, kein <h1>) mit:
1. Executive Summary (3-5 vollständige Sätze)
2. Pro Kategorie (nur wenn Meldungen vorhanden): Überschrift (h2),
   darunter je Meldung: Überschrift (h3), 2-4 vollständige Sätze
   Zusammenfassung, "Relevanz:" (1 vollständiger Satz für
   DACH-Professionals), und
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

    # Report IMMER als Datei speichern - unabhängig davon, ob der
    # Mailversand danach klappt. So geht bei einem Versand-Fehler
    # (z.B. fehlendes/falsches Secret) nicht der ganze Rechercheinhalt
    # verloren.
    output_dir = os.environ.get("OUTPUT_DIR", "output")
    os.makedirs(output_dir, exist_ok=True)
    safe_label = week_label.replace(" ", "_").replace("/", "-")
    output_path = os.path.join(output_dir, f"KI-Briefing_{safe_label}.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_html)
    logger.info(f"Report gespeichert unter: {output_path}")

    try:
        service = get_gmail_service()
        send_email(service, recipient, subject, full_html)
        logger.info("Mail erfolgreich versendet.")
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
        subject=f"🤖 KI-Briefing KW {week} – {today.strftime('%d.%m.%Y')}",
    )
