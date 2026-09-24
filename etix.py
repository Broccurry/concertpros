"""Etix integration.

Real, user-bound API access as of 2026-09-24 -- ETIX_AUTH_CODE/
ETIX_REFRESH_TOKEN (from Etix's "Manage API Key" screen, NOT the same
thing as ETIX_CLIENT_ID/ETIX_CLIENT_SECRET, which only ever got
client_credentials/app-only access to /public/events). Confirmed live
against the real, previously-blocked /v3/events private endpoint before
anything was built on it -- this is a materially different, non-standard
OAuth flow: the "Authorization Code" goes directly in the Basic header
(not base64(id:secret) -- Etix's own value already is opaque base64),
and the "API Key" shown in their UI is passed as grant_type=refresh_token's
refresh_token param. See Etix's "Manage API Keys" help doc for the exact
shape; their client_credentials grant (still used nowhere in this file
now) is a different, more limited registration type.
"""
import json
import os
import time as time_module
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TOKEN_URL = "https://authorization.etix.com/v1/token/authorize"
API_BASE = "https://api.etix.com/v3"

# Looked up by hand via the public events feed before private access
# worked (see project memory) -- kept as the known-good mapping now that
# /v3/events (private, real venue data) is reachable too.
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
    auth_code = os.environ["ETIX_AUTH_CODE"]
    refresh_token = os.environ["ETIX_REFRESH_TOKEN"]
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token}).encode()
    req = urllib.request.Request(
        TOKEN_URL, method="POST",
        headers={"Authorization": f"Basic {auth_code}", "Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
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
