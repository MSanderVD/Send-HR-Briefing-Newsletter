"""
render.py – Baut den HTML-Newsletter aus den geprüften Meldungen.

Das HTML entsteht hier in Python und nicht mehr im Modell. Der Grund
steht im fertigen Newsletter der KW 38: dort war die Ausblick-Sektion
leer, zeigte aber den Anleitungssatz 'Falls nichts Belastbares vorliegt:
"Für die kommende Woche wurden ..."' als sichtbaren Fließtext, und unter
"Warum das wichtig ist" stand als zweiter Punkt wörtlich derselbe Satz
wie in der Handlungsbedarf-Spalte der Tabelle. Beides sind Fehler, die
gar nicht erst entstehen können, wenn die Vorlage nicht durch das Modell
läuft.

Optik und Farbcodierung sind unverändert die des Original-PhiBox-
Templates - die Empfänger sollen keinen Bruch sehen, nur mehr Inhalt.
"""

import re
import html
import logging
import datetime

import hot_topics

logger = logging.getLogger(__name__)

CATEGORY_ICON = {
    "Gesetzesvorhaben": "📋",
    "BMF-Schreiben": "📄",
    "Urteile": "⚖️",
    "Verordnungen": "🇪🇺",
    "Gesetzgebungsverfahren": "🔄",
    "HR-Digitalisierung": "💻",
    "Newsletter-Auswertung": "📰",
}

CATEGORY_COLOR = {
    "Gesetzesvorhaben": "#1a3c6e",
    "BMF-Schreiben": "#0f766e",
    "Urteile": "#9a2d2d",
    "Verordnungen": "#1e5fa8",
    "Gesetzgebungsverfahren": "#a86d00",
    "HR-Digitalisierung": "#5b3a8e",
    "Newsletter-Auswertung": "#4a5568",
}

CATEGORY_BG = {
    "Gesetzesvorhaben": "#f0f4fb",
    "BMF-Schreiben": "#effaf8",
    "Urteile": "#fcf3f3",
    "Verordnungen": "#eef5fc",
    "Gesetzgebungsverfahren": "#fdf8ef",
    "HR-Digitalisierung": "#f5f1fb",
    "Newsletter-Auswertung": "#f4f5f7",
}

RADAR_FARBE = "#8a4b0f"
RADAR_BG = "#fdf6ef"

LEER_HINWEIS = "Keine belastbare neue Entwicklung im Recherchezeitraum gefunden."


def _e(wert) -> str:
    """HTML-sicher ausgeben. Modelltext kann '&' oder '<' enthalten, was
    die Mail sonst zerlegt."""
    return html.escape(str(wert or "").strip(), quote=True)


def _faktenzeilen(meldung: dict, fakten: list) -> str:
    """Rendert die kategoriespezifischen Pflichtfelder als beschriftete
    Zeilen - das ist die aus dem Original wiederhergestellte Substanz.
    Felder ohne Inhalt werden weggelassen, nicht mit Platzhaltern
    gefüllt."""
    zeilen = []
    for feld, beschriftung in fakten:
        wert = (meldung.get(feld) or "").strip()
        if not wert:
            continue
        zeilen.append(
            f'  <p style="margin: 0 0 4px; font-size: 14px;">'
            f'<strong>{_e(beschriftung)}:</strong> {_e(wert)}</p>'
        )
    return "\n".join(zeilen)


def _meldung_html(meldung: dict, farbe: str, hintergrund: str, fakten: list) -> str:
    # Reihenfolge der Aufzählung: Folge, dann Prüfpunkt. Der
    # "handlungsbedarf" ist die Kurzform für die Übersichtstabelle und
    # kommt hier nur zum Zug, wenn kein Prüfpunkt geliefert wurde -
    # sonst stünde er wörtlich zweimal im selben Newsletter, wie in den
    # bisherigen Ausgaben.
    punkte = []
    for feld in ("hr_relevanz", "pruefpunkt"):
        wert = (meldung.get(feld) or "").strip()
        if wert:
            punkte.append(f'    <li>{_e(wert)}</li>')
    if len(punkte) < 2:
        ersatz = (meldung.get("handlungsbedarf") or "").strip()
        if ersatz:
            punkte.append(f'    <li>{_e(ersatz)}</li>')

    warum = ""
    if punkte:
        warum = (
            '  <p style="margin: 8px 0 4px; font-weight: bold;">Warum das wichtig ist:</p>\n'
            '  <ul style="margin: 0 0 10px; padding-left: 20px;">\n'
            + "\n".join(punkte) + "\n  </ul>"
        )

    fakten_html = _faktenzeilen(meldung, fakten)
    if fakten_html:
        fakten_html = "\n" + fakten_html

    quelle_name = (meldung.get("quelle_name") or "Quelle").strip()
    quelle_url = (meldung.get("quelle_url") or "").strip()
    az = (meldung.get("aktenzeichen") or "").strip()
    az_zusatz = f' &middot; Az. {_e(az)}' if az else ""

    return f"""<div style="margin-bottom: 16px; padding: 14px 18px; background: {hintergrund}; border-left: 3px solid {farbe}; border-radius: 6px;">
  <p style="margin: 0 0 8px; font-weight: bold; color: {farbe};">&#9658; {_e(meldung.get('ueberschrift'))}</p>
  <p style="margin: 0 0 8px;"><strong>Kurz erklärt:</strong> {_e(meldung.get('kurz_erklaert'))}</p>{fakten_html}
{warum}
  <p style="margin: 8px 0 0; font-size: 13px; color: #5a6b80;">📎 Quelle: <a href="{_e(quelle_url)}" style="color: {farbe};">{_e(quelle_name)}</a>{az_zusatz}</p>
</div>"""


def _kurzueberblick(meldungen: list) -> str:
    """Die Tabelle wird aus denselben Daten gebaut wie die Meldungen -
    dadurch kann sie gar nicht mehr von ihnen abweichen."""
    if not meldungen:
        return ""
    zeilen = []
    for i, m in enumerate(meldungen[:10]):
        hintergrund = "#ffffff" if i % 2 == 0 else "#f7f9fc"
        zeilen.append(f"""    <tr style="background: {hintergrund};">
      <td style="padding: 8px 12px; border: 1px solid #dde3ec;">{_e(m.get('kategorie'))}</td>
      <td style="padding: 8px 12px; border: 1px solid #dde3ec;">{_e(m.get('ueberschrift'))}</td>
      <td style="padding: 8px 12px; border: 1px solid #dde3ec;">{_e(m.get('relevanz'))}</td>
      <td style="padding: 8px 12px; border: 1px solid #dde3ec;">{_e(m.get('handlungsbedarf'))}</td>
    </tr>""")

    return f"""<h2 style="color: #1a3c6e; font-size: 18px; border-bottom: 1px solid #dde3ec; padding-bottom: 6px;">Kurzüberblick</h2>
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
{chr(10).join(zeilen)}
  </tbody>
</table>"""


def _radar(themen: list) -> str:
    """Die Langzeit-Sektion: Themen, die über die aktuelle Woche hinaus
    laufen, mit Stichtag und Countdown.

    Das ist die Antwort auf den Redaktionswunsch, ein Thema bis zum
    Inkrafttreten und eine Karenzzeit darüber hinaus zu begleiten. Jede
    Zeile trägt ihren Belegsatz aus der Quelle mit - ein Stichtag ohne
    Beleg wäre genau die Sorte Behauptung, die wir loswerden wollten."""
    if not themen:
        return ""

    bloecke = []
    for t in themen:
        countdown = hot_topics.countdown_text(t)
        beleg = (t.get("beleg") or "").strip()
        beleg_html = ""
        if beleg:
            quelle = (t.get("beleg_quelle") or "").strip()
            name = (t.get("beleg_quelle_name") or "Quelle").strip() or "Quelle"
            link = (f'<a href="{_e(quelle)}" style="color: {RADAR_FARBE};">{_e(name)}</a>'
                    if quelle else _e(name))
            beleg_html = (
                f'\n  <p style="margin: 6px 0 0; font-size: 13px; color: #5a6b80;">'
                f'Beleg: „{_e(beleg)}" &middot; {link}</p>'
            )

        seit = ""
        if t.get("laeufe", 0) > 1:
            seit = (f' &middot; seit {t["laeufe"]} Ausgaben im Blick'
                    if t.get("laeufe") else "")

        bloecke.append(f"""<div style="margin-bottom: 12px; padding: 12px 16px; background: {RADAR_BG}; border-left: 3px solid {RADAR_FARBE}; border-radius: 6px;">
  <p style="margin: 0 0 4px; font-weight: bold; color: {RADAR_FARBE};">&#9202; {_e(t.get('anzeige'))}</p>
  <p style="margin: 0; font-size: 14px;">{_e(countdown)}{seit}</p>{beleg_html}
</div>""")

    return f"""<h2 style="color: {RADAR_FARBE}; font-size: 18px; border-bottom: 1px solid #dde3ec; padding-bottom: 6px;">⏲ Auf dem Radar – über diese Woche hinaus</h2>
<p style="margin: 0 0 12px; font-size: 14px; color: #5a6b80;">Themen mit festem Stichtag, die wir bis zum Inkrafttreten und eine Zeit lang darüber hinaus weiter begleiten.</p>
{chr(10).join(bloecke)}
<div style="margin-bottom: 28px;"></div>"""


def _ausblick(punkte: list) -> str:
    """Anders als bisher: liegt nichts vor, steht hier genau ein Satz -
    und nicht die Anleitung, was man schreiben soll, wenn nichts
    vorliegt."""
    if punkte:
        inhalt = (
            '  <p style="margin: 0 0 6px;">Was in den kommenden Wochen ansteht:</p>\n'
            '  <ul style="margin: 0; padding-left: 20px;">\n'
            + "\n".join(f'    <li>{_e(p)}</li>' for p in punkte)
            + "\n  </ul>"
        )
    else:
        inhalt = ('  <p style="margin: 0; color: #555;">Für die kommenden Wochen wurden keine '
                  'belastbaren, konkret terminierten HR-relevanten Ereignisse gefunden.</p>')

    return f"""<h2 style="color: #1a3c6e; font-size: 18px; border-bottom: 1px solid #dde3ec; padding-bottom: 6px;">🔭 Ausblick</h2>
<div style="margin-bottom: 24px; padding: 14px 18px; background: #f7f9fc; border-radius: 6px;">
{inhalt}
</div>"""


def baue_body(
    meldungen_je_kategorie: dict,
    kategorie_reihenfolge: list,
    executive_summary: dict,
    radar_themen: list,
    week_label: str,
    today_str: str,
    schema: dict,
) -> str:
    """Setzt den kompletten Mail-Body zusammen."""
    alle = [m for k in kategorie_reihenfolge
            for m in meldungen_je_kategorie.get(k, [])]

    summary_text = (executive_summary or {}).get("text", "").strip()
    summary_punkte = [p for p in (executive_summary or {}).get("radar", []) if p]

    summary_html = ""
    if summary_text or summary_punkte:
        punkte_html = ""
        if summary_punkte:
            punkte_html = (
                '\n  <p style="margin: 0 0 6px; font-weight: bold;">Was jetzt auf den Radar gehört:</p>\n'
                '  <ul style="margin: 0; padding-left: 20px;">\n'
                + "\n".join(f'    <li>{_e(p)}</li>' for p in summary_punkte[:4])
                + "\n  </ul>"
            )
        summary_html = f"""<div style="background: #f0f4fb; border-left: 4px solid #1a3c6e; padding: 16px 20px; border-radius: 6px; margin-bottom: 28px;">
  <p style="margin: 0 0 10px; font-weight: bold; color: #1a3c6e; font-size: 16px;">Executive Summary</p>
  <p style="margin: 0 0 12px;">{_e(summary_text)}</p>{punkte_html}
</div>"""

    kategorie_bloecke = []
    for kategorie in kategorie_reihenfolge:
        meldungen = meldungen_je_kategorie.get(kategorie, [])
        farbe = CATEGORY_COLOR.get(kategorie, "#1a3c6e")
        hintergrund = CATEGORY_BG.get(kategorie, "#f0f4fb")
        icon = CATEGORY_ICON.get(kategorie, "•")
        fakten = schema.get(kategorie, {}).get("fakten", [])

        kopf = (f'<h2 style="color: {farbe}; font-size: 18px; '
                f'border-bottom: 1px solid #dde3ec; padding-bottom: 6px;">'
                f'{icon} {_e(kategorie)}</h2>')

        if meldungen:
            koerper = "\n".join(
                _meldung_html(m, farbe, hintergrund, fakten) for m in meldungen
            )
        else:
            koerper = (f'<p style="margin: 0 0 28px; color: #777; font-style: italic;">'
                       f'{LEER_HINWEIS}</p>')
        kategorie_bloecke.append(kopf + "\n" + koerper)

    teile = [
        f"""<div style="border-bottom: 4px solid #1a3c6e; padding-bottom: 12px; margin-bottom: 8px;">
  <h1 style="color: #1a3c6e; font-size: 24px; margin: 0;">HR-Wissen Weekly</h1>
  <p style="color: #5a6b80; font-size: 15px; margin: 4px 0 0;">{_e(week_label)} &middot; Arbeitsrecht, Lohnsteuer &amp; Sozialversicherung</p>
  <p style="color: #99a3b0; font-size: 12px; margin: 2px 0 0;">Stand: {_e(today_str)}</p>
</div>

<p style="font-size: 15px; margin: 18px 0 6px;"><strong>Guten Morgen,</strong></p>
<p style="font-size: 15px; margin: 0 0 24px;">hier kommt das aktuelle HR-Wissen Weekly mit den wichtigsten Entwicklungen für HR, Payroll und Arbeitgeberpraxis.</p>""",
        summary_html,
        _kurzueberblick(alle),
        _radar(radar_themen),
        "\n\n".join(kategorie_bloecke),
        _ausblick((executive_summary or {}).get("ausblick", [])),
    ]
    return "\n\n".join(t for t in teile if t)


def _liegt_in_der_vergangenheit(text: str) -> bool:
    """Prüft, ob ein Freitext-Termin ('7. Juni 2026', 'ab 01.01.2027')
    bereits verstrichen ist.

    Nötig, weil die Termine aus den Meldungsfeldern Freitext sind. Ohne
    diese Prüfung landet unter der Überschrift 'Ausblick' ein Datum, das
    Monate zurückliegt - was den Abschnitt wertlos macht."""
    heute = datetime.date.today()
    for treffer in re.finditer(hot_topics.DATUM_REGEX, text, re.IGNORECASE):
        datum = hot_topics._parse_datum(treffer)
        if datum and datum >= heute:
            return False    # mindestens ein Termin liegt noch vor uns
    # Enthält der Text gar kein erkennbares Datum, ist er eine
    # Verfahrensangabe ("Zweite Lesung im Bundestag") - die darf stehen.
    return any(hot_topics._parse_datum(t)
               for t in re.finditer(hot_topics.DATUM_REGEX, text, re.IGNORECASE))


def baue_ausblick_punkte(radar_themen: list, meldungen: list) -> list:
    """Füllt den Ausblick aus belegten Terminen: Stichtage aus dem
    Themenradar und Inkrafttretens-Angaben aus den Meldungen dieser
    Woche. Alles davon ist bereits gegen den Quelltext geprüft - hier
    wird nichts Neues behauptet, nur zusammengeführt."""
    punkte, gesehen = [], set()

    for t in radar_themen:
        tage = t.get("tage_bis_stichtag")
        if tage is None or tage < 0:
            continue        # Rückblick gehört nicht in den Ausblick
        schluessel = t.get("schluessel")
        if schluessel in gesehen:
            continue
        gesehen.add(schluessel)
        datum = datetime.date.fromisoformat(t["stichtag"]).strftime("%d.%m.%Y")
        punkte.append(f'{t.get("anzeige")} – {t.get("stichtag_art") or "Stichtag"} am {datum}')

    for m in meldungen:
        for feld in ("geplantes_inkrafttreten", "geltung_ab", "ab_wann", "naechster_schritt"):
            wert = (m.get(feld) or "").strip()
            if not wert or len(wert) < 4:
                continue
            if _liegt_in_der_vergangenheit(wert):
                continue
            titel = (m.get("ueberschrift") or "").rstrip(":").strip()
            eintrag = f'{titel} – {wert}'
            if eintrag.lower() in gesehen:
                continue
            gesehen.add(eintrag.lower())
            punkte.append(eintrag)
            break

    return punkte[:6]
