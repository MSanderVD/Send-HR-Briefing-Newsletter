"""
meldungen.py – Strukturierte Extraktion der Einzelmeldungen, je Kategorie
ein eigener LLM-Aufruf, mit feldweiser Grounding-Prüfung.

━━━ Warum dieser Umbau nötig war ━━━
Vorher ging EIN einziger Prompt an das Modell: sämtliche Quellen aller
Kategorien plus das komplette HTML-Gerüst, und das Modell sollte in einem
Zug recherchieren, auswählen, formulieren und HTML bauen. Das hatte drei
Folgen, die alle im fertigen Newsletter sichtbar waren:

1. KONTEXT-ÜBERLAUF. Ein Lauf sammelt gut 40 Quellen; bei 3.000 Zeichen
   je Quelle sind das ~120.000 Zeichen ≈ 38.000 Token. Zugelassen waren
   aber Modelle ab 8.000 Token Kontext. Ein solches Modell sieht rund ein
   Fünftel - den Anfang. Deshalb stand bei der zuletzt einsortierten
   Kategorie (HR-Digitalisierung) Woche für Woche "Keine belastbare neue
   Entwicklung": ihre Quellen hat das Modell nie zu Gesicht bekommen.
2. VERFLACHUNG. Der Original-PhiBox-Agent verlangte je Kategorie eigene
   Pflichtfelder - bei Gesetzgebungsverfahren etwa "Aktueller Stand",
   "Nächster Schritt", "Geplantes Inkrafttreten", "Verzögerung/Blockade"
   als vier getrennte Zeilen. Bei der Portierung blieb davon ein
   generisches "Kurz erklärt: 2-4 Sätze" für alle sechs Kategorien übrig.
   Genau dieser Informationsgehalt hat in der Redaktion gefehlt.
3. FORMATFEHLER. Weil das Modell das HTML selbst baute, landeten
   Anleitungssätze im sichtbaren Text ("Falls nichts Belastbares
   vorliegt: ...") und die zweite Aufzählung wiederholte wörtlich die
   Handlungsbedarf-Spalte der Tabelle.

Hier läuft es deshalb umgekehrt: je Kategorie ein kleiner Aufruf mit nur
den Quellen DIESER Kategorie, Rückgabe als JSON mit festen Feldern. Das
HTML baut danach render.py in Python - Formatfehler sind damit
strukturell ausgeschlossen, und jedes Feld lässt sich einzeln gegen den
Quelltext prüfen, statt nur einen Warnbanner über den ganzen Report zu
hängen.
"""

import re
import json
import logging
import unicodedata

logger = logging.getLogger(__name__)

# Zeichen je Quelle im Kategorie-Prompt. Deutlich großzügiger als vorher
# möglich, weil jetzt nur noch eine Kategorie gleichzeitig im Kontext ist.
ZEICHEN_JE_QUELLE = 5000

# Obergrenze für den gesamten Kontext EINER Kategorie. Hält den Prompt
# auch bei einer Kategorie mit vielen Treffern im Rahmen dessen, was ein
# kostenloses Modell zuverlässig verarbeitet.
ZEICHEN_JE_KATEGORIE = 45000


# ---------------------------------------------------------------------------
# Kategorie-Schemata - wiederhergestellt aus dem Original-PhiBox-Agenten
# ---------------------------------------------------------------------------
# "fakten" sind die kategoriespezifischen Zusatzfelder, die als eigene
# beschriftete Zeilen gerendert werden. Genau sie machen den Unterschied
# zwischen "hier ist eine Meldung" und "hier steht, was Sie wissen
# müssen". Leere Felder werden beim Rendern weggelassen - ein Feld ohne
# Beleg im Kontext soll leer bleiben, nicht geraten werden.

KATEGORIE_SCHEMA = {
    "Gesetzesvorhaben": {
        "auftrag": (
            "Gesetzentwürfe, Referentenentwürfe und Vorhaben der Bundesregierung "
            "oder der Ministerien mit Bezug zu HR, Arbeitsrecht, Lohnsteuer oder "
            "Sozialversicherung."
        ),
        "fakten": [
            ("verfahrensstand", "Aktueller Stand"),
            ("geplantes_inkrafttreten", "Geplantes Inkrafttreten"),
        ],
    },
    "BMF-Schreiben": {
        "auftrag": (
            "Neue Schreiben des Bundesfinanzministeriums mit Auswirkung auf "
            "Lohnbuchhaltung, Payroll oder Arbeitgeberpflichten."
        ),
        "fakten": [
            ("schreiben_datum", "Datum des Schreibens"),
            ("aktenzeichen", "Aktenzeichen"),
            ("auswirkung_payroll", "Auswirkung auf die Lohnabrechnung"),
        ],
    },
    "Urteile": {
        "auftrag": (
            "Entscheidungen von BAG, BFH, BSG, EuGH oder Landesarbeitsgerichten "
            "mit Praxisfolgen für Arbeitgeber."
        ),
        "fakten": [
            ("gericht", "Gericht"),
            ("aktenzeichen", "Aktenzeichen"),
            ("entscheidungsdatum", "Entscheidungsdatum"),
            ("kernaussage", "Kernaussage des Gerichts"),
        ],
    },
    "Verordnungen": {
        "auftrag": (
            "Neue oder geänderte Verordnungen aus Deutschland und der EU mit "
            "HR-, Arbeitsrechts-, Steuer- oder Sozialversicherungsbezug."
        ),
        "fakten": [
            ("anwendungsbereich", "Anwendungsbereich"),
            ("geltung_ab", "Geltung ab"),
            ("arbeitgeberpflichten", "Betroffene Arbeitgeberpflichten"),
        ],
    },
    "Gesetzgebungsverfahren": {
        "auftrag": (
            "Der Stand laufender Gesetzgebungsverfahren mit HR-Relevanz - "
            "einschließlich Verzögerungen und Blockaden, wenn sie belegt sind."
        ),
        "fakten": [
            ("verfahrensstand", "Aktueller Stand"),
            ("naechster_schritt", "Nächster Schritt"),
            ("geplantes_inkrafttreten", "Geplantes Inkrafttreten"),
            ("verzoegerung", "Verzögerung / Blockade"),
        ],
    },
    "HR-Digitalisierung": {
        "auftrag": (
            "Entwicklungen zur Digitalisierung im HR-Bereich, insbesondere neue "
            "Pflichten und Compliance-Anforderungen (KI im Personalwesen, "
            "digitale Personalakte, Arbeitszeiterfassung, Datenschutz)."
        ),
        "fakten": [
            ("was_ist_neu", "Was ist neu"),
            ("ab_wann", "Ab wann gilt es"),
            ("was_aendert_sich", "Was ändert sich konkret"),
        ],
    },
    # Auffangkategorie für die Newsletter aus dem Gmail-Postfach; sie hat
    # im Original kein eigenes Schema, bekommt also nur die Grundfelder.
    "Newsletter-Auswertung": {
        "auftrag": (
            "HR-relevante Meldungen aus den eingegangenen Fachnewslettern, "
            "sofern sie nicht schon in einer anderen Kategorie stehen."
        ),
        "fakten": [],
    },
}

def _schema_fuer(kategorie: str) -> dict:
    return KATEGORIE_SCHEMA.get(kategorie, {"auftrag": kategorie, "fakten": []})


# ---------------------------------------------------------------------------
# Prompt-Bau
# ---------------------------------------------------------------------------

def _quellenblock(entries: list[dict]) -> tuple[str, dict]:
    """Baut den Kontext EINER Kategorie, mit Budgetgrenze. Detailseiten
    stehen zuerst, weil sie den eigentlichen Inhalt tragen - eine
    Übersichtsseite liefert nur Überschriften.

    Jede Quelle bekommt eine NUMMER, und das Modell gibt später nur diese
    Nummer zurück statt der URL. Grund: im ersten Produktivlauf liessen
    die freien Modelle das URL-Feld reihenweise leer - in den Kategorien
    "Urteile" und "Gesetzesvorhaben" fielen dadurch ALLE Meldungen durch
    die Grounding-Prüfung. Eine 120 Zeichen lange URL fehlerfrei
    abzuschreiben ist für ein kleines Modell eine andere Aufgabe als eine
    einstellige Zahl zu nennen.

    Nebeneffekt, der das Grounding verbessert: Eine URL kann so gar nicht
    mehr erfunden werden, weil sie in Python aus der Nummer aufgelöst
    wird und nie durch das Modell läuft.

    Rückgabe: (Kontexttext, {Nummer: Quelleneintrag})."""
    sortiert = sorted(
        (e for e in entries if e.get("status") == "ok" and e.get("text")),
        key=lambda e: 0 if e.get("ist_detailseite") else 1,
    )
    bloecke, verbraucht, register = [], 0, {}
    for nummer, e in enumerate(sortiert, start=1):
        text = e["text"][:ZEICHEN_JE_QUELLE]
        block = (
            f"--- QUELLE {nummer}: {e['name']} | "
            f"Format: {e.get('format', '?')} ---\n{text}\n"
        )
        if verbraucht + len(block) > ZEICHEN_JE_KATEGORIE:
            break
        bloecke.append(block)
        register[nummer] = e
        verbraucht += len(block)
    return "\n".join(bloecke), register


def _feldliste(schema: dict) -> str:
    zeilen = [
        '  "ueberschrift":    "Kurze, aussagekräftige Überschrift der Meldung",',
        '  "kurz_erklaert":   "2-4 vollständige Sätze: worum geht es konkret",',
        '  "relevanz":        "Hoch | Mittel | Niedrig",',
        '  "hr_relevanz":     "Ein Satz: welche Folge hat das konkret für HR/Payroll",',
        '  "pruefpunkt":      "Ein Satz: was genau sollte im Unternehmen geprüft werden - ANDERE Aussage als hr_relevanz und handlungsbedarf",',
        '  "handlungsbedarf": "Max. 12 Wörter für die Übersichtstabelle, was HR jetzt tun sollte",',
        '  "quelle_nr":       <Zahl>,  // PFLICHT: Nummer der Quelle, aus der die Meldung stammt (die Zahl hinter "QUELLE" im Kontext)',
        '  "quelle_name":     "Institution oder Medium, z.B. BAG, BMF, Haufe",',
    ]
    for feld, beschriftung in schema["fakten"]:
        zeilen.append(f'  "{feld}":{" " * max(1, 17 - len(feld))}"{beschriftung} - leer lassen, wenn der Kontext das nicht hergibt",')
    return "\n".join(zeilen)


def baue_prompt(kategorie: str, entries: list[dict],
                zeitraum: str) -> tuple[str, dict] | None:
    schema = _schema_fuer(kategorie)
    kontext, register = _quellenblock(entries)
    if not kontext.strip():
        return None

    return f"""Du wertest abgerufene Webseiten für ein deutsches HR-Fachbriefing aus.
Kategorie: {kategorie}
Gesucht: {schema['auftrag']}
Betrachtungszeitraum: {zeitraum}

━━━ PFLICHTREGELN ━━━
1. GROUNDING: Verwende ausschließlich Informationen, die im Kontext unten
   stehen. Erfinde keine Aktenzeichen, Daten, Gerichte, Zahlen oder Links.
   Gibt der Kontext ein Feld nicht her, schreibe "" - niemals raten,
   niemals aus eigenem Wissen ergänzen.
2. VOLLSTÄNDIGKEIT: Schöpfe den Kontext aus. Wenn dort fünf belegte
   Meldungen stehen, gib fünf zurück - nicht eine. Übernimm konkrete
   Details, die im Kontext stehen: Beträge, Fristen, Prozentsätze,
   Paragraphen, Aktenzeichen. Genau diese Details sind der Zweck des
   Briefings; eine Meldung, die nur die Überschrift umformuliert, ist
   wertlos.
3. KEINE DOPPLUNG: "hr_relevanz", "pruefpunkt" und "handlungsbedarf"
   müssen drei unterschiedliche Aussagen sein. "hr_relevanz" erklärt die
   FOLGE, "pruefpunkt" nennt, was im Unternehmen zu PRÜFEN ist,
   "handlungsbedarf" ist das Stichwort für die Übersichtstabelle.
   Schreibe niemals zweimal denselben Satz.
4. SPRACHE: Deutsch, vollständige Sätze, keine abgebrochenen Sätze, keine
   eckigen Klammern, kein HTML, keine Markdown-Formatierung im Text.
5. QUELLE: "quelle_nr" ist Pflicht und muss die Zahl sein, die im Kontext
   hinter "QUELLE" steht - also die Quelle, aus der die Meldung stammt.
   Schreibe dort NUR die Zahl, keine URL und keinen Text. Eine Meldung
   ohne gültige "quelle_nr" wird verworfen.
6. AUSGABE: NUR ein JSON-Array, ohne Code-Fence, ohne Vor- oder Nachwort.

Jedes Element hat genau diese Felder:
{{
{_feldliste(schema)}
}}

Sind im Kontext keine belastbaren Meldungen dieser Kategorie enthalten,
gib ein leeres Array zurück: []

━━━ KONTEXT (einzige zulässige Faktenquelle) ━━━
{kontext}
""", register


# ---------------------------------------------------------------------------
# Antwort einlesen
# ---------------------------------------------------------------------------

def pruefe_json_antwort(text: str) -> str | None:
    """Qualitätsprüfung für die JSON-Antwort. Ersetzt looks_garbled() für
    diesen Aufruftyp - dort wird auf <h2> und 'Quelle:' geprüft, was für
    JSON sinnlos ist."""
    if not text or not text.strip():
        return "Leere Antwort"
    fremde_schrift = re.findall(
        r'[฀-๿一-鿿぀-ヿ가-힯'
        r'ऀ-ॿ؀-ۿ֐-׿Ѐ-ӿ]', text
    )
    if fremde_schrift:
        return f"Unerwartete Schriftzeichen ({''.join(fremde_schrift[:5])})"
    if re.search(r'<\s*[A-Z_]{3,}\s*>', text):
        return "Durchgesickerte Platzhalter-Token"
    if parse_json_array(text) is None:
        return f"Kein auswertbares JSON-Array (Anfang: {text[:120]!r})"
    return None


def parse_json_array(text: str) -> list | None:
    """Liest das JSON-Array aus einer Modellantwort. Kleine Modelle
    verpacken es gern in eine Code-Fence oder schreiben einen Satz davor;
    beides wird toleriert, statt den Lauf daran scheitern zu lassen."""
    if not text:
        return None

    kandidaten = []
    fence = re.search(r'```(?:json)?\s*(.+?)```', text, re.DOTALL)
    if fence:
        kandidaten.append(fence.group(1).strip())
    kandidaten.append(text.strip())

    start = text.find("[")
    ende = text.rfind("]")
    if start != -1 and ende > start:
        kandidaten.append(text[start:ende + 1])

    for roh in kandidaten:
        try:
            daten = json.loads(roh)
        except (json.JSONDecodeError, ValueError):
            # Häufigster Defekt kleiner Modelle: ein Komma vor der
            # schließenden Klammer. Einmal reparieren, dann aufgeben.
            try:
                daten = json.loads(re.sub(r',\s*([\]}])', r'\1', roh))
            except (json.JSONDecodeError, ValueError):
                continue
        if isinstance(daten, list):
            return daten
        if isinstance(daten, dict):
            for schluessel in ("meldungen", "items", "data", "results"):
                if isinstance(daten.get(schluessel), list):
                    return daten[schluessel]
            return [daten]
    return None


# ---------------------------------------------------------------------------
# Feldweise Grounding-Prüfung
# ---------------------------------------------------------------------------

def _normal(s: str) -> str:
    """Vergleichsform: Kleinbuchstaben, nur Buchstaben und Ziffern. Damit
    matcht '5 AZR 37/25' auch gegen '5 AZR 37/25' mit anderem Leerraum
    oder geschütztem Leerzeichen."""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _datumsvarianten(wert: str) -> list[str]:
    """Erzeugt die Schreibweisen, in denen dasselbe Datum im Quelltext
    stehen kann. '11.06.2026' steht dort oft als '11. Juni 2026'."""
    varianten = {_normal(wert)}
    m = re.search(r'(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})', wert)
    if m:
        tag, monat, jahr = int(m.group(1)), int(m.group(2)), m.group(3)
        monate = ["januar", "februar", "märz", "april", "mai", "juni", "juli",
                  "august", "september", "oktober", "november", "dezember"]
        if 1 <= monat <= 12:
            varianten.add(_normal(f"{tag}. {monate[monat - 1]} {jahr}"))
            varianten.add(_normal(f"{tag:02d}.{monat:02d}.{jahr}"))
            varianten.add(_normal(f"{tag}.{monat}.{jahr}"))
    return [v for v in varianten if v]


# Felder, deren Inhalt wörtlich im Quelltext stehen MUSS. Das sind genau
# die, bei denen eine Erfindung teuer wäre: ein falsches Aktenzeichen
# oder ein falsches Datum macht die Meldung unbrauchbar und beschädigt
# das Vertrauen in den ganzen Newsletter.
HARTE_FELDER = ("aktenzeichen", "entscheidungsdatum", "schreiben_datum")


def pruefe_meldung(meldung: dict, quelltexte: str, quelltexte_norm: str,
                   bekannte_urls: set, register: dict) -> tuple[dict, list]:
    """Prüft eine einzelne Meldung gegen die abgerufenen Quelltexte.

    Es werden ZWEI Fassungen des Quelltexts gebraucht. Aktenzeichen und
    Daten werden gegen die normalisierte Fassung geprüft ('5 AZR 37/25'
    steht in der Quelle mit anderem Leerraum als in der Modellantwort);
    Beträge und Prozentwerte dagegen gegen den Rohtext, weil dort die
    Trennzeichen die Bedeutung tragen. Wird nur der gesuchte Wert
    normalisiert und der Heuhaufen nicht, findet der Vergleich nie etwas
    und die Prüfung wirft auch korrekte Angaben weg.

    Rückgabe: (bereinigte Meldung oder None, Liste der Beanstandungen).
    Anders als bisher wird nicht nur gewarnt: eine Meldung mit erfundener
    URL fliegt raus, ein nicht belegtes Aktenzeichen wird entfernt statt
    mitgedruckt. Der Newsletter soll ohne Warnbanner verschickt werden
    können - ein Banner, den ohnehin niemand nachprüft, ist kein
    Qualitätssicherungsmittel."""
    beanstandungen = []

    # Quelle bestimmen: bevorzugt über die Nummer, die das Modell nennt.
    # Eine so aufgelöste URL stammt garantiert aus dem Abruf, weil sie
    # gar nicht erst durch das Modell läuft.
    quelle = None
    roh_nr = meldung.get("quelle_nr")
    if roh_nr is not None:
        try:
            quelle = register.get(int(str(roh_nr).strip()))
        except (TypeError, ValueError):
            quelle = None

    if quelle is None:
        # Notweg für Modelle, die statt der Nummer doch eine URL liefern.
        url = (meldung.get("quelle_url") or "").strip()
        if url in bekannte_urls:
            quelle = {"url": url, "name": meldung.get("quelle_name") or ""}

    if quelle is None:
        return None, [
            "Meldung verworfen - keine gültige Quelle angegeben "
            f"(quelle_nr={roh_nr!r}): {meldung.get('ueberschrift') or '(ohne Titel)'!r}"
        ]

    url = quelle["url"]

    if not (meldung.get("ueberschrift") or "").strip():
        return None, ["Meldung verworfen - keine Überschrift"]
    if len((meldung.get("kurz_erklaert") or "").strip()) < 40:
        return None, [f"Meldung verworfen - Beschreibung zu dünn: {meldung.get('ueberschrift')!r}"]

    sauber = dict(meldung)
    sauber["quelle_url"] = url
    if not (sauber.get("quelle_name") or "").strip():
        sauber["quelle_name"] = (quelle.get("name") or "Quelle").split(" → ")[0]

    # Harte Felder: wörtlich belegt oder raus.
    for feld in HARTE_FELDER:
        wert = (sauber.get(feld) or "").strip()
        if not wert:
            continue
        if any(v and v in quelltexte_norm for v in _datumsvarianten(wert)):
            continue
        beanstandungen.append(
            f"{feld} {wert!r} steht in keiner abgerufenen Quelle - Feld entfernt "
            f"({sauber.get('ueberschrift')!r})"
        )
        sauber[feld] = ""

    # Prozent- und Geldbeträge im Fließtext: eine erfundene Zahl ist der
    # gefährlichste Halluzinationstyp, weil sie am glaubwürdigsten wirkt.
    for feld in ("kurz_erklaert", "hr_relevanz", "was_aendert_sich",
                 "auswirkung_payroll", "kernaussage"):
        wert = sauber.get(feld) or ""
        if not wert:
            continue
        for zahl in re.findall(r'\d+(?:[.,]\d+)?\s?(?:%|Prozent|Euro|EUR|€)', wert):
            ziffern = re.sub(r'[^\d.,]', '', zahl)
            if not ziffern:
                continue
            if (ziffern.replace(",", ".") in quelltexte
                    or ziffern.replace(".", ",") in quelltexte
                    or ziffern in quelltexte):
                continue
            beanstandungen.append(
                f"Zahl {zahl!r} in '{feld}' ist in keiner Quelle belegt "
                f"({sauber.get('ueberschrift')!r})"
            )

    # Relevanz auf die drei zulässigen Werte zwingen - im alten Lauf stand
    # hier gelegentlich ein ganzer Satz.
    relevanz = (sauber.get("relevanz") or "").strip().rstrip(".").capitalize()
    sauber["relevanz"] = relevanz if relevanz in ("Hoch", "Mittel", "Niedrig") else "Mittel"

    # Dopplung auflösen: im alten Newsletter war der zweite Aufzählungs-
    # punkt regelmäßig wörtlich die Handlungsbedarf-Spalte der Tabelle.
    # Gleiche Aussagen werden geleert, damit render.py sie nicht zweimal
    # ausgibt; welcher der beiden Punkte übrig bleibt, entscheidet die
    # Reihenfolge (die spezifischere Aussage gewinnt).
    handlung = _normal(sauber.get("handlungsbedarf", ""))
    for feld in ("hr_relevanz", "pruefpunkt"):
        if handlung and _normal(sauber.get(feld, "")) == handlung:
            sauber[feld] = ""
    if (_normal(sauber.get("pruefpunkt", ""))
            and _normal(sauber.get("pruefpunkt", "")) == _normal(sauber.get("hr_relevanz", ""))):
        sauber["pruefpunkt"] = ""

    for feld in list(sauber):
        if isinstance(sauber.get(feld), str):
            sauber[feld] = sauber[feld].strip()

    return sauber, beanstandungen


# ---------------------------------------------------------------------------
# Einstieg
# ---------------------------------------------------------------------------

def extrahiere_alle(collected: dict, zeitraum: str, llm_aufruf) -> tuple[dict, list]:
    """Läuft über alle Kategorien und gibt {Kategorie: [geprüfte Meldungen]}
    zurück. `llm_aufruf(prompt, validator)` wird von hr_briefing.py
    hereingereicht, damit dieses Modul nichts über OpenRouter wissen muss.

    Der Ausfall EINER Kategorie beendet den Lauf nicht - dann ist eben
    diese Kategorie leer, wie es die Quellen-Ausfallregel ohnehin
    vorsieht."""
    bekannte_urls = {
        e["url"] for entries in collected.values() for e in entries
    }
    quelltexte = unicodedata.normalize("NFKC", " ".join(
        e.get("text", "") for entries in collected.values() for e in entries
        if e.get("status") == "ok"
    ))
    # Zweite, auf Buchstaben und Ziffern reduzierte Fassung - gegen sie
    # werden Aktenzeichen und Daten geprüft (siehe pruefe_meldung).
    quelltexte_norm = _normal(quelltexte)

    ergebnis, alle_beanstandungen = {}, []

    for kategorie, entries in collected.items():
        gebaut = baue_prompt(kategorie, entries, zeitraum)
        if gebaut is None:
            logger.info(f"[{kategorie}] keine abrufbaren Quellen - übersprungen.")
            ergebnis[kategorie] = []
            continue
        prompt, register = gebaut

        logger.info(f"[{kategorie}] Extraktion, Prompt {len(prompt)} Zeichen.")
        try:
            antwort = llm_aufruf(prompt, pruefe_json_antwort)
        except Exception as exc:
            logger.warning(f"[{kategorie}] Extraktion fehlgeschlagen: {exc}")
            ergebnis[kategorie] = []
            alle_beanstandungen.append(f"Kategorie {kategorie}: {exc}")
            continue

        roh = parse_json_array(antwort) or []
        geprueft = []
        for eintrag in roh:
            if not isinstance(eintrag, dict):
                continue
            sauber, hinweise = pruefe_meldung(
                eintrag, quelltexte, quelltexte_norm, bekannte_urls, register
            )
            alle_beanstandungen.extend(hinweise)
            if sauber:
                sauber["kategorie"] = kategorie
                geprueft.append(sauber)

        logger.info(
            f"[{kategorie}] {len(roh)} Meldung(en) geliefert, "
            f"{len(geprueft)} nach Grounding-Prüfung übernommen."
        )
        ergebnis[kategorie] = geprueft

    return ergebnis, alle_beanstandungen
