"""Etix integration — currently just the public event feed via a
client_credentials token, which is all our current Etix app is
authorized for (see project memory: the password grant needed for
ticket-count/settlement data is still waiting on Etix support).

The client_credentials token is app-only, no per-user data, so it can
only reach /public/events -- Etix's global public catalog, not a
private "your venues" endpoint. We filter it by our own venueId per
room instead.
"""
import base64
import json
import os
import time as time_module
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TOKEN_URL = "https://authorization.etix.com/v1/token/authorize"
API_BASE = "https://api.etix.com/v3"

# Looked up by hand via the public events feed (see project memory) --
# Etix has no endpoint to resolve "our" venues without the private,
# user-bound API this app doesn't have access to yet.
VENUE_IDS = {
    "Frankies": 13808,
    "Ottawa Tavern": 13807,
    "Cla-Zel Theater": 37752,
}

_token_cache = {"token": None, "expires_at": 0}


def _get_token():
    now = time_module.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    client_id = os.environ["ETIX_CLIENT_ID"]
    client_secret = os.environ["ETIX_CLIENT_SECRET"]
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        TOKEN_URL, method="POST",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data=body,
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    return _token_cache["token"]


def find_ticket_link(venue_name, show_date):
    """show_date: a date. Matches on venue + local calendar date (Etix's
    startTime is UTC and needs converting back, not compared as a raw
    string) -- returns the Etix purchase URL, or None if no venue mapping
    or no matching show."""
    venue_id = VENUE_IDS.get(venue_name)
    if venue_id is None:
        return None
    token = _get_token()
    qs = urllib.parse.urlencode({"venueId": venue_id, "pageSize": 100})
    req = urllib.request.Request(f"{API_BASE}/public/events?{qs}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    tz = ZoneInfo("America/New_York")
    for ev in data.get("data", []):
        start = ev.get("startTime")
        if not start:
            continue
        local_date = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(tz).date()
        if local_date == show_date:
            return ev.get("purchaseURL")
    return None
