"""ConcertPros Flask app.

Every route that returns event data goes through permissions.events_for()
— see CLAUDE.md rule 1. No route queries `events`, `settlements`,
`ticket_tiers`, or `event_tasks` directly; that logic lives in
permissions.py and only there.

No self-signup (CLAUDE.md): accounts are created with scripts/create_person.py,
run by whoever has database access, not through an HTTP endpoint.
"""
import json
import os
import urllib.request
from datetime import date, datetime, time, timezone, timedelta
from decimal import Decimal
from functools import wraps

from flask import Flask, g, jsonify, redirect, render_template, request
from flask.json.provider import DefaultJSONProvider

from dotenv import load_dotenv

import artists as artists_module
import audit
import auth
import crypto
import db
import email_client
import permissions
import etix
import offer_pdf
import resend_client
import settlement_calc
import settlement_pdf
import storage
from permissions import Viewer

VALID_HOLD_STATUSES = ("hold1", "hold2", "hold3", "confirmed")  # what a NEW show can be booked as
ALL_STATUSES = ("hold1", "hold2", "hold3", "confirmed", "complete", "dead")  # what an EXISTING show can move to
TASK_TEMPLATE = ("Website", "Marketing", "Offer", "Contract")  # spawned on every new booking

load_dotenv()  # local dev only — a no-op if .env doesn't exist (Railway sets real env vars directly)


def _anthropic_complete(api_key, prompt):
    """One plain HTTPS call to the Messages API — not worth a whole SDK
    dependency for the single request this app makes."""
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        method="POST",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        data=json.dumps({
            "model": "claude-sonnet-5", "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8"),
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    # content[0] isn't reliably the text block -- a "thinking" block can
    # come first, so find the actual text block instead of assuming position.
    text_block = next((b for b in data["content"] if b.get("type") == "text"), None)
    if text_block is None:
        raise ValueError(f"no text block in Anthropic response: {data}")
    return text_block["text"].strip()


def _parse_number(v):
    """None/""/missing all mean "not set" — a band's guarantee, paid, and
    walkups are all optional until the show's actually happened."""
    if v in (None, ""):
        return None, True
    try:
        return float(v), True
    except (TypeError, ValueError):
        return None, False


_BILL_ROLES = ("Touring", "Direct Support", "Support", "Local")
_PAYMENT_METHODS = ("Cash", "Check", "Deposit", "Wire", "Venmo")


def _parse_optional_time(raw):
    """None/"" both mean "not set" (a headliner's set_time_end is
    routinely left open — "10:30-?" on the printed sheet)."""
    if raw in (None, ""):
        return None, True
    try:
        return time.fromisoformat(raw), True
    except (TypeError, ValueError):
        return None, False


def _parse_acts(raw):
    """Parses an `acts`/`artists` field into a list of act dicts — the one
    place that decides what a valid bill looks like, used by both event
    creation and PUT .../artists so there's no second, slightly different
    rule to drift out of sync with this one. Order in `raw` is the bill
    order; the first act is the headliner by convention (sort_order 0)."""
    if not isinstance(raw, list) or not raw:
        return None, "at_least_one_act_required"
    acts = []
    for item in raw:
        if isinstance(item, dict):
            name = (item.get("name") or "").strip()
            confirmed = bool(item.get("confirmed"))
            declined = bool(item.get("declined"))
            notes = (item.get("notes") or "").strip() or None
            bill_role = item.get("bill_role") or None
            if bill_role is not None and bill_role not in _BILL_ROLES:
                return None, "invalid_bill_role"
            guarantee, ok1 = _parse_number(item.get("guarantee"))
            paid, ok2 = _parse_number(item.get("paid"))
            walkups = item.get("walkups")
            if walkups in (None, ""):
                walkups, ok3 = None, True
            else:
                try:
                    walkups, ok3 = int(walkups), True
                except (TypeError, ValueError):
                    walkups, ok3 = None, False
            if not (ok1 and ok2 and ok3):
                return None, "invalid_act_figure"
            set_time, ok4 = _parse_optional_time(item.get("set_time"))
            set_time_end, ok5 = _parse_optional_time(item.get("set_time_end"))
            if not (ok4 and ok5):
                return None, "invalid_set_time"
            payment_method = item.get("payment_method") or None
            if payment_method is not None and payment_method not in _PAYMENT_METHODS:
                return None, "invalid_payment_method"
            payment_cleared = bool(item.get("payment_cleared"))
        elif isinstance(item, str):
            name = item.strip()
            confirmed = declined = False
            notes = bill_role = guarantee = paid = walkups = set_time = set_time_end = payment_method = None
            payment_cleared = False
        else:
            name = ""
            confirmed = declined = False
            notes = bill_role = guarantee = paid = walkups = set_time = set_time_end = payment_method = None
            payment_cleared = False
        if not name:
            return None, "every_act_needs_a_name"
        acts.append({"name": name, "confirmed": confirmed, "declined": declined,
                     "notes": notes, "bill_role": bill_role, "set_time": set_time, "set_time_end": set_time_end,
                     "guarantee": guarantee, "paid": paid, "walkups": walkups,
                     "payment_method": payment_method, "payment_cleared": payment_cleared})
    return acts, None


def _replace_event_artists(conn, event_id, acts):
    """Replaces a show's whole bill — same replace-the-set pattern as
    ticket_tiers. Each act is resolved independently via find_or_create,
    in order, so typing 'Foo Fighters' then 'Nirvana' always creates two
    acts, never one artist named 'Foo Fighters Nirvana'. Re-creating the
    rows on every save (rather than diffing) does mean a band's contact
    log would be orphaned if we ever deleted rows out from under it —
    the DELETE+INSERT below only touches this event's rows, and
    event_artist_contacts cascades off event_artists.id, so a band that's
    removed from the bill entirely does lose its contact history with
    it, same as removing a task loses its own history. That's accepted;
    reordering/editing an existing act does NOT delete+recreate it below
    it's matched by artist_id so its id (and contact log) survives."""
    cur = conn.cursor()
    cur.execute("SELECT id, artist_id FROM event_artists WHERE event_id = %s", (event_id,))
    existing_by_artist = {artist_id: row_id for row_id, artist_id in cur.fetchall()}
    seen_artist_ids = set()
    sort_order = 0
    for act in acts:
        artist_id = artists_module.find_or_create(conn, act["name"])
        if artist_id in seen_artist_ids:
            continue
        seen_artist_ids.add(artist_id)
        if artist_id in existing_by_artist:
            cur.execute(
                "UPDATE event_artists SET confirmed=%s, declined=%s, sort_order=%s, guarantee=%s, "
                "paid=%s, walkups=%s, notes=%s, bill_role=%s, set_time=%s, set_time_end=%s, "
                "payment_method=%s, payment_cleared=%s WHERE id = %s",
                (act["confirmed"], act["declined"], sort_order, act["guarantee"], act["paid"],
                 act["walkups"], act["notes"], act["bill_role"], act["set_time"], act["set_time_end"],
                 act["payment_method"], act["payment_cleared"], existing_by_artist[artist_id]),
            )
        else:
            cur.execute(
                "INSERT INTO event_artists (event_id, artist_id, confirmed, declined, sort_order, "
                "guarantee, paid, walkups, notes, bill_role, set_time, set_time_end, "
                "payment_method, payment_cleared) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (event_id, artist_id, act["confirmed"], act["declined"], sort_order,
                 act["guarantee"], act["paid"], act["walkups"], act["notes"], act["bill_role"],
                 act["set_time"], act["set_time_end"], act["payment_method"], act["payment_cleared"]),
            )
        sort_order += 1
    cur.execute(
        "DELETE FROM event_artists WHERE event_id = %s AND artist_id != ALL(%s)",
        (event_id, list(seen_artist_ids) or [0]),
    )


VALID_EVENT_TYPES = ("concert", "dance_party", "rental", "private_event")


def _parse_optional_event_fields(body):
    """Every optional field an event can carry beyond venue/date/status/
    acts, with the coercion rules a date/time/number field needs — used
    identically whether the show is being booked for the first time or
    edited later, so that rule only exists once (CLAUDE.md rule 2).
    Returns (updates, error); updates only has the keys present in body."""
    updates = {}
    for key in ("deal_type", "deal_notes", "notes", "ticket_link"):
        if key in body:
            updates[key] = body[key]
    for key in ("announce_date", "onsale_date"):
        if key in body:
            val = body[key]
            if val in (None, ""):
                updates[key] = None
            else:
                try:
                    updates[key] = date.fromisoformat(val)
                except ValueError:
                    return None, f"invalid_{key}"
    for key in ("doors", "show_time"):
        if key in body:
            val = body[key]
            if val in (None, ""):
                updates[key] = None
            else:
                try:
                    updates[key] = time.fromisoformat(val)
                except ValueError:
                    return None, f"invalid_{key}"
    for key in ("guarantee", "backend_pct"):
        if key in body:
            parsed, ok = _parse_number(body[key])
            if not ok:
                return None, f"invalid_{key}"
            updates[key] = parsed
    if "event_types" in body:
        raw = body["event_types"]
        if not isinstance(raw, list) or any(v not in VALID_EVENT_TYPES for v in raw):
            return None, "invalid_event_types"
        updates["event_types"] = list(dict.fromkeys(raw))  # de-dupe, keep order
    if "promoters" in body:
        raw = body["promoters"]
        if not isinstance(raw, list):
            return None, "promoters_must_be_a_list"
        seen, promoters = set(), []
        for p in raw:
            p = (p or "").strip() if isinstance(p, str) else ""
            if p and p.lower() not in seen:
                seen.add(p.lower())
                promoters.append(p)
        updates["promoters"] = promoters
    return updates, None


# A multi-day hold's fields split two ways on update: these describe THIS
# candidate date and always apply to the id in the URL. Everything else
# describes the eventual show and redirects to the group's anchor date —
# see _group_anchor_id.
PER_DATE_EVENT_FIELDS = {"venue_id", "status", "show_date", "doors", "show_time"}

_SHARED_CHILD_TABLES = ("event_artists", "ticket_tiers", "event_tasks", "assignments",
                        "settlements", "settlement_expenses", "settlement_ticket_tiers", "event_messages")
_SHARED_SCALAR_COLUMNS = ("deal_type", "guarantee", "backend_pct", "deal_notes",
                          "announce_date", "onsale_date", "notes", "ticket_link")


def _group_anchor_id(conn, event_id):
    """The date that actually owns the bill/deal/tasks/messages for a
    multi-day hold is whichever member has the lowest id — the one
    created first. An event with no group is its own anchor."""
    cur = conn.cursor()
    cur.execute("SELECT hold_group_id FROM events WHERE id = %s", (event_id,))
    row = cur.fetchone()
    if row is None or row[0] is None:
        return event_id
    cur.execute("SELECT MIN(id) FROM events WHERE hold_group_id = %s", (row[0],))
    return cur.fetchone()[0]


def _migrate_shared_event_data(conn, from_id, to_id):
    """Moves the acts/tiers/tasks/staff/settlement/messages and the
    shared deal/notes fields from one event row to another — used when
    the date that currently owns them is about to stop existing (either
    it's being confirmed and isn't the anchor, or it IS the anchor and
    is being removed from the group)."""
    cur = conn.cursor()
    for table in _SHARED_CHILD_TABLES:
        cur.execute(f"UPDATE {table} SET event_id = %s WHERE event_id = %s", (to_id, from_id))
    cols = ", ".join(_SHARED_SCALAR_COLUMNS)
    cur.execute(f"SELECT {cols} FROM events WHERE id = %s", (from_id,))
    row = cur.fetchone()
    if row is not None:
        set_clause = ", ".join(f"{c} = %s" for c in _SHARED_SCALAR_COLUMNS)
        cur.execute(f"UPDATE events SET {set_clause} WHERE id = %s", list(row) + [to_id])

SESSION_COOKIE = "cp_session"
SESSION_LIFETIME = timedelta(days=90)

# Login brute-force guard -- in-process only (fine at our one-gunicorn-
# worker scale; see Procfile), keyed by email rather than IP so a
# password-guessing run against one account is throttled no matter where
# it's coming from. Not persisted -- a deploy resets it, which is fine.
_LOGIN_FAILURES: dict[str, list] = {}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = timedelta(seconds=90)


def _login_is_rate_limited(email):
    now = datetime.now(timezone.utc)
    attempts = [t for t in _LOGIN_FAILURES.get(email, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_FAILURES[email] = attempts
    return len(attempts) >= _LOGIN_MAX_ATTEMPTS


def _login_record_failure(email):
    _LOGIN_FAILURES.setdefault(email, []).append(datetime.now(timezone.utc))


class ConcertProsJSONProvider(DefaultJSONProvider):
    """psycopg returns date/time/Decimal for DATE/TIME/NUMERIC columns;
    Flask's default encoder doesn't know what to do with any of them."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date, time)):
            return obj.isoformat()
        return super().default(obj)


def create_app():
    app = Flask(__name__)
    app.json = ConcertProsJSONProvider(app)
    secure_cookies = os.environ.get("SECURE_COOKIES", "1") != "0"

    @app.before_request
    def open_db():
        g.db = db.get_connection()

    @app.teardown_request
    def close_db(exc):
        conn = g.pop("db", None)
        if conn is None:
            return
        if exc is not None:
            conn.rollback()
        else:
            conn.commit()
        conn.close()

    @app.context_processor
    def inject_site_social():
        # Every public template can reach the shared social links without
        # each /site route having to fetch and pass them along itself --
        # one place decides what's in the footer, not one per route.
        cur = g.db.cursor()
        cur.execute("SELECT facebook_url, instagram_url, tiktok_url, twitter_url FROM site_settings WHERE id = TRUE")
        row = cur.fetchone() or (None, None, None, None)
        return {"site_social": {
            "facebook": row[0], "instagram": row[1], "tiktok": row[2], "twitter": row[3],
        }}

    def current_viewer():
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        cur = g.db.cursor()
        cur.execute(
            """
            SELECT p.id, p.access_level, s.expires_at
            FROM sessions s JOIN people p ON p.id = s.person_id
            WHERE s.token = %s AND p.active
            """,
            (token,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        person_id, access_level, expires_at = row
        if expires_at is not None and expires_at < datetime.now(timezone.utc):
            return None
        cur.execute("UPDATE sessions SET last_seen = now() WHERE token = %s", (token,))
        return Viewer(id=person_id, access_level=access_level)

    def require_auth(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            viewer = current_viewer()
            if viewer is None:
                return jsonify(error="not_authenticated"), 401
            g.viewer = viewer
            return fn(*args, **kwargs)
        return wrapper

    def require_booker(fn):
        """Booking a show (creating/editing a hold, deal terms, etc.) is
        booker/owner only — crew must be structurally unable to do this,
        not merely have the button hidden client-side."""
        @wraps(fn)
        @require_auth
        def wrapper(*args, **kwargs):
            if g.viewer.access_level not in ("booker", "owner"):
                return jsonify(error="forbidden"), 403
            return fn(*args, **kwargs)
        return wrapper

    def require_owner(fn):
        """The master genre list is Broc's call, not a booker's -- Cody or
        Christian can pick from it (and add sub-genres freely), but only
        the owner decides what's on it in the first place."""
        @wraps(fn)
        @require_auth
        def wrapper(*args, **kwargs):
            if g.viewer.access_level != "owner":
                return jsonify(error="forbidden"), 403
            return fn(*args, **kwargs)
        return wrapper

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    @app.get("/")
    def index():
        return render_template("index.html")

    def _time12(t):
        if t is None:
            return None
        hour, minute = t.hour, t.minute
        ap = "pm" if hour >= 12 else "am"
        h = hour % 12 or 12
        return f"{h}{ap}" if minute == 0 else f"{h}:{minute:02d}{ap}"

    def _public_shows(conn, event_ids=None, when="current", venue_id=None):
        """The public site's own query — deliberately never permissions.
        events_for(), which is written for an authenticated Viewer and
        would need to be told, correctly, every single time, not to leak
        deal/hold/settlement columns to an anonymous visitor. A published
        show is the most restrictive tier there is, so it gets its own
        query that structurally cannot select those columns at all.

        A single-show lookup (event_ids given) always finds the show
        regardless of when/venue -- those two only filter the listing page,
        otherwise an old show's own page would 404 once it's no longer
        "current"."""
        cur = conn.cursor()
        where = ["ew.published = TRUE"]
        params: dict = {}
        if event_ids is not None:
            where.append("e.id = ANY(%(ids)s)")
            params["ids"] = event_ids
            order = "e.show_date"
        else:
            if venue_id:
                where.append("e.venue_id = %(venue_id)s")
                params["venue_id"] = venue_id
            if when == "past":
                where.append("e.show_date < CURRENT_DATE")
                order = "e.show_date DESC"
            else:
                where.append("e.show_date >= CURRENT_DATE")
                order = "e.show_date"
        cur.execute(
            f"""SELECT e.id, e.show_date, e.doors, e.ticket_link, v.id AS venue_id, v.name AS venue,
                       ew.blurb, ew.hero_file_id
                FROM events e
                JOIN event_website ew ON ew.event_id = e.id
                JOIN venues v ON v.id = e.venue_id
                WHERE {" AND ".join(where)}
                ORDER BY {order}""",
            params,
        )
        cols = [c.name for c in cur.description]
        shows = [dict(zip(cols, row)) for row in cur.fetchall()]
        if not shows:
            return shows
        ids = [s["id"] for s in shows]

        cur.execute(
            "SELECT ea.event_id, a.name, ea.declined, ea.set_time, ea.set_time_end "
            "FROM event_artists ea JOIN artists a ON a.id = ea.artist_id "
            "WHERE ea.event_id = ANY(%s) ORDER BY ea.event_id, ea.sort_order",
            (ids,),
        )
        acts_by_event: dict[int, list[dict]] = {}
        for event_id, name, declined, set_time, set_time_end in cur.fetchall():
            if declined:
                continue
            acts_by_event.setdefault(event_id, []).append(
                {"name": name, "set_time": set_time, "set_time_end": set_time_end})

        cur.execute(
            "SELECT event_id, label, price FROM ticket_tiers WHERE event_id = ANY(%s) ORDER BY sort_order",
            (ids,),
        )
        tiers_by_event: dict[int, list[dict]] = {}
        for event_id, label, price in cur.fetchall():
            tiers_by_event.setdefault(event_id, []).append({"label": label, "price": price})

        hero_ids = [s["hero_file_id"] for s in shows if s["hero_file_id"]]
        files_by_id = {}
        if hero_ids:
            cur.execute("SELECT id, storage_key, filename FROM event_files WHERE id = ANY(%s)", (hero_ids,))
            files_by_id = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

        for s in shows:
            acts = acts_by_event.get(s["id"], [])
            for a in acts:
                if a["set_time"]:
                    a["time_display"] = _time12(a["set_time"]) + " - " + (_time12(a["set_time_end"]) or "?")
                else:
                    a["time_display"] = None
            s["artists"] = acts
            s["ticket_tiers"] = tiers_by_event.get(s["id"], [])
            for tier in s["ticket_tiers"]:
                tier["price_display"] = f"${tier['price']:.0f}" if tier["price"] is not None else None
            s["headliner"] = acts[0]["name"] if acts else None
            s["date_display"] = s["show_date"].strftime("%A, %B %-d, %Y") if os.name != "nt" \
                else s["show_date"].strftime("%A, %B ") + str(s["show_date"].day) + s["show_date"].strftime(", %Y")
            s["doors_display"] = _time12(s["doors"])
            s["hero_url"] = None
            if s["hero_file_id"] and s["hero_file_id"] in files_by_id:
                key, filename = files_by_id[s["hero_file_id"]]
                s["hero_url"] = storage.presign_download(key, filename)
        return shows

    @app.get("/site")
    def public_site_list():
        when = "past" if request.args.get("when") == "past" else "current"
        venue_id = request.args.get("venue", type=int)
        cur = g.db.cursor()
        cur.execute("SELECT id, name FROM venues ORDER BY name")
        venues = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
        return render_template(
            "site_list.html",
            shows=_public_shows(g.db, when=when, venue_id=venue_id),
            when=when, venue_id=venue_id, venues=venues,
        )

    @app.get("/site/<int:event_id>")
    def public_site_show(event_id):
        shows = _public_shows(g.db, event_ids=[event_id])
        if not shows:
            return render_template("site_404.html"), 404
        return render_template("site_show.html", show=shows[0])

    @app.get("/site/<int:event_id>/flyer")
    def public_site_flyer(event_id):
        cur = g.db.cursor()
        cur.execute(
            """SELECT f.storage_key, f.filename
               FROM events e JOIN event_website ew ON ew.event_id = e.id
               JOIN event_files f ON f.id = ew.hero_file_id
               WHERE e.id = %s AND ew.published = TRUE""",
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            return render_template("site_404.html"), 404
        storage_key, filename = row
        return redirect(storage.presign_download(storage_key, filename, disposition="attachment"))

    @app.get("/site/venues")
    def public_site_venues():
        cur = g.db.cursor()
        cur.execute("SELECT id, name, address, phone, description, hero_storage_key, hero_filename FROM venues ORDER BY name")
        venues = []
        for vid, name, address, phone, description, hero_key, hero_filename in cur.fetchall():
            venues.append({
                "id": vid, "name": name, "address": address, "phone": phone, "description": description,
                "hero_url": storage.presign_download(hero_key, hero_filename) if hero_key else None,
            })
        return render_template("site_venues.html", venues=venues)

    @app.get("/site/contact")
    def public_site_contact():
        cur = g.db.cursor()
        cur.execute("SELECT id, name FROM venues ORDER BY name")
        venues = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
        return render_template("site_contact.html", venues=venues)

    @app.post("/api/contact")
    def submit_contact():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()[:200]
        email = (body.get("email") or "").strip()[:200]
        message = (body.get("message") or "").strip()[:4000]
        venue_id = body.get("venue_id") or None
        if not name or not email or not message:
            return jsonify(error="all_fields_required"), 400
        if "@" not in email:
            return jsonify(error="invalid_email"), 400
        cur = g.db.cursor()
        if venue_id is not None:
            cur.execute("SELECT 1 FROM venues WHERE id = %s", (venue_id,))
            if cur.fetchone() is None:
                venue_id = None
        cur.execute(
            "INSERT INTO contact_messages (name, email, venue_id, message) VALUES (%s, %s, %s, %s) RETURNING id",
            (name, email, venue_id, message),
        )
        return jsonify(ok=True), 201

    @app.get("/api/contact_messages")
    @require_booker
    def list_contact_messages():
        cur = g.db.cursor()
        cur.execute(
            """SELECT cm.id, cm.name, cm.email, cm.venue_id, v.name, cm.message, cm.read_at, cm.created_at
               FROM contact_messages cm LEFT JOIN venues v ON v.id = cm.venue_id
               ORDER BY cm.created_at DESC"""
        )
        cols = ["id", "name", "email", "venue_id", "venue_name", "message", "read_at", "created_at"]
        messages = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d["read_at"] = d["read_at"].isoformat() if d["read_at"] else None
            d["created_at"] = d["created_at"].isoformat()
            messages.append(d)
        return jsonify(messages=messages)

    @app.patch("/api/contact_messages/<int:msg_id>")
    @require_booker
    def update_contact_message(msg_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM contact_messages WHERE id = %s", (msg_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        body = request.get_json(silent=True) or {}
        if body.get("read"):
            cur.execute("UPDATE contact_messages SET read_at = now() WHERE id = %s", (msg_id,))
        return jsonify(ok=True)

    @app.delete("/api/contact_messages/<int:msg_id>")
    @require_booker
    def delete_contact_message(msg_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM contact_messages WHERE id = %s", (msg_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        cur.execute("DELETE FROM contact_messages WHERE id = %s", (msg_id,))
        return jsonify(ok=True)

    @app.get("/api/marketing/contacts")
    @require_booker
    def list_marketing_contacts():
        cur = g.db.cursor()
        cur.execute(
            "SELECT c.id, c.email, c.name, c.phone, c.source, c.venue_id, v.name AS venue_name, "
            "c.tags, c.email_opt_out, c.created_at "
            "FROM marketing_contacts c LEFT JOIN venues v ON v.id = c.venue_id "
            "ORDER BY c.created_at DESC"
        )
        cols = [col.name for col in cur.description]
        return jsonify(contacts=[dict(zip(cols, row)) for row in cur.fetchall()])

    @app.post("/api/marketing/contacts/import")
    @require_booker
    def import_marketing_contacts():
        """v1 is deliberately a paste box, not a CSV file upload -- Broc's
        source right now is a Mailchimp export he can paste in, and this
        app has no CSV-parsing infra anywhere else to reuse (rule 2 cuts
        both ways: don't invent a second import mechanism, but don't
        build one at all before there's a real second use for it)."""
        body = request.get_json(silent=True) or {}
        raw = body.get("contacts")
        if not isinstance(raw, list):
            return jsonify(error="contacts_must_be_a_list"), 400
        venue_id = body.get("venue_id") or None
        if venue_id is not None:
            cur = g.db.cursor()
            cur.execute("SELECT 1 FROM venues WHERE id = %s", (venue_id,))
            if cur.fetchone() is None:
                return jsonify(error="unknown_venue"), 400
        source = body.get("source") or "manual"

        cur = g.db.cursor()
        added, skipped = 0, 0
        for entry in raw:
            if isinstance(entry, str):
                email, name = entry.strip(), None
            elif isinstance(entry, dict):
                email = (entry.get("email") or "").strip()
                name = (entry.get("name") or "").strip() or None
            else:
                continue
            if not email or "@" not in email:
                skipped += 1
                continue
            cur.execute(
                "INSERT INTO marketing_contacts (email, name, source, venue_id) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (email) DO NOTHING",
                (email.lower(), name, source, venue_id),
            )
            if cur.rowcount:
                added += 1
            else:
                skipped += 1
        audit.record(g.db, g.viewer, "marketing_contacts", 0, "import", {"added": added, "skipped": skipped})
        return jsonify(added=added, skipped=skipped), 201

    @app.delete("/api/marketing/contacts/<int:contact_id>")
    @require_booker
    def delete_marketing_contact(contact_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM marketing_contacts WHERE id = %s", (contact_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        cur.execute("DELETE FROM marketing_contacts WHERE id = %s", (contact_id,))
        return jsonify(ok=True)

    def _marketing_audience(venue_id, tag):
        """The one place a segment definition turns into a WHERE clause --
        called by both the live count (while composing) and the actual
        send, so they can't ever disagree about who's in the audience."""
        where = ["NOT email_opt_out"]
        params: list = []
        if venue_id:
            where.append("venue_id = %s")
            params.append(venue_id)
        if tag:
            where.append("%s = ANY(tags)")
            params.append(tag)
        return " AND ".join(where), params

    @app.get("/api/marketing/audience")
    @require_booker
    def marketing_audience():
        venue_id = request.args.get("venue_id", type=int)
        tag = request.args.get("tag") or None
        where, params = _marketing_audience(venue_id, tag)
        cur = g.db.cursor()
        cur.execute(f"SELECT count(*) FROM marketing_contacts WHERE {where}", params)
        count = cur.fetchone()[0]
        cur.execute(f"SELECT email, name FROM marketing_contacts WHERE {where} ORDER BY created_at DESC LIMIT 5", params)
        sample = [{"email": e, "name": n} for e, n in cur.fetchall()]
        return jsonify(count=count, sample=sample)

    @app.get("/api/marketing/campaigns")
    @require_booker
    def list_marketing_campaigns():
        cur = g.db.cursor()
        cur.execute(
            "SELECT c.id, c.subject, c.segment, c.recipient_count, c.sent_count, c.fail_count, "
            "c.created_at, c.sent_at, p.name AS created_by_name "
            "FROM marketing_campaigns c LEFT JOIN people p ON p.id = c.created_by "
            "ORDER BY c.created_at DESC"
        )
        cols = [col.name for col in cur.description]
        return jsonify(campaigns=[dict(zip(cols, row)) for row in cur.fetchall()])

    @app.post("/api/marketing/test")
    @require_booker
    def send_marketing_test():
        if not resend_client.is_configured():
            return jsonify(error="email_not_configured"), 400
        body = request.get_json(silent=True) or {}
        to = (body.get("to") or "").strip()
        subject = (body.get("subject") or "").strip()
        html_body = (body.get("body") or "").strip()
        if not to or "@" not in to:
            return jsonify(error="invalid_to"), 400
        if not subject or not html_body:
            return jsonify(error="subject_and_body_required"), 400
        ok, err = resend_client.send(to, "[TEST] " + subject, html_body.replace("\n", "<br>"))
        if not ok:
            return jsonify(error="send_failed", detail=err), 502
        return jsonify(ok=True)

    @app.post("/api/marketing/send")
    @require_booker
    def send_marketing_campaign():
        """Sends inline, synchronously -- fine at this contact-list size
        (thousands, not tens of thousands); revisit with a background
        job only if that stops being true. Still records the campaign
        row even when email isn't configured yet, so composing/testing
        the feature doesn't require a live Resend account first."""
        body = request.get_json(silent=True) or {}
        subject = (body.get("subject") or "").strip()
        html_body = (body.get("body") or "").strip()
        venue_id = body.get("venue_id") or None
        tag = body.get("tag") or None
        if not subject or not html_body:
            return jsonify(error="subject_and_body_required"), 400

        where, params = _marketing_audience(venue_id, tag)
        cur = g.db.cursor()
        cur.execute(f"SELECT email, name FROM marketing_contacts WHERE {where}", params)
        recipients = cur.fetchall()

        cur.execute(
            "INSERT INTO marketing_campaigns (subject, body, segment, recipient_count, created_by) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (subject, html_body, json.dumps({"venue_id": venue_id, "tag": tag}), len(recipients), g.viewer.id),
        )
        campaign_id = cur.fetchone()[0]

        if not resend_client.is_configured():
            audit.record(g.db, g.viewer, "marketing_campaign", campaign_id, "queued_unconfigured",
                         {"recipient_count": len(recipients)})
            return jsonify(id=campaign_id, recipient_count=len(recipients), sent_count=0,
                           error="email_not_configured"), 200

        sent, failed = 0, 0
        for email, name in recipients:
            personalized = html_body.replace("{{name}}", name or "there").replace("\n", "<br>")
            ok, _ = resend_client.send(email, subject, personalized)
            if ok:
                sent += 1
            else:
                failed += 1
        cur.execute(
            "UPDATE marketing_campaigns SET sent_count = %s, fail_count = %s, sent_at = now() WHERE id = %s",
            (sent, failed, campaign_id),
        )
        audit.record(g.db, g.viewer, "marketing_campaign", campaign_id, "sent",
                     {"sent": sent, "failed": failed})
        return jsonify(id=campaign_id, recipient_count=len(recipients), sent_count=sent, fail_count=failed)

    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True) or {}
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        if email and _login_is_rate_limited(email):
            return jsonify(error="too_many_attempts"), 429
        cur = g.db.cursor()
        cur.execute(
            "SELECT id, password_hash, access_level FROM people WHERE lower(email) = %s AND active",
            (email,),
        )
        row = cur.fetchone()
        if row is None or row[1] is None or not auth.verify_password(password, row[1]):
            if email:
                _login_record_failure(email)
            return jsonify(error="invalid_credentials"), 401
        person_id, _, access_level = row
        _LOGIN_FAILURES.pop(email, None)
        token = auth.new_session_token()
        expires_at = datetime.now(timezone.utc) + SESSION_LIFETIME
        cur.execute(
            "INSERT INTO sessions (token, person_id, expires_at) VALUES (%s, %s, %s)",
            (token, person_id, expires_at),
        )
        resp = jsonify(id=person_id, access_level=access_level)
        resp.set_cookie(
            SESSION_COOKIE, token, httponly=True, samesite="Lax",
            secure=secure_cookies, max_age=int(SESSION_LIFETIME.total_seconds()),
        )
        return resp

    @app.post("/api/logout")
    @require_auth
    def logout():
        token = request.cookies.get(SESSION_COOKIE)
        cur = g.db.cursor()
        cur.execute("DELETE FROM sessions WHERE token = %s", (token,))
        resp = jsonify(ok=True)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @app.post("/api/change-password")
    @require_auth
    def change_password():
        body = request.get_json(silent=True) or {}
        current = body.get("current_password") or ""
        new = body.get("new_password") or ""
        if len(new) < 8:
            return jsonify(error="password_too_short"), 400
        cur = g.db.cursor()
        cur.execute("SELECT password_hash FROM people WHERE id = %s", (g.viewer.id,))
        (stored,) = cur.fetchone()
        if stored is None or not auth.verify_password(current, stored):
            return jsonify(error="wrong_current_password"), 401
        cur.execute(
            "UPDATE people SET password_hash = %s, updated_at = now() WHERE id = %s",
            (auth.hash_password(new), g.viewer.id),
        )
        return jsonify(ok=True)

    @app.get("/api/me")
    @require_auth
    def me():
        cur = g.db.cursor()
        cur.execute("SELECT name, email, access_level FROM people WHERE id = %s", (g.viewer.id,))
        name, email, access_level = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM event_message_mentions WHERE person_id = %s AND read_at IS NULL",
            (g.viewer.id,),
        )
        mention_count = cur.fetchone()[0]
        # Follow-ups have no separate read/unread flag like a message mention
        # does — "due" IS the notification, and it clears itself the moment
        # the date moves out or the card's reassigned/cleared, same as a
        # todo's due date rather than an inbox item to dismiss.
        cur.execute(
            "SELECT count(*) FROM vision_cards WHERE follow_up_person_id = %s AND follow_up_date <= CURRENT_DATE",
            (g.viewer.id,),
        )
        followup_count = cur.fetchone()[0]
        return jsonify(id=g.viewer.id, name=name, email=email, access_level=access_level,
                       mention_count=mention_count, followup_count=followup_count)

    @app.get("/api/events")
    @require_auth
    def list_events():
        try:
            date_from = date.fromisoformat(request.args["date_from"]) if "date_from" in request.args else None
            date_to = date.fromisoformat(request.args["date_to"]) if "date_to" in request.args else None
        except ValueError:
            return jsonify(error="invalid_date"), 400
        venue_id = request.args.get("venue_id", type=int)
        events = permissions.events_for(g.db, g.viewer, date_from=date_from, date_to=date_to, venue_id=venue_id)
        return jsonify(events=events)

    @app.get("/api/venues")
    @require_auth
    def list_venues():
        cur = g.db.cursor()
        cur.execute(
            "SELECT id, name, address, phone, description, hero_storage_key, hero_filename "
            "FROM venues ORDER BY name"
        )
        venues = []
        for vid, name, address, phone, description, hero_key, hero_filename in cur.fetchall():
            venues.append({
                "id": vid, "name": name, "address": address, "phone": phone, "description": description,
                "hero_filename": hero_filename,
                "hero_url": storage.presign_download(hero_key, hero_filename) if hero_key else None,
            })
        return jsonify(venues=venues)

    @app.patch("/api/venues/<int:venue_id>")
    @require_booker
    def update_venue(venue_id):
        cur = g.db.cursor()
        cur.execute("SELECT hero_storage_key FROM venues WHERE id = %s", (venue_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        old_hero_key = row[0]
        body = request.get_json(silent=True) or {}
        updates = {}
        for field in ("address", "phone", "description"):
            if field in body:
                updates[field] = (body[field] or "").strip() or None
        if "hero_storage_key" in body:
            updates["hero_storage_key"] = body["hero_storage_key"] or None
            updates["hero_filename"] = (body.get("hero_filename") or "").strip() or None
        if not updates:
            return jsonify(error="no_fields_to_update"), 400
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(f"UPDATE venues SET {set_clause} WHERE id = %s", list(updates.values()) + [venue_id])
        if "hero_storage_key" in updates and old_hero_key and old_hero_key != updates["hero_storage_key"]:
            storage.delete_object(old_hero_key)
        audit.record(g.db, g.viewer, "venue", venue_id, "update", {k: audit.jsonable(v) for k, v in updates.items()})
        return jsonify(ok=True)

    @app.post("/api/venues/<int:venue_id>/hero-upload-url")
    @require_booker
    def create_venue_hero_upload_url(venue_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM venues WHERE id = %s", (venue_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        body = request.get_json(silent=True) or {}
        filename = (body.get("filename") or "").strip()
        if not filename:
            return jsonify(error="filename_required"), 400
        content_type = body.get("content_type") or "application/octet-stream"
        storage_key = storage.new_storage_key(None, filename, venue_id)
        upload_url = storage.presign_upload(storage_key, content_type)
        return jsonify(storage_key=storage_key, filename=filename, upload_url=upload_url), 201

    @app.get("/api/site_settings")
    @require_booker
    def get_site_settings():
        cur = g.db.cursor()
        cur.execute("SELECT facebook_url, instagram_url, tiktok_url, twitter_url FROM site_settings WHERE id = TRUE")
        row = cur.fetchone() or (None, None, None, None)
        return jsonify(facebook_url=row[0], instagram_url=row[1], tiktok_url=row[2], twitter_url=row[3])

    @app.put("/api/site_settings")
    @require_booker
    def update_site_settings():
        body = request.get_json(silent=True) or {}
        updates = {}
        for k in ("facebook_url", "instagram_url", "tiktok_url", "twitter_url"):
            if k in body:
                updates[k] = (body[k] or "").strip() or None
        if not updates:
            return jsonify(error="no_fields_to_update"), 400
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur = g.db.cursor()
        cur.execute(f"UPDATE site_settings SET {set_clause}, updated_at = now() WHERE id = TRUE", list(updates.values()))
        audit.record(g.db, g.viewer, "site_settings", 1, "update", {k: audit.jsonable(v) for k, v in updates.items()})
        return jsonify(ok=True)

    @app.get("/api/artists")
    @require_auth
    def list_artists():
        # Crew has no reason to see the artist roster (agent/management
        # contacts, booking history) — booker/owner only, same boundary as
        # creating a show.
        if g.viewer.access_level not in ("booker", "owner"):
            return jsonify(error="forbidden"), 403
        return jsonify(artists=artists_module.list_artists(g.db))

    @app.patch("/api/artists/<int:artist_id>")
    @require_booker
    def update_artist(artist_id):
        """The band's card — tier/genre/tags/location. Name is deliberately
        not editable here: renaming goes through find_or_create when
        booking, not a direct edit, so it stays subject to the same
        normalization/dedup rule everywhere."""
        cur = g.db.cursor()
        cur.execute(
            "SELECT tier, genre, sub_genre, tags, location, instagram, facebook, website, spotify, notes, active "
            "FROM artists WHERE id = %s",
            (artist_id,),
        )
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        before = dict(zip(
            ["tier", "genre", "sub_genre", "tags", "location", "instagram", "facebook", "website", "spotify", "notes", "active"], row))

        body = request.get_json(silent=True) or {}
        updates = {}
        if "active" in body:
            updates["active"] = bool(body["active"])
        if "tier" in body:
            tier = body["tier"] or None
            if tier not in ("Local", "Regional", "National", None):
                return jsonify(error="invalid_tier"), 400
            updates["tier"] = tier
        if "genre" in body:
            genre = (body["genre"] or "").strip() or None
            if genre is not None:
                cur.execute("SELECT 1 FROM genres WHERE name = %s", (genre,))
                if cur.fetchone() is None:
                    return jsonify(error="unknown_genre"), 400
            updates["genre"] = genre
        if "sub_genre" in body:
            # Free text, deliberately not validated against a list --
            # booking staff can add any sub-genre; the dropdown that offers
            # previously-used ones back is just a client-side convenience
            # built from existing values, not an enforced vocabulary.
            updates["sub_genre"] = (body["sub_genre"] or "").strip() or None
        if "location" in body:
            updates["location"] = (body["location"] or "").strip() or None
        if "notes" in body:
            updates["notes"] = (body["notes"] or "").strip() or None
        for key in ("instagram", "facebook", "website", "spotify"):
            if key in body:
                updates[key] = (body[key] or "").strip() or None
        if "tags" in body:
            raw_tags = body["tags"]
            if not isinstance(raw_tags, list):
                return jsonify(error="tags_must_be_a_list"), 400
            seen, tags = set(), []
            for t in raw_tags:
                t = (t or "").strip() if isinstance(t, str) else ""
                if t and t.lower() not in seen:
                    seen.add(t.lower())
                    tags.append(t)
            updates["tags"] = tags

        if not updates:
            return jsonify(error="no_fields_to_update"), 400

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(f"UPDATE artists SET {set_clause}, updated_at = now() WHERE id = %s",
                    list(updates.values()) + [artist_id])
        audit.record(g.db, g.viewer, "artist", artist_id, "update",
                     {"before": {k: audit.jsonable(before.get(k)) for k in updates},
                      "after": {k: audit.jsonable(v) for k, v in updates.items()}})
        return jsonify(ok=True)

    @app.delete("/api/artists/<int:artist_id>")
    @require_booker
    def delete_artist(artist_id):
        """A band with any show or vision-board history can't be
        hard-deleted — that's real booking history/pipeline, not something
        to lose because a band stopped touring. Archive it instead (PATCH
        active=false). Only a band that's never actually been booked or
        chased is safe to remove outright, e.g. a duplicate from a typo."""
        cur = g.db.cursor()
        cur.execute("SELECT name FROM artists WHERE id = %s", (artist_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        cur.execute("SELECT 1 FROM event_artists WHERE artist_id = %s LIMIT 1", (artist_id,))
        has_shows = cur.fetchone() is not None
        cur.execute("SELECT 1 FROM vision_cards WHERE artist_id = %s LIMIT 1", (artist_id,))
        has_offers = cur.fetchone() is not None
        if has_shows or has_offers:
            return jsonify(error="has_history_archive_instead"), 400

        cur.execute("DELETE FROM artist_members WHERE artist_id = %s", (artist_id,))
        cur.execute("DELETE FROM artists WHERE id = %s", (artist_id,))
        audit.record(g.db, g.viewer, "artist", artist_id, "delete", {"name": row[0]})
        return jsonify(ok=True)

    @app.get("/api/genres")
    @require_booker
    def list_genres():
        cur = g.db.cursor()
        cur.execute("SELECT id, name FROM genres ORDER BY name")
        return jsonify(genres=[{"id": r[0], "name": r[1]} for r in cur.fetchall()])

    @app.post("/api/genres")
    @require_owner
    def create_genre():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify(error="name_required"), 400
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM genres WHERE lower(name) = lower(%s)", (name,))
        if cur.fetchone() is not None:
            return jsonify(error="genre_already_exists"), 400
        cur.execute("INSERT INTO genres (name) VALUES (%s) RETURNING id", (name,))
        genre_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "genre", genre_id, "create", {"name": name})
        return jsonify(id=genre_id), 201

    @app.delete("/api/genres/<int:genre_id>")
    @require_owner
    def delete_genre(genre_id):
        cur = g.db.cursor()
        cur.execute("SELECT name FROM genres WHERE id = %s", (genre_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        cur.execute("DELETE FROM genres WHERE id = %s", (genre_id,))
        audit.record(g.db, g.viewer, "genre", genre_id, "delete", {"name": row[0]})
        return jsonify(ok=True)

    # Each booker/owner connects their OWN mailbox -- there's no cross-person
    # access here at all (every query below is scoped to g.viewer.id), so
    # require_booker is the whole permission check: it already keeps crew
    # out entirely, and nobody can reach anyone else's connected account
    # because nothing here ever takes a person_id from the request.
    def _email_account_row(person_id):
        cur = g.db.cursor()
        cur.execute("""SELECT email_address, imap_host, imap_port, smtp_host, smtp_port,
                              username, encrypted_password
                       FROM email_accounts WHERE person_id = %s""", (person_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "email_address": row[0], "imap_host": row[1], "imap_port": row[2],
            "smtp_host": row[3], "smtp_port": row[4], "username": row[5],
            "password": crypto.decrypt(row[6]),
        }

    @app.get("/api/email/account")
    @require_booker
    def get_email_account():
        cur = g.db.cursor()
        cur.execute("""SELECT email_address, imap_host, imap_port, smtp_host, smtp_port
                       FROM email_accounts WHERE person_id = %s""", (g.viewer.id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(account=None)
        return jsonify(account={
            "email_address": row[0], "imap_host": row[1], "imap_port": row[2],
            "smtp_host": row[3], "smtp_port": row[4],
        })

    @app.post("/api/email/account")
    @require_booker
    def connect_email_account():
        """Tests the connection for real before saving anything -- a typo'd
        app password should fail loudly right here, not silently sit broken
        until someone opens the Email tab days later."""
        body = request.get_json(silent=True) or {}
        email_address = (body.get("email_address") or "").strip()
        password = body.get("password") or ""
        if not email_address or not password:
            return jsonify(error="email_and_password_required"), 400
        account = {
            "email_address": email_address,
            "username": (body.get("username") or email_address).strip(),
            "password": password,
            "imap_host": (body.get("imap_host") or "imap.zoho.com").strip(),
            "imap_port": int(body.get("imap_port") or 993),
            "smtp_host": (body.get("smtp_host") or "smtp.zoho.com").strip(),
            "smtp_port": int(body.get("smtp_port") or 465),
        }
        try:
            email_client.test_connection(account)
        except email_client.EmailAuthError as e:
            return jsonify(error="connection_failed", detail=str(e)), 400

        encrypted = crypto.encrypt(password)
        cur = g.db.cursor()
        cur.execute("""INSERT INTO email_accounts
                           (person_id, email_address, imap_host, imap_port, smtp_host, smtp_port, username, encrypted_password)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (person_id) DO UPDATE SET
                           email_address = EXCLUDED.email_address, imap_host = EXCLUDED.imap_host,
                           imap_port = EXCLUDED.imap_port, smtp_host = EXCLUDED.smtp_host,
                           smtp_port = EXCLUDED.smtp_port, username = EXCLUDED.username,
                           encrypted_password = EXCLUDED.encrypted_password, updated_at = now()""",
                    (g.viewer.id, account["email_address"], account["imap_host"], account["imap_port"],
                     account["smtp_host"], account["smtp_port"], account["username"], encrypted))
        audit.record(g.db, g.viewer, "email_account", g.viewer.id, "connect", {"email_address": email_address})
        return jsonify(ok=True)

    @app.delete("/api/email/account")
    @require_booker
    def disconnect_email_account():
        cur = g.db.cursor()
        cur.execute("DELETE FROM email_accounts WHERE person_id = %s", (g.viewer.id,))
        audit.record(g.db, g.viewer, "email_account", g.viewer.id, "disconnect", {})
        return jsonify(ok=True)

    @app.get("/api/email/inbox")
    @require_booker
    def email_inbox():
        account = _email_account_row(g.viewer.id)
        if account is None:
            return jsonify(error="not_connected"), 400
        try:
            messages = email_client.list_messages(account)
        except email_client.EmailAuthError as e:
            return jsonify(error="connection_failed", detail=str(e)), 502
        return jsonify(messages=messages)

    @app.get("/api/email/message/<uid>")
    @require_booker
    def email_message(uid):
        account = _email_account_row(g.viewer.id)
        if account is None:
            return jsonify(error="not_connected"), 400
        try:
            message = email_client.get_message(account, uid)
        except email_client.EmailAuthError as e:
            return jsonify(error="connection_failed", detail=str(e)), 502
        if message is None:
            return jsonify(error="not_found"), 404
        return jsonify(message=message)

    @app.post("/api/email/send")
    @require_booker
    def email_send():
        account = _email_account_row(g.viewer.id)
        if account is None:
            return jsonify(error="not_connected"), 400
        body = request.get_json(silent=True) or {}
        to = (body.get("to") or "").strip()
        subject = (body.get("subject") or "").strip()
        message_body = body.get("body") or ""
        if not to or not subject:
            return jsonify(error="to_and_subject_required"), 400
        try:
            email_client.send_message(account, to, subject, message_body)
        except email_client.EmailAuthError as e:
            return jsonify(error="connection_failed", detail=str(e)), 502
        audit.record(g.db, g.viewer, "email_account", g.viewer.id, "send", {"to": to, "subject": subject})
        return jsonify(ok=True)

    @app.post("/api/artists")
    @require_booker
    def create_artist():
        """The '+ Add band' button — goes through the same find_or_create
        as booking does, so typing a name that already exists opens that
        band's own card instead of minting a duplicate."""
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify(error="name_required"), 400
        artist_id = artists_module.find_or_create(g.db, name)
        audit.record(g.db, g.viewer, "artist", artist_id, "create_or_find", {"name": name})
        return jsonify(id=artist_id), 201

    @app.put("/api/artists/<int:artist_id>/members")
    @require_booker
    def set_artist_members(artist_id):
        """Replaces the whole list — same pattern as ticket_tiers/acts.
        A band can have more than one point of contact; each gets their
        own name/phone/email rather than the single legacy contact fields."""
        body = request.get_json(silent=True) or {}
        raw = body.get("members")
        if not isinstance(raw, list):
            return jsonify(error="members_must_be_a_list"), 400
        cleaned = []
        for m in raw:
            name = (m.get("name") or "").strip() if isinstance(m, dict) else ""
            if not name:
                return jsonify(error="every_member_needs_a_name"), 400
            phone = (m.get("phone") or "").strip() if isinstance(m, dict) else ""
            email = (m.get("email") or "").strip() if isinstance(m, dict) else ""
            cleaned.append((name, phone or None, email or None))

        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM artists WHERE id = %s", (artist_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404

        cur.execute("DELETE FROM artist_members WHERE artist_id = %s", (artist_id,))
        for i, (name, phone, email) in enumerate(cleaned):
            cur.execute(
                "INSERT INTO artist_members (artist_id, name, phone, email, sort_order) "
                "VALUES (%s, %s, %s, %s, %s)",
                (artist_id, name, phone, email, i),
            )
        audit.record(g.db, g.viewer, "artist", artist_id, "set_members",
                     {"members": [{"name": n} for n, _, _ in cleaned]})
        return jsonify(ok=True)

    @app.post("/api/event_artists/<int:event_artist_id>/contacts")
    @require_booker
    def log_event_artist_contact(event_artist_id):
        """One entry in a band's contact log for this show — the method,
        who did it, and when. The whole point is that a second booker
        opening this act sees it and doesn't call the same band again."""
        body = request.get_json(silent=True) or {}
        method = body.get("method")
        if method not in ("text", "email", "phone", "messenger"):
            return jsonify(error="invalid_method"), 400
        note = (body.get("note") or "").strip() or None
        cur = g.db.cursor()
        cur.execute("SELECT event_id FROM event_artists WHERE id = %s", (event_artist_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        event_id = row[0]
        cur.execute(
            "INSERT INTO event_artist_contacts (event_artist_id, method, person_id, note) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (event_artist_id, method, g.viewer.id, note),
        )
        contact_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "event", event_id, "log_contact",
                     {"event_artist_id": event_artist_id, "method": method, "note": note})
        return jsonify(id=contact_id), 201

    def _create_default_shifts(conn, event_id):
        """A newly-confirmed show gets one shift each for Door, Sound, and
        Bartender by default -- enough to open the room; more get added as
        the date gets closer. Only backfills whichever of the three is
        actually missing, so re-confirming (or a hold group collapsing
        into an already-confirmed date) never creates duplicates."""
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM roles WHERE name IN ('Door', 'Sound', 'Bartender')")
        for role_id, _name in cur.fetchall():
            cur.execute("SELECT 1 FROM assignments WHERE event_id = %s AND role_id = %s LIMIT 1", (event_id, role_id))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO assignments (event_id, role_id, person_id) VALUES (%s, %s, NULL)",
                    (event_id, role_id),
                )

    @app.post("/api/events")
    @require_booker
    def create_event():
        body = request.get_json(silent=True) or {}
        venue_id = body.get("venue_id")
        show_date_raw = body.get("show_date")
        status = body.get("status", "hold1")

        acts, err = _parse_acts(body.get("acts"))
        if err:
            return jsonify(error=err), 400
        if not venue_id or not show_date_raw:
            return jsonify(error="venue_id, acts, and show_date are required"), 400
        if status not in VALID_HOLD_STATUSES:
            return jsonify(error="invalid_status"), 400
        try:
            show_date = date.fromisoformat(show_date_raw)
        except ValueError:
            return jsonify(error="invalid_date"), 400

        extra_dates_raw = body.get("extra_dates") or []
        if not isinstance(extra_dates_raw, list):
            return jsonify(error="extra_dates_must_be_a_list"), 400
        extra_dates = []
        for d in extra_dates_raw:
            try:
                extra_dates.append(date.fromisoformat(d))
            except (TypeError, ValueError):
                return jsonify(error="invalid_extra_date"), 400

        optional, err = _parse_optional_event_fields(body)
        if err:
            return jsonify(error=err), 400

        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM venues WHERE id = %s", (venue_id,))
        if cur.fetchone() is None:
            return jsonify(error="unknown_venue"), 400

        # A multi-day hold gets a group; the date created here always ends
        # up with the lowest id in it, so it's automatically the anchor
        # that owns the acts/deal/tasks — no separate bookkeeping needed.
        group_id = None
        if extra_dates:
            cur.execute("INSERT INTO hold_groups DEFAULT VALUES RETURNING id")
            group_id = cur.fetchone()[0]

        cols = ["venue_id", "show_date", "status", "hold_group_id", "created_by"] + list(optional.keys())
        vals = [venue_id, show_date, status, group_id, g.viewer.id] + list(optional.values())
        cur.execute(
            f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(vals))}) RETURNING id",
            vals,
        )
        event_id = cur.fetchone()[0]
        _replace_event_artists(g.db, event_id, acts)
        for i, label in enumerate(TASK_TEMPLATE):
            cur.execute(
                "INSERT INTO event_tasks (event_id, label, sort_order) VALUES (%s, %s, %s)",
                (event_id, label, i),
            )
        if status == "confirmed":
            _create_default_shifts(g.db, event_id)
        for extra_date in extra_dates:
            cur.execute(
                "INSERT INTO events (venue_id, show_date, status, hold_group_id, created_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (venue_id, extra_date, status, group_id, g.viewer.id),
            )
        audit.record(g.db, g.viewer, "event", event_id, "create",
                     {"venue_id": venue_id, "acts": [a["name"] for a in acts],
                      "show_date": show_date_raw, "status": status, "extra_dates": extra_dates_raw})
        return jsonify(id=event_id), 201

    @app.patch("/api/events/<int:event_id>")
    @require_booker
    def update_event(event_id):
        """Partial update. Every caller must send the `version` it last
        read; a mismatch means someone else edited this show first (Cody
        and Christian both open Oct 3) and returns 409 rather than
        silently overwriting their change — see CLAUDE.md / the events
        table's `version` column."""
        body = request.get_json(silent=True) or {}
        if "version" not in body:
            return jsonify(error="version_required"), 400
        try:
            expected_version = int(body["version"])
        except (TypeError, ValueError):
            return jsonify(error="invalid_version"), 400

        cur = g.db.cursor()
        cur.execute("SELECT hold_group_id FROM events WHERE id = %s", (event_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        group_id = row[0]

        cur.execute(
            """SELECT venue_id, show_date, doors, show_time, status,
                      deal_type, guarantee, backend_pct, deal_notes, announce_date, onsale_date,
                      ticket_link, notes, event_types, promoters
               FROM events WHERE id = %s""",
            (event_id,),
        )
        row = cur.fetchone()
        before_cols = ["venue_id", "show_date", "doors", "show_time", "status",
                       "deal_type", "guarantee", "backend_pct", "deal_notes", "announce_date",
                       "onsale_date", "ticket_link", "notes", "event_types", "promoters"]
        before = dict(zip(before_cols, row))

        updates, err = _parse_optional_event_fields(body)
        if err:
            return jsonify(error=err), 400
        if "venue_id" in body:
            cur.execute("SELECT 1 FROM venues WHERE id = %s", (body["venue_id"],))
            if cur.fetchone() is None:
                return jsonify(error="unknown_venue"), 400
            updates["venue_id"] = body["venue_id"]
        if "status" in body:
            if body["status"] not in ALL_STATUSES:
                return jsonify(error="invalid_status"), 400
            updates["status"] = body["status"]
        if "show_date" in body:
            val = body["show_date"]
            if val in (None, ""):
                updates["show_date"] = None
            else:
                try:
                    updates["show_date"] = date.fromisoformat(val)
                except ValueError:
                    return jsonify(error="invalid_show_date"), 400

        if not updates:
            return jsonify(error="no_fields_to_update"), 400

        if group_id is None:
            # The common case — one row, one version check, exactly as
            # before grouping existed.
            set_clause = ", ".join(f"{k} = %s" for k in updates)
            cur.execute(
                f"UPDATE events SET {set_clause}, version = version + 1, updated_at = now() "
                f"WHERE id = %s AND version = %s RETURNING version",
                list(updates.values()) + [event_id, expected_version],
            )
            result = cur.fetchone()
            if result is None:
                cur.execute("SELECT version FROM events WHERE id = %s", (event_id,))
                current = cur.fetchone()
                if current is None:
                    return jsonify(error="not_found"), 404
                return jsonify(error="version_conflict", current_version=current[0]), 409
            new_version = result[0]
            if updates.get("status") == "confirmed" and before["status"] != "confirmed":
                _create_default_shifts(g.db, event_id)
        else:
            # Part of a multi-day hold — per-date fields land on this id,
            # everything else redirects to the group's anchor. The version
            # check only covers the per-date write; two different rows
            # can't share one meaningful version number.
            per_date = {k: v for k, v in updates.items() if k in PER_DATE_EVENT_FIELDS}
            shared = {k: v for k, v in updates.items() if k not in PER_DATE_EVENT_FIELDS}
            anchor_id = _group_anchor_id(g.db, event_id)

            if per_date:
                set_clause = ", ".join(f"{k} = %s" for k in per_date)
                cur.execute(
                    f"UPDATE events SET {set_clause}, version = version + 1, updated_at = now() "
                    f"WHERE id = %s AND version = %s RETURNING version",
                    list(per_date.values()) + [event_id, expected_version],
                )
                result = cur.fetchone()
                if result is None:
                    cur.execute("SELECT version FROM events WHERE id = %s", (event_id,))
                    current = cur.fetchone()
                    if current is None:
                        return jsonify(error="not_found"), 404
                    return jsonify(error="version_conflict", current_version=current[0]), 409
                new_version = result[0]
            else:
                cur.execute("SELECT version FROM events WHERE id = %s", (event_id,))
                new_version = cur.fetchone()[0]

            if shared:
                set_clause = ", ".join(f"{k} = %s" for k in shared)
                cur.execute(f"UPDATE events SET {set_clause}, updated_at = now() WHERE id = %s",
                            list(shared.values()) + [anchor_id])

            # Confirming a held date collapses the group: this date
            # inherits the anchor's shared data if it wasn't already the
            # anchor, then every other candidate date is deleted outright
            # — Broc's call, no "didn't work out" trail for them.
            if per_date.get("status") == "confirmed" and before["status"] != "confirmed":
                _create_default_shifts(g.db, event_id)
            if per_date.get("status") == "confirmed":
                if anchor_id != event_id:
                    _migrate_shared_event_data(g.db, anchor_id, event_id)
                cur.execute("DELETE FROM events WHERE hold_group_id = %s AND id != %s", (group_id, event_id))
                cur.execute("UPDATE events SET hold_group_id = NULL WHERE id = %s", (event_id,))

        audit.record(
            g.db, g.viewer, "event", event_id, "update",
            {
                "before": {k: audit.jsonable(before.get(k)) for k in updates},
                "after": {k: audit.jsonable(v) for k, v in updates.items()},
            },
        )
        return jsonify(ok=True, version=new_version)

    @app.post("/api/events/<int:event_id>/pull_ticket_link")
    @require_booker
    def pull_ticket_link(event_id):
        """Matches this show to Etix's public event feed by venue + local
        calendar date (see etix.py)."""
        cur = g.db.cursor()
        cur.execute(
            "SELECT e.show_date, v.name FROM events e JOIN venues v ON v.id = e.venue_id WHERE e.id = %s",
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        show_date, venue_name = row
        try:
            link = etix.find_ticket_link(venue_name, show_date)
        except Exception as e:
            return jsonify(error="etix_lookup_failed", detail=str(e)), 502
        if not link:
            return jsonify(error="no_matching_etix_event"), 404
        cur.execute("UPDATE events SET ticket_link = %s, updated_at = now() WHERE id = %s", (link, event_id))
        audit.record(g.db, g.viewer, "event", event_id, "pull_ticket_link", {"ticket_link": link})
        return jsonify(ticket_link=link)

    @app.post("/api/events/<int:event_id>/ticket_sales/pull_etix")
    @require_booker
    def pull_etix_ticket_sales(event_id):
        """A manual "pull now," on top of the daily scheduled snapshot
        (scripts/pull_etix_daily_sales.py) -- same underlying call
        (etix.pull_and_store_snapshot), so a manual pull and the nightly
        one can never disagree about how a count got there."""
        cur = g.db.cursor()
        cur.execute(
            "SELECT e.show_date, v.name FROM events e JOIN venues v ON v.id = e.venue_id WHERE e.id = %s",
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        show_date, venue_name = row
        try:
            tickets_sold = etix.pull_and_store_snapshot(g.db, event_id, venue_name, show_date)
        except Exception as e:
            return jsonify(error="etix_lookup_failed", detail=str(e)), 502
        if tickets_sold is None:
            return jsonify(error="no_matching_etix_event"), 404
        audit.record(g.db, g.viewer, "event", event_id, "pull_etix_ticket_sales", {"tickets_sold": tickets_sold})
        return jsonify(ok=True, tickets_sold=tickets_sold)

    @app.post("/api/events/<int:event_id>/hold_dates")
    @require_booker
    def add_hold_dates(event_id):
        """Adds one or more candidate dates to this show's hold, creating
        the hold_group on first use if it wasn't already part of one.
        Each new date starts on the same status as the event you're
        adding from — edit it individually afterward. Each entry in
        `dates` is either a plain date string (defaults to this event's
        own venue) or {"date": ..., "venue_id": ...} — "1st hold at
        Frankies, 2nd hold at Cla-Zel" is the same show held at two
        different rooms on the same or different dates."""
        body = request.get_json(silent=True) or {}
        dates_raw = body.get("dates")
        if not isinstance(dates_raw, list) or not dates_raw:
            return jsonify(error="dates_required"), 400

        cur = g.db.cursor()
        cur.execute("SELECT hold_group_id, venue_id, status FROM events WHERE id = %s", (event_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        group_id, default_venue_id, status = row

        entries = []
        for item in dates_raw:
            if isinstance(item, dict):
                raw_date, raw_venue = item.get("date"), item.get("venue_id")
            else:
                raw_date, raw_venue = item, None
            try:
                parsed_date = date.fromisoformat(raw_date)
            except (TypeError, ValueError):
                return jsonify(error="invalid_date"), 400
            venue_id = default_venue_id
            if raw_venue:
                cur.execute("SELECT 1 FROM venues WHERE id = %s", (raw_venue,))
                if cur.fetchone() is None:
                    return jsonify(error="unknown_venue"), 400
                venue_id = raw_venue
            entries.append((parsed_date, venue_id))

        if group_id is None:
            cur.execute("INSERT INTO hold_groups DEFAULT VALUES RETURNING id")
            group_id = cur.fetchone()[0]
            cur.execute("UPDATE events SET hold_group_id = %s WHERE id = %s", (group_id, event_id))

        created_ids = []
        for parsed_date, venue_id in entries:
            cur.execute(
                "INSERT INTO events (venue_id, show_date, status, hold_group_id, created_by) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (venue_id, parsed_date, status, group_id, g.viewer.id),
            )
            created_ids.append(cur.fetchone()[0])
        audit.record(g.db, g.viewer, "event", event_id, "add_hold_dates", {"dates": dates_raw})
        return jsonify(ids=created_ids, hold_group_id=group_id), 201

    @app.delete("/api/events/<int:event_id>/hold_date")
    @require_booker
    def remove_hold_date(event_id):
        """Removes ONE date from a multi-day hold. Refuses to remove the
        last date on its own — that's deleting the show, not shrinking
        the hold, and show deletion isn't built."""
        cur = g.db.cursor()
        cur.execute("SELECT hold_group_id FROM events WHERE id = %s", (event_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        group_id = row[0]
        if group_id is None:
            return jsonify(error="not_part_of_a_hold_group"), 400
        cur.execute("SELECT count(*) FROM events WHERE hold_group_id = %s", (group_id,))
        if cur.fetchone()[0] <= 1:
            return jsonify(error="cannot_remove_the_only_date"), 400

        anchor_id = _group_anchor_id(g.db, event_id)
        if anchor_id == event_id:
            cur.execute("SELECT MIN(id) FROM events WHERE hold_group_id = %s AND id != %s",
                        (group_id, event_id))
            new_anchor_id = cur.fetchone()[0]
            _migrate_shared_event_data(g.db, event_id, new_anchor_id)
        cur.execute("DELETE FROM events WHERE id = %s", (event_id,))
        audit.record(g.db, g.viewer, "event", event_id, "remove_hold_date", None)
        return jsonify(ok=True)

    @app.delete("/api/events/<int:event_id>")
    @require_booker
    def delete_event(event_id):
        """A show can be deleted any time before it actually happens — a
        hold that never went anywhere, a confirmed date that fell
        through, or a dead one someone wants off the books. Once it's
        Complete there's real settlement history attached to it, so
        deletion is refused — mark a mistake some other way, don't erase
        the record."""
        cur = g.db.cursor()
        cur.execute("SELECT status, venue_id, show_date, hold_group_id FROM events WHERE id = %s", (event_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        status, venue_id, show_date, group_id = row
        if status == "complete":
            return jsonify(error="cannot_delete_a_completed_show"), 400

        if group_id is not None:
            anchor_id = _group_anchor_id(g.db, event_id)
            if anchor_id == event_id:
                cur.execute("SELECT MIN(id) FROM events WHERE hold_group_id = %s AND id != %s",
                            (group_id, event_id))
                new_anchor_row = cur.fetchone()
                if new_anchor_row and new_anchor_row[0]:
                    _migrate_shared_event_data(g.db, event_id, new_anchor_row[0])

        cur.execute("DELETE FROM events WHERE id = %s", (event_id,))
        audit.record(g.db, g.viewer, "event", event_id, "delete",
                     {"status": status, "venue_id": venue_id, "show_date": audit.jsonable(show_date)})
        return jsonify(ok=True)

    @app.get("/api/people")
    @require_booker
    def list_people():
        # Who's working a show is a booking-office concern; crew has no
        # reason to see the roster (and its own read of a show already
        # limits them to only their own assignment — see permissions.py).
        # Returns everyone, active or not — the people-admin screen needs
        # to see and reactivate inactive accounts; the staff-assignment
        # picker filters to active ones client-side.
        cur = g.db.cursor()
        cur.execute("SELECT id, name, email, phone, active, access_level FROM people ORDER BY name")
        cols = [c.name for c in cur.description]
        people = [dict(zip(cols, row)) for row in cur.fetchall()]

        cur.execute(
            """SELECT pr.person_id, r.id, r.name FROM person_roles pr
               JOIN roles r ON r.id = pr.role_id"""
        )
        roles_by_person: dict[int, list[dict]] = {}
        for person_id, role_id, role_name in cur.fetchall():
            roles_by_person.setdefault(person_id, []).append({"id": role_id, "name": role_name})
        for p in people:
            p["roles"] = roles_by_person.get(p["id"], [])
        return jsonify(people=people)

    @app.get("/api/roles")
    @require_booker
    def list_roles():
        cur = g.db.cursor()
        cur.execute("SELECT id, name FROM roles ORDER BY name")
        cols = [c.name for c in cur.description]
        return jsonify(roles=[dict(zip(cols, row)) for row in cur.fetchall()])

    @app.post("/api/people")
    @require_booker
    def create_person():
        """No self-signup, ever (CLAUDE.md) — this is the one place an
        account gets created through the app, and it's gated by rank:
        a booker may only create crew; only the owner can create a
        booker or another owner."""
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip().lower()
        access_level = body.get("access_level", "crew")
        phone = (body.get("phone") or "").strip() or None

        if not name or not email:
            return jsonify(error="name_and_email_required"), 400
        if access_level not in ("crew", "booker", "owner"):
            return jsonify(error="invalid_access_level"), 400
        if not g.viewer.can_manage_access_level(access_level):
            return jsonify(error="forbidden"), 403

        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM people WHERE lower(email) = %s", (email,))
        if cur.fetchone() is not None:
            return jsonify(error="email_already_in_use"), 400

        temp_password = auth.generate_temp_password()
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level, phone)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (name, email, auth.hash_password(temp_password), access_level, phone),
        )
        person_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "person", person_id, "create",
                     {"name": name, "email": email, "access_level": access_level})
        return jsonify(id=person_id, temp_password=temp_password), 201

    @app.patch("/api/people/<int:person_id>")
    @require_booker
    def update_person(person_id):
        cur = g.db.cursor()
        cur.execute("SELECT name, phone, active, access_level FROM people WHERE id = %s", (person_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        before = dict(zip(["name", "phone", "active", "access_level"], row))

        # A booker can't touch a person who outranks them, even to change
        # something as harmless-looking as a phone number.
        if not g.viewer.can_manage_access_level(before["access_level"]):
            return jsonify(error="forbidden"), 403

        body = request.get_json(silent=True) or {}
        updates = {}
        if "name" in body:
            name = (body["name"] or "").strip()
            if not name:
                return jsonify(error="name_cannot_be_empty"), 400
            updates["name"] = name
        if "phone" in body:
            updates["phone"] = (body["phone"] or "").strip() or None
        if "active" in body:
            updates["active"] = bool(body["active"])
        if "access_level" in body:
            new_level = body["access_level"]
            if new_level not in ("crew", "booker", "owner"):
                return jsonify(error="invalid_access_level"), 400
            if not g.viewer.can_manage_access_level(new_level):
                return jsonify(error="forbidden"), 403  # can't promote past your own rank
            updates["access_level"] = new_level

        if not updates:
            return jsonify(error="no_fields_to_update"), 400

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(f"UPDATE people SET {set_clause}, updated_at = now() WHERE id = %s",
                    list(updates.values()) + [person_id])
        audit.record(g.db, g.viewer, "person", person_id, "update",
                     {"before": {k: audit.jsonable(before.get(k)) for k in updates},
                      "after": {k: audit.jsonable(v) for k, v in updates.items()}})
        return jsonify(ok=True)

    @app.post("/api/people/<int:person_id>/reset-password")
    @require_booker
    def reset_password(person_id):
        cur = g.db.cursor()
        cur.execute("SELECT access_level FROM people WHERE id = %s", (person_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        if not g.viewer.can_manage_access_level(row[0]):
            return jsonify(error="forbidden"), 403
        temp_password = auth.generate_temp_password()
        cur.execute("UPDATE people SET password_hash = %s, updated_at = now() WHERE id = %s",
                    (auth.hash_password(temp_password), person_id))
        audit.record(g.db, g.viewer, "person", person_id, "reset_password", None)
        return jsonify(temp_password=temp_password)

    @app.put("/api/people/<int:person_id>/roles")
    @require_booker
    def set_person_roles(person_id):
        """Replaces the whole set — simpler than incremental add/remove
        for a checkbox-style UI, and there's no history worth keeping
        beyond what audit_log already records here."""
        body = request.get_json(silent=True) or {}
        role_ids = body.get("role_ids")
        if not isinstance(role_ids, list):
            return jsonify(error="role_ids_must_be_a_list"), 400

        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM people WHERE id = %s", (person_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        if role_ids:
            cur.execute("SELECT id FROM roles WHERE id = ANY(%s)", (role_ids,))
            found = {r[0] for r in cur.fetchall()}
            unknown = set(role_ids) - found
            if unknown:
                return jsonify(error="unknown_role_ids", unknown=list(unknown)), 400

        cur.execute("DELETE FROM person_roles WHERE person_id = %s", (person_id,))
        for rid in role_ids:
            cur.execute("INSERT INTO person_roles (person_id, role_id) VALUES (%s, %s)", (person_id, rid))
        audit.record(g.db, g.viewer, "person", person_id, "set_roles", {"role_ids": role_ids})
        return jsonify(ok=True)

    @app.post("/api/events/<int:event_id>/assignments")
    @require_booker
    def create_assignment(event_id):
        """person_id is optional — leaving it out books an open TBA slot
        for that role (see the assignments table comment). Booking a
        second TBA (or a second named person) on the same role is how
        "we need 2 security" gets represented: one row per body needed,
        each independently fillable later."""
        body = request.get_json(silent=True) or {}
        role_id = body.get("role_id")
        person_id = body.get("person_id") or None
        if not role_id:
            return jsonify(error="role_id_required"), 400
        scheduled_time = None
        if body.get("scheduled_time"):
            try:
                scheduled_time = time.fromisoformat(body["scheduled_time"])
            except ValueError:
                return jsonify(error="invalid_scheduled_time"), 400

        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        cur.execute("SELECT 1 FROM roles WHERE id = %s", (role_id,))
        if cur.fetchone() is None:
            return jsonify(error="unknown_role"), 400
        person_name = None
        if person_id is not None:
            cur.execute("SELECT name FROM people WHERE id = %s AND active", (person_id,))
            person_row = cur.fetchone()
            if person_row is None:
                return jsonify(error="unknown_person"), 400
            person_name = person_row[0]

        cur.execute(
            "INSERT INTO assignments (event_id, role_id, person_id, scheduled_time) VALUES (%s, %s, %s, %s) RETURNING id",
            (event_id, role_id, person_id, scheduled_time),
        )
        assignment_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "event", event_id, "assign",
                     {"role_id": role_id, "person_id": person_id, "person_name": person_name})
        return jsonify(id=assignment_id), 201

    @app.post("/api/assignments/<int:assignment_id>/clock")
    @require_auth
    def clock_assignment(assignment_id):
        """Clocking in/out is the one write a crew viewer can make directly
        — scoped to their OWN assignment row and only these two timestamp
        fields, never anything else about the show. A booker/owner may
        clock in on behalf of anyone (someone forgot their phone at the
        door)."""
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        if action not in ("in", "out"):
            return jsonify(error="invalid_action"), 400

        cur = g.db.cursor()
        cur.execute(
            "SELECT event_id, person_id, clocked_in_at, clocked_out_at FROM assignments WHERE id = %s",
            (assignment_id,),
        )
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        event_id, person_id, clocked_in_at, clocked_out_at = row

        if g.viewer.access_level not in ("booker", "owner") and g.viewer.id != person_id:
            return jsonify(error="forbidden"), 403
        if person_id is None:
            return jsonify(error="unfilled_slot"), 400

        if action == "in":
            if clocked_in_at is not None:
                return jsonify(error="already_clocked_in"), 400
            cur.execute("UPDATE assignments SET clocked_in_at = now() WHERE id = %s RETURNING clocked_in_at", (assignment_id,))
            clocked_in_at = cur.fetchone()[0]
        else:
            if clocked_in_at is None:
                return jsonify(error="not_clocked_in_yet"), 400
            if clocked_out_at is not None:
                return jsonify(error="already_clocked_out"), 400
            cur.execute("UPDATE assignments SET clocked_out_at = now() WHERE id = %s RETURNING clocked_out_at", (assignment_id,))
            clocked_out_at = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "event", event_id, f"clock_{action}",
                     {"assignment_id": assignment_id, "person_id": person_id})
        return jsonify(ok=True, clocked_in_at=clocked_in_at, clocked_out_at=clocked_out_at)

    @app.patch("/api/assignments/<int:assignment_id>")
    @require_booker
    def update_assignment(assignment_id):
        """Changing who's filling a slot (TBA -> a real name, or moving it
        to someone else) or its scheduled time — the role itself isn't
        editable here; swap the assignment for a different role instead
        of repurposing this one, so a role never silently becomes a
        different role."""
        body = request.get_json(silent=True) or {}
        cur = g.db.cursor()
        cur.execute("SELECT event_id FROM assignments WHERE id = %s", (assignment_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        event_id = row[0]

        updates = {}
        if "person_id" in body:
            person_id = body["person_id"] or None
            if person_id is not None:
                cur.execute("SELECT 1 FROM people WHERE id = %s AND active", (person_id,))
                if cur.fetchone() is None:
                    return jsonify(error="unknown_person"), 400
            updates["person_id"] = person_id
            # A slot moving to someone new (or back to TBA) starts its
            # clock state over — the old clock-in wasn't this person's.
            updates["clocked_in_at"] = None
            updates["clocked_out_at"] = None
        if "scheduled_time" in body:
            val = body["scheduled_time"]
            if val in (None, ""):
                updates["scheduled_time"] = None
            else:
                try:
                    updates["scheduled_time"] = time.fromisoformat(val)
                except ValueError:
                    return jsonify(error="invalid_scheduled_time"), 400
        if not updates:
            return jsonify(error="no_fields_to_update"), 400

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(f"UPDATE assignments SET {set_clause} WHERE id = %s", list(updates.values()) + [assignment_id])
        audit.record(g.db, g.viewer, "event", event_id, "update_assignment",
                     {"assignment_id": assignment_id, **{k: audit.jsonable(v) for k, v in updates.items()}})
        return jsonify(ok=True)

    @app.delete("/api/assignments/<int:assignment_id>")
    @require_booker
    def delete_assignment(assignment_id):
        cur = g.db.cursor()
        cur.execute("SELECT event_id, role_id, person_id FROM assignments WHERE id = %s", (assignment_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        event_id, role_id, person_id = row
        cur.execute("DELETE FROM assignments WHERE id = %s", (assignment_id,))
        audit.record(g.db, g.viewer, "event", event_id, "unassign",
                     {"role_id": role_id, "person_id": person_id})
        return jsonify(ok=True)

    def _recompute_settlement(conn, event_id, merged):
        """The one place a settlement's artist_payout gets computed --
        called after every settlement save and every expense-line save,
        so the stored number can never drift from settlement_calc's
        formula (see that module for the real, confirmed-against-real-
        settlements deal math). Returns the final values dict, including
        the freshly computed expenses total and artist_payout."""
        cur = conn.cursor()
        cur.execute("SELECT deal_type, guarantee, backend_pct FROM events WHERE id = %s", (event_id,))
        deal_type, guarantee, backend_pct = cur.fetchone()

        cur.execute("SELECT COALESCE(SUM(actual), 0) FROM settlement_expenses WHERE event_id = %s", (event_id,))
        expenses = float(cur.fetchone()[0])

        # tickets_sold/gross become a synced total of the tier breakdown --
        # same convention as `expenses` above -- but ONLY once at least one
        # tier row exists, so a show settled before tiers existed (real
        # production data: one already-settled show has tickets_sold/gross
        # recorded with no tiers behind it) keeps its number rather than
        # reading as zero the moment this feature ships.
        cur.execute(
            "SELECT COALESCE(SUM(sold), 0), COALESCE(SUM(sold * price), 0), COUNT(*) "
            "FROM settlement_ticket_tiers WHERE event_id = %s",
            (event_id,),
        )
        tiers_sold, tiers_gross, tier_count = cur.fetchone()
        if tier_count:
            merged = {**merged, "tickets_sold": int(tiers_sold), "gross": float(tiers_gross)}

        net_gross, _breakdown = settlement_calc.compute_net_gross(
            gross=merged.get("gross") or 0,
            sales_tax_rate=merged.get("sales_tax_rate") or 0,
            facility_fee_per_ticket=merged.get("facility_fee_per_ticket") or 0,
            tickets_sold=merged.get("tickets_sold") or 0,
            ticketing_fee_rate=merged.get("ticketing_fee_rate") or 0,
        )
        net_after_expenses = net_gross - expenses
        artist_payout = settlement_calc.compute_artist_payout(
            deal_type, guarantee, backend_pct, net_gross, net_after_expenses,
            door_split_from_dollar_one=merged.get("door_split_from_dollar_one") or False,
        )
        return {
            **merged, "expenses": expenses, "artist_payout": artist_payout,
            "_deal_type": deal_type, "_guarantee": guarantee, "_backend_pct": backend_pct,
            "_net_gross": net_gross, "_net_after_expenses": net_after_expenses,
        }

    SETTLEMENT_FILENAME = "Settlement Summary.pdf"

    def _write_settlement_file(conn, viewer, event_id, merged):
        """Auto-files a plain-text settlement snapshot into the show's own
        Files section on every save (2026-09-25, Broc: "they save on the
        show card and the show file folder") -- replaces any prior
        snapshot for this event rather than piling up duplicates, so
        there's always exactly one, current settlement document per show."""
        cur = conn.cursor()
        cur.execute(
            "SELECT e.show_date, v.name FROM events e JOIN venues v ON v.id = e.venue_id WHERE e.id = %s",
            (event_id,),
        )
        show_date, venue_name = cur.fetchone()
        cur.execute(
            "SELECT a.name FROM event_artists ea JOIN artists a ON a.id = ea.artist_id "
            "WHERE ea.event_id = %s AND NOT ea.declined ORDER BY ea.sort_order",
            (event_id,),
        )
        acts = [row[0] for row in cur.fetchall()]
        cur.execute(
            "SELECT label, actual FROM settlement_expenses WHERE event_id = %s AND actual IS NOT NULL "
            "ORDER BY sort_order",
            (event_id,),
        )
        expense_lines = cur.fetchall()

        pdf_bytes = settlement_pdf.generate(
            show_title=", ".join(acts) or "Untitled show",
            venue_name=venue_name, show_date=str(show_date),
            deal_type=merged["_deal_type"], guarantee=merged.get("_guarantee"), backend_pct=merged.get("_backend_pct"),
            tickets_sold=merged.get("tickets_sold"), gross=merged.get("gross"), net_gross=merged["_net_gross"],
            expense_lines=expense_lines, expenses_total=merged["expenses"],
            net_after_expenses=merged["_net_after_expenses"], artist_payout=merged["artist_payout"],
            settled=merged.get("settled") or False,
        )

        cur.execute(
            "SELECT id, storage_key FROM event_files WHERE event_id = %s AND filename = %s",
            (event_id, SETTLEMENT_FILENAME),
        )
        existing = cur.fetchone()
        if existing:
            file_id, storage_key = existing
            storage.put_bytes(storage_key, pdf_bytes, "application/pdf")
            cur.execute("UPDATE event_files SET size_bytes = %s WHERE id = %s", (len(pdf_bytes), file_id))
        else:
            storage_key = storage.new_storage_key(event_id, SETTLEMENT_FILENAME)
            storage.put_bytes(storage_key, pdf_bytes, "application/pdf")
            cur.execute(
                "INSERT INTO event_files (event_id, filename, storage_key, content_type, size_bytes, uploaded_by) "
                "VALUES (%s, %s, %s, 'application/pdf', %s, %s)",
                (event_id, SETTLEMENT_FILENAME, storage_key, len(pdf_bytes), viewer.id),
            )

    _SETTLEMENT_DEFAULTS = {
        "tickets_sold": None, "gross": None, "sales_tax_rate": None, "facility_fee_per_ticket": None,
        "ticketing_fee_rate": None, "door_split_from_dollar_one": False, "template": "simple",
        "settled": False, "notes": None,
    }

    @app.put("/api/events/<int:event_id>/settlement")
    @require_booker
    def upsert_settlement(event_id):
        """One row per event (settlements.event_id is its own primary
        key), so this is a plain upsert rather than separate create/update
        endpoints — there's no meaningful 'doesn't exist yet' state a
        caller needs to distinguish. Crew never reaches this at all
        (require_booker), and never sees the result either — events_for's
        crew branch doesn't join settlements in the first place.
        expenses/artist_payout are NOT accepted from the client -- they're
        always recomputed server-side (see _recompute_settlement) so a
        stale or hand-edited number can never get stored."""
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404

        cur.execute(
            "SELECT tickets_sold, gross, sales_tax_rate, facility_fee_per_ticket, ticketing_fee_rate, "
            "door_split_from_dollar_one, template, settled, notes FROM settlements WHERE event_id = %s",
            (event_id,),
        )
        row = cur.fetchone()
        before = dict(zip(
            ["tickets_sold", "gross", "sales_tax_rate", "facility_fee_per_ticket", "ticketing_fee_rate",
             "door_split_from_dollar_one", "template", "settled", "notes"], row,
        )) if row else dict(_SETTLEMENT_DEFAULTS)

        body = request.get_json(silent=True) or {}
        values = {}
        if "tickets_sold" in body:
            v = body["tickets_sold"]
            if v in (None, ""):
                values["tickets_sold"] = None
            else:
                try:
                    values["tickets_sold"] = int(v)
                except (TypeError, ValueError):
                    return jsonify(error="invalid_tickets_sold"), 400
        for key in ("gross", "sales_tax_rate", "facility_fee_per_ticket", "ticketing_fee_rate"):
            if key in body:
                parsed, ok = _parse_number(body[key])
                if not ok:
                    return jsonify(error=f"invalid_{key}"), 400
                values[key] = parsed
        if "door_split_from_dollar_one" in body:
            values["door_split_from_dollar_one"] = bool(body["door_split_from_dollar_one"])
        if "template" in body:
            if body["template"] not in ("simple", "detailed"):
                return jsonify(error="invalid_template"), 400
            values["template"] = body["template"]
        if "settled" in body:
            values["settled"] = bool(body["settled"])
        if "notes" in body:
            values["notes"] = body["notes"]

        if not values:
            return jsonify(error="no_fields_to_update"), 400

        # Fill anything not sent this call from what's already stored, so a
        # partial save (just ticking "settled") doesn't null out the rest.
        merged = _recompute_settlement(g.db, event_id, {**before, **values})
        cur.execute(
            """INSERT INTO settlements (event_id, tickets_sold, gross, sales_tax_rate,
                       facility_fee_per_ticket, ticketing_fee_rate, expenses, door_split_from_dollar_one,
                       template, artist_payout, settled, notes)
               VALUES (%(event_id)s, %(tickets_sold)s, %(gross)s, %(sales_tax_rate)s,
                       %(facility_fee_per_ticket)s, %(ticketing_fee_rate)s, %(expenses)s,
                       %(door_split_from_dollar_one)s, %(template)s, %(artist_payout)s, %(settled)s, %(notes)s)
               ON CONFLICT (event_id) DO UPDATE SET
                   tickets_sold = EXCLUDED.tickets_sold, gross = EXCLUDED.gross,
                   sales_tax_rate = EXCLUDED.sales_tax_rate,
                   facility_fee_per_ticket = EXCLUDED.facility_fee_per_ticket,
                   ticketing_fee_rate = EXCLUDED.ticketing_fee_rate, expenses = EXCLUDED.expenses,
                   door_split_from_dollar_one = EXCLUDED.door_split_from_dollar_one,
                   template = EXCLUDED.template, artist_payout = EXCLUDED.artist_payout,
                   settled = EXCLUDED.settled, notes = EXCLUDED.notes, updated_at = now()""",
            {"event_id": event_id, **merged},
        )
        audit.record(
            g.db, g.viewer, "event", event_id, "settle",
            {
                "before": {k: audit.jsonable(before.get(k)) for k in values},
                "after": {k: audit.jsonable(v) for k, v in values.items()},
            },
        )
        _write_settlement_file(g.db, g.viewer, event_id, merged)
        return jsonify(ok=True, expenses=merged["expenses"], artist_payout=merged["artist_payout"])

    @app.put("/api/events/<int:event_id>/settlement_expenses")
    @require_booker
    def set_settlement_expenses(event_id):
        """Replaces the whole set, same convention as set_ticket_tiers --
        a booker re-sends the full expense list each time rather than
        this endpoint tracking incremental add/remove/reorder. Triggers
        a settlement recompute afterward since the expense total feeds
        directly into the payout math."""
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404

        body = request.get_json(silent=True) or {}
        raw = body.get("lines")
        if not isinstance(raw, list):
            return jsonify(error="lines_must_be_a_list"), 400

        lines = []
        for entry in raw:
            if not isinstance(entry, dict) or not (entry.get("label") or "").strip():
                return jsonify(error="each_line_needs_a_label"), 400
            budget, ok1 = _parse_number(entry.get("budget"))
            actual, ok2 = _parse_number(entry.get("actual"))
            if not ok1 or not ok2:
                return jsonify(error="invalid_amount"), 400
            lines.append((entry["label"].strip(), budget, actual))

        cur.execute("DELETE FROM settlement_expenses WHERE event_id = %s", (event_id,))
        for i, (label, budget, actual) in enumerate(lines):
            cur.execute(
                "INSERT INTO settlement_expenses (event_id, label, budget, actual, sort_order) "
                "VALUES (%s, %s, %s, %s, %s)",
                (event_id, label, budget, actual, i),
            )

        cur.execute(
            "SELECT tickets_sold, gross, sales_tax_rate, facility_fee_per_ticket, ticketing_fee_rate, "
            "door_split_from_dollar_one, template, settled, notes FROM settlements WHERE event_id = %s",
            (event_id,),
        )
        row = cur.fetchone()
        before = dict(zip(
            ["tickets_sold", "gross", "sales_tax_rate", "facility_fee_per_ticket", "ticketing_fee_rate",
             "door_split_from_dollar_one", "template", "settled", "notes"], row,
        )) if row else dict(_SETTLEMENT_DEFAULTS)
        merged = _recompute_settlement(g.db, event_id, before)
        cur.execute(
            """INSERT INTO settlements (event_id, tickets_sold, gross, sales_tax_rate,
                       facility_fee_per_ticket, ticketing_fee_rate, expenses, door_split_from_dollar_one,
                       template, artist_payout, settled, notes)
               VALUES (%(event_id)s, %(tickets_sold)s, %(gross)s, %(sales_tax_rate)s,
                       %(facility_fee_per_ticket)s, %(ticketing_fee_rate)s, %(expenses)s,
                       %(door_split_from_dollar_one)s, %(template)s, %(artist_payout)s, %(settled)s, %(notes)s)
               ON CONFLICT (event_id) DO UPDATE SET
                   expenses = EXCLUDED.expenses, artist_payout = EXCLUDED.artist_payout, updated_at = now()""",
            {"event_id": event_id, **merged},
        )
        audit.record(g.db, g.viewer, "event", event_id, "settlement_expenses", {"line_count": len(lines)})
        return jsonify(ok=True, expenses=merged["expenses"], artist_payout=merged["artist_payout"])

    @app.put("/api/events/<int:event_id>/settlement_ticket_tiers")
    @require_booker
    def set_settlement_ticket_tiers(event_id):
        """Replaces the whole set, same convention as settlement_expenses
        and ticket_tiers -- a booker re-sends the full tier list each save.
        Triggers a settlement recompute afterward since tickets_sold/gross
        become a synced total of these rows (see _recompute_settlement)."""
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404

        body = request.get_json(silent=True) or {}
        raw = body.get("tiers")
        if not isinstance(raw, list):
            return jsonify(error="tiers_must_be_a_list"), 400

        tiers = []
        for entry in raw:
            if not isinstance(entry, dict) or not (entry.get("label") or "").strip():
                return jsonify(error="each_tier_needs_a_label"), 400
            price, ok1 = _parse_number(entry.get("price"))
            sold_raw = entry.get("sold")
            if sold_raw in (None, ""):
                sold, ok2 = None, True
            else:
                try:
                    sold, ok2 = int(sold_raw), True
                except (TypeError, ValueError):
                    sold, ok2 = None, False
            if not (ok1 and ok2):
                return jsonify(error="invalid_amount"), 400
            source = entry.get("source") if entry.get("source") in ("manual", "etix") else "manual"
            tiers.append((entry["label"].strip(), price, sold, source))

        cur.execute("DELETE FROM settlement_ticket_tiers WHERE event_id = %s", (event_id,))
        for i, (label, price, sold, source) in enumerate(tiers):
            cur.execute(
                "INSERT INTO settlement_ticket_tiers (event_id, label, price, sold, source, sort_order) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (event_id, label, price, sold, source, i),
            )

        cur.execute(
            "SELECT tickets_sold, gross, sales_tax_rate, facility_fee_per_ticket, ticketing_fee_rate, "
            "door_split_from_dollar_one, template, settled, notes FROM settlements WHERE event_id = %s",
            (event_id,),
        )
        row = cur.fetchone()
        before = dict(zip(
            ["tickets_sold", "gross", "sales_tax_rate", "facility_fee_per_ticket", "ticketing_fee_rate",
             "door_split_from_dollar_one", "template", "settled", "notes"], row,
        )) if row else dict(_SETTLEMENT_DEFAULTS)
        merged = _recompute_settlement(g.db, event_id, before)
        cur.execute(
            """INSERT INTO settlements (event_id, tickets_sold, gross, sales_tax_rate,
                       facility_fee_per_ticket, ticketing_fee_rate, expenses, door_split_from_dollar_one,
                       template, artist_payout, settled, notes)
               VALUES (%(event_id)s, %(tickets_sold)s, %(gross)s, %(sales_tax_rate)s,
                       %(facility_fee_per_ticket)s, %(ticketing_fee_rate)s, %(expenses)s,
                       %(door_split_from_dollar_one)s, %(template)s, %(artist_payout)s, %(settled)s, %(notes)s)
               ON CONFLICT (event_id) DO UPDATE SET
                   tickets_sold = EXCLUDED.tickets_sold, gross = EXCLUDED.gross,
                   expenses = EXCLUDED.expenses, artist_payout = EXCLUDED.artist_payout, updated_at = now()""",
            {"event_id": event_id, **merged},
        )
        audit.record(g.db, g.viewer, "event", event_id, "settlement_ticket_tiers", {"tier_count": len(tiers)})
        return jsonify(ok=True, tickets_sold=merged["tickets_sold"], gross=merged["gross"],
                       expenses=merged["expenses"], artist_payout=merged["artist_payout"])

    @app.post("/api/events/<int:event_id>/settlement_ticket_tiers/pull_etix")
    @require_booker
    def pull_etix_ticket_tiers(event_id):
        """A per-tier breakdown, distinct from the plain ticket_sales pull
        (POST .../ticket_sales/pull_etix) -- that one gives a single
        aggregate count/gross; this returns Advance/Day-of-Advance/etc as
        separate lines with their own sold count and price, built from
        real per-ticket order data (see etix.get_price_breakdown for why
        that's used instead of Etix's Settlement API). Does NOT write
        anything to the DB -- the settlement editor merges the result into
        its in-progress draft and only settlement_ticket_tiers's own PUT
        persists it, same as every other field on this sheet."""
        cur = g.db.cursor()
        cur.execute(
            "SELECT e.show_date, v.name FROM events e JOIN venues v ON v.id = e.venue_id WHERE e.id = %s",
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        show_date, venue_name = row
        try:
            performance = etix._find_public_event(venue_name, show_date) or etix._find_private_event(venue_name, show_date)
            if performance is None:
                return jsonify(error="no_matching_etix_event"), 404
            tiers = etix.get_price_breakdown(performance["id"])
        except Exception as e:
            return jsonify(error="etix_lookup_failed", detail=str(e)), 502
        audit.record(g.db, g.viewer, "event", event_id, "pull_etix_ticket_tiers", {"tier_count": len(tiers)})
        return jsonify(ok=True, tiers=tiers)

    @app.put("/api/events/<int:event_id>/ticket_tiers")
    @require_booker
    def set_ticket_tiers(event_id):
        """Replaces the whole set — same pattern as person_roles. A booker
        editing prices re-sends the full tier list each time rather than
        this endpoint tracking incremental add/remove/reorder."""
        body = request.get_json(silent=True) or {}
        tiers = body.get("tiers")
        if not isinstance(tiers, list):
            return jsonify(error="tiers_must_be_a_list"), 400

        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404

        cleaned = []
        for t in tiers:
            label = (t.get("label") or "").strip() if isinstance(t, dict) else ""
            if not label:
                return jsonify(error="every_tier_needs_a_label"), 400
            price = t.get("price")
            if price in (None, ""):
                price = None
            else:
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    return jsonify(error="invalid_price"), 400
            cleaned.append((label, price))

        cur.execute("DELETE FROM ticket_tiers WHERE event_id = %s", (event_id,))
        for i, (label, price) in enumerate(cleaned):
            cur.execute(
                "INSERT INTO ticket_tiers (event_id, label, price, sort_order) VALUES (%s, %s, %s, %s)",
                (event_id, label, price, i),
            )
        audit.record(g.db, g.viewer, "event", event_id, "set_ticket_tiers",
                     {"tiers": [{"label": l, "price": p} for l, p in cleaned]})
        return jsonify(ok=True)

    @app.post("/api/events/<int:event_id>/ticket_sales")
    @require_booker
    def log_ticket_sale(event_id):
        """One day's snapshot, upserted -- re-logging the same date just
        corrects that day's count rather than creating a duplicate."""
        body = request.get_json(silent=True) or {}
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        try:
            sale_date = date.fromisoformat(body.get("sale_date") or "")
        except ValueError:
            return jsonify(error="invalid_sale_date"), 400
        try:
            tickets_sold = int(body.get("tickets_sold"))
            if tickets_sold < 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify(error="invalid_tickets_sold"), 400
        # gross explicitly cleared on a manual entry -- it's the etix
        # snapshot's own figure, tied to whatever count etix reported; a
        # booker overriding that count by hand invalidates it, so it
        # shouldn't keep sitting next to a now-different number.
        cur.execute(
            "INSERT INTO ticket_sales (event_id, sale_date, tickets_sold, gross, source) "
            "VALUES (%s, %s, %s, NULL, 'manual') "
            "ON CONFLICT (event_id, sale_date) DO UPDATE SET tickets_sold = %s, gross = NULL, source = 'manual'",
            (event_id, sale_date, tickets_sold, tickets_sold),
        )
        audit.record(g.db, g.viewer, "event", event_id, "log_ticket_sale",
                     {"sale_date": str(sale_date), "tickets_sold": tickets_sold})
        return jsonify(ok=True), 201

    @app.delete("/api/events/<int:event_id>/ticket_sales/<sale_date>")
    @require_booker
    def delete_ticket_sale(event_id, sale_date):
        try:
            parsed_date = date.fromisoformat(sale_date)
        except ValueError:
            return jsonify(error="invalid_sale_date"), 400
        cur = g.db.cursor()
        cur.execute("DELETE FROM ticket_sales WHERE event_id = %s AND sale_date = %s", (event_id, parsed_date))
        audit.record(g.db, g.viewer, "event", event_id, "delete_ticket_sale", {"sale_date": sale_date})
        return jsonify(ok=True)

    @app.put("/api/events/<int:event_id>/artists")
    @require_booker
    def set_event_artists(event_id):
        """Replaces a show's whole bill — same pattern as ticket_tiers.
        Each act's confirmed flag travels with it in the same payload, so
        this also carries any confirm/unconfirm toggles made on acts that
        weren't persisted yet (a brand-new act added this editing session)."""
        body = request.get_json(silent=True) or {}
        acts, err = _parse_acts(body.get("artists"))
        if err:
            return jsonify(error=err), 400
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        _replace_event_artists(g.db, event_id, acts)
        audit.record(g.db, g.viewer, "event", event_id, "set_artists", {"artists": acts})
        return jsonify(ok=True)

    @app.patch("/api/event_artists/<int:event_artist_id>")
    @require_booker
    def toggle_event_artist(event_artist_id):
        """The double-click-to-confirm action on one act — saved
        immediately, same as a task checkbox, rather than waiting for the
        main Save button. Only reachable on an act that's already been
        persisted at least once (has its own id)."""
        body = request.get_json(silent=True) or {}
        if "confirmed" not in body:
            return jsonify(error="confirmed_required"), 400
        cur = g.db.cursor()
        cur.execute("SELECT event_id FROM event_artists WHERE id = %s", (event_artist_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        event_id = row[0]
        confirmed = bool(body["confirmed"])
        cur.execute("UPDATE event_artists SET confirmed = %s WHERE id = %s", (confirmed, event_artist_id))
        audit.record(g.db, g.viewer, "event", event_id, "confirm_artist",
                     {"event_artist_id": event_artist_id, "confirmed": confirmed})
        return jsonify(ok=True)

    @app.post("/api/events/<int:event_id>/tasks")
    @require_booker
    def add_task(event_id):
        body = request.get_json(silent=True) or {}
        label = (body.get("label") or "").strip()
        if not label:
            return jsonify(error="label_required"), 400
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM event_tasks WHERE event_id = %s", (event_id,))
        next_order = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO event_tasks (event_id, label, sort_order) VALUES (%s, %s, %s) RETURNING id",
            (event_id, label, next_order),
        )
        task_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "event", event_id, "add_task", {"label": label})
        return jsonify(id=task_id), 201

    @app.patch("/api/tasks/<int:task_id>")
    @require_booker
    def update_task(task_id):
        body = request.get_json(silent=True) or {}
        cur = g.db.cursor()
        cur.execute("SELECT event_id FROM event_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        event_id = row[0]

        updates = {}
        if "done" in body:
            updates["done"] = bool(body["done"])
        if "owner_person_id" in body:
            v = body["owner_person_id"]
            updates["owner_person_id"] = None if v in (None, "") else v
        if not updates:
            return jsonify(error="no_fields_to_update"), 400

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(f"UPDATE event_tasks SET {set_clause} WHERE id = %s", list(updates.values()) + [task_id])
        audit.record(g.db, g.viewer, "event", event_id, "update_task",
                     {"task_id": task_id, **{k: audit.jsonable(v) for k, v in updates.items()}})
        return jsonify(ok=True)

    @app.delete("/api/tasks/<int:task_id>")
    @require_booker
    def delete_task(task_id):
        cur = g.db.cursor()
        cur.execute("SELECT event_id FROM event_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        event_id = row[0]
        cur.execute("DELETE FROM event_tasks WHERE id = %s", (task_id,))
        audit.record(g.db, g.viewer, "event", event_id, "delete_task", {"task_id": task_id})
        return jsonify(ok=True)

    def _file_row(row):
        return {
            "id": row[0], "event_id": row[1], "filename": row[2], "content_type": row[3],
            "size_bytes": row[4], "uploaded_by": row[5], "uploaded_by_name": row[6],
            "created_at": row[7].isoformat(), "venue_id": row[8], "folder_id": row[9],
        }

    @app.get("/api/files")
    @require_booker
    def list_files():
        # ?event_id=X scopes to one show's folder (the show editor's Files
        # section); no filter returns everything, which is what the Files
        # tab needs to build its folder-per-show (and per-venue) view
        # without a second round trip per folder.
        event_id = request.args.get("event_id", type=int)
        cur = g.db.cursor()
        if "event_id" in request.args:
            cur.execute(
                """SELECT f.id, f.event_id, f.filename, f.content_type, f.size_bytes,
                          f.uploaded_by, p.name, f.created_at, f.venue_id, f.folder_id
                   FROM event_files f LEFT JOIN people p ON p.id = f.uploaded_by
                   WHERE f.event_id IS NOT DISTINCT FROM %s
                   ORDER BY f.created_at DESC""",
                (event_id,),
            )
        else:
            cur.execute(
                """SELECT f.id, f.event_id, f.filename, f.content_type, f.size_bytes,
                          f.uploaded_by, p.name, f.created_at, f.venue_id, f.folder_id
                   FROM event_files f LEFT JOIN people p ON p.id = f.uploaded_by
                   ORDER BY f.created_at DESC"""
            )
        return jsonify(files=[_file_row(r) for r in cur.fetchall()])

    @app.get("/api/file_folders")
    @require_booker
    def list_file_folders():
        cur = g.db.cursor()
        cur.execute("SELECT id, name, year FROM file_folders ORDER BY name")
        return jsonify(folders=[{"id": r[0], "name": r[1], "year": r[2]} for r in cur.fetchall()])

    @app.post("/api/file_folders")
    @require_booker
    def create_file_folder():
        # "+ New folder" lives inside whichever section you're already
        # looking at (General or a specific year) and files the new folder
        # there -- not always at the top level regardless of context
        # (Broc, 2026-09-24: "if i need a new folder in 2027 folder it
        # creates there").
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify(error="name_required"), 400
        year = body.get("year") or None
        cur = g.db.cursor()
        cur.execute("INSERT INTO file_folders (name, year, created_by) VALUES (%s, %s, %s) RETURNING id",
                    (name, year, g.viewer.id))
        folder_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "file_folder", folder_id, "create", {"name": name, "year": year})
        return jsonify(id=folder_id), 201

    @app.delete("/api/file_folders/<int:folder_id>")
    @require_booker
    def delete_file_folder(folder_id):
        cur = g.db.cursor()
        cur.execute("SELECT name FROM file_folders WHERE id = %s", (folder_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        cur.execute("DELETE FROM file_folders WHERE id = %s", (folder_id,))
        audit.record(g.db, g.viewer, "file_folder", folder_id, "delete", {"name": row[0]})
        return jsonify(ok=True)

    @app.post("/api/files/upload-url")
    @require_booker
    def create_upload_url():
        body = request.get_json(silent=True) or {}
        filename = (body.get("filename") or "").strip()
        if not filename:
            return jsonify(error="filename_required"), 400
        event_id = body.get("event_id")
        venue_id = body.get("venue_id") or None
        folder_id = body.get("folder_id") or None
        content_type = body.get("content_type") or "application/octet-stream"
        size_bytes = body.get("size_bytes")

        cur = g.db.cursor()
        if event_id is not None:
            cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
            if cur.fetchone() is None:
                return jsonify(error="event_not_found"), 404
        if venue_id is not None:
            cur.execute("SELECT 1 FROM venues WHERE id = %s", (venue_id,))
            if cur.fetchone() is None:
                return jsonify(error="unknown_venue"), 400
        if folder_id is not None:
            cur.execute("SELECT 1 FROM file_folders WHERE id = %s", (folder_id,))
            if cur.fetchone() is None:
                return jsonify(error="unknown_folder"), 400

        storage_key = storage.new_storage_key(event_id, filename, venue_id, folder_id)
        cur.execute(
            """INSERT INTO event_files (event_id, venue_id, folder_id, filename, storage_key, content_type, size_bytes, uploaded_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (event_id, venue_id, folder_id, filename, storage_key, content_type, size_bytes, g.viewer.id),
        )
        file_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "event" if event_id else "file", event_id or file_id,
                     "upload_file", {"filename": filename, "file_id": file_id, "venue_id": venue_id, "folder_id": folder_id})
        upload_url = storage.presign_upload(storage_key, content_type)
        return jsonify(id=file_id, upload_url=upload_url), 201

    @app.get("/api/files/<int:file_id>/download-url")
    @require_booker
    def get_download_url(file_id):
        cur = g.db.cursor()
        cur.execute("SELECT storage_key, filename FROM event_files WHERE id = %s", (file_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        storage_key, filename = row
        return jsonify(url=storage.presign_download(storage_key, filename))

    @app.delete("/api/files/<int:file_id>")
    @require_booker
    def delete_file(file_id):
        cur = g.db.cursor()
        cur.execute("SELECT event_id, storage_key, filename FROM event_files WHERE id = %s", (file_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        event_id, storage_key, filename = row
        cur.execute("DELETE FROM event_files WHERE id = %s", (file_id,))
        storage.delete_object(storage_key)
        audit.record(g.db, g.viewer, "event" if event_id else "file", event_id or file_id,
                     "delete_file", {"filename": filename, "file_id": file_id})
        return jsonify(ok=True)

    @app.get("/api/events/<int:event_id>/website")
    @require_booker
    def get_event_website(event_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        cur.execute("SELECT hero_file_id, blurb, published FROM event_website WHERE event_id = %s", (event_id,))
        row = cur.fetchone()
        hero_file_id, blurb, published = row if row else (None, None, False)
        return jsonify(hero_file_id=hero_file_id, blurb=blurb, published=published)

    @app.put("/api/events/<int:event_id>/website")
    @require_booker
    def set_event_website(event_id):
        body = request.get_json(silent=True) or {}
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        hero_file_id = body.get("hero_file_id") or None
        if hero_file_id is not None:
            cur.execute("SELECT 1 FROM event_files WHERE id = %s AND event_id = %s", (hero_file_id, event_id))
            if cur.fetchone() is None:
                return jsonify(error="unknown_file"), 400
        blurb = (body.get("blurb") or "").strip() or None
        published = bool(body.get("published"))
        cur.execute(
            "INSERT INTO event_website (event_id, hero_file_id, blurb, published, updated_at) "
            "VALUES (%s, %s, %s, %s, now()) "
            "ON CONFLICT (event_id) DO UPDATE SET hero_file_id = %s, blurb = %s, published = %s, updated_at = now()",
            (event_id, hero_file_id, blurb, published, hero_file_id, blurb, published),
        )
        audit.record(g.db, g.viewer, "event", event_id, "set_website", {"published": published})
        return jsonify(ok=True)

    @app.post("/api/events/<int:event_id>/website/blurb")
    @require_booker
    def generate_event_blurb(event_id):
        """A starting draft, not a final copy — always returned for the
        booker to read and edit before it's saved, never written directly
        to event_website.blurb by this endpoint itself."""
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return jsonify(error="anthropic_not_configured"), 400
        cur = g.db.cursor()
        cur.execute("SELECT show_date FROM events WHERE id = %s", (event_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        events = permissions.events_for(g.db, g.viewer, date_from=row[0], date_to=row[0])
        ev = next((e for e in events if e["id"] == event_id), None)
        acts = [a for a in (ev["artists"] if ev else []) if not a.get("declined")]
        if not acts:
            return jsonify(error="no_headliner"), 400
        headliner, support = acts[0]["name"], [a["name"] for a in acts[1:]]
        prompt = f"Write a short, exciting 2-3 sentence promotional blurb for a concert headlined by {headliner}"
        if support:
            prompt += f", with support from {', '.join(support)}"
        prompt += (f" at {ev['venue']} on {ev['show_date']}. "
                   "No hashtags, no emoji, no marketing clichés like 'don't miss out' — just compelling, "
                   "specific copy suitable for a venue's website.")
        try:
            blurb = _anthropic_complete(api_key, prompt)
        except Exception as e:
            return jsonify(error="blurb_generation_failed", detail=str(e)), 502
        return jsonify(blurb=blurb)

    @app.get("/api/events/<int:event_id>/messages")
    @require_booker
    def list_event_messages(event_id):
        """Fetched on demand when a show's editor opens — not bundled into
        /api/events like tasks/staff, since a thread can grow unbounded
        over a show's life and shouldn't bloat every calendar load."""
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        cur.execute(
            "SELECT m.id, m.body, m.created_at, m.person_id, p.name AS person_name, m.parent_message_id "
            "FROM event_messages m LEFT JOIN people p ON p.id = m.person_id "
            "WHERE m.event_id = %s ORDER BY m.created_at",
            (event_id,),
        )
        cols = [c.name for c in cur.description]
        messages = [dict(zip(cols, row)) for row in cur.fetchall()]

        if messages:
            ids = [m["id"] for m in messages]
            cur.execute(
                "SELECT a.message_id, a.person_id, p.name AS person_name, a.created_at "
                "FROM event_message_acks a JOIN people p ON p.id = a.person_id "
                "WHERE a.message_id = ANY(%(ids)s) ORDER BY a.created_at",
                {"ids": ids},
            )
            acols = [c.name for c in cur.description]
            acks_by_message: dict[int, list[dict]] = {}
            for row in cur.fetchall():
                d = dict(zip(acols, row))
                acks_by_message.setdefault(d.pop("message_id"), []).append(d)

            # Who a message is actually asking — an ack only means
            # something if it comes from one of these people (or from
            # anyone, if the message asked no one in particular).
            cur.execute(
                "SELECT message_id, person_id FROM event_message_mentions WHERE message_id = ANY(%(ids)s)",
                {"ids": ids},
            )
            mentioned_by_message: dict[int, list[int]] = {}
            for message_id, person_id in cur.fetchall():
                mentioned_by_message.setdefault(message_id, []).append(person_id)

            for m in messages:
                m["acks"] = acks_by_message.get(m["id"], [])
                m["mentioned_person_ids"] = mentioned_by_message.get(m["id"], [])

        # Opening this thread is what "reading" a mention means here — no
        # separate mark-as-read click.
        cur.execute(
            "UPDATE event_message_mentions SET read_at = now() "
            "WHERE person_id = %s AND read_at IS NULL "
            "AND message_id IN (SELECT id FROM event_messages WHERE event_id = %s)",
            (g.viewer.id, event_id),
        )
        return jsonify(messages=messages)

    @app.post("/api/messages/<int:message_id>/ack")
    @require_booker
    def ack_message(message_id):
        """A lightweight "seen this" — separate from a mention's read_at
        (private, per-recipient inbox state); an ack is public, so anyone
        opening the thread can see who's already acknowledged it and skip
        chasing them. Acking twice is a no-op, not an error.

        If the message @mentioned specific people, only THEY can ack it —
        otherwise "Cody, can you confirm the deposit" could be marked
        acknowledged by anyone else in the thread, which would tell Broc
        the wrong person answered. A message that asked no one in
        particular can be acked by any booker/owner."""
        cur = g.db.cursor()
        cur.execute("SELECT event_id FROM event_messages WHERE id = %s", (message_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        cur.execute("SELECT person_id FROM event_message_mentions WHERE message_id = %s", (message_id,))
        mentioned_ids = [r[0] for r in cur.fetchall()]
        if mentioned_ids and g.viewer.id not in mentioned_ids:
            return jsonify(error="not_the_person_being_asked"), 403
        cur.execute(
            "INSERT INTO event_message_acks (message_id, person_id) VALUES (%s, %s) "
            "ON CONFLICT (message_id, person_id) DO NOTHING",
            (message_id, g.viewer.id),
        )
        return jsonify(ok=True)

    def _record_mentions(conn, message_id, text):
        """@Name in a message body — matched against real booker/owner
        names (longest name first, so '@Cody Sizemore' doesn't also
        half-match a shorter 'Cody' if both existed). No autocomplete on
        the way in; this is a plain-text convention, not a rich editor."""
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM people WHERE access_level IN ('booker','owner') AND active")
        people = sorted(cur.fetchall(), key=lambda p: len(p[1]), reverse=True)
        lowered = text.lower()
        matched_ids = set()
        for person_id, name in people:
            if f"@{name.lower()}" in lowered and person_id != g.viewer.id:
                matched_ids.add(person_id)
        for person_id in matched_ids:
            cur.execute(
                "INSERT INTO event_message_mentions (message_id, person_id) VALUES (%s, %s)",
                (message_id, person_id),
            )

    @app.post("/api/events/<int:event_id>/messages")
    @require_booker
    def create_event_message(event_id):
        body = request.get_json(silent=True) or {}
        text = (body.get("body") or "").strip()
        if not text:
            return jsonify(error="body_required"), 400
        parent_message_id = body.get("parent_message_id") or None
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        if parent_message_id is not None:
            cur.execute("SELECT 1 FROM event_messages WHERE id = %s AND event_id = %s",
                        (parent_message_id, event_id))
            if cur.fetchone() is None:
                return jsonify(error="unknown_parent_message"), 400
        cur.execute(
            "INSERT INTO event_messages (event_id, person_id, body, parent_message_id) VALUES (%s, %s, %s, %s) RETURNING id",
            (event_id, g.viewer.id, text, parent_message_id),
        )
        message_id = cur.fetchone()[0]
        _record_mentions(g.db, message_id, text)
        audit.record(g.db, g.viewer, "event", event_id, "message", {"message_id": message_id})
        return jsonify(id=message_id), 201

    @app.get("/api/my_mentions")
    @require_booker
    def list_my_mentions():
        """Unread @mentions across every show, newest first — what the
        notification badge in the topbar is counting."""
        cur = g.db.cursor()
        cur.execute(
            "SELECT mm.id, e.id AS event_id, e.show_date, v.name AS venue, "
            "m.body, m.created_at, p.name AS from_name "
            "FROM event_message_mentions mm "
            "JOIN event_messages m ON m.id = mm.message_id "
            "JOIN events e ON e.id = m.event_id "
            "JOIN venues v ON v.id = e.venue_id "
            "LEFT JOIN people p ON p.id = m.person_id "
            "WHERE mm.person_id = %s AND mm.read_at IS NULL "
            "ORDER BY m.created_at DESC",
            (g.viewer.id,),
        )
        cols = [c.name for c in cur.description]
        return jsonify(mentions=[dict(zip(cols, row)) for row in cur.fetchall()])

    @app.get("/api/my_followups")
    @require_booker
    def list_my_followups():
        """Vision-board follow-ups assigned to me that are due or overdue —
        the other half of what the notification bell counts, alongside
        message mentions."""
        cur = g.db.cursor()
        cur.execute(
            "SELECT c.id, c.title, c.column_key, c.follow_up_date, a.name AS artist_name "
            "FROM vision_cards c LEFT JOIN artists a ON a.id = c.artist_id "
            "WHERE c.follow_up_person_id = %s AND c.follow_up_date <= CURRENT_DATE "
            "ORDER BY c.follow_up_date",
            (g.viewer.id,),
        )
        cols = [c.name for c in cur.description]
        return jsonify(followups=[dict(zip(cols, row)) for row in cur.fetchall()])

    @app.delete("/api/messages/<int:message_id>")
    @require_booker
    def delete_event_message(message_id):
        """A booker can remove their own message; only the owner can
        remove someone else's — same shape as the people-admin rank rule,
        just for moderation instead of access level."""
        cur = g.db.cursor()
        cur.execute("SELECT event_id, person_id FROM event_messages WHERE id = %s", (message_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        event_id, author_id = row
        if author_id != g.viewer.id and g.viewer.access_level != "owner":
            return jsonify(error="forbidden"), 403
        cur.execute("DELETE FROM event_messages WHERE id = %s", (message_id,))
        audit.record(g.db, g.viewer, "event", event_id, "delete_message", {"message_id": message_id})
        return jsonify(ok=True)

    @app.get("/api/hivemind")
    @require_auth
    def list_hivemind_ideas():
        """The one board in this app open to every access level — no
        pricing/hold/deal data lives here, so there's nothing for rule 1
        to gate. Top-level ideas newest first (most recent suggestion at
        the top of the board); each idea's own replies nested underneath
        it in normal oldest-first conversation order."""
        cur = g.db.cursor()
        cur.execute(
            "SELECT i.id, i.body, i.created_at, i.person_id, p.name AS person_name, i.parent_idea_id "
            "FROM hivemind_ideas i LEFT JOIN people p ON p.id = i.person_id "
            "ORDER BY i.created_at"
        )
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        by_id = {r["id"]: r for r in rows}
        for r in rows:
            r["replies"] = []
        ideas = []
        for r in rows:
            parent_id = r.pop("parent_idea_id")
            if parent_id is None:
                ideas.append(r)
            elif parent_id in by_id:
                by_id[parent_id]["replies"].append(r)
        ideas.sort(key=lambda i: i["created_at"], reverse=True)
        return jsonify(ideas=ideas)

    @app.post("/api/hivemind")
    @require_auth
    def create_hivemind_idea():
        body = request.get_json(silent=True) or {}
        text = (body.get("body") or "").strip()
        if not text:
            return jsonify(error="body_required"), 400
        parent_idea_id = body.get("parent_idea_id") or None
        cur = g.db.cursor()
        if parent_idea_id is not None:
            cur.execute("SELECT 1 FROM hivemind_ideas WHERE id = %s", (parent_idea_id,))
            if cur.fetchone() is None:
                return jsonify(error="unknown_parent_idea"), 400
        cur.execute(
            "INSERT INTO hivemind_ideas (person_id, body, parent_idea_id) VALUES (%s, %s, %s) RETURNING id",
            (g.viewer.id, text, parent_idea_id),
        )
        idea_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "hivemind", idea_id, "post", {})
        return jsonify(id=idea_id), 201

    @app.delete("/api/hivemind/<int:idea_id>")
    @require_auth
    def delete_hivemind_idea(idea_id):
        """Same moderation shape as event messages: you can remove your
        own idea/reply; only the owner can remove someone else's."""
        cur = g.db.cursor()
        cur.execute("SELECT person_id FROM hivemind_ideas WHERE id = %s", (idea_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        author_id = row[0]
        if author_id != g.viewer.id and g.viewer.access_level != "owner":
            return jsonify(error="forbidden"), 403
        cur.execute("DELETE FROM hivemind_ideas WHERE id = %s", (idea_id,))
        audit.record(g.db, g.viewer, "hivemind", idea_id, "delete", {})
        return jsonify(ok=True)

    _VISION_COLUMNS = ("idea", "in_progress", "follow_up")

    @app.get("/api/vision_cards")
    @require_booker
    def list_vision_cards():
        cur = g.db.cursor()
        cur.execute(
            "SELECT c.id, c.title, c.column_key, c.sort_order, c.artist_id, a.name AS artist_name, "
            "c.venue_id, cv.name AS venue_name, "
            "c.trigger_event_id, e.show_date AS trigger_show_date, v.name AS trigger_venue, "
            "c.notes, c.link, c.follow_up_date, c.follow_up_person_id, fp.name AS follow_up_person_name, "
            "c.created_by, c.created_at "
            "FROM vision_cards c "
            "LEFT JOIN artists a ON a.id = c.artist_id "
            "LEFT JOIN venues cv ON cv.id = c.venue_id "
            "LEFT JOIN events e ON e.id = c.trigger_event_id "
            "LEFT JOIN venues v ON v.id = e.venue_id "
            "LEFT JOIN people fp ON fp.id = c.follow_up_person_id "
            "ORDER BY c.column_key, c.sort_order"
        )
        cols = [c.name for c in cur.description]
        cards = [dict(zip(cols, row)) for row in cur.fetchall()]
        if cards:
            ids = [c["id"] for c in cards]
            cur.execute(
                "SELECT event_id, artist_id FROM event_artists WHERE event_id = ANY(%(ids)s)",
                {"ids": [c["trigger_event_id"] for c in cards if c["trigger_event_id"]] or [0]},
            )
            bill_by_event: dict[int, list[int]] = {}
            for event_id, artist_id in cur.fetchall():
                bill_by_event.setdefault(event_id, []).append(artist_id)
            for c in cards:
                c["trigger_bill_artist_ids"] = bill_by_event.get(c["trigger_event_id"], []) if c["trigger_event_id"] else []
        return jsonify(cards=cards)

    def _parse_vision_card_fields(body, require_title=False):
        """One place deciding what a valid card looks like, used by both
        create and update. Returns (updates, error)."""
        updates = {}
        if "title" in body or require_title:
            title = (body.get("title") or "").strip()
            if not title:
                return None, "title_required"
            updates["title"] = title
        if "column_key" in body:
            if body["column_key"] not in _VISION_COLUMNS:
                return None, "invalid_column"
            updates["column_key"] = body["column_key"]
        if "artist_id" in body:
            updates["artist_id"] = body["artist_id"] or None
        if "venue_id" in body:
            venue_id = body["venue_id"] or None
            if venue_id is not None:
                cur = g.db.cursor()
                cur.execute("SELECT 1 FROM venues WHERE id = %s", (venue_id,))
                if cur.fetchone() is None:
                    return None, "unknown_venue"
            updates["venue_id"] = venue_id
        if "trigger_event_id" in body:
            updates["trigger_event_id"] = body["trigger_event_id"] or None
        if "notes" in body:
            updates["notes"] = (body["notes"] or "").strip() or None
        if "link" in body:
            updates["link"] = (body["link"] or "").strip() or None
        if "follow_up_date" in body:
            val = body["follow_up_date"]
            if val in (None, ""):
                updates["follow_up_date"] = None
            else:
                try:
                    updates["follow_up_date"] = date.fromisoformat(val)
                except ValueError:
                    return None, "invalid_follow_up_date"
        if "follow_up_person_id" in body:
            person_id = body["follow_up_person_id"] or None
            if person_id is not None:
                # Crew never sees the Vision Board at all (require_booker on
                # every route here), so assigning one a follow-up would be a
                # notification nobody could ever act on.
                cur = g.db.cursor()
                cur.execute(
                    "SELECT 1 FROM people WHERE id = %s AND active AND access_level != 'crew'",
                    (person_id,),
                )
                if cur.fetchone() is None:
                    return None, "unknown_person"
            updates["follow_up_person_id"] = person_id
        return updates, None

    @app.post("/api/vision_cards")
    @require_booker
    def create_vision_card():
        body = request.get_json(silent=True) or {}
        updates, err = _parse_vision_card_fields(body, require_title=True)
        if err:
            return jsonify(error=err), 400
        title = updates.pop("title")
        column_key = updates.pop("column_key", "idea")
        cur = g.db.cursor()
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM vision_cards WHERE column_key = %s", (column_key,))
        sort_order = cur.fetchone()[0]
        cols = ["title", "column_key", "sort_order", "created_by"] + list(updates.keys())
        vals = [title, column_key, sort_order, g.viewer.id] + list(updates.values())
        cur.execute(
            f"INSERT INTO vision_cards ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(vals))}) RETURNING id",
            vals,
        )
        card_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "vision_card", card_id, "create", {"title": title})
        return jsonify(id=card_id), 201

    @app.patch("/api/vision_cards/<int:card_id>")
    @require_booker
    def update_vision_card(card_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM vision_cards WHERE id = %s", (card_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        body = request.get_json(silent=True) or {}
        updates, err = _parse_vision_card_fields(body)
        if err:
            return jsonify(error=err), 400
        if not updates:
            return jsonify(error="no_fields_to_update"), 400
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(f"UPDATE vision_cards SET {set_clause}, updated_at = now() WHERE id = %s",
                    list(updates.values()) + [card_id])
        audit.record(g.db, g.viewer, "vision_card", card_id, "update",
                     {k: audit.jsonable(v) for k, v in updates.items()})
        return jsonify(ok=True)

    @app.post("/api/vision_cards/reorder")
    @require_booker
    def reorder_vision_cards():
        """Persists a drag: the client sends the full, final ordered list
        of card ids for whichever column(s) changed. Simpler and more
        robust than trying to compute an insertion index server-side —
        the client already knows the exact order it just rendered."""
        body = request.get_json(silent=True) or {}
        column_key = body.get("column_key")
        card_ids = body.get("card_ids")
        if column_key not in _VISION_COLUMNS:
            return jsonify(error="invalid_column"), 400
        if not isinstance(card_ids, list) or not card_ids:
            return jsonify(error="card_ids_required"), 400
        cur = g.db.cursor()
        for i, card_id in enumerate(card_ids):
            cur.execute(
                "UPDATE vision_cards SET column_key = %s, sort_order = %s WHERE id = %s",
                (column_key, i, card_id),
            )
        return jsonify(ok=True)

    @app.delete("/api/vision_cards/<int:card_id>")
    @require_booker
    def delete_vision_card(card_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM vision_cards WHERE id = %s", (card_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        cur.execute("DELETE FROM vision_cards WHERE id = %s", (card_id,))
        audit.record(g.db, g.viewer, "vision_card", card_id, "delete", None)
        return jsonify(ok=True)

    _OFFER_COLUMNS = ("mao", "needed", "sent", "confirmed")

    @app.get("/api/offers")
    @require_booker
    def list_offers():
        cur = g.db.cursor()
        cur.execute(
            "SELECT o.id, o.title, o.column_key, o.sort_order, o.dead, "
            "o.artist_id, a.name AS artist_name, o.venue_id, v.name AS venue_name, "
            "o.notes, o.link, o.created_by, o.created_at, "
            "o.deal_type, o.guarantee, o.backend_pct, o.template, o.event_id "
            "FROM offers o "
            "LEFT JOIN artists a ON a.id = o.artist_id "
            "LEFT JOIN venues v ON v.id = o.venue_id "
            "ORDER BY o.column_key, o.sort_order"
        )
        cols = [c.name for c in cur.description]
        offers = [dict(zip(cols, row)) for row in cur.fetchall()]
        if not offers:
            return jsonify(offers=offers)

        ids = [o["id"] for o in offers]
        cur.execute(
            "SELECT offer_id, id, label, budget, sort_order FROM offer_expenses "
            "WHERE offer_id = ANY(%(ids)s) ORDER BY sort_order",
            {"ids": ids},
        )
        cols = [c.name for c in cur.description]
        expenses_by_offer: dict[int, list[dict]] = {}
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            expenses_by_offer.setdefault(d.pop("offer_id"), []).append(d)

        cur.execute(
            "SELECT offer_id, id, label, price, capacity, sort_order FROM offer_ticket_tiers "
            "WHERE offer_id = ANY(%(ids)s) ORDER BY sort_order",
            {"ids": ids},
        )
        cols = [c.name for c in cur.description]
        tiers_by_offer: dict[int, list[dict]] = {}
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            tiers_by_offer.setdefault(d.pop("offer_id"), []).append(d)

        for o in offers:
            o["expenses"] = expenses_by_offer.get(o["id"], [])
            o["ticket_tiers"] = tiers_by_offer.get(o["id"], [])
        return jsonify(offers=offers)

    def _parse_offer_fields(body, require_title=False):
        """One place deciding what a valid offer looks like, used by both
        create and update -- same shape as _parse_vision_card_fields."""
        updates = {}
        if "title" in body or require_title:
            title = (body.get("title") or "").strip()
            if not title:
                return None, "title_required"
            updates["title"] = title
        if "column_key" in body:
            if body["column_key"] not in _OFFER_COLUMNS:
                return None, "invalid_column"
            updates["column_key"] = body["column_key"]
        if "artist_id" in body:
            updates["artist_id"] = body["artist_id"] or None
        if "venue_id" in body:
            venue_id = body["venue_id"] or None
            if venue_id is not None:
                cur = g.db.cursor()
                cur.execute("SELECT 1 FROM venues WHERE id = %s", (venue_id,))
                if cur.fetchone() is None:
                    return None, "unknown_venue"
            updates["venue_id"] = venue_id
        if "notes" in body:
            updates["notes"] = (body["notes"] or "").strip() or None
        if "link" in body:
            updates["link"] = (body["link"] or "").strip() or None
        if "dead" in body:
            updates["dead"] = bool(body["dead"])
        if "deal_type" in body:
            updates["deal_type"] = body["deal_type"] or None
        for key in ("guarantee", "backend_pct"):
            if key in body:
                parsed, ok = _parse_number(body[key])
                if not ok:
                    return None, f"invalid_{key}"
                updates[key] = parsed
        if "template" in body:
            if body["template"] not in ("simple", "detailed"):
                return None, "invalid_template"
            updates["template"] = body["template"]
        return updates, None

    @app.post("/api/offers")
    @require_booker
    def create_offer():
        body = request.get_json(silent=True) or {}
        updates, err = _parse_offer_fields(body, require_title=True)
        if err:
            return jsonify(error=err), 400
        if updates.get("artist_id"):
            cur = g.db.cursor()
            cur.execute("SELECT 1 FROM artists WHERE id = %s", (updates["artist_id"],))
            if cur.fetchone() is None:
                return jsonify(error="unknown_artist"), 400
        title = updates.pop("title")
        column_key = updates.pop("column_key", "mao")
        cur = g.db.cursor()
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM offers WHERE column_key = %s", (column_key,))
        sort_order = cur.fetchone()[0]
        cols = ["title", "column_key", "sort_order", "created_by"] + list(updates.keys())
        vals = [title, column_key, sort_order, g.viewer.id] + list(updates.values())
        cur.execute(
            f"INSERT INTO offers ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(vals))}) RETURNING id",
            vals,
        )
        offer_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "offer", offer_id, "create", {"title": title, "column_key": column_key})
        return jsonify(id=offer_id), 201

    @app.patch("/api/offers/<int:offer_id>")
    @require_booker
    def update_offer(offer_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM offers WHERE id = %s", (offer_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        body = request.get_json(silent=True) or {}
        updates, err = _parse_offer_fields(body)
        if err:
            return jsonify(error=err), 400
        if not updates:
            return jsonify(error="no_fields_to_update"), 400
        if updates.get("artist_id"):
            cur.execute("SELECT 1 FROM artists WHERE id = %s", (updates["artist_id"],))
            if cur.fetchone() is None:
                return jsonify(error="unknown_artist"), 400
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(f"UPDATE offers SET {set_clause}, updated_at = now() WHERE id = %s",
                    list(updates.values()) + [offer_id])
        audit.record(g.db, g.viewer, "offer", offer_id, "update",
                     {k: audit.jsonable(v) for k, v in updates.items()})
        return jsonify(ok=True)

    @app.post("/api/offers/reorder")
    @require_booker
    def reorder_offers():
        """Same shape as /api/vision_cards/reorder -- the client sends the
        full, final ordered list of ids for whichever board changed."""
        body = request.get_json(silent=True) or {}
        column_key = body.get("column_key")
        offer_ids = body.get("offer_ids")
        if column_key not in _OFFER_COLUMNS:
            return jsonify(error="invalid_column"), 400
        if not isinstance(offer_ids, list) or not offer_ids:
            return jsonify(error="offer_ids_required"), 400
        cur = g.db.cursor()
        for i, offer_id in enumerate(offer_ids):
            cur.execute(
                "UPDATE offers SET column_key = %s, sort_order = %s WHERE id = %s",
                (column_key, i, offer_id),
            )
        return jsonify(ok=True)

    @app.delete("/api/offers/<int:offer_id>")
    @require_booker
    def delete_offer(offer_id):
        cur = g.db.cursor()
        cur.execute("SELECT title FROM offers WHERE id = %s", (offer_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        cur.execute("DELETE FROM offers WHERE id = %s", (offer_id,))
        audit.record(g.db, g.viewer, "offer", offer_id, "delete", {"title": row[0]})
        return jsonify(ok=True)

    @app.put("/api/offers/<int:offer_id>/expenses")
    @require_booker
    def set_offer_expenses(offer_id):
        """Replaces the whole set, same convention as settlement_expenses
        -- budget only, since nothing's actually been spent at offer
        stage. Once this offer is linked to a real show, these budget
        lines seed that show's settlement_expenses on first open."""
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM offers WHERE id = %s", (offer_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404

        body = request.get_json(silent=True) or {}
        raw = body.get("lines")
        if not isinstance(raw, list):
            return jsonify(error="lines_must_be_a_list"), 400

        lines = []
        for entry in raw:
            if not isinstance(entry, dict) or not (entry.get("label") or "").strip():
                return jsonify(error="each_line_needs_a_label"), 400
            budget, ok = _parse_number(entry.get("budget"))
            if not ok:
                return jsonify(error="invalid_amount"), 400
            lines.append((entry["label"].strip(), budget))

        cur.execute("DELETE FROM offer_expenses WHERE offer_id = %s", (offer_id,))
        for i, (label, budget) in enumerate(lines):
            cur.execute(
                "INSERT INTO offer_expenses (offer_id, label, budget, sort_order) VALUES (%s, %s, %s, %s)",
                (offer_id, label, budget, i),
            )
        audit.record(g.db, g.viewer, "offer", offer_id, "offer_expenses", {"line_count": len(lines)})
        return jsonify(ok=True)

    @app.put("/api/offers/<int:offer_id>/ticket_tiers")
    @require_booker
    def set_offer_ticket_tiers(offer_id):
        """Replaces the whole set. Capacity, not sold -- an offer's tiers
        project a POSSIBLE gross at full sellout (Broc: "100 seats at $20
        that tier is $2000... 200 capacity $3000 possible gross on
        sellout"), not a real sold count, since the show hasn't happened
        yet (or in most cases isn't even confirmed)."""
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM offers WHERE id = %s", (offer_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404

        body = request.get_json(silent=True) or {}
        raw = body.get("tiers")
        if not isinstance(raw, list):
            return jsonify(error="tiers_must_be_a_list"), 400

        tiers = []
        for entry in raw:
            if not isinstance(entry, dict) or not (entry.get("label") or "").strip():
                return jsonify(error="each_tier_needs_a_label"), 400
            price, ok1 = _parse_number(entry.get("price"))
            capacity_raw = entry.get("capacity")
            if capacity_raw in (None, ""):
                capacity, ok2 = None, True
            else:
                try:
                    capacity, ok2 = int(capacity_raw), True
                except (TypeError, ValueError):
                    capacity, ok2 = None, False
            if not (ok1 and ok2):
                return jsonify(error="invalid_amount"), 400
            tiers.append((entry["label"].strip(), price, capacity))

        cur.execute("DELETE FROM offer_ticket_tiers WHERE offer_id = %s", (offer_id,))
        for i, (label, price, capacity) in enumerate(tiers):
            cur.execute(
                "INSERT INTO offer_ticket_tiers (offer_id, label, price, capacity, sort_order) "
                "VALUES (%s, %s, %s, %s, %s)",
                (offer_id, label, price, capacity, i),
            )
        audit.record(g.db, g.viewer, "offer", offer_id, "offer_ticket_tiers", {"tier_count": len(tiers)})
        return jsonify(ok=True)

    def _offer_pdf_bytes(conn, offer_id):
        """The one place an offer turns into a document -- used by both
        the on-demand download and the file-to-show-card step below, so
        they can never disagree about what an offer's PDF says."""
        cur = conn.cursor()
        cur.execute(
            "SELECT o.title, o.deal_type, o.guarantee, o.backend_pct, a.name, v.name "
            "FROM offers o LEFT JOIN artists a ON a.id = o.artist_id "
            "LEFT JOIN venues v ON v.id = o.venue_id WHERE o.id = %s",
            (offer_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        title, deal_type, guarantee, backend_pct, artist_name, venue_name = row
        cur.execute(
            "SELECT label, budget FROM offer_expenses WHERE offer_id = %s ORDER BY sort_order",
            (offer_id,),
        )
        expense_lines = cur.fetchall()
        expenses_total = sum(float(b) for _l, b in expense_lines if b is not None)
        cur.execute(
            "SELECT label, price, capacity FROM offer_ticket_tiers WHERE offer_id = %s ORDER BY sort_order",
            (offer_id,),
        )
        tier_lines = cur.fetchall()
        total_capacity = sum(c for _l, _p, c in tier_lines if c is not None)
        possible_gross = sum(float(p or 0) * (c or 0) for _l, p, c in tier_lines)
        return offer_pdf.generate(
            title=title, venue_name=venue_name, artist_name=artist_name,
            deal_type=deal_type, guarantee=guarantee, backend_pct=backend_pct,
            expense_lines=expense_lines, expenses_total=expenses_total,
            tier_lines=tier_lines, total_capacity=total_capacity, possible_gross=possible_gross,
        )

    OFFER_PDF_FILENAME = "Offer Sheet.pdf"

    @app.get("/api/offers/<int:offer_id>/pdf")
    @require_booker
    def get_offer_pdf(offer_id):
        """Regenerates fresh every call -- an offer changes hands and
        gets re-edited often during negotiation, and this is a low-volume
        action (sent to an agent a handful of times per offer), so always-
        current beats caching a possibly-stale copy."""
        pdf_bytes = _offer_pdf_bytes(g.db, offer_id)
        if pdf_bytes is None:
            return jsonify(error="not_found"), 404
        storage_key = f"offer-{offer_id}/{OFFER_PDF_FILENAME}"
        storage.put_bytes(storage_key, pdf_bytes, "application/pdf")
        return jsonify(url=storage.presign_download(storage_key, OFFER_PDF_FILENAME, disposition="attachment"))

    @app.post("/api/offers/<int:offer_id>/link_event")
    @require_booker
    def link_offer_to_event(offer_id):
        """"once the show is confirmed the offer gets tagged to the show
        card and also the file for the show" (Broc, 2026-09-26) -- sets
        the one link a settlement follows to pull its starting numbers
        from, and files a real copy of the offer PDF into that show's own
        Files section, same upsert-by-filename convention
        _write_settlement_file already uses for its own document."""
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM offers WHERE id = %s", (offer_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        body = request.get_json(silent=True) or {}
        event_id = body.get("event_id")
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="unknown_event"), 400

        cur.execute("UPDATE offers SET event_id = %s, updated_at = now() WHERE id = %s", (event_id, offer_id))

        pdf_bytes = _offer_pdf_bytes(g.db, offer_id)
        cur.execute(
            "SELECT id, storage_key FROM event_files WHERE event_id = %s AND filename = %s",
            (event_id, OFFER_PDF_FILENAME),
        )
        existing = cur.fetchone()
        if existing:
            file_id, storage_key = existing
            storage.put_bytes(storage_key, pdf_bytes, "application/pdf")
            cur.execute("UPDATE event_files SET size_bytes = %s WHERE id = %s", (len(pdf_bytes), file_id))
        else:
            storage_key = storage.new_storage_key(event_id, OFFER_PDF_FILENAME)
            storage.put_bytes(storage_key, pdf_bytes, "application/pdf")
            cur.execute(
                "INSERT INTO event_files (event_id, filename, storage_key, content_type, size_bytes, uploaded_by) "
                "VALUES (%s, %s, %s, 'application/pdf', %s, %s)",
                (event_id, OFFER_PDF_FILENAME, storage_key, len(pdf_bytes), g.viewer.id),
            )
        audit.record(g.db, g.viewer, "offer", offer_id, "link_event", {"event_id": event_id})
        return jsonify(ok=True)

    @app.get("/api/todos")
    @require_auth
    def list_todos():
        return jsonify(todos=permissions.todos_for(g.db, g.viewer))

    @app.post("/api/todos")
    @require_booker
    def create_todo():
        body = request.get_json(silent=True) or {}
        title = (body.get("title") or "").strip()
        if not title:
            return jsonify(error="title_required"), 400
        assigned_to = body.get("assigned_to") or None
        if assigned_to is not None:
            cur = g.db.cursor()
            cur.execute("SELECT 1 FROM people WHERE id = %s AND active", (assigned_to,))
            if cur.fetchone() is None:
                return jsonify(error="unknown_person"), 400
        venue_id = body.get("venue_id") or None
        if venue_id is not None:
            cur = g.db.cursor()
            cur.execute("SELECT 1 FROM venues WHERE id = %s", (venue_id,))
            if cur.fetchone() is None:
                return jsonify(error="unknown_venue"), 400
        due_date = None
        if body.get("due_date"):
            try:
                due_date = date.fromisoformat(body["due_date"])
            except ValueError:
                return jsonify(error="invalid_due_date"), 400
        cur = g.db.cursor()
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM todos WHERE venue_id IS NOT DISTINCT FROM %s", (venue_id,))
        sort_order = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO todos (title, assigned_to, venue_id, sort_order, due_date, notes, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (title, assigned_to, venue_id, sort_order, due_date, (body.get("notes") or "").strip() or None, g.viewer.id),
        )
        todo_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "todo", todo_id, "create", {"title": title, "assigned_to": assigned_to})
        return jsonify(id=todo_id), 201

    @app.post("/api/todos/reorder")
    @require_booker
    def reorder_todos():
        """Same shape as /api/vision_cards/reorder -- the client sends the
        full, final ordered list of ids for whichever board changed."""
        body = request.get_json(silent=True) or {}
        venue_id = body.get("venue_id") or None
        todo_ids = body.get("todo_ids")
        if venue_id is not None:
            cur = g.db.cursor()
            cur.execute("SELECT 1 FROM venues WHERE id = %s", (venue_id,))
            if cur.fetchone() is None:
                return jsonify(error="unknown_venue"), 400
        if not isinstance(todo_ids, list) or not todo_ids:
            return jsonify(error="todo_ids_required"), 400
        cur = g.db.cursor()
        for i, todo_id in enumerate(todo_ids):
            cur.execute(
                "UPDATE todos SET venue_id = %s, sort_order = %s WHERE id = %s",
                (venue_id, i, todo_id),
            )
        return jsonify(ok=True)

    @app.patch("/api/todos/<int:todo_id>")
    @require_auth
    def update_todo(todo_id):
        """Crew can toggle `done` and edit `notes` (a status update on
        their own progress), only on a todo assigned to them — everything
        else (title, assignment, due date) stays booker/owner-only, same
        shape as the assignment clock-in boundary. Booker/owner can edit
        any field on any todo."""
        cur = g.db.cursor()
        cur.execute("SELECT assigned_to FROM todos WHERE id = %s", (todo_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        assigned_to = row[0]
        body = request.get_json(silent=True) or {}

        if g.viewer.access_level not in ("booker", "owner"):
            if g.viewer.id != assigned_to:
                return jsonify(error="forbidden"), 403
            if set(body.keys()) - {"done", "notes"}:
                return jsonify(error="forbidden"), 403
            crew_updates = {}
            if "done" in body:
                crew_updates["done"] = bool(body["done"])
                crew_updates["completed_at"] = datetime.now(timezone.utc) if crew_updates["done"] else None
            if "notes" in body:
                crew_updates["notes"] = (body["notes"] or "").strip() or None
            if not crew_updates:
                return jsonify(error="no_fields_to_update"), 400
            set_clause = ", ".join(f"{k} = %s" for k in crew_updates)
            cur.execute(f"UPDATE todos SET {set_clause}, updated_at = now() WHERE id = %s",
                        list(crew_updates.values()) + [todo_id])
            audit.record(g.db, g.viewer, "todo", todo_id, "update",
                         {k: audit.jsonable(v) for k, v in crew_updates.items()})
            return jsonify(ok=True)

        updates = {}
        if "title" in body:
            title = (body["title"] or "").strip()
            if not title:
                return jsonify(error="title_required"), 400
            updates["title"] = title
        if "done" in body:
            updates["done"] = bool(body["done"])
            updates["completed_at"] = datetime.now(timezone.utc) if updates["done"] else None
        if "assigned_to" in body:
            assigned = body["assigned_to"] or None
            if assigned is not None:
                cur.execute("SELECT 1 FROM people WHERE id = %s AND active", (assigned,))
                if cur.fetchone() is None:
                    return jsonify(error="unknown_person"), 400
            updates["assigned_to"] = assigned
        if "venue_id" in body:
            venue = body["venue_id"] or None
            if venue is not None:
                cur.execute("SELECT 1 FROM venues WHERE id = %s", (venue,))
                if cur.fetchone() is None:
                    return jsonify(error="unknown_venue"), 400
            updates["venue_id"] = venue
        if "due_date" in body:
            val = body["due_date"]
            if val in (None, ""):
                updates["due_date"] = None
            else:
                try:
                    updates["due_date"] = date.fromisoformat(val)
                except ValueError:
                    return jsonify(error="invalid_due_date"), 400
        if "notes" in body:
            updates["notes"] = (body["notes"] or "").strip() or None
        if not updates:
            return jsonify(error="no_fields_to_update"), 400
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(f"UPDATE todos SET {set_clause}, updated_at = now() WHERE id = %s",
                    list(updates.values()) + [todo_id])
        audit.record(g.db, g.viewer, "todo", todo_id, "update", {k: audit.jsonable(v) for k, v in updates.items()})
        return jsonify(ok=True)

    @app.delete("/api/todos/<int:todo_id>")
    @require_booker
    def delete_todo(todo_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM todos WHERE id = %s", (todo_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        cur.execute("DELETE FROM todos WHERE id = %s", (todo_id,))
        audit.record(g.db, g.viewer, "todo", todo_id, "delete", None)
        return jsonify(ok=True)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
