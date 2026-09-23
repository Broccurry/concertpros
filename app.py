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

from flask import Flask, g, jsonify, render_template, request
from flask.json.provider import DefaultJSONProvider

from dotenv import load_dotenv

import artists as artists_module
import audit
import auth
import db
import permissions
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
        elif isinstance(item, str):
            name = item.strip()
            confirmed = declined = False
            notes = bill_role = guarantee = paid = walkups = set_time = set_time_end = None
        else:
            name = ""
            confirmed = declined = False
            notes = bill_role = guarantee = paid = walkups = set_time = set_time_end = None
        if not name:
            return None, "every_act_needs_a_name"
        acts.append({"name": name, "confirmed": confirmed, "declined": declined,
                     "notes": notes, "bill_role": bill_role, "set_time": set_time, "set_time_end": set_time_end,
                     "guarantee": guarantee, "paid": paid, "walkups": walkups})
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
                "paid=%s, walkups=%s, notes=%s, bill_role=%s, set_time=%s, set_time_end=%s WHERE id = %s",
                (act["confirmed"], act["declined"], sort_order, act["guarantee"], act["paid"],
                 act["walkups"], act["notes"], act["bill_role"], act["set_time"], act["set_time_end"],
                 existing_by_artist[artist_id]),
            )
        else:
            cur.execute(
                "INSERT INTO event_artists (event_id, artist_id, confirmed, declined, sort_order, "
                "guarantee, paid, walkups, notes, bill_role, set_time, set_time_end) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (event_id, artist_id, act["confirmed"], act["declined"], sort_order,
                 act["guarantee"], act["paid"], act["walkups"], act["notes"], act["bill_role"],
                 act["set_time"], act["set_time_end"]),
            )
        sort_order += 1
    cur.execute(
        "DELETE FROM event_artists WHERE event_id = %s AND artist_id != ALL(%s)",
        (event_id, list(seen_artist_ids) or [0]),
    )


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
    return updates, None


# A multi-day hold's fields split two ways on update: these describe THIS
# candidate date and always apply to the id in the URL. Everything else
# describes the eventual show and redirects to the group's anchor date —
# see _group_anchor_id.
PER_DATE_EVENT_FIELDS = {"venue_id", "status", "show_date", "doors", "show_time"}

_SHARED_CHILD_TABLES = ("event_artists", "ticket_tiers", "event_tasks", "assignments",
                        "settlements", "event_messages")
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

    def _public_shows(conn, event_ids=None):
        """The public site's own query — deliberately never permissions.
        events_for(), which is written for an authenticated Viewer and
        would need to be told, correctly, every single time, not to leak
        deal/hold/settlement columns to an anonymous visitor. A published
        show is the most restrictive tier there is, so it gets its own
        query that structurally cannot select those columns at all."""
        cur = conn.cursor()
        where = "ew.published = TRUE" + (" AND e.id = ANY(%(ids)s)" if event_ids is not None else "")
        cur.execute(
            f"""SELECT e.id, e.show_date, e.doors, e.ticket_link, v.name AS venue,
                       ew.blurb, ew.hero_file_id
                FROM events e
                JOIN event_website ew ON ew.event_id = e.id
                JOIN venues v ON v.id = e.venue_id
                WHERE {where}
                ORDER BY e.show_date""",
            {"ids": event_ids},
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
        return render_template("site_list.html", shows=_public_shows(g.db))

    @app.get("/site/<int:event_id>")
    def public_site_show(event_id):
        shows = _public_shows(g.db, event_ids=[event_id])
        if not shows:
            return render_template("site_404.html"), 404
        return render_template("site_show.html", show=shows[0])

    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True) or {}
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        cur = g.db.cursor()
        cur.execute(
            "SELECT id, password_hash, access_level FROM people WHERE lower(email) = %s AND active",
            (email,),
        )
        row = cur.fetchone()
        if row is None or row[1] is None or not auth.verify_password(password, row[1]):
            return jsonify(error="invalid_credentials"), 401
        person_id, _, access_level = row
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
        cur.execute("SELECT id, name FROM venues ORDER BY name")
        cols = [c.name for c in cur.description]
        return jsonify(venues=[dict(zip(cols, row)) for row in cur.fetchall()])

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
            "SELECT tier, genre, tags, location, instagram, facebook, website, spotify, notes, active "
            "FROM artists WHERE id = %s",
            (artist_id,),
        )
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        before = dict(zip(
            ["tier", "genre", "tags", "location", "instagram", "facebook", "website", "spotify", "notes", "active"], row))

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
            updates["genre"] = (body["genre"] or "").strip() or None
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
                      ticket_link, notes
               FROM events WHERE id = %s""",
            (event_id,),
        )
        row = cur.fetchone()
        before_cols = ["venue_id", "show_date", "doors", "show_time", "status",
                       "deal_type", "guarantee", "backend_pct", "deal_notes", "announce_date",
                       "onsale_date", "ticket_link", "notes"]
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

    @app.put("/api/events/<int:event_id>/settlement")
    @require_booker
    def upsert_settlement(event_id):
        """One row per event (settlements.event_id is its own primary
        key), so this is a plain upsert rather than separate create/update
        endpoints — there's no meaningful 'doesn't exist yet' state a
        caller needs to distinguish. Crew never reaches this at all
        (require_booker), and never sees the result either — events_for's
        crew branch doesn't join settlements in the first place."""
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404

        cur.execute(
            "SELECT tickets_sold, gross, expenses, artist_payout, settled, notes "
            "FROM settlements WHERE event_id = %s",
            (event_id,),
        )
        row = cur.fetchone()
        before_cols = ["tickets_sold", "gross", "expenses", "artist_payout", "settled", "notes"]
        # settled is NOT NULL DEFAULT FALSE on the table — default it the
        # same way here for a first-ever save, or the INSERT below fails.
        before = dict(zip(before_cols, row)) if row else {
            "tickets_sold": None, "gross": None, "expenses": None,
            "artist_payout": None, "settled": False, "notes": None,
        }

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
        for key in ("gross", "expenses", "artist_payout"):
            if key in body:
                v = body[key]
                if v in (None, ""):
                    values[key] = None
                else:
                    try:
                        values[key] = float(v)
                    except (TypeError, ValueError):
                        return jsonify(error=f"invalid_{key}"), 400
        if "settled" in body:
            values["settled"] = bool(body["settled"])
        if "notes" in body:
            values["notes"] = body["notes"]

        if not values:
            return jsonify(error="no_fields_to_update"), 400

        # Fill anything not sent this call from what's already stored, so a
        # partial save (just ticking "settled") doesn't null out the rest.
        merged = {**before, **values}
        cur.execute(
            """INSERT INTO settlements (event_id, tickets_sold, gross, expenses, artist_payout, settled, notes)
               VALUES (%(event_id)s, %(tickets_sold)s, %(gross)s, %(expenses)s, %(artist_payout)s,
                       %(settled)s, %(notes)s)
               ON CONFLICT (event_id) DO UPDATE SET
                   tickets_sold = EXCLUDED.tickets_sold, gross = EXCLUDED.gross,
                   expenses = EXCLUDED.expenses, artist_payout = EXCLUDED.artist_payout,
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
        return jsonify(ok=True)

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
        cur.execute(
            "INSERT INTO ticket_sales (event_id, sale_date, tickets_sold, source) "
            "VALUES (%s, %s, %s, 'manual') "
            "ON CONFLICT (event_id, sale_date) DO UPDATE SET tickets_sold = %s, source = 'manual'",
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
            "created_at": row[7].isoformat(),
        }

    @app.get("/api/files")
    @require_booker
    def list_files():
        # ?event_id=X scopes to one show's folder (the show editor's Files
        # section); no filter returns everything, which is what the Files
        # tab needs to build its folder-per-show view without a second
        # round trip per folder.
        event_id = request.args.get("event_id", type=int)
        cur = g.db.cursor()
        if "event_id" in request.args:
            cur.execute(
                """SELECT f.id, f.event_id, f.filename, f.content_type, f.size_bytes,
                          f.uploaded_by, p.name, f.created_at
                   FROM event_files f LEFT JOIN people p ON p.id = f.uploaded_by
                   WHERE f.event_id IS NOT DISTINCT FROM %s
                   ORDER BY f.created_at DESC""",
                (event_id,),
            )
        else:
            cur.execute(
                """SELECT f.id, f.event_id, f.filename, f.content_type, f.size_bytes,
                          f.uploaded_by, p.name, f.created_at
                   FROM event_files f LEFT JOIN people p ON p.id = f.uploaded_by
                   ORDER BY f.created_at DESC"""
            )
        return jsonify(files=[_file_row(r) for r in cur.fetchall()])

    @app.post("/api/files/upload-url")
    @require_booker
    def create_upload_url():
        body = request.get_json(silent=True) or {}
        filename = (body.get("filename") or "").strip()
        if not filename:
            return jsonify(error="filename_required"), 400
        event_id = body.get("event_id")
        content_type = body.get("content_type") or "application/octet-stream"
        size_bytes = body.get("size_bytes")

        cur = g.db.cursor()
        if event_id is not None:
            cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
            if cur.fetchone() is None:
                return jsonify(error="event_not_found"), 404

        storage_key = storage.new_storage_key(event_id, filename)
        cur.execute(
            """INSERT INTO event_files (event_id, filename, storage_key, content_type, size_bytes, uploaded_by)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (event_id, filename, storage_key, content_type, size_bytes, g.viewer.id),
        )
        file_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "event" if event_id else "file", event_id or file_id,
                     "upload_file", {"filename": filename, "file_id": file_id})
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

    _VISION_COLUMNS = ("idea", "offer_sent")

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
        due_date = None
        if body.get("due_date"):
            try:
                due_date = date.fromisoformat(body["due_date"])
            except ValueError:
                return jsonify(error="invalid_due_date"), 400
        cur = g.db.cursor()
        cur.execute(
            "INSERT INTO todos (title, assigned_to, due_date, notes, created_by) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (title, assigned_to, due_date, (body.get("notes") or "").strip() or None, g.viewer.id),
        )
        todo_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "todo", todo_id, "create", {"title": title, "assigned_to": assigned_to})
        return jsonify(id=todo_id), 201

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
        if "assigned_to" in body:
            assigned = body["assigned_to"] or None
            if assigned is not None:
                cur.execute("SELECT 1 FROM people WHERE id = %s AND active", (assigned,))
                if cur.fetchone() is None:
                    return jsonify(error="unknown_person"), 400
            updates["assigned_to"] = assigned
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
