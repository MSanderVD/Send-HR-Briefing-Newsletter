"""
mail_graph.py – Mailversand über Microsoft Graph API (Exchange Online / M365)
statt Gmail.

Nutzt den APP-ONLY-Flow (OAuth2 Client Credentials Grant) mit einer Azure-AD-
App-Registrierung, die die Application-Permission "Mail.Send" besitzt. Das
ist der richtige Ansatz für unbeaufsichtigte Skripte (GitHub/GitLab Actions),
weil dort niemand interaktiv einen Login-Screen bestätigen kann - anders als
beim Gmail-Refresh-Token-Verfahren gibt es hier keinen "Mensch loggt sich
einmalig ein"-Schritt, sondern die App selbst authentifiziert sich mit
Client-ID + Client-Secret direkt gegenüber Azure AD.

Voraussetzung: Exchange Online / Microsoft 365 (funktioniert NICHT mit
einem klassischen On-Premises-Exchange-Server ohne Hybrid-Anbindung).

━━━ Einmaliges Setup durch einen Azure-AD-Admin ━━━
1. Azure Portal → Azure Active Directory → App registrations → New registration
   (z.B. Name "HR-Briefing-Mailer")
2. API permissions → Add a permission → Microsoft Graph → Application
   permissions → "Mail.Send" auswählen
3. WICHTIG: "Grant admin consent for <Tenant>" klicken - ohne Admin Consent
   schlägt der Versand mit 403 fehl, da es sich um eine Application- und
   keine Delegated-Permission handelt.
4. Certificates & secrets → New client secret → Wert SOFORT kopieren
   (wird nur einmal angezeigt) → das ist GRAPH_CLIENT_SECRET.
5. Overview-Seite → "Application (client) ID" kopieren → GRAPH_CLIENT_ID.
6. Overview-Seite → "Directory (tenant) ID" kopieren → GRAPH_TENANT_ID
   (alternativ funktioniert auch die Domain, z.B. "dashoefer.onmicrosoft.com").
7. Empfehlenswert: Application Access Policy in Exchange Online einrichten
   (New-ApplicationAccessPolicy per PowerShell), damit die App NUR das
   Absender-Postfach ansprechen darf und nicht technisch jedes Postfach
   im Tenant - Mail.Send als Application-Permission erlaubt sonst per
   Default den Versand "as any user" im ganzen Tenant.

Benötigte Umgebungsvariablen zur Laufzeit:
  GRAPH_TENANT_ID      – Tenant-ID oder Domain (z.B. dashoefer.onmicrosoft.com)
  GRAPH_CLIENT_ID      – Application (client) ID der App-Registrierung
  GRAPH_CLIENT_SECRET  – Client-Secret der App-Registrierung
  GRAPH_SENDER_UPN     – Absender-Postfach (z.B. ki@dashoefer.onmicrosoft.com)
"""

import os
import requests

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_SEND_MAIL_URL_TEMPLATE = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
TIMEOUT_SECONDS = 30


def _get_access_token() -> str:
    """Holt ein App-Only-Access-Token per Client Credentials Grant.
    Wirft eine aussagekräftige Exception, wenn eine der drei Auth-
    Umgebungsvariablen fehlt oder Azure AD den Request ablehnt (z.B.
    falsches Secret, fehlender Admin Consent)."""
    tenant = os.environ["GRAPH_TENANT_ID"]
    client_id = os.environ["GRAPH_CLIENT_ID"]
    client_secret = os.environ["GRAPH_CLIENT_SECRET"]

    resp = requests.post(
        GRAPH_TOKEN_URL_TEMPLATE.format(tenant=tenant),
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": GRAPH_SCOPE,
            "grant_type": "client_credentials",
        },
        timeout=TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Azure AD Token-Request fehlgeschlagen: HTTP {resp.status_code} - "
            f"{resp.text[:500]} (Prüfen: GRAPH_TENANT_ID/GRAPH_CLIENT_ID/"
            f"GRAPH_CLIENT_SECRET korrekt? Admin Consent erteilt?)"
        )
    return resp.json()["access_token"]


def send_email(to: str, subject: str, html_body: str) -> None:
    """Sendet eine HTML-Mail über Microsoft Graph (App-Only, Mail.Send).
    Wirft bei jedem Fehler eine Exception - kein stilles Scheitern, damit
    der aufrufende Code (hr_briefing.py) den Report trotzdem als Datei
    sichert und den Fehler sichtbar loggt."""
    sender = os.environ["GRAPH_SENDER_UPN"]
    token = _get_access_token()

    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
        "saveToSentItems": "true",
    }

    resp = requests.post(
        GRAPH_SEND_MAIL_URL_TEMPLATE.format(sender=sender),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=message,
        timeout=TIMEOUT_SECONDS,
    )
    # Microsoft Graph sendMail liefert bei Erfolg 202 Accepted mit leerem Body.
    if resp.status_code != 202:
        raise RuntimeError(
            f"Microsoft Graph sendMail fehlgeschlagen: HTTP {resp.status_code} - "
            f"{resp.text[:500]}"
        )
