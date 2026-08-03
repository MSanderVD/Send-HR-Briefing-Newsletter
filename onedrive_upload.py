"""
onedrive_upload.py – Lädt den fertigen HR-Briefing-Report per Microsoft
Graph API direkt in einen OneDrive-Ordner hoch, der lokal auf dem PC des
Empfängers synchronisiert ist. Landet die Datei dort, taucht sie
automatisch im lokalen Explorer-Ordner auf, ohne dass jemand manuell ein
Actions-Artifact herunterladen muss.

Nutzt DENSELBEN App-Only-Auth-Ansatz wie mail_graph.py (Client
Credentials Grant). Das Muster ist im Newsletter-Analyse-Repo
(analyse.py, Funktionen _get_onedrive_token/upload_to_onedrive) bereits
im Einsatz - falls dort der OneDrive-Upload schon produktiv läuft,
können die dortigen Azure-AD-App-Zugangsdaten 1:1 wiederverwendet
werden (siehe Umgebungsvariablen unten) und es ist KEIN neuer
Admin-Consent-Schritt nötig.

━━━ Benötigte Umgebungsvariablen ━━━
  ONEDRIVE_TENANT_ID     – Tenant-ID/Domain (z.B. dashoefer.onmicrosoft.com)
  ONEDRIVE_CLIENT_ID     – Application (client) ID der Azure-AD-App
  ONEDRIVE_CLIENT_SECRET – Client-Secret der Azure-AD-App
  ONEDRIVE_USER_EMAIL    – Postfach/Konto, dessen OneDrive angesprochen
                           wird (z.B. m.sander@dashoefer.onmicrosoft.com)
  ONEDRIVE_FOLDER_PATH   – Zielordner-Pfad RELATIV zum OneDrive-Wurzel-
                           verzeichnis (siehe Hinweis unten zur Pfad-
                           Ermittlung), z.B.:
                           "PM/KI-Plattform/03 AdJus/HR-Newsletter"

━━━ Falls die Azure-AD-App noch KEIN Files.ReadWrite.All hat ━━━
(nur nötig, falls die Wiederverwendung der Newsletter-Analyse-App aus
irgendeinem Grund nicht klappt - z.B. weil dort eine andere Berechtigung
hinterlegt ist):
1. Azure Portal → App registrations → (bestehende oder neue App)
2. API permissions → Add a permission → Microsoft Graph → Application
   permissions → "Files.ReadWrite.All"
3. "Grant admin consent" klicken - ohne das schlägt der Upload mit 403 fehl.

━━━ Hinweis zur Pfad-Ermittlung (ONEDRIVE_FOLDER_PATH) ━━━
Der lokale Windows-Pfad
  C:\\Users\\m.sander\\OneDrive - Verlag Dashöfer GmbH\\...\\PM\\KI-Plattform\\03 AdJus\\HR-Newsletter
entspricht (ab dem OneDrive-Synchronisationsstamm) einem Graph-relativen
Pfad. "OneDrive - Verlag Dashöfer GmbH" ist der Name des synchronisierten
Laufwerks selbst, NICHT Teil des Graph-Pfads - der zählt erst danach.
ANNAHME hier: der doppelte Ordnername im ursprünglich genannten Pfad war
ein Kopierfehler, deshalb wird unten NUR EINMAL "PM/KI-Plattform/
03 AdJus/HR-Newsletter" verwendet. Falls der Ordner tatsächlich
verschachtelt existiert, einfach den ONEDRIVE_FOLDER_PATH-Secret-Wert um
das Präfix ergänzen - keine Code-Änderung nötig.
"""

import os
import logging

import requests

logger = logging.getLogger(__name__)

GRAPH_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
TIMEOUT_SECONDS = 60


def _get_onedrive_token() -> str:
    tenant = os.environ["ONEDRIVE_TENANT_ID"]
    resp = requests.post(
        GRAPH_TOKEN_URL_TEMPLATE.format(tenant=tenant),
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["ONEDRIVE_CLIENT_ID"],
            "client_secret": os.environ["ONEDRIVE_CLIENT_SECRET"],
            "scope": GRAPH_SCOPE,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Azure AD Token-Request (OneDrive) fehlgeschlagen: HTTP "
            f"{resp.status_code} - {resp.text[:500]}"
        )
    return resp.json()["access_token"]


def upload_to_onedrive(content: str, filename: str) -> None:
    """Lädt `content` (Text, z.B. HTML) als Datei `filename` in den
    konfigurierten OneDrive-Ordner hoch. Überschreibt eine gleichnamige
    Datei automatisch (Graph-Standardverhalten bei PUT auf denselben
    Pfad) - passt zu unserem Muster "eine Datei pro Kalenderwoche"."""
    token = _get_onedrive_token()
    user = os.environ["ONEDRIVE_USER_EMAIL"]
    folder = os.environ.get(
        "ONEDRIVE_FOLDER_PATH", "PM/KI-Plattform/03 AdJus/HR-Newsletter"
    ).strip("/")

    url = (
        f"https://graph.microsoft.com/v1.0/users/{user}"
        f"/drive/root:/{folder}/{filename}:/content"
    )
    resp = requests.put(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "text/html; charset=utf-8",
        },
        data=content.encode("utf-8"),
        timeout=TIMEOUT_SECONDS,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"OneDrive-Upload fehlgeschlagen: HTTP {resp.status_code} - "
            f"{resp.text[:500]} (Pfad: {folder}/{filename}, Konto: {user})"
        )
    logger.info(f"OneDrive-Upload erfolgreich: {folder}/{filename}")
