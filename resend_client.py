"""Resend integration for the Marketing tab -- a separate account/API key
from SellHQ's Resend usage (each project gets its own, per CLAUDE.md: no
shared credentials between the two apps). Not configured yet as of
2026-09-25 -- RESEND_API_KEY doesn't exist in the environment until Broc
signs up and verifies a sending domain. is_configured()/send() are built
now so the rest of the Marketing tab (contacts, audience, campaign
records) doesn't have to wait on that; callers check is_configured()
first and show a clear "not connected yet" state instead of pretending
to send.
"""
import json
import os
import urllib.error
import urllib.request

API_URL = "https://api.resend.com/emails"


def is_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY") and os.environ.get("MARKETING_FROM_EMAIL"))


def send(to: str, subject: str, html_body: str) -> tuple[bool, str | None]:
    """Returns (ok, error_message). Never raises -- a bulk campaign send
    calls this once per recipient and needs to keep going past a single
    bad address rather than aborting the whole batch."""
    api_key = os.environ.get("RESEND_API_KEY")
    from_email = os.environ.get("MARKETING_FROM_EMAIL")
    from_name = os.environ.get("MARKETING_FROM_NAME", "Innovation Concerts")
    if not api_key or not from_email:
        return False, "email_not_configured"
    payload = json.dumps({
        "from": f"{from_name} <{from_email}>",
        "to": [to],
        "subject": subject,
        "html": html_body,
    }).encode()
    req = urllib.request.Request(
        API_URL, method="POST", data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True, None
    except urllib.error.HTTPError as e:
        return False, e.read().decode(errors="replace")[:500]
    except urllib.error.URLError as e:
        return False, str(e)
