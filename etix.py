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
from datetime import date, datetime, timedelta
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

# All three venues share the same Etix organization ("Innovation
# Concepts, LLC") -- confirmed 2026-09-26 by checking GET /venues/{id}
# for each of the three VENUE_IDS above and getting back the same
# organizationId every time.
ORGANIZATION_ID = 5475

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


def _find_public_event(venue_name, show_date):
    """show_date: a date. Matches on venue + local calendar date (Etix's
    startTime is UTC and needs converting back, not compared as a raw
    string) -- returns the raw matched event dict (has 'id', 'purchaseURL',
    etc.), or None if no venue mapping or no matching show. The one place
    this lookup happens, so find_ticket_link and the ticket-sales pull
    can't drift into matching a show differently from each other."""
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
            return ev
    return None


def _find_private_event(venue_name, show_date):
    """Etix's private GET /events endpoint (VIEW_VENUE scope) -- unlike
    /public/events this is NOT limited to on-sale/upcoming shows, so
    it's what can find a performance AFTER it has already happened.
    Confirmed 2026-09-25: a real past show (Merkules, Frankies, already
    played) had dropped off /public/events entirely -- all events that
    feed returned were upcoming/onSale -- but this endpoint found it by
    venue + date range. Only used as a fallback for ticket-sales pulls
    (see pull_and_store_snapshot); find_ticket_link still uses the
    public feed only, since a played show has no purchase link to show.
    Brackets a day either side of the local calendar date because
    beginDatetime/endDatetime are UTC and a tight window can clip a show
    sitting near the day's edge."""
    venue_id = VENUE_IDS.get(venue_name)
    if venue_id is None:
        return None
    token = _get_token()
    begin = datetime(show_date.year, show_date.month, show_date.day) - timedelta(days=1)
    end = datetime(show_date.year, show_date.month, show_date.day) + timedelta(days=2)
    qs = urllib.parse.urlencode({
        "venueId": venue_id,
        "beginDatetime": begin.strftime("%Y-%m-%dT00:00:00Z"),
        "endDatetime": end.strftime("%Y-%m-%dT23:59:59Z"),
        "showPrivate": "true",
    })
    req = urllib.request.Request(
        f"{API_BASE}/events?{qs}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    events = data if isinstance(data, list) else data.get("data", [])
    tz = ZoneInfo("America/New_York")
    for ev in events:
        start = ev.get("beginTimestamp8601")
        if not start:
            continue
        local_date = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(tz).date()
        if local_date == show_date:
            return ev
    return None


def find_ticket_link(venue_name, show_date):
    ev = _find_public_event(venue_name, show_date)
    return ev.get("purchaseURL") if ev else None


def get_snapshot(performance_id):
    """A live, point-in-time ticket count -- not historical. Etix has no
    endpoint that returns a retroactive day-by-day ticket COUNT; only
    day-by-day REVENUE is available historically (see get_daily_sales).
    A real day-by-day count history has to be built by calling this
    repeatedly over time and storing it ourselves (see
    pull_and_store_snapshot / scripts/pull_etix_daily_sales.py)."""
    token = _get_token()
    req = urllib.request.Request(
        f"{API_BASE}/events/{performance_id}/data/snapshot",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def get_daily_sales(performance_id):
    """Real historical data, straight from Etix -- revenue ($) per day,
    not a ticket count. Etix doesn't have to be asked repeatedly to build
    this one; it's already retained on their side."""
    token = _get_token()
    req = urllib.request.Request(
        f"{API_BASE}/events/{performance_id}/data/dailysales",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read()).get("dailySales", [])


def get_price_breakdown(performance_id):
    """Ticket counts and prices broken out by Etix price code (e.g.
    "ADVANCED", "DAY OF") for a performance, built from real per-ticket
    order data -- NOT Etix's "Settlement API" (/settlements/{id}), which
    would be the more obvious source but needs a VIEW_SETTLEMENT_DATA
    scope this app's API key doesn't have (confirmed live: a real call
    returned 406 invalid_scope). This uses GET /organizations/{id}/orders
    instead, which only needs VIEW_ORDERS -- already granted -- and
    returns every ticket in every order for the event, each carrying its
    own priceCode/price.

    Excludes box-office-pulled comp/kill tickets (Broc: "do not include
    pre prints in the numbers") by dropping any order whose salesChannel
    is 'SALES_CHANNEL.INTERNAL' -- confirmed 2026-09-26 against the real
    Merkules show: exactly 65 tickets matched that channel (all fee=0.0,
    delivery PRINT_AT_BOXOFFICE_DELIVERY), the exact same 65 the
    snapshot's own pulledTickets field reports; the remaining 47 tickets
    matched revenueProducingTickets (47) and summed to $1,455 -- the
    exact figure get_daily_sales already independently produces. Real,
    cross-checked, not a guess."""
    token = _get_token()
    qs = urllib.parse.urlencode({"eventId": performance_id, "pageSize": 1000, "excludeVoidTickets": "true"})
    req = urllib.request.Request(
        f"{API_BASE}/organizations/{ORGANIZATION_ID}/orders?{qs}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    tiers = {}
    for order in data.get("createdOrders", []):
        if order.get("salesChannel") == "SALES_CHANNEL.INTERNAL":
            continue
        for ticket in order.get("tickets", []):
            label = ticket.get("priceCode") or "Unknown"
            entry = tiers.setdefault(label, {"label": label, "price": ticket.get("price"), "sold": 0})
            entry["sold"] += 1
    return list(tiers.values())


def pull_and_store_snapshot(conn, event_id, venue_name, show_date, sale_date=None):
    """Finds the matching Etix performance -- tries the public on-sale
    feed first, then falls back to the private VIEW_VENUE lookup for a
    show that's already played and dropped off that feed -- pulls its
    live ticket count and real sales revenue, and upserts one
    ticket_sales row (source='etix') -- the one place this decision is
    made, called by both the manual "Pull from Etix" button and the
    daily scheduled script, so they can't drift. Returns the
    tickets_sold count on success, or None if no Etix match exists for
    this show. Callers that also want the gross figure get it back
    through the normal event refresh (ticket_sales.gross), not a second
    return value -- keeps this function's contract unchanged for the
    existing tickets_sold-only callers.

    Gross comes from get_daily_sales, NOT the snapshot's own
    salesByCurrency -- confirmed against real production data (2026-09-24)
    that the snapshot's total includes the face value of pulledTickets
    (comps/kills taken out of inventory, not real sales): one real show
    had 0 revenueProducingTickets and a $320 snapshot total, entirely
    from 40 pulled tickets, while its real daily-sales revenue was $0.
    Daily sales is per-day historical revenue and doesn't carry that
    contamination, so summing it gives the real cumulative sales total."""
    ev = _find_public_event(venue_name, show_date) or _find_private_event(venue_name, show_date)
    if ev is None:
        return None
    snapshot = get_snapshot(ev["id"])
    tickets_sold = snapshot.get("revenueProducingTickets", 0)
    daily_sales = get_daily_sales(ev["id"])
    gross = sum(p.get("price", 0) for day in daily_sales for p in (day.get("salesByCurrency") or []))
    sale_date = sale_date or date.today()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ticket_sales (event_id, sale_date, tickets_sold, gross, source) VALUES (%s, %s, %s, %s, 'etix') "
        "ON CONFLICT (event_id, sale_date) DO UPDATE SET tickets_sold = %s, gross = %s, source = 'etix'",
        (event_id, sale_date, tickets_sold, gross, tickets_sold, gross),
    )
    return tickets_sold
