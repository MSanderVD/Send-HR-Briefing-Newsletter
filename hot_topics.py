"""
hot_topics.py – Langzeit-Beobachtung von HR-Themen ("Hot Topics") über
mehrere Wochen hinweg, statt nur "was ist diese Woche passiert".

━━━ Warum es das gibt ━━━
Der Newsletter hat bisher ausschließlich einen Wochenausschnitt betrachtet:
abgerufen wurde, was gerade auf den Übersichtsseiten stand, und die
Web-Suche war hart auf "letzter Monat" begrenzt (tbs=qdr:m). Ein Thema wie
die Entgelttransparenzrichtlinie oder die Teilkrankschreibung ist damit
genau in der Woche sichtbar, in der eine Pressemitteilung erscheint - und
danach nie wieder, obwohl die eigentlich interessante Zeit erst beginnt:
der Weg bis zum Inkrafttreten und die ersten Monate danach.

Der Original-PhiBox-Agent hatte diese Regel noch ("Ältere Meldungen
(>4 Wochen) nur aufnehmen, wenn sie für einen aktuellen Verfahrensstand,
eine aktuelle Frist, eine bevorstehende Pflicht oder eine neue praktische
Relevanz weiterhin wichtig sind"), sie ist bei der Portierung nach Python
verloren gegangen. Dieses Modul stellt sie wieder her - und zwar
automatisch aus dem Verlauf, ohne handgepflegte Themenliste.

━━━ Wie ein Thema entsteht (vollautomatisch) ━━━
1. In JEDEM abgerufenen Quelltext wird nach Stichtagsformulierungen
   gesucht: "tritt am 1. Januar 2027 in Kraft", "gilt ab dem 01.07.2026",
   "bis zum 7. Juni 2026 umzusetzen", "Übergangsfrist endet am ...".
   Das sind die Sätze, an denen ein Thema einen Lebenszyklus bekommt.
2. Im Textfenster um diese Fundstelle wird der Themenbegriff gesucht -
   in der Verwaltungssprache fast immer ein langes Kompositum
   ("Entgelttransparenzrichtlinie", "Teilkrankschreibung",
   "Arbeitszeiterfassung") oder eine Gesetzesabkürzung ("EntgTranspG").
3. Begriff + Stichtag + Belegsatz wandern in den Zustand
   (state/hot_topics.json), der über Läufe hinweg im Repo fortgeschrieben
   wird. Taucht dasselbe Thema in weiteren Läufen auf, steigt sein
   Zähler - das ist die "automatische Ableitung aus dem Verlauf".
4. Ein Thema bleibt aktiv, solange `heute <= Stichtag + KARENZ_TAGE`.
   Danach fällt es raus. Themen ohne erkannten Stichtag brauchen
   mindestens MINDEST_LAEUFE Läufe und laufen OHNE_STICHTAG_TAGE nach
   der letzten Erwähnung aus.

━━━ Wichtig: das hier erfindet nichts ━━━
Jeder Stichtag stammt wörtlich aus einem abgerufenen Quelltext und wird
mit Belegsatz und Quell-URL gespeichert. Es gibt keine Datumsableitung
aus Modellwissen. Kann ein Datum nicht aus dem Text gelesen werden, hat
das Thema eben keinen Stichtag.
"""

import os
import re
import json
import logging
import datetime
import unicodedata

logger = logging.getLogger(__name__)

STATE_PATH = os.environ.get("HOT_TOPICS_STATE", "state/hot_topics.json")
STATE_VERSION = 2

# Version der ERKENNUNGSREGELN. Jedes Thema merkt sich, unter welchen
# Regeln es aufgenommen wurde; beim Laden fliegt alles heraus, was unter
# älteren Regeln entstanden ist.
#
# Nötig, weil eine verschärfte Regel sonst nur für neue Funde gilt. Ein
# einmal aufgenommener Fehltreffer bleibt bis zum Ablauf seines
# Stichtags im Radar - "Entsprechend" überlebte so die Adverb-Regel und
# "Beschlossen" die Satzanfang-Regel, obwohl beide bereits aktiv waren.
# Eine reine Nachprüfung über den Wortlaut reicht dafür nicht: Die
# Satzanfang-Regel ist positionsabhängig und lässt sich am gespeicherten
# Begriff gar nicht mehr nachvollziehen.
#
# Nach einer Regeländerung baut sich der Radar EINMAL neu auf: Themen,
# die noch in den Quellen stehen, sind im selben Lauf wieder da;
# Fehltreffer nicht. Diese Zahl bei jeder Änderung an den
# Erkennungsregeln erhöhen.
REGEL_VERSION = 3

# Wie lange ein Thema nach seinem Stichtag noch mitläuft. Der Wunsch aus
# der Redaktion war ausdrücklich "bis Inkrafttreten plus Karenzzeit" -
# denn die praktischen Fragen (Wie setzen wir das um? Was sagt die erste
# Rechtsprechung?) kommen erst NACH dem Inkrafttreten.
KARENZ_TAGE = int(os.environ.get("HOT_TOPIC_KARENZ_TAGE", "120"))

# Wie weit im Voraus ein Stichtag noch als "auf dem Radar" gilt. Alles
# darüber hinaus ist für ein Wochenbriefing zu weit weg (z.B. AI-Act-
# Fristen 2030) und würde die Sektion zumüllen.
VORLAUF_TAGE_MAX = int(os.environ.get("HOT_TOPIC_VORLAUF_TAGE", "550"))

# Themen OHNE erkannten Stichtag brauchen mehr Belege, weil bei ihnen die
# Fehlalarmgefahr höher ist.
MINDEST_LAEUFE = 2
OHNE_STICHTAG_TAGE = 42

# Wie viele Themen maximal in die Radar-Sektion kommen.
MAX_AKTIVE_THEMEN = int(os.environ.get("HOT_TOPIC_MAX", "6"))

MONATE = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
    "november": 11, "dezember": 12,
}

# Ein Datum in den Schreibweisen, die in Behördentexten wirklich vorkommen:
# "1. Januar 2027", "01.01.2027", "1.1.2027". Bewusst OHNE das nackte
# "Januar 2027" - das ist zu unscharf und produziert Fehltreffer.
_TAG = r'(\d{1,2})'
_JAHR = r'(20\d{2})'
DATUM_REGEX = (
    r'(?:'
    rf'{_TAG}\.\s*(' + "|".join(MONATE) + rf')\s+{_JAHR}'
    r'|'
    rf'{_TAG}\.\s*(\d{{1,2}})\.\s*{_JAHR}'
    r')'
)

# Formulierungen, die einem Datum einen Lebenszyklus geben. Die Art des
# Stichtags wird mitgeführt, weil sie im Newsletter etwas ganz anderes
# bedeutet: "Umsetzungsfrist" richtet sich an den Gesetzgeber, "gilt ab"
# an den Arbeitgeber.
#
# ACHTUNG beim Füller zwischen Verb und Wendung: dort steht praktisch
# immer das Datum selbst, und deutsche Datumsangaben enthalten Punkte
# ("tritt am 1. Januar 2027 in Kraft"). Ein Füller `[^.]` kann so eine
# Stelle deshalb NIE überspringen - er muss Punkte durchlassen. Die
# Suche läuft ohnehin nur auf dem engen Fenster um den Datumstreffer,
# das begrenzt die Reichweite besser als eine Satzgrenze es täte.
STICHTAG_MUSTER = [
    (r'\btritt\b.{0,80}?in\s+Kraft', "Inkrafttreten"),
    (r'\btreten\b.{0,80}?in\s+Kraft', "Inkrafttreten"),
    (r'\btrat\b.{0,80}?in\s+Kraft', "Inkrafttreten"),
    (r'Inkrafttreten', "Inkrafttreten"),
    (r'\bin\s+Kraft\b', "Inkrafttreten"),
    (r'\bgilt\s+(?:ab|erst\s+ab|bereits\s+ab)\b', "gilt ab"),
    (r'\bgelten\s+(?:ab|erst\s+ab|bereits\s+ab)\b', "gilt ab"),
    (r'\bab\s+dem\b.{0,60}?\b(?:gilt|gelten|verpflichtend|Pflicht)\b', "gilt ab"),
    (r'\bwirksam\s+(?:ab|zum)\b', "gilt ab"),
    (r'\banzuwenden\b', "Anwendung ab"),
    (r'\banwendbar\b', "Anwendung ab"),
    (r'Umsetzungsfrist', "Umsetzungsfrist"),
    (r'\bumzusetzen\b', "Umsetzungsfrist"),
    (r'\bumsetzen\b', "Umsetzungsfrist"),
    (r'\bumgesetzt\b', "Umsetzungsfrist"),
    (r'Übergangsfrist', "Übergangsfrist"),
    (r'Übergangsregelung', "Übergangsfrist"),
    (r'\bFrist\b.{0,40}?\b(?:endet|läuft|abgelaufen)\b', "Fristende"),
    (r'\bStichtag\b', "Stichtag"),
    (r'\bspätestens\b', "Frist"),
    (r'\berstmals\s+(?:für|ab|zum|im)\b', "erstmals ab"),
    (r'\beingeführt\s+(?:wird|werden)\b', "Einführung"),
]

# Themenbegriff: langes deutsches Kompositum oder Gesetzesabkürzung.
# Beides zusammen deckt die Verwaltungssprache erstaunlich vollständig ab.
BEGRIFF_REGEX = re.compile(
    r'\b('
    r'[A-ZÄÖÜ][a-zäöüß]+(?:[A-Za-zÄÖÜäöüß-]*[a-zäöüß]){9,}'   # Kompositum ab ~11 Zeichen
    r'|'
    r'[A-ZÄÖÜ][A-Za-zÄÖÜäöü]{2,}(?:G|VO|RL|StV)\b'            # EntgTranspG, ArbZG, DSGVO
    r')'
)

# Begriffe, die formal wie ein Thema aussehen, aber keins sind: Behörden,
# Gattungsbegriffe, Textbausteine. Ohne diese Liste besteht die halbe
# Radar-Sektion aus "Bundesministerium" und "Pressemitteilung".
STOPPBEGRIFFE = {
    "bundesregierung", "bundesministerium", "bundesministeriums",
    "bundesministerin", "bundesminister", "bundesarbeitsgericht",
    "bundesfinanzhof", "bundessozialgericht", "bundesverfassungsgericht",
    "bundesfinanzministerium", "bundesfinanzministeriums", "bundesanzeiger",
    "pressemitteilung", "pressemitteilungen", "pressestelle",
    "veroeffentlichung", "veröffentlichung", "veröffentlichungen",
    "bekanntmachung", "bekanntmachungen", "entscheidungen",
    "arbeitnehmerinnen", "arbeitnehmern", "arbeitnehmer",
    "arbeitgeberinnen", "arbeitgebern", "beschaeftigten", "beschäftigten",
    "beschäftigte", "unternehmen", "unternehmens", "informationen",
    "voraussetzungen", "möglichkeiten", "moeglichkeiten",
    "zusammenarbeit", "entwicklungen", "entscheidung", "begründung",
    "begruendung", "einzelheiten", "gesetzgebungsverfahren",
    "bundesgesetzblatt", "bundestagsdrucksache", "drucksache",
    "datenschutzerklärung", "datenschutzerklaerung", "barrierefreiheit",
    "inhaltsverzeichnis", "newsletterversand", "einverständnis",
    "gesetzentwurf", "gesetzentwurfs", "referentenentwurf",
    "referentenentwurfs", "regierungsentwurf", "vorschriften",
    "bestimmungen", "regelungen", "anforderungen", "verpflichtungen",
    "berücksichtigung", "beruecksichtigung", "inanspruchnahme",
    "arbeitsgemeinschaft", "interessenvertretung", "öffentlichkeit",
    "oeffentlichkeit", "bundesvereinigung", "spitzenverband",
    # Gattungsbegriffe: sie beschreiben die ART eines Rechtsakts, nicht
    # das Thema. Ohne sie steht im Radar irgendwann "Verordnung" - eine
    # Zeile, die niemandem etwas sagt.
    "verordnung", "richtlinie", "neuregelung", "neuregelungen",
    "uebergangsfrist", "umsetzungsfrist", "uebergangsregelung",
    "rechtsgrundlage", "rechtsgrundlagen", "rechtsprechung",
    "transparenzbestimmungen", "schlussbestimmungen", "uebergangsvorschrift",
    "gesetzesaenderung", "gesetzesaenderungen", "aenderungsgesetz",
    "verwaltungsanweisung", "verwaltungsanweisungen",
    "arbeitsverhaeltnis", "arbeitsverhaeltnisse", "arbeitsvertrag",
    "geschaeftsfuehrung", "verantwortlichkeit", "verhaeltnismaessigkeit",
}


def _norm(text: str) -> str:
    """Schlüsselform eines Begriffs: kleingeschrieben, ohne Umlaute und
    ohne Bindestriche. 'Entgelt-Transparenz-Richtlinie' und
    'Entgelttransparenzrichtlinie' sollen dasselbe Thema sein."""
    t = text.lower().replace("ß", "ss")
    t = (t.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue"))
    t = unicodedata.normalize("NFKD", t)
    return re.sub(r'[^a-z0-9]', '', t)


def _parse_datum(match: re.Match) -> datetime.date | None:
    """Wandelt einen DATUM_REGEX-Treffer in ein echtes Datum. Gibt None
    zurück, wenn die Zahlenkombination kein gültiges Datum ergibt (z.B.
    '31. 02. 2027' aus einem Aktenzeichen-Fragment)."""
    groups = match.groups()
    try:
        if groups[1]:  # Variante mit Monatsnamen
            tag, monat_name, jahr = int(groups[0]), groups[1].lower(), int(groups[2])
            monat = MONATE[monat_name]
        else:          # rein numerische Variante
            tag, monat, jahr = int(groups[3]), int(groups[4]), int(groups[5])
        return datetime.date(jahr, monat, tag)
    except (TypeError, ValueError, KeyError):
        return None


# Satzgrenze. Der negative Lookbehind auf "Ziffer + Punkt" ist der ganze
# Trick: ohne ihn zerlegt jeder Punkt einer deutschen Datumsangabe den
# Satz, und als "Beleg" bleibt ein Fragment wie "Der EU AI Act ist seit
# 2." übrig.
#
# Er muss ZWEI Zeichen weit zurückschauen. Ein `(?<![0-9])` an derselben
# Stelle wie `(?<=[.!?])` prüft das Zeichen unmittelbar links der
# Trennstelle - und das ist immer der Punkt selbst, nie die Ziffer davor.
# Die Bedingung wäre damit stets erfüllt und der Schutz wirkungslos.
SATZ_GRENZE = re.compile(r'(?<![0-9]\.)(?<=[.!?])\s+(?=[A-ZÄÖÜ„"(])')


def _in_saetze(text: str) -> list[str]:
    """Zerlegt einen Text in Aussagen, ohne an Datumsangaben zu zerbrechen.

    ZUERST am Zeilenumbruch trennen, dann innerhalb der Zeile am
    Satzzeichen. Der Umbruch ist die verlässlichere Grenze: die Quellen
    kommen aus BeautifulSoups get_text(separator="\\n"), und dort steht
    je Überschrift, Listenpunkt und Tabellenzelle eine eigene Zeile -
    meist ganz ohne Satzzeichen. Wird der Umbruch vorher weggeglättet,
    verschmelzen Meldungen zu einem Block, und ein Datum bekommt den
    Begriff der Nachbarmeldung zugeordnet."""
    aussagen = []
    for zeile in text.splitlines():
        zeile = re.sub(r'[ \t]+', ' ', zeile).strip()
        if not zeile:
            continue
        aussagen.extend(s.strip() for s in SATZ_GRENZE.split(zeile) if s.strip())
    return aussagen


# Endungen, an denen ein Begriff als Rechtsakt erkennbar ist. Solche
# Begriffe sind fast immer das eigentliche Thema - auch wenn ein anderes
# Wort im selben Satz zufällig länger ist ("Jahressteuergesetz" schlägt
# "Erklärungsverfahren").
RECHTSAKT_ENDUNGEN = (
    "gesetz", "gesetze", "gesetzes", "gesetzen",
    "verordnung", "verordnungen", "richtlinie", "richtlinien",
    "vertrag", "vertrages", "abkommen", "reform", "reformen",
    "novelle", "beschluss", "erlass", "anordnung",
)

# Flexionsformen, die nur die Schreibweise betreffen, nicht das Thema.
FLEXION = [
    ("gesetzes", "gesetz"), ("gesetzen", "gesetz"), ("gesetze", "gesetz"),
    ("verordnungen", "verordnung"), ("richtlinien", "richtlinie"),
    ("reformen", "reform"), ("vertrages", "vertrag"), ("vertrags", "vertrag"),
]


def _grundform(wort: str) -> str:
    """Schneidet reine Flexionsendungen ab, damit 'Barrierefreiheits-
    stärkungsgesetzes' und '...gesetz' ein Thema sind - und der
    Anzeigename im Newsletter nicht im Genitiv steht.

    Die Ersatzendung ist kleingeschrieben; bei einem Wort, das GANZ aus
    der Endung besteht ('Verordnungen' -> 'verordnung'), muss der
    Großbuchstabe am Anfang erhalten bleiben."""
    unten = wort.lower()
    for lang, kurz in FLEXION:
        if unten.endswith(lang):
            ergebnis = wort[: -len(lang)] + kurz
            return ergebnis[:1].upper() + ergebnis[1:] if wort[:1].isupper() else ergebnis
    return wort


# Endungen, an denen ein Begriff als reiner Gattungsbegriff erkennbar
# ist: "Transparenzpflichten", "Compliance-Fristen", "Kernbestimmungen".
# Formal sehen sie aus wie ein Thema, benennen aber nur die Art der
# Rechtsfolge. Eine Endungsregel ist hier einer Wortliste überlegen -
# neue Varianten entstehen ständig.
GATTUNGS_ENDUNGEN = (
    "pflicht", "pflichten", "frist", "fristen",
    "bestimmung", "bestimmungen", "anforderung", "anforderungen",
    "regelung", "regelungen", "vorschrift", "vorschriften",
    "massnahme", "massnahmen", "maßnahme", "maßnahmen",
    "grundsaetze", "grundsätze", "voraussetzung", "voraussetzungen",
    "zuordnung", "zuordnungen", "aenderung", "änderung",
    "aenderungen", "änderungen", "mitteilung", "mitteilungen",
)

# Adverbien und Partizipien, die am Satzanfang großgeschrieben stehen und
# lang genug für das Kompositum-Muster sind. Im ersten Produktivlauf kam
# so "Entsprechend" als Thema in den Radar. Eine Endungsregel fängt die
# ganze Wortklasse ab, statt sie einzeln zu sammeln.
ADVERB_ENDUNGEN = (
    "lich", "liche", "lichen", "licher", "liches",
    "weise", "mäßig", "maessig", "halber", "seits", "dessen",
    "end", "ends", "endes", "endem", "enden", "ender",
    "gemäß", "gemaess", "artig", "wegen", "sichtlich",
)

# Ab dieser Länge ist ein "Satz" keiner mehr - dann hat die Zerlegung
# versagt, weil der Text aus Tabellenzellen oder Aufzählungen ohne
# Satzzeichen besteht. In so einem Block stehen beliebige Begriffe und
# Daten nebeneinander, ohne etwas miteinander zu tun zu haben.
MAX_SATZLAENGE = 350

# Wie weit der Themenbegriff höchstens vom Datum entfernt stehen darf.
# Ohne diese Nähe-Bedingung greift die Erkennung quer durch lange,
# mit Semikolon verbundene Sätze: in "Ab dem 2. August 2026 treten die
# Transparenzpflichten des EU AI Act in Kraft; Unternehmen müssen die
# Dokumentation ..." wurde sonst "Dokumentation" zum Thema des Stichtags.
BEGRIFF_FENSTER = 130


def begriff_zulaessig(wort: str) -> bool:
    """Ist dieses Wort als Themenname brauchbar?

    Eigene Funktion, weil die Prüfung an ZWEI Stellen gebraucht wird:
    beim Erkennen neuer Themen und beim Laden des gespeicherten
    Zustands. Ohne die zweite Stelle wirkt eine verschärfte Regel nicht
    rückwirkend - im Lauf vom 19.09. stand "Entsprechend" weiterhin im
    Radar, obwohl die Adverb-Regel bereits aktiv war: Das Thema kam aus
    dem Zustand des Vorlaufs und wurde nie wieder geprüft."""
    schluessel = _norm(wort)
    if len(schluessel) < 8 or schluessel in STOPPBEGRIFFE:
        return False
    unten = (wort or "").lower()
    if unten.endswith(GATTUNGS_ENDUNGEN):
        return False
    if unten.endswith(ADVERB_ENDUNGEN) and not unten.endswith(RECHTSAKT_ENDUNGEN):
        return False
    return True


def _ist_rechtsakt(wort: str) -> bool:
    return (wort.lower().endswith(RECHTSAKT_ENDUNGEN)
            or re.fullmatch(r'[A-ZÄÖÜ][A-Za-zÄÖÜäöü]{2,}(?:G|VO|RL|StV)', wort) is not None)


def _finde_begriff(fenster: str, satzanfaenge: set | None = None) -> tuple[str, str] | None:
    """Sucht im Textfenster um ein Stichtagsdatum den Themenbegriff.

    Rangfolge: erst Rechtsakte (Gesetz/Verordnung/Richtlinie/Abkürzung),
    dann Länge. Ohne die erste Stufe gewinnt regelmäßig ein beliebiges
    langes Kompositum aus demselben Satz.

    `satzanfaenge` enthält die ersten Wörter der betrachteten Sätze. Die
    werden übersprungen, sofern es keine Rechtsakte sind: Am Satzanfang
    ist JEDES Wort großgeschrieben, die Großschreibung taugt dort also
    nicht als Hinweis auf ein Substantiv. So kamen nacheinander
    "Entsprechend" und "Beschlossen" in den Radar - Endungslisten
    fangen diese Klasse nur stückweise, die Position im Satz fängt sie
    ganz."""
    satzanfaenge = satzanfaenge or set()
    kandidaten = []
    for m in BEGRIFF_REGEX.finditer(fenster):
        wort = _grundform(m.group(1).rstrip("-"))
        if not begriff_zulaessig(wort):
            continue
        rechtsakt = _ist_rechtsakt(wort)
        if wort in satzanfaenge and not rechtsakt:
            continue
        kandidaten.append((0 if rechtsakt else 1, -len(wort), wort, _norm(wort)))

    if not kandidaten:
        return None
    kandidaten.sort()
    return kandidaten[0][2], kandidaten[0][3]


def finde_stichtage(text: str) -> list[dict]:
    """Kernfunktion: findet in einem Quelltext alle Stellen, an denen ein
    Thema mit einem Datum verknüpft wird. Gibt je Fundstelle Begriff,
    Datum, Art des Stichtags und Belegsatz zurück.

    Die Auswertung läuft SATZWEISE. Datum und Stichtagswendung müssen im
    selben Satz stehen; der Themenbegriff darf auch aus dem Satz davor
    kommen ("Mit der Teilkrankschreibung ... . Die Neuregelung tritt am
    1. Januar 2027 in Kraft.").

    Ein größeres Zeichenfenster wäre bequemer, produziert aber falsche
    Zuordnungen: in einem dicht gesetzten Text stand so das Urteilsdatum
    eines BFH-Beschlusses als "Inkrafttreten" eines Gesetzes neben ihm,
    nur weil irgendwo in Reichweite "in Kraft" vorkam."""
    if not text:
        return []

    saetze = _in_saetze(text)
    treffer = []

    for i, satz in enumerate(saetze):
        art = None
        for muster, bezeichnung in STICHTAG_MUSTER:
            if re.search(muster, satz, re.IGNORECASE):
                art = bezeichnung
                break
        if not art:
            continue

        for datum_match in re.finditer(DATUM_REGEX, satz, re.IGNORECASE):
            datum = _parse_datum(datum_match)
            if not datum:
                continue

            # Der Begriff muss NAHE am Datum stehen, sonst gehört er zu
            # einer anderen Aussage desselben Satzes.
            pos = datum_match.start()
            suchraum = satz[max(0, pos - BEGRIFF_FENSTER):pos + BEGRIFF_FENSTER]

            nachbar_erlaubt = len(satz) <= MAX_SATZLAENGE
            if not nachbar_erlaubt:
                # Kein echter Satz, sondern ein Textblock ohne
                # Satzzeichen. Dann muss auch die Stichtagswendung im
                # engen Fenster stehen, nicht irgendwo im Block.
                if not any(re.search(mu, suchraum, re.IGNORECASE)
                           for mu, _ in STICHTAG_MUSTER):
                    continue

            # Erste Wörter der betrachteten Sätze - dort ist jedes Wort
            # großgeschrieben und damit kein Substantiv-Hinweis.
            satzanfaenge = set()
            for quelle in (satz, saetze[i - 1] if i > 0 else ""):
                erstes = quelle.split()[:1]
                if erstes:
                    satzanfaenge.add(erstes[0].strip('„"(»').rstrip(',.;:!?'))

            gefunden = _finde_begriff(suchraum, satzanfaenge)
            # Der Begriff steht oft eine Aussage früher ("Mit der
            # Teilkrankschreibung ... . Die Neuregelung tritt am ...").
            if not gefunden and nachbar_erlaubt and i > 0:
                gefunden = _finde_begriff(saetze[i - 1], satzanfaenge)
            if not gefunden:
                continue
            anzeige, schluessel = gefunden

            treffer.append({
                "schluessel": schluessel,
                "anzeige": anzeige,
                "stichtag": datum.isoformat(),
                "stichtag_art": art,
                # Bei einem echten Satz ist der Satz der Beleg; bei einem
                # Textblock nur das ausgewertete Fenster - sonst zitiert
                # der Newsletter etwas, das die Zuordnung gar nicht trägt.
                "beleg": (satz if len(satz) <= MAX_SATZLAENGE else suchraum)[:400],
            })
            break   # ein Stichtag je Satz genügt

    return treffer


# ---------------------------------------------------------------------------
# Zustand über Läufe hinweg
# ---------------------------------------------------------------------------

def _nachpruefen(state: dict) -> int:
    """Wirft gespeicherte Themen weg, die nach den HEUTIGEN Regeln kein
    Thema mehr wären.

    Ohne diesen Schritt bleibt ein einmal aufgenommener Fehltreffer für
    immer im Radar: Die Erkennungsregeln greifen nur bei neuen Funden,
    der Zustand wird sonst unbesehen übernommen. Genau so überlebte
    "Entsprechend" die Einführung der Adverb-Regel."""
    raus, veraltet = [], []
    for schluessel, thema in state.get("themen", {}).items():
        if thema.get("regeln") != REGEL_VERSION:
            veraltet.append(schluessel)
        elif not begriff_zulaessig(thema.get("anzeige") or schluessel):
            raus.append(schluessel)

    for schluessel in raus + veraltet:
        del state["themen"][schluessel]

    if raus:
        logger.info(
            f"Hot-Topic-Verlauf: {len(raus)} Thema/Themen entsprechen den "
            f"aktuellen Regeln nicht mehr: {raus}"
        )
    if veraltet:
        logger.info(
            f"Hot-Topic-Verlauf: {len(veraltet)} Thema/Themen stammen aus "
            f"älteren Erkennungsregeln und werden neu aufgebaut: {veraltet}"
        )
    return len(raus) + len(veraltet)


def lade_state(pfad: str = STATE_PATH) -> dict:
    """Liest den fortgeschriebenen Themenzustand. Fehlt die Datei oder ist
    sie beschädigt, wird mit leerem Zustand weitergemacht - ein kaputter
    Zustand darf niemals das ganze Briefing verhindern."""
    try:
        with open(pfad, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("version") != STATE_VERSION:
            logger.info(
                f"Hot-Topic-Zustand hat Version {state.get('version')}, "
                f"erwartet {STATE_VERSION} - Themen werden übernommen, "
                "Format wird beim Speichern angehoben."
            )
        state.setdefault("themen", {})
        _nachpruefen(state)
        return state
    except FileNotFoundError:
        logger.info(f"Kein Hot-Topic-Zustand unter {pfad} - starte mit leerem Verlauf.")
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Hot-Topic-Zustand nicht lesbar ({exc}) - starte mit leerem Verlauf.")
    return {"version": STATE_VERSION, "themen": {}}


def speichere_state(state: dict, pfad: str = STATE_PATH) -> None:
    state["version"] = STATE_VERSION
    state["aktualisiert"] = datetime.date.today().isoformat()
    try:
        os.makedirs(os.path.dirname(pfad) or ".", exist_ok=True)
        with open(pfad, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        logger.info(f"Hot-Topic-Zustand gespeichert: {len(state['themen'])} Thema/Themen in {pfad}")
    except OSError as exc:
        logger.warning(f"Hot-Topic-Zustand konnte nicht gespeichert werden: {exc}")


def aktualisiere_aus_quellen(state: dict, collected: dict) -> int:
    """Wertet ALLE erfolgreich abgerufenen Quelltexte dieses Laufs aus und
    schreibt den Themenzustand fort.

    Bewusst über die Rohtexte statt über die fertigen Newsletter-Meldungen:
    ein Thema soll auch dann auf den Radar kommen, wenn es diese Woche
    keine eigene Meldung bekommen hat. Genau so wachsen Dauerthemen von
    selbst in den Verlauf hinein."""
    heute = datetime.date.today().isoformat()
    neu = 0

    for entries in collected.values():
        for e in entries:
            if e.get("status") != "ok" or not e.get("text"):
                continue
            for t in finde_stichtage(e["text"]):
                schluessel = t["schluessel"]
                thema = state["themen"].get(schluessel)

                if thema is None:
                    thema = {
                        "anzeige": t["anzeige"],
                        "erstmals_gesehen": heute,
                        "laeufe": [],
                        "erwaehnungen": 0,
                        "quellen": [],
                    }
                    state["themen"][schluessel] = thema
                    neu += 1

                thema["zuletzt_gesehen"] = heute
                thema["regeln"] = REGEL_VERSION
                thema["erwaehnungen"] = thema.get("erwaehnungen", 0) + 1
                if heute not in thema.setdefault("laeufe", []):
                    thema["laeufe"].append(heute)
                    thema["laeufe"] = thema["laeufe"][-20:]

                # Der längere Anzeigename gewinnt: "Entgelttransparenz"
                # wird von "Entgelttransparenzrichtlinie" verdrängt.
                if len(t["anzeige"]) > len(thema.get("anzeige", "")):
                    thema["anzeige"] = t["anzeige"]

                # Frühester noch nicht abgelaufener Stichtag gewinnt - das
                # ist der, der als nächstes Handlungsbedarf auslöst.
                alt = thema.get("stichtag")
                if alt is None or t["stichtag"] < alt:
                    thema["stichtag"] = t["stichtag"]
                    thema["stichtag_art"] = t["stichtag_art"]
                    thema["beleg"] = t["beleg"]
                    thema["beleg_quelle"] = e["url"]
                    thema["beleg_quelle_name"] = e.get("name", "")

                if e["url"] not in thema["quellen"]:
                    thema["quellen"].append(e["url"])
                    thema["quellen"] = thema["quellen"][-8:]

    logger.info(
        f"Hot-Topic-Verlauf: {neu} neue(s) Thema/Themen erkannt, "
        f"{len(state['themen'])} insgesamt im Zustand."
    )
    return neu


def _tage_bis(datum_iso: str) -> int | None:
    try:
        return (datetime.date.fromisoformat(datum_iso) - datetime.date.today()).days
    except (ValueError, TypeError):
        return None


def aktive_themen(state: dict, maximum: int = MAX_AKTIVE_THEMEN) -> list[dict]:
    """Bestimmt, welche Themen diese Woche auf den Radar gehören.

    Mit Stichtag: aktiv von jetzt bis `Stichtag + KARENZ_TAGE` - das ist
    die von der Redaktion gewünschte Betrachtung "bis Inkrafttreten plus
    Karenzzeit". Ohne Stichtag: nur bei wiederholtem Auftreten und nur für
    OHNE_STICHTAG_TAGE nach der letzten Erwähnung.

    Sortiert nach Dringlichkeit: was am nächsten am Stichtag liegt (davor
    oder knapp danach), steht oben."""
    aktiv = []
    for schluessel, thema in state.get("themen", {}).items():
        stichtag = thema.get("stichtag")

        if stichtag:
            tage = _tage_bis(stichtag)
            if tage is None:
                continue
            if tage > VORLAUF_TAGE_MAX:
                continue           # zu weit weg für ein Wochenbriefing
            if tage < -KARENZ_TAGE:
                continue           # Karenzzeit abgelaufen
            dringlichkeit = abs(tage)
        else:
            if len(thema.get("laeufe", [])) < MINDEST_LAEUFE:
                continue
            seit = _tage_bis(thema.get("zuletzt_gesehen", ""))
            if seit is None or -seit > OHNE_STICHTAG_TAGE:
                continue
            tage = None
            dringlichkeit = 10_000 - thema.get("erwaehnungen", 0)

        aktiv.append({
            "schluessel": schluessel,
            "anzeige": thema.get("anzeige", schluessel),
            "stichtag": stichtag,
            "stichtag_art": thema.get("stichtag_art"),
            "tage_bis_stichtag": tage,
            "beleg": thema.get("beleg"),
            "beleg_quelle": thema.get("beleg_quelle"),
            "beleg_quelle_name": thema.get("beleg_quelle_name"),
            "erwaehnungen": thema.get("erwaehnungen", 0),
            "laeufe": len(thema.get("laeufe", [])),
            "erstmals_gesehen": thema.get("erstmals_gesehen"),
            "quellen": thema.get("quellen", []),
            "dringlichkeit": dringlichkeit,
        })

    aktiv.sort(key=lambda t: t["dringlichkeit"])
    return aktiv[:maximum]


def aufraeumen(state: dict) -> int:
    """Entfernt Themen, deren Karenzzeit abgelaufen ist und die auch
    zuletzt nicht mehr auftauchten. Hält state/hot_topics.json klein und
    lesbar - die Datei soll auch von Hand überprüfbar bleiben."""
    entfernt = []
    for schluessel, thema in list(state.get("themen", {}).items()):
        stichtag = thema.get("stichtag")
        seit_letzter = _tage_bis(thema.get("zuletzt_gesehen", ""))
        lange_weg = seit_letzter is not None and -seit_letzter > 180

        if stichtag:
            tage = _tage_bis(stichtag)
            if tage is not None and tage < -(KARENZ_TAGE + 60) and lange_weg:
                entfernt.append(schluessel)
        elif lange_weg:
            entfernt.append(schluessel)

    for schluessel in entfernt:
        del state["themen"][schluessel]
    if entfernt:
        logger.info(f"Hot-Topic-Verlauf: {len(entfernt)} ausgelaufene(s) Thema/Themen entfernt.")
    return len(entfernt)


def countdown_text(thema: dict) -> str:
    """Formuliert die Zeitangabe für die Radar-Sektion. Bewusst sprechend
    statt nur ein Datum: 'in 84 Tagen' sagt einer Personalleitung mehr als
    '01.01.2027'."""
    tage = thema.get("tage_bis_stichtag")
    art = thema.get("stichtag_art") or "Stichtag"
    if thema.get("stichtag"):
        datum = datetime.date.fromisoformat(thema["stichtag"]).strftime("%d.%m.%Y")
    else:
        return "läuft ohne festen Stichtag"

    if tage is None:
        return f"{art}: {datum}"
    if tage > 1:
        return f"{art} {datum} – noch {tage} Tage"
    if tage == 1:
        return f"{art} {datum} – morgen"
    if tage == 0:
        return f"{art} {datum} – heute"
    return f"{art} war am {datum} – seit {abs(tage)} Tagen in Kraft"
