"""
HR-Briefing – wöchentliches HR-Wissen-Weekly per Email
Läuft als GitHub Actions Workflow (wöchentlich / manuell).

Gebaut nach dem Muster des KI-Briefings (Send-AI-Briefing-Newsletter),
inkl. der dort gesammelten Lessons Learned. Ablauf:

  1. Feste Quellen (Bundestag, Bundesregierung, BMAS, BMF, BAG, BFH, BSG,
     Bundesrat, ...) werden ECHT abgerufen (requests + BeautifulSoup) -
     kein Modellwissen.
  2. Von den Übersichtsseiten wird den Links zu den EINZELMELDUNGEN
     gefolgt (siehe folge_detailseiten). Ohne diesen Schritt steht im
     Kontext nur eine Liste aus Datum und Überschrift, und das Modell
     kann nichts Konkretes schreiben, ohne zu erfinden.
  3. Je Kategorie EIN eigener LLM-Aufruf, der strukturiertes JSON mit
     festen Pflichtfeldern liefert (meldungen.py). Der frühere Ansatz -
     ein einziger Prompt mit allen Quellen plus HTML-Gerüst - überschritt
     das Kontextfenster kostenloser Modelle um ein Mehrfaches.
  4. Feldweise Grounding-Prüfung: Aktenzeichen und Daten müssen wörtlich
     in einer abgerufenen Quelle stehen, sonst wird das Feld entfernt;
     eine Meldung mit unbekannter URL wird ganz verworfen.
  5. Das HTML baut render.py in Python, nicht das Modell.
  6. hot_topics.py führt Dauerthemen über Läufe hinweg fort - bis zum
     Inkrafttreten und eine Karenzzeit darüber hinaus.
  7. Versand per Gmail API (OAuth2 mit Refresh-Token) von
     vdnewsletteranalyse@gmail.com an REPORT_RECIPIENT_EMAIL. Lesen der
     Newsletter und Versand laufen über EIN gemeinsames Token (Scopes
     gmail.readonly + gmail.send), erzeugt mit generate_token.py.

Ursprung: PhiBox-Agent "Send Email HR Briefing" (agent-send-email-
hr-briefing.json) - Kategorien, Quellen-Prioritäten, HTML-Template und
Anti-Halluzinations-Regeln stammen aus dieser Vorlage.
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
from urllib.parse import urljoin, urlparse

import requests
import langdetect
langdetect.DetectorFactory.seed = 0  # deterministische Ergebnisse statt zufälliger Schwankungen
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import onedrive_upload
import web_search_sources
import hot_topics
import meldungen as meldungen_modul
import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; VD-HR-Briefing-Bot/1.0; +internal-use)"
TIMEOUT_SECONDS = 15

# Ein Token für beides: Newsletter lesen UND Briefing versenden.
# Muss identisch sein mit der Liste in generate_token.py - weicht sie ab,
# lehnt Google die Token-Nutzung ab bzw. der Versand scheitert mit 403.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# Betrachtungszeitraum. Der Original-PhiBox-Agent arbeitete mit "letzte
# ca. vier Wochen" und erlaubte ausdrücklich ältere Meldungen, wenn sie
# einen Verfahrensstand, eine Frist oder eine bevorstehende Pflicht
# betreffen. Diese Regel war bei der Portierung nach Python verloren
# gegangen - der Newsletter kannte danach nur noch die laufende Woche.
BETRACHTUNGSZEITRAUM = (
    "die letzten rund vier Wochen; ältere Meldungen sind ausdrücklich "
    "erwünscht, wenn sie einen aktuellen Verfahrensstand, eine laufende "
    "Frist oder eine bevorstehende Pflicht betreffen"
)

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
        ("LTO – Arbeitsrecht", "https://www.lto.de/rechtsgebiete/arbeitsrecht-urteile-gesetzesaenderungen-nachrichten",
         "Fachmedium", "Nachrichten-Hub Arbeitsrecht"),
    ],
}

# Reihenfolge der Kategorien im fertigen Newsletter.
KATEGORIE_REIHENFOLGE = list(SOURCES) + ["Newsletter-Auswertung"]


# ---------------------------------------------------------------------------
# Schritt 1: Echtes Abrufen der Quellen
# ---------------------------------------------------------------------------

def fetch_and_extract(url: str, max_chars: int = 6000, mit_links: bool = False) -> dict:
    """Ruft eine URL wirklich ab und liefert bereinigten Text zurück.
    status == 'error' bedeutet: NICHT verwenden, keine Ersatzinhalte.

    Mit `mit_links=True` werden zusätzlich alle Links samt Linktext
    gesammelt - Grundlage für folge_detailseiten()."""
    result = {
        "url": url, "status": "error", "http_status": None,
        "title": None, "text": "", "error": None, "links": [],
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
        result["http_status"] = resp.status_code
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        if mit_links:
            # Links einsammeln, nachdem Navigation/Kopf/Fußzeile entfernt
            # sind - dort stehen nur Rubriken, keine Meldungen.
            for tag in soup(["nav", "footer", "header"]):
                tag.decompose()
            for a in soup.find_all("a", href=True):
                text = a.get_text(separator=" ", strip=True)
                if text:
                    result["links"].append((text, urljoin(url, a["href"])))

        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()

        title_tag = soup.find("title")
        result["title"] = title_tag.get_text(strip=True) if title_tag else None
        result["text"] = soup.get_text(separator="\n", strip=True)[:max_chars]
        result["status"] = "ok"
    except requests.exceptions.RequestException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


# Linktexte, die nie zu einer Einzelmeldung führen. Ohne diesen Filter
# besteht die halbe Ausbeute aus "Mehr erfahren" und "Zur Startseite".
LINK_STOPPWOERTER = (
    "mehr erfahren", "weiterlesen", "zur startseite", "startseite",
    "datenschutz", "impressum", "kontakt", "barrierefrei", "newsletter",
    "cookie", "suche", "sitemap", "nach oben", "drucken", "teilen",
    "english", "leichte sprache", "gebärdensprache",
    "übersicht", "alle anzeigen", "mehr anzeigen", "zurück zur",
)

MAX_DETAILSEITEN_JE_QUELLE = int(os.environ.get("MAX_DETAILSEITEN", "4"))


def _ist_meldungslink(text: str, ziel: str, basis: str) -> bool:
    """Entscheidet, ob ein Link auf eine Einzelmeldung zeigt. Heuristisch,
    aber bewusst konservativ: lieber eine Meldung verpassen als die
    Rubrik 'Datenschutz' in den Kontext holen."""
    text = (text or "").strip()
    if len(text) < 30:            # Überschriften sind lang, Navigation ist kurz
        return False
    if any(w in text.lower() for w in LINK_STOPPWOERTER):
        return False

    ziel_teile, basis_teile = urlparse(ziel), urlparse(basis)
    if ziel_teile.scheme not in ("http", "https"):
        return False
    if ziel_teile.netloc != basis_teile.netloc:
        return False              # nur dieselbe Behörde/Redaktion
    if ziel.rstrip("/") == basis.rstrip("/"):
        return False
    if re.search(r'\.(pdf|zip|docx?|xlsx?|jpe?g|png|mp4)(\?|$)', ziel, re.IGNORECASE):
        return False              # Binärdateien kann BeautifulSoup nicht lesen
    return True


def folge_detailseiten(collected: dict) -> int:
    """Lädt zu jeder Übersichtsseite die verlinkten Einzelmeldungen nach.

    DAS ist der eigentliche Hebel gegen den dünnen Informationsgehalt.
    Bisher stand im Kontext nur die Pressemitteilungs-LISTE des BAG oder
    des BMF: Datum plus Überschrift, nach wenigen tausend Zeichen
    abgeschnitten. Daraus kann kein Modell schreiben, was ein Urteil
    entschieden hat oder was ein BMF-Schreiben anordnet - es kann die
    Überschrift nur umformulieren, und genau so las sich der Newsletter.

    Die nachgeladenen Seiten laufen durch dieselbe fetch_and_extract()
    und unterliegen damit automatisch demselben Grounding-Mechanismus
    wie alle anderen Quellen."""
    bekannte = {e["url"] for entries in collected.values() for e in entries}
    nachgeladen = 0

    for category, entries in list(collected.items()):
        for eintrag in list(entries):
            if eintrag.get("status") != "ok" or not eintrag.get("links"):
                continue
            if eintrag.get("ist_detailseite"):
                continue           # nicht rekursiv weiterlaufen

            gefunden = 0
            for text, ziel in eintrag["links"]:
                if gefunden >= MAX_DETAILSEITEN_JE_QUELLE:
                    break
                if ziel in bekannte:
                    continue
                if not _ist_meldungslink(text, ziel, eintrag["url"]):
                    continue

                detail = fetch_and_extract(ziel, max_chars=7000)
                bekannte.add(ziel)
                if detail["status"] != "ok" or len(detail["text"]) < 400:
                    continue

                collected[category].append({
                    "name": f"{eintrag['name']} → {text[:70]}",
                    "url": ziel,
                    "format": "Einzelmeldung",
                    "access_hint": f"Von {eintrag['name']} verlinkte Einzelmeldung",
                    "ist_detailseite": True,
                    **detail,
                })
                gefunden += 1
                nachgeladen += 1

            if gefunden:
                logger.info(f"[{category}] {eintrag['name']}: {gefunden} Einzelmeldung(en) nachgeladen.")

    logger.info(f"Detailseiten insgesamt nachgeladen: {nachgeladen}")
    return nachgeladen


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
            fetch_result = fetch_and_extract(url, mit_links=True)
            entry = {
                "name": name, "url": url, "format": fmt,
                "access_hint": access_hint, "ist_detailseite": False,
                **fetch_result,
            }
            status_note = "✅" if entry["status"] == "ok" else f"❌ {entry['error']}"
            logger.info(f"[{category}] {name}: {status_note}")
            collected[category].append(entry)
    return collected


def collect_search_based_sources(collected: dict, laufende_themen: list | None = None) -> None:
    """Ergänzt `collected` (in-place) um Quellen, die per echter Web-Suche
    (Firecrawl) gefunden wurden - schließt die Lücke zu den festen
    SOURCES-URLs, die nur Behörden-Übersichtsseiten abdecken und daher
    tagesaktuelle/spezialisierte Fachblog-Analysen verpassen (siehe
    web_search_sources.py für den Hintergrund). Jede gefundene URL wird
    über die GLEICHE fetch_and_extract()-Funktion abgerufen wie die
    festen Quellen - unterliegt also automatisch demselben Grounding-
    Mechanismus, keine Sonderbehandlung nötig.

    Mit `laufende_themen` wird zusätzlich gezielt nach den Dauerthemen
    des Themenradars gesucht, und zwar OHNE die Ein-Monats-Schranke der
    normalen Suche: ein Thema, zu dem es vier Wochen lang nichts Neues
    gab, wäre sonst unauffindbar, obwohl sein Stichtag näher rückt.

    Bereits über die festen SOURCES abgedeckte URLs werden übersprungen.
    Scheitert die Suche komplett (kein API-Key, Netzwerkproblem),
    passiert einfach nichts - das Briefing läuft dann nur mit den festen
    Quellen weiter."""
    known_urls = {
        e["url"] for entries in collected.values() for e in entries
    }

    urls_per_category = web_search_sources.find_urls_per_category()

    if laufende_themen:
        namen = [t["anzeige"] for t in laufende_themen]
        treffer = web_search_sources.find_urls_fuer_themen(namen)
        if treffer:
            urls_per_category["Hot Topics"] = treffer

    for category, url_tuples in urls_per_category.items():
        for title, url in url_tuples:
            if url in known_urls:
                continue
            fetch_result = fetch_and_extract(url)
            entry = {
                "name": title[:80] or url,
                "url": url,
                "format": "Web-Suche (Firecrawl)",
                "access_hint": "Per Web-Suche gefunden, nicht aus fester Quellenliste",
                "ist_detailseite": True,   # inhaltlich eine Einzelmeldung
                **fetch_result,
            }
            status_note = "✅" if entry["status"] == "ok" else f"❌ {entry['error']}"
            logger.info(f"[{category}, Web-Suche] {title[:60]}: {status_note}")
            # Treffer zu Dauerthemen bekommen keinen eigenen Abschnitt -
            # sie gehören inhaltlich zu den laufenden Verfahren.
            ziel = "Gesetzgebungsverfahren" if category == "Hot Topics" else category
            collected.setdefault(ziel, []).append(entry)
            known_urls.add(url)


# ---------------------------------------------------------------------------
# Zusätzliche Quelle: HR-relevante Newsletter aus Gmail
# ---------------------------------------------------------------------------
# Ergänzt die Behörden-/Fachmedien-Quellen um tatsächlich empfangene
# Newsletter-Emails aus dem bestehenden Newsletter-Analyse-Postfach
# (vdnewsletteranalyse@gmail.com), gefiltert auf HR-Relevanz per
# Keyword-Vorfilter (spart LLM-Kosten - nicht jede Mail muss teuer
# klassifiziert werden). Die gefundenen Mails durchlaufen danach
# DENSELBEN Grounding-Prozess wie alle anderen Quellen - anders als im
# einfacheren Newsletter-Analyse-Repo, das ungeprüft direkt ans LLM geht.
#
# Scheitert dieser Schritt komplett (z.B. Gmail-Auth-Problem), ist das
# NICHT fatal - das Briefing läuft dann einfach ohne die Newsletter-
# Ergänzung weiter (lieber weniger Quellen als ein komplett
# fehlschlagendes Briefing).

NEWSLETTER_DAYS_BACK = 7

HR_KEYWORDS = [
    "arbeitsrecht", "lohnsteuer", "sozialversicherung", "human resources",
    "payroll", "gehaltsabrechnung", "kündigung", "arbeitsvertrag",
    "mitarbeiter", "personalwesen", "personalabteilung",
    "bundesarbeitsgericht", "bundesfinanzhof", "bundessozialgericht",
    "bmas", "bundestag", "bundesrat", "gesetzentwurf", "referentenentwurf",
    "verordnung", "urteil", "rechtsprechung", "elternzeit",
    "urlaubsanspruch", "aufstiegsfortbildung", "a1-bescheinigung",
    "entgeltabrechnung", "arbeitszeugnis", "abmahnung", "betriebsrat",
    "diskriminierung", "agg", "homeoffice", "mobiles arbeiten",
    "weiterbildung", "recruiting", "onboarding", "hr-digitalisierung",
    "betriebsprüfung", "sozialversicherungsbeitrag",
]


def _extract_email_body(payload: dict) -> str:
    """Extrahiert den Klartext-Body einer Gmail-Nachricht (rekursiv für
    Multipart-Mails). Identisch zur Logik im Newsletter-Analyse-Repo."""
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain":
                data = part["body"].get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
        for part in payload["parts"]:
            nested = _extract_email_body(part)
            if nested:
                return nested
        return ""
    data = payload.get("body", {}).get("data", "")
    if data:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    return ""


def _get_gmail_service():
    """Gmail-Zugriff für BEIDES: Lesen der Newsletter (gmail.readonly)
    und Versand des Briefings (gmail.send) - über ein gemeinsames
    Refresh-Token im Secret GMAIL_TOKEN_JSON.

    Wichtig: Das Token MUSS mit beiden Scopes erzeugt worden sein, sonst
    scheitert der Versand mit einem 403 "insufficient authentication
    scopes". Zum Neuerzeugen siehe generate_token.py."""
    creds_data = json.loads(os.environ["GMAIL_TOKEN_JSON"])
    client_info = json.loads(os.environ["GMAIL_CREDENTIALS_JSON"])["installed"]
    creds = Credentials(
        token=creds_data.get("token"),
        refresh_token=creds_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_info["client_id"],
        client_secret=client_info["client_secret"],
        scopes=GMAIL_SCOPES,
    )
    return build("gmail", "v1", credentials=creds)


def fetch_hr_newsletter_sources(days_back: int = NEWSLETTER_DAYS_BACK) -> list[dict]:
    """Liest die letzten Newsletter-Emails, filtert per Keyword-Vorfilter
    auf HR-Relevanz und gibt sie im selben Format wie die übrigen
    Quellen zurück (name/url/format/access_hint/status/text), damit sie
    denselben Grounding-Checks unterliegen wie alle anderen Quellen.
    Jede zurückgegebene Quelle bekommt einen echten Gmail-Deeplink als
    URL (funktioniert beim Öffnen im selben Konto)."""
    try:
        service = _get_gmail_service()
    except Exception as exc:
        logger.warning(f"Newsletter-Postfach nicht erreichbar (Auth-Problem?): {exc}")
        return []

    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=days_back)).strftime("%Y/%m/%d")
    try:
        result = service.users().messages().list(
            userId="me", q=f"after:{since}", maxResults=200
        ).execute()
    except Exception as exc:
        logger.warning(f"Newsletter-Postfach: Abruf der Nachrichtenliste fehlgeschlagen: {exc}")
        return []

    message_refs = result.get("messages", [])
    logger.info(f"Newsletter-Postfach: {len(message_refs)} Email(s) der letzten {days_back} Tage gefunden.")

    sources = []
    for ref in message_refs:
        try:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="full"
            ).execute()
        except Exception:
            continue  # einzelne Mail nicht lesbar - einfach überspringen

        subject = sender = ""
        for h in msg["payload"].get("headers", []):
            if h["name"] == "Subject":
                subject = h["value"]
            if h["name"] == "From":
                sender = h["value"]

        body = _extract_email_body(msg["payload"])
        haystack = f"{subject} {body[:1500]}".lower()

        if not any(kw in haystack for kw in HR_KEYWORDS):
            continue  # kein HR-Bezug erkennbar - Vorfilter spart LLM-Kosten

        gmail_link = f"https://mail.google.com/mail/u/0/#inbox/{msg['id']}"
        sources.append({
            "name": f"Newsletter: {sender[:60]}",
            "url": gmail_link,
            "format": "Newsletter-Email",
            "access_hint": f"Betreff: {subject[:100]}",
            "status": "ok",
            "text": f"Betreff: {subject}\n\n{body[:5000]}",
            "http_status": 200,
            "title": subject,
            "error": None,
            "links": [],
            "ist_detailseite": True,
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })

    logger.info(
        f"Newsletter-Postfach: {len(sources)} von {len(message_refs)} "
        "Email(s) als HR-relevant eingestuft."
    )
    return sources


# ---------------------------------------------------------------------------
# Schritt 2: LLM-Aufrufe
# ---------------------------------------------------------------------------

def looks_garbled(text: str) -> str | None:
    """Erkennt typische Ausfallmuster kleiner/schlecht geeigneter Modelle
    in FLIESSTEXT-Antworten (Executive Summary).

    Für die JSON-Antworten der Kategorie-Extraktion gilt stattdessen
    meldungen.pruefe_json_antwort - dort wären die früheren Prüfungen auf
    '<h2' oder 'Quelle:' sinnlos, weil das HTML jetzt in render.py
    entsteht und gar nicht mehr vom Modell kommt."""

    # 1) Fremde Schriftsysteme mitten im deutschen Text
    unexpected_scripts = re.findall(
        r'[฀-๿'    # Thai
        r'一-鿿'     # CJK (Chinesisch)
        r'぀-ヿ'     # Hiragana/Katakana (Japanisch)
        r'가-힯'     # Hangul (Koreanisch)
        r'ऀ-ॿ'     # Devanagari (Hindi)
        r'؀-ۿ'     # Arabisch
        r'֐-׿'     # Hebräisch
        r'Ѐ-ӿ'     # Kyrillisch
        r']', text
    )
    if unexpected_scripts:
        sample = "".join(unexpected_scripts[:5])
        return f"Unerwartete Schriftzeichen gefunden (z.B. '{sample}') - vermutlich korrupte Ausgabe"

    # 2) Durchgesickerte interne Platzhalter-/Steuer-Token, z.B. <TASKBODY>
    leaked_tokens = re.findall(r'<\s*[A-Z_]{3,}\s*>', text)
    if leaked_tokens:
        return f"Durchgesickerte Platzhalter-Token gefunden ({leaked_tokens[:3]})"

    # 3) Liegengebliebene eckige Klammern im sichtbaren Text - meist ein
    # nicht ersetzter Platzhalter aus einer Vorlage. Eckige Klammern
    # kommen in deutschen Rechtstexten praktisch nie im Fließtext vor,
    # daher niedriges Fehlalarm-Risiko.
    if re.search(r'[\[\]]', re.sub(r'<[^>]+>', ' ', text)):
        return "Eckige Klammern im Fließtext gefunden - vermutlich ein nicht ersetzter Platzhalter"

    # 4) Echte Sprach-Prüfung, satzweise statt dokumentweise (siehe Lesson
    #    Learned #8 - vermeidet, dass gemischtsprachige Ausgaben
    #    durchrutschen)
    plain_text = re.sub(r'<[^>]+>', ' ', text)
    plain_text = re.sub(r'\s+', ' ', plain_text).strip()
    sentences = re.split(r'(?<=[.!?])\s+', plain_text)
    substantial_sentences = [s for s in sentences if len(s) >= 40]

    if len(substantial_sentences) >= 4:
        non_german_count = checked_count = 0
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

    return None


# Mindest-Kontextfenster für ein zugelassenes Modell.
#
# Hier stand vorher 8.000 Token. Ein Lauf sammelt gut 40 Quellen; der
# frühere Sammel-Prompt (alle Kategorien plus HTML-Gerüst in einem Zug)
# kam damit auf rund 120.000 Zeichen ≈ 38.000 Token. Ein Modell mit
# 8.000 Token Kontext sieht davon etwa ein Fünftel - den Anfang - und
# schneidet den Rest lautlos ab. Genau deshalb stand bei der zuletzt
# einsortierten Kategorie (HR-Digitalisierung) Woche für Woche "Keine
# belastbare neue Entwicklung": ihre Quellen hat das Modell nie gesehen.
MIN_CONTEXT_LENGTH = int(os.environ.get("MIN_CONTEXT_LENGTH", "32000"))


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
        "openai/gpt-oss-120b:free",
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

    def passt(m: dict, mindest_kontext: int) -> bool:
        return (
            m.get("pricing", {}).get("prompt") == "0"
            and m.get("pricing", {}).get("completion") == "0"
            and m.get("id", "").endswith(":free")
            and m.get("context_length", 0) >= mindest_kontext
            and not any(kw in m.get("id", "").lower() for kw in exclude_keywords)
            and estimated_param_billions(m.get("id", "")) <= 150
        )

    free_models = [m for m in all_models if passt(m, MIN_CONTEXT_LENGTH)]
    if not free_models:
        # Lieber ein kleineres Fenster als gar kein Modell - der
        # Kategorie-Prompt wird dann eben beschnitten, aber der Lauf
        # bricht nicht ab.
        logger.warning(
            f"Kein kostenloses Modell mit mindestens {MIN_CONTEXT_LENGTH} Token "
            "Kontext gefunden - weiche auf 16.000 aus."
        )
        free_models = [m for m in all_models if passt(m, 16000)]
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
    logger.info(f"Kostenlose Modelle mit ausreichendem Kontext (Top 6): {ids}")
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


# Der Modellkatalog wird einmal je Lauf geholt, nicht je Kategorie -
# sonst kostet die Aufteilung in Kategorien acht zusätzliche
# Katalogabrufe.
_MODELL_CACHE: list[str] = []

# Welches Modell zuletzt eine brauchbare Antwort geliefert hat, und wie
# oft ein Modell in diesem Lauf schon versagt hat.
#
# Ohne dieses Gedächtnis läuft die Fallback-Kette bei JEDEM der acht
# Aufrufe von vorn. Antwortet das erste Modell gerade mit 429, wird
# achtmal dieselbe Wartezeit abgesessen, bevor achtmal dasselbe zweite
# Modell übernimmt. Bei einem Aufruf je Ausgabe fiel das nicht auf.
_BEWAEHRTES_MODELL: str | None = None
_FEHLVERSUCHE: dict[str, int] = {}

# Ab so vielen Fehlversuchen wird ein Modell für den Rest des Laufs
# übersprungen. Ein Modell, das zweimal nicht lieferte, liefert
# erfahrungsgemäß auch beim dritten Mal nicht - es kostet nur Zeit.
MAX_FEHLVERSUCHE = 2

# Hartes Timeout je Anfrage. Die Kategorie-Prompts sind deutlich kleiner
# als der frühere Sammel-Prompt, brauchen also keine sechs Minuten.
HARD_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "180"))


def _modellreihenfolge() -> list[str]:
    """Bewährtes Modell zuerst, ausgefallene ans Ende bzw. heraus."""
    kandidaten = [
        m for m in _MODELL_CACHE
        if _FEHLVERSUCHE.get(m, 0) < MAX_FEHLVERSUCHE
    ] or list(_MODELL_CACHE)   # lieber alle nochmal als gar keins

    if _BEWAEHRTES_MODELL in kandidaten:
        kandidaten.remove(_BEWAEHRTES_MODELL)
        kandidaten.insert(0, _BEWAEHRTES_MODELL)
    return kandidaten


def call_openrouter(prompt: str, validator=None) -> str:
    """Schickt einen Prompt an das erste kostenlose Modell, das eine
    brauchbare Antwort liefert.

    `validator(text) -> Grund|None` prüft die Ausgabe. Schlägt sie fehl,
    wird das nächste Modell versucht. Ohne Angabe gilt looks_garbled()
    (Fließtext); die Kategorie-Extraktion reicht ihre eigene
    JSON-Prüfung herein."""
    global _MODELL_CACHE, _BEWAEHRTES_MODELL

    validator = validator or looks_garbled
    api_key = os.environ["OPENROUTER_API_KEY"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/",
    }
    if not _MODELL_CACHE:
        _MODELL_CACHE = get_free_models(headers)

    last_error = None
    for model in _modellreihenfolge():
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
                    headers, payload, hard_timeout=HARD_TIMEOUT,
                )
            except requests.exceptions.RequestException as exc:
                last_error = f"{model}: Verbindungsfehler/Timeout - {exc}"
                logger.warning(last_error)
                _FEHLVERSUCHE[model] = _FEHLVERSUCHE.get(model, 0) + 1
                break
            if resp.status_code == 429:
                # Einmal kurz warten, dann weiterziehen. Lange Wartezeiten
                # lohnen nicht mehr, seit pro Ausgabe mehrere Aufrufe
                # laufen - ein anderes Modell ist schneller als die
                # Geduld mit diesem.
                _FEHLVERSUCHE[model] = _FEHLVERSUCHE.get(model, 0) + 1
                if attempt == 0:
                    logger.info(f"429 bei {model} - warte 20s")
                    time.sleep(20)
                    continue
                last_error = f"{model}: dauerhaft 429 (Rate-Limit)"
                logger.warning(last_error)
                break
            if resp.status_code in (401, 403):
                # Ungültiger/fehlender API-Key betrifft ALLE Modelle gleich -
                # sinnlos, hier weitere Modelle durchzuprobieren.
                raise PermissionError(
                    f"OpenRouter meldet HTTP {resp.status_code} (API-Key ungültig, "
                    f"fehlend oder widerrufen) - Details: {resp.text[:300]}. "
                    "Bitte OPENROUTER_API_KEY-Secret prüfen."
                )
            if resp.status_code in (400, 404, 500, 502, 503):
                last_error = f"{model}: HTTP {resp.status_code} - {resp.text[:300]}"
                logger.warning(f"Modell {model} fehlgeschlagen: {last_error}")
                _FEHLVERSUCHE[model] = _FEHLVERSUCHE.get(model, 0) + 1
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
                _FEHLVERSUCHE[model] = _FEHLVERSUCHE.get(model, 0) + 1
                break

            grund = validator(content)
            if grund:
                last_error = f"{model}: Ausgabe verworfen - {grund}"
                logger.warning(last_error)
                _FEHLVERSUCHE[model] = _FEHLVERSUCHE.get(model, 0) + 1
                break

            logger.info(f"Modell {model} erfolgreich, Ausgabe-Qualitätscheck bestanden")
            _BEWAEHRTES_MODELL = model
            return content
    raise ValueError(
        f"Alle Modelle fehlgeschlagen oder lieferten fehlerhafte Ausgaben. "
        f"Letzter Fehler: {last_error}"
    )


def _json_ausschneiden(text: str) -> str:
    """Holt das JSON-Objekt aus einer Modellantwort, auch wenn ein Satz
    oder eine Code-Fence drumherum steht."""
    if not text:
        return "{}"
    fence = re.search(r'```(?:json)?\s*(.+?)```', text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, ende = text.find("{"), text.rfind("}")
    return text[start:ende + 1] if start != -1 and ende > start else text.strip()


def generiere_executive_summary(alle_meldungen: list, week_label: str) -> dict:
    """Erzeugt die Executive Summary AUS den bereits geprüften Meldungen.

    Der Kontext ist damit winzig und besteht ausschließlich aus Text, der
    die Grounding-Prüfung schon bestanden hat. Die frühere REGEL 8
    ("erfinde in der Summary keine Zahlen") war nur eine Bitte an das
    Modell und hat genau deshalb nicht zuverlässig gewirkt; jetzt kann
    die Summary gar nichts anderes sehen als die geprüften Meldungen."""
    if not alle_meldungen:
        return {"text": "", "radar": []}

    zusammenfassung = "\n".join(
        f"- [{m.get('kategorie')}] {m.get('ueberschrift')}: {m.get('kurz_erklaert')} "
        f"(Relevanz {m.get('relevanz')}; Handlungsbedarf: {m.get('handlungsbedarf')})"
        for m in alle_meldungen[:15]
    )

    prompt = f"""Du schreibst die Executive Summary eines deutschen HR-Wochenbriefings
für {week_label}.

Unten stehen die bereits ausgewählten und geprüften Meldungen dieser
Ausgabe. Fasse sie zusammen.

REGELN:
- Nur Sachverhalte aus der Liste. Keine zusätzlichen Zahlen, Prozentwerte,
  Aktenzeichen oder Behauptungen - auch nicht, wenn sie plausibel klingen.
- Deutsch, 3 bis 5 vollständige Sätze, sachlich, ohne Werbesprache.
- Danach genau 3 kurze Stichpunkte "Was jetzt auf den Radar gehört".
- Keine eckigen Klammern, kein HTML, kein Markdown.

Antworte als reines JSON, ohne Code-Fence:
{{"text": "Die 3-5 Saetze.", "radar": ["Punkt 1", "Punkt 2", "Punkt 3"]}}

MELDUNGEN DIESER AUSGABE:
{zusammenfassung}
"""

    def pruefe(antwort: str) -> str | None:
        try:
            daten = json.loads(_json_ausschneiden(antwort))
        except (json.JSONDecodeError, ValueError, TypeError):
            return "Keine auswertbare JSON-Antwort für die Executive Summary"
        if not isinstance(daten, dict) or len(str(daten.get("text", ""))) < 80:
            return "Executive Summary zu kurz oder ohne Textfeld"
        return looks_garbled(str(daten.get("text", "")))

    try:
        antwort = call_openrouter(prompt, pruefe)
        daten = json.loads(_json_ausschneiden(antwort))
        return {
            "text": str(daten.get("text", "")).strip(),
            "radar": [str(p).strip() for p in daten.get("radar", []) if str(p).strip()][:3],
        }
    except Exception as exc:
        # Die Summary ist Beiwerk - ihr Ausfall darf den Newsletter nicht
        # verhindern, die Meldungen stehen ja bereits fest.
        logger.warning(f"Executive Summary konnte nicht erzeugt werden: {exc}")
        return {"text": "", "radar": []}


def validate_output_urls(html_text: str, collected: dict) -> list[str]:
    """Letzte Sicherung: extrahiert alle URLs aus dem fertigen Newsletter
    und prüft sie gegen die Liste tatsächlich abgerufener Quellen.

    Nach dem Umbau sollte hier nichts mehr auffallen - jede Meldung mit
    unbekannter URL wird schon in meldungen.pruefe_meldung() verworfen.
    Der Check bleibt trotzdem: er kostet nichts und deckt auf, falls das
    Rendering doch einmal eine fremde URL einschleust."""
    known_urls = {
        e["url"] for entries in collected.values() for e in entries
    }
    found_urls = set(re.findall(r'href=[\'"]?(https?://[^\'" >]+)', html_text))
    unknown = sorted(u for u in found_urls if u not in known_urls)
    if unknown:
        logger.warning(f"{len(unknown)} unbekannte URL(s) im fertigen Newsletter: {unknown}")
    return unknown


# ---------------------------------------------------------------------------
# Mailversand: Gmail API (OAuth2-Refresh-Token)
# ---------------------------------------------------------------------------
# Absender ist das Konto, zu dem GMAIL_TOKEN_JSON gehört
# (vdnewsletteranalyse@gmail.com). userId="me" bezieht sich immer auf
# genau dieses Konto - ein abweichender From-Header würde von Gmail
# ohnehin überschrieben, deshalb setzen wir keinen.


def send_email_gmail(to: str, subject: str, html_body: str) -> None:
    """Sendet das Briefing als HTML-Mail über die Gmail API.

    Wirft bei jedem Fehler eine Exception - kein stilles Scheitern, damit
    der aufrufende Code (run()) den Report trotzdem als Datei sichert und
    der Actions-Lauf sichtbar rot wird."""
    service = _get_gmail_service()

    message = MIMEText(html_body, "html", "utf-8")
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------

def run(week_label: str, subject: str, testlauf: bool = False):
    """Erzeugt das Briefing und versendet es.

    Mit `testlauf=True` wird nur eine einzige Kategorie abgerufen, die
    Web-Suche und das Newsletter-Postfach bleiben außen vor, und es wird
    WEDER versendet NOCH nach OneDrive hochgeladen - das Ergebnis landet
    nur in output/. Gedacht zum Prüfen von Änderungen: ein vollständiger
    Lauf dauert eine halbe Stunde, was beim Entwickeln unbrauchbar ist.
    Welche Kategorie geprüft wird, steuert TEST_KATEGORIE."""
    recipient = os.environ.get("REPORT_RECIPIENT_EMAIL") if testlauf \
        else os.environ["REPORT_RECIPIENT_EMAIL"]

    # --- Quellen -----------------------------------------------------------
    if testlauf:
        kategorie = os.environ.get("TEST_KATEGORIE", "Urteile")
        if kategorie not in SOURCES:
            raise SystemExit(
                f"TEST_KATEGORIE={kategorie!r} gibt es nicht. "
                f"Möglich: {', '.join(SOURCES)}"
            )
        logger.info(f"TESTLAUF - nur Kategorie {kategorie!r}, kein Versand.")
        collected = {kategorie: []}
        for name, url, fmt, hinweis in SOURCES[kategorie]:
            ergebnis = fetch_and_extract(url, mit_links=True)
            logger.info(f"[{kategorie}] {name}: "
                        f"{'✅' if ergebnis['status'] == 'ok' else '❌ ' + str(ergebnis['error'])}")
            collected[kategorie].append({
                "name": name, "url": url, "format": fmt,
                "access_hint": hinweis, "ist_detailseite": False, **ergebnis,
            })
        folge_detailseiten(collected)
        state = hot_topics.lade_state()
        hot_topics.aktualisiere_aus_quellen(state, collected)
    else:
        logger.info("Rufe alle HR-Quellen ab...")
        collected = collect_all_sources()

        logger.info("Lade verlinkte Einzelmeldungen nach...")
        folge_detailseiten(collected)

        # Themenzustand VOR der Web-Suche laden, damit gezielt nach den
        # laufenden Dauerthemen gesucht werden kann.
        state = hot_topics.lade_state()
        hot_topics.aktualisiere_aus_quellen(state, collected)
        laufende_themen = hot_topics.aktive_themen(state)

        logger.info("Suche zusätzlich per Web-Suche nach aktuellen Fachbeiträgen...")
        collect_search_based_sources(collected, laufende_themen)

        newsletter_sources = fetch_hr_newsletter_sources()
        if newsletter_sources:
            collected["Newsletter-Auswertung"] = newsletter_sources

    # Zweiter Durchgang: die neu hinzugekommenen Quellen können Stichtage
    # enthalten, die den Themenzustand präzisieren.
    if not testlauf:
        hot_topics.aktualisiere_aus_quellen(state, collected)
        hot_topics.aufraeumen(state)
    radar_themen = hot_topics.aktive_themen(state)
    logger.info(
        f"Themenradar: {len(radar_themen)} laufende(s) Thema/Themen - "
        f"{[t['anzeige'] for t in radar_themen]}"
    )

    ok_count = sum(1 for entries in collected.values() for e in entries if e["status"] == "ok")
    error_count = sum(1 for entries in collected.values() for e in entries if e["status"] != "ok")
    logger.info(f"{ok_count} Quellen erfolgreich abgerufen, {error_count} fehlgeschlagen.")

    output_dir = os.environ.get("OUTPUT_DIR", "output")
    os.makedirs(output_dir, exist_ok=True)
    safe_label = week_label.replace(" ", "_").replace("/", "-")
    output_path = os.path.join(output_dir, f"HR-Briefing_{safe_label}.html")
    now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    today_str = datetime.date.today().strftime("%d.%m.%Y")

    # --- Inhalt ------------------------------------------------------------
    try:
        if ok_count == 0:
            raise ValueError("Keine einzige Quelle erfolgreich abgerufen - Abbruch.")

        meldungen_je_kategorie, beanstandungen = meldungen_modul.extrahiere_alle(
            collected, BETRACHTUNGSZEITRAUM, call_openrouter
        )
        alle_meldungen = [
            m for k in KATEGORIE_REIHENFOLGE
            for m in meldungen_je_kategorie.get(k, [])
        ]
        if not alle_meldungen:
            raise ValueError(
                "Kein einziger Beitrag hat die Grounding-Prüfung bestanden - "
                "es gibt nichts zu versenden."
            )
        logger.info(f"{len(alle_meldungen)} Meldung(en) insgesamt im Newsletter.")

        summary = generiere_executive_summary(alle_meldungen, week_label)
        summary["ausblick"] = render.baue_ausblick_punkte(radar_themen, alle_meldungen)

        body_html = render.baue_body(
            meldungen_je_kategorie=meldungen_je_kategorie,
            kategorie_reihenfolge=KATEGORIE_REIHENFOLGE,
            executive_summary=summary,
            radar_themen=radar_themen,
            week_label=week_label,
            today_str=today_str,
            schema=meldungen_modul.KATEGORIE_SCHEMA,
        )
    except Exception as exc:
        # Selbst bei einem Totalausfall (z.B. ungültiger API-Key, alle
        # Modelle fehlgeschlagen) soll NICHT die gesamte Recherche
        # spurlos verloren gehen - mindestens eine Diagnose-Datei mit
        # den erfolgreich abgerufenen Quellen wird gespeichert, damit
        # ein Actions-Artifact entsteht statt gar nichts.
        source_list = "".join(
            f"<li>{'✅' if e['status'] == 'ok' else '❌'} {e['name']} - {e['url']}</li>"
            for entries in collected.values() for e in entries
        )
        error_html = f"""<!DOCTYPE html>
<html lang="de">
<head><meta charset="utf-8"><title>HR-Briefing FEHLGESCHLAGEN: {week_label}</title></head>
<body style="font-family:Arial,sans-serif;max-width:800px;margin:40px auto;padding:0 20px;">
<div style="background:#f8d7da;border:1px solid #dc3545;padding:16px;">
<h1 style="color:#721c24;">HR-Briefing konnte nicht erstellt werden</h1>
<p><strong>Fehler:</strong> {type(exc).__name__}: {exc}</p>
</div>
<h2>Trotzdem erfolgreich abgerufene Quellen ({ok_count} von {ok_count + error_count}):</h2>
<ul>{source_list}</ul>
<div style="margin-top:40px;font-size:12px;color:#888;">
Automatisch erstellt am {now_str} &middot; Alle Angaben ohne Gewähr
</div>
</body>
</html>"""
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(error_html)
        logger.info(f"Fehler-Diagnose gespeichert unter: {output_path}")
        # Der Themenzustand wird trotzdem gesichert: die Quellen wurden ja
        # abgerufen, und die darin gefundenen Stichtage sollen für den
        # nächsten Lauf nicht verloren gehen.
        if not testlauf:
            hot_topics.speichere_state(state)
        raise  # Job soll weiterhin als fehlgeschlagen markiert werden

    # Im Testlauf den Zustand NICHT schreiben - ein Lauf über eine
    # einzige Kategorie würde sonst das Gedächtnis des Themenradars mit
    # einem Ausschnitt überschreiben.
    if not testlauf:
        hot_topics.speichere_state(state)

    # --- Prüfen und ausliefern --------------------------------------------
    unknown_urls = validate_output_urls(body_html, collected)

    for hinweis in beanstandungen:
        logger.warning(f"Grounding: {hinweis}")

    # Der frühere Warnbanner stand über JEDEM Newsletter und meldete
    # überwiegend Fehlalarme (z.B. "LTO", das sehr wohl konfiguriert ist).
    # Jetzt wird beanstandeter Inhalt gar nicht erst aufgenommen, und der
    # Banner erscheint nur noch, wenn eine wirklich fremde URL im
    # fertigen Dokument steht.
    warning_banner = ""
    if unknown_urls:
        warning_banner = (
            "<div style='background:#fff3cd;border:1px solid #ffc107;"
            "padding:12px;margin-bottom:16px;'>"
            "⚠️ Im fertigen Newsletter stehen URLs, die nicht aus dem Abruf "
            "stammen - bitte vor der Weitergabe prüfen:"
            f"<ul>{''.join(f'<li>{u}</li>' for u in unknown_urls)}</ul></div>"
        )

    pruefnotiz = ""
    if beanstandungen:
        eintraege = "".join(f"<li>{b}</li>" for b in beanstandungen[:40])
        pruefnotiz = (
            f"<details><summary>{len(beanstandungen)} Angabe(n) von der "
            f"Grounding-Prüfung entfernt oder beanstandet</summary>"
            f"<ul>{eintraege}</ul></details>"
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

    full_html = f"""<!DOCTYPE html>
<html lang="de">
<head><meta charset="utf-8"><title>HR-Wissen Weekly: {week_label}</title></head>
<body style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 720px; margin: 0 auto; color: #222; line-height: 1.6; background: #ffffff;">
{warning_banner}
{body_html}
{pruefnotiz}
{failed_sources_note}
<hr style="border: none; border-top: 1px solid #e5e9f0; margin: 28px 0 14px;">
<p style="font-size: 12px; color: #99a3b0; margin: 0 0 8px;">Recherchiert mit KI-Unterstützung &nbsp;|&nbsp; Alle Angaben ohne Gewähr &nbsp;|&nbsp; Automatisch erstellt am {now_str} &middot; {ok_count} Quellen abgerufen, {error_count} fehlgeschlagen</p>
<p style="font-size: 12px; color: #99a3b0; margin: 0;">Hinweis: Diese Zusammenstellung dient der allgemeinen Information und ersetzt keine rechtliche, steuerliche oder sozialversicherungsrechtliche Beratung. Bei konkreten Einzelfragen bitte Fachberatung einbeziehen.</p>
</body>
</html>"""

    # Report speichern - unabhängig davon, ob der Mailversand danach
    # klappt. So geht bei einem Versand-Fehler (z.B. fehlendes/falsches
    # Secret) nicht der ganze Rechercheinhalt verloren.
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_html)
    logger.info(f"Report gespeichert unter: {output_path}")

    if testlauf:
        logger.info(
            f"TESTLAUF beendet - {len(alle_meldungen)} Meldung(en), "
            "kein Versand, kein OneDrive-Upload, Themenradar unverändert."
        )
        return

    try:
        onedrive_upload.upload_to_onedrive(
            full_html, f"HR-Briefing_{safe_label}.html"
        )
    except Exception as exc:
        # Nicht fatal - der Report liegt ja bereits lokal (siehe oben)
        # und als Actions-Artifact vor.
        logger.warning(f"OneDrive-Upload fehlgeschlagen: {exc}")

    try:
        send_email_gmail(recipient, subject, full_html)
        logger.info("Mail erfolgreich versendet (Gmail API).")
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
    parser.add_argument(
        "--mode", choices=["weekly", "test"], default="weekly",
        help="weekly = vollständiger Lauf mit Versand; "
             "test = eine Kategorie, kein Versand, kein OneDrive, "
             "Themenradar bleibt unverändert (Kategorie über TEST_KATEGORIE)",
    )
    args = parser.parse_args()

    today = datetime.date.today()
    week = today.isocalendar()[1]
    run(
        week_label=f"KW {week} / {today.strftime('%B %Y')}",
        subject=f"HR-Wissen Weekly – KW {week} / {today.strftime('%B %Y')}",
        testlauf=(args.mode == "test"),
    )
