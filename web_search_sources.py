"""
web_search_sources.py – Echte, dynamische Web-Suche pro Kategorie über
die Firecrawl-Such-API, um die im Vergleich zum Original-PhiBox-Agenten
fehlende Tiefe/Breite auszugleichen.

Hintergrund: Die feste SOURCES-Liste in hr_briefing.py crawlt nur die
rohen Behörden-Pressemitteilungs-Seiten selbst (BAG, BFH, BSG, BMAS,
...). Das Original (PhiBox-Agent) konnte dagegen frei im Web suchen und
fand dadurch spezialisierte Arbeitsrechts-/HR-Fachblogs (z.B. kliemt.blog,
personalwirtschaft.de, cms.law), die Urteile/Gesetzesvorhaben bereits
HR-praxisnah eingeordnet hatten - das war die Hauptursache für den
Qualitätsunterschied. Dieses Modul schließt genau diese Lücke.

Architektur-Prinzip (WICHTIG): Firecrawl liefert hier NUR die
Treffer-URLs (kein automatisches Scraping über Firecrawl - das würde
mehr Credits kosten). Der eigentliche Seiteninhalt wird weiterhin über
die bestehende, bewährte fetch_and_extract()-Funktion aus hr_briefing.py
geholt (gleicher Hard-Timeout, gleiche BeautifulSoup-Bereinigung,
gleicher Grounding-Mechanismus wie bei allen anderen Quellen - keine
Extra-Sonderbehandlung nötig).

Free-Tier-Hinweis: Firecrawl bietet 1.000 Credits/Monat kostenlos, KEINE
Kreditkarte nötig, läuft monatlich neu auf (Stand August 2026 - wie bei
allen Drittanbieter-Konditionen: vor Produktivbetrieb auf firecrawl.dev/
pricing nochmal verifizieren, das ändert sich erfahrungsgemäß schnell).
Eine Suche mit limit=5 kostet ca. 1 Credit; bei 6 Kategorien x 2 Queries
x wöchentlichem Lauf sind das ca. 50-60 Credits/Monat - weit im
kostenlosen Rahmen.

━━━ Benötigte Umgebungsvariable ━━━
  FIRECRAWL_API_KEY – API-Key von firecrawl.dev (Signup ohne Kreditkarte)
"""

import os
import logging

import requests

logger = logging.getLogger(__name__)

FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"
TIMEOUT_SECONDS = 30
RESULTS_PER_QUERY = 5
MAX_URLS_PER_CATEGORY = 5  # Deckel, damit der Kontext nicht explodiert

# 2 Suchanfragen pro Kategorie - bewusst allgemein gehalten (kein
# includeDomains-Filter), damit die Suche wie beim Original frei im Web
# findet, was gerade aktuell/relevant ist, statt auf eine feste
# Domain-Liste beschränkt zu sein.
SEARCH_QUERIES = {
    "Gesetzesvorhaben": [
        "Referentenentwurf Arbeitsrecht 2026 BMAS",
        "neuer Gesetzentwurf Arbeitsrecht HR Personalwesen 2026",
    ],
    "BMF-Schreiben": [
        "neues BMF-Schreiben Lohnsteuer 2026",
        "BMF Schreiben Sozialversicherung Lohnsteuer aktuell 2026",
    ],
    "Urteile": [
        "BAG Urteil Arbeitsrecht 2026 HR-relevant",
        "BFH Urteil Lohnsteuer 2026",
        "BSG Urteil Sozialversicherung 2026",
    ],
    "Verordnungen": [
        "EU Verordnung Arbeitsrecht HR 2026",
        "EU AI Act HR Pflichten Transparenz 2026",
    ],
    "Gesetzgebungsverfahren": [
        "Jahressteuergesetz 2026 Stand Gesetzgebungsverfahren",
        "Reformpaket Arbeitsrecht Koalitionsausschuss 2026",
    ],
    "HR-Digitalisierung": [
        "HR Digitalisierung Personalwesen Trends 2026",
        "digitale Personalakte Entgeltakte 2026",
    ],
}


def _search_firecrawl(query: str, limit: int = RESULTS_PER_QUERY) -> list[dict]:
    """Führt eine einzelne Firecrawl-Web-Suche aus. Gibt bei jedem Fehler
    (fehlender/ungültiger Key, Rate-Limit, Netzwerkproblem) eine leere
    Liste zurück statt einer Exception - eine fehlgeschlagene Suche soll
    das Briefing nicht zum Absturz bringen, nur diese eine Anfrage liefert
    dann eben keine zusätzlichen Treffer."""
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return []

    try:
        resp = requests.post(
            FIRECRAWL_SEARCH_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "limit": limit,
                "sources": ["web"],
                "tbs": "qdr:m",  # nur Ergebnisse aus dem letzten Monat
                "lang": "de",
                "country": "DE",
            },
            timeout=TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            logger.warning(
                f"Firecrawl-Suche '{query}' fehlgeschlagen: HTTP "
                f"{resp.status_code} - {resp.text[:300]}"
            )
            return []
        data = resp.json()
        # Erwartetes Format: {"success": true, "data": {"web": [{...}]}}
        # oder je nach API-Version {"data": [...]} - beides abfangen.
        web_results = data.get("data", {})
        if isinstance(web_results, dict):
            web_results = web_results.get("web", [])
        return web_results or []
    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.warning(f"Firecrawl-Suche '{query}' fehlgeschlagen: {exc}")
        return []


def find_urls_per_category() -> dict[str, list[tuple[str, str]]]:
    """Führt für jede Kategorie die konfigurierten Suchanfragen aus und
    gibt pro Kategorie eine Liste von (Titel, URL)-Tupeln zurück -
    dedupliziert und auf MAX_URLS_PER_CATEGORY gedeckelt. Liefert ein
    leeres Dict, wenn kein FIRECRAWL_API_KEY gesetzt ist (Feature dann
    einfach inaktiv, kein Fehler)."""
    if not os.environ.get("FIRECRAWL_API_KEY"):
        logger.info("FIRECRAWL_API_KEY nicht gesetzt - Web-Suche übersprungen.")
        return {}

    found: dict[str, list[tuple[str, str]]] = {}
    for category, queries in SEARCH_QUERIES.items():
        seen_urls = set()
        category_results = []
        for query in queries:
            for item in _search_firecrawl(query):
                url = item.get("url", "")
                title = item.get("title", url)
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                category_results.append((title, url))
                if len(category_results) >= MAX_URLS_PER_CATEGORY:
                    break
            if len(category_results) >= MAX_URLS_PER_CATEGORY:
                break
        logger.info(f"Web-Suche [{category}]: {len(category_results)} URL(s) gefunden.")
        found[category] = category_results
    return found
