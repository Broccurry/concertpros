"""ConcertPros Flask app.

Every route that returns event data goes through permissions.events_for()
— see CLAUDE.md rule 1. No route queries `events`, `settlements`,
`ticket_tiers`, or `event_tasks` directly; that logic lives in
permissions.py and only there.

No self-signup (CLAUDE.md): accounts are created with scripts/create_person.py,
run by whoever has database access, not through an HTTP endpoint.
"""
import os
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
from permissions import Viewer

VALID_HOLD_STATUSES = ("hold1", "hold2", "hold3", "confirmed")  # what a NEW show can be booked as
ALL_STATUSES = ("hold1", "hold2", "hold3", "confirmed", "complete", "dead")  # what an EXISTING show can move to
TASK_TEMPLATE = ("Website", "Marketing", "Offer", "Contract")  # spawned on every new booking

load_dotenv()  # local dev only — a no-op if .env doesn't exist (Railway sets real env vars directly)


def _parse_number(v):
    """None/""/missing all mean "not set" — a band's guarantee, paid, and
    walkups are all optional until the show's actually happened."""
    if v in (None, ""):
        return None, True
    try:
        return float(v), True
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
        elif isinstance(item, str):
            name, confirmed, guarantee, paid, walkups = item.strip(), False, None, None, None
        else:
            name, confirmed, guarantee, paid, walkups = "", False, None, None, None
        if not name:
            return None, "every_act_needs_a_name"
        acts.append({"name": name, "confirmed": confirmed, "guarantee": guarantee,
                     "paid": paid, "walkups": walkups})
    return acts, None


def _replace_event_artists(conn, event_id, acts):
    """Replaces a show's whole bill — same replace-the-set pattern as
    ticket_tiers. Each act is resolved independently via find_or_create,
    in order, so typing 'Foo Fighters' then 'Nirvana' always creates two
    acts, never one artist named 'Foo Fighters Nirvana'."""
    cur = conn.cursor()
    cur.execute("DELETE FROM event_artists WHERE event_id = %s", (event_id,))
    seen_artist_ids = set()
    sort_order = 0
    for act in acts:
        artist_id = artists_module.find_or_create(conn, act["name"])
        if artist_id in seen_artist_ids:
            continue
        seen_artist_ids.add(artist_id)
        cur.execute(
            "INSERT INTO event_artists (event_id, artist_id, confirmed, sort_order, guarantee, paid, walkups) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (event_id, artist_id, act["confirmed"], sort_order, act["guarantee"], act["paid"], act["walkups"]),
        )
        sort_order += 1

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


def _apply_pending_schema_additions():
    """TEMPORARY: the SSH tunnel used to run schema.sql by hand against
    production is down (Railway-side, unrelated to this app), so these two
    new tables get created here instead — idempotent, additive only, safe
    to run on every boot. Remove this once the tunnel's back and schema.sql
    has been applied the normal way."""
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vision_notes (
            id              SERIAL PRIMARY KEY,
            content         TEXT NOT NULL,
            target_quarter  INTEGER CHECK (target_quarter BETWEEN 1 AND 4),
            target_year     INTEGER,
            created_by      INTEGER REFERENCES people(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ma_offers (
            id              SERIAL PRIMARY KEY,
            title           TEXT NOT NULL,
            artist_id       INTEGER REFERENCES artists(id),
            notes           TEXT,
            link            TEXT,
            submitted_date  DATE NOT NULL DEFAULT CURRENT_DATE,
            follow_up_date  DATE,
            status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
            created_by      INTEGER REFERENCES people(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ma_offers_follow_up ON ma_offers(follow_up_date) WHERE status = 'open'")
    conn.commit()
    conn.close()


def create_app():
    app = Flask(__name__)
    app.json = ConcertProsJSONProvider(app)
    secure_cookies = os.environ.get("SECURE_COOKIES", "1") != "0"
    _apply_pending_schema_additions()

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
        return jsonify(id=g.viewer.id, name=name, email=email, access_level=access_level)

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
        cur.execute("SELECT tier, genre, tags, location FROM artists WHERE id = %s", (artist_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        before = dict(zip(["tier", "genre", "tags", "location"], row))

        body = request.get_json(silent=True) or {}
        updates = {}
        if "tier" in body:
            tier = body["tier"] or None
            if tier not in ("Local", "Regional", "National", None):
                return jsonify(error="invalid_tier"), 400
            updates["tier"] = tier
        if "genre" in body:
            updates["genre"] = (body["genre"] or "").strip() or None
        if "location" in body:
            updates["location"] = (body["location"] or "").strip() or None
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

        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM venues WHERE id = %s", (venue_id,))
        if cur.fetchone() is None:
            return jsonify(error="unknown_venue"), 400

        cur.execute(
            """INSERT INTO events (venue_id, show_date, status, created_by)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (venue_id, show_date, status, g.viewer.id),
        )
        event_id = cur.fetchone()[0]
        _replace_event_artists(g.db, event_id, acts)
        for i, label in enumerate(TASK_TEMPLATE):
            cur.execute(
                "INSERT INTO event_tasks (event_id, label, sort_order) VALUES (%s, %s, %s)",
                (event_id, label, i),
            )
        audit.record(g.db, g.viewer, "event", event_id, "create",
                     {"venue_id": venue_id, "acts": [a["name"] for a in acts],
                      "show_date": show_date_raw, "status": status})
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
        cur.execute(
            """SELECT venue_id, show_date, doors, show_time, status,
                      deal_type, guarantee, backend_pct, deal_notes, announce_date, onsale_date, notes
               FROM events WHERE id = %s""",
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        before_cols = ["venue_id", "show_date", "doors", "show_time", "status",
                       "deal_type", "guarantee", "backend_pct", "deal_notes", "announce_date",
                       "onsale_date", "notes"]
        before = dict(zip(before_cols, row))

        updates = {}
        if "venue_id" in body:
            cur.execute("SELECT 1 FROM venues WHERE id = %s", (body["venue_id"],))
            if cur.fetchone() is None:
                return jsonify(error="unknown_venue"), 400
            updates["venue_id"] = body["venue_id"]
        if "status" in body:
            if body["status"] not in ALL_STATUSES:
                return jsonify(error="invalid_status"), 400
            updates["status"] = body["status"]
        for key in ("deal_type", "deal_notes", "notes"):
            if key in body:
                updates[key] = body[key]
        for key in ("show_date", "announce_date", "onsale_date"):
            if key in body:
                val = body[key]
                if val in (None, ""):
                    updates[key] = None
                else:
                    try:
                        updates[key] = date.fromisoformat(val)
                    except ValueError:
                        return jsonify(error=f"invalid_{key}"), 400
        for key in ("doors", "show_time"):
            if key in body:
                val = body[key]
                if val in (None, ""):
                    updates[key] = None
                else:
                    try:
                        updates[key] = time.fromisoformat(val)
                    except ValueError:
                        return jsonify(error=f"invalid_{key}"), 400
        for key in ("guarantee", "backend_pct"):
            if key in body:
                val = body[key]
                if val in (None, ""):
                    updates[key] = None
                else:
                    try:
                        updates[key] = float(val)
                    except (TypeError, ValueError):
                        return jsonify(error=f"invalid_{key}"), 400

        if not updates:
            return jsonify(error="no_fields_to_update"), 400

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

        audit.record(
            g.db, g.viewer, "event", event_id, "update",
            {
                "before": {k: audit.jsonable(before.get(k)) for k in updates},
                "after": {k: audit.jsonable(v) for k, v in updates.items()},
            },
        )
        return jsonify(ok=True, version=result[0])

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
        body = request.get_json(silent=True) or {}
        role_id = body.get("role_id")
        person_id = body.get("person_id")
        if not role_id or not person_id:
            return jsonify(error="role_id and person_id are required"), 400

        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM events WHERE id = %s", (event_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        cur.execute("SELECT 1 FROM roles WHERE id = %s", (role_id,))
        if cur.fetchone() is None:
            return jsonify(error="unknown_role"), 400
        cur.execute("SELECT name FROM people WHERE id = %s AND active", (person_id,))
        person_row = cur.fetchone()
        if person_row is None:
            return jsonify(error="unknown_person"), 400

        cur.execute(
            "INSERT INTO assignments (event_id, role_id, person_id) VALUES (%s, %s, %s) RETURNING id",
            (event_id, role_id, person_id),
        )
        assignment_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "event", event_id, "assign",
                     {"role_id": role_id, "person_id": person_id, "person_name": person_row[0]})
        return jsonify(id=assignment_id), 201

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

    @app.get("/api/vision_notes")
    @require_booker
    def list_vision_notes():
        cur = g.db.cursor()
        cur.execute(
            "SELECT id, content, target_quarter, target_year, created_by, created_at "
            "FROM vision_notes ORDER BY target_year NULLS LAST, target_quarter NULLS LAST, created_at"
        )
        cols = [c.name for c in cur.description]
        return jsonify(notes=[dict(zip(cols, row)) for row in cur.fetchall()])

    def _parse_quarter_year(body):
        """quarter and year are a pair — both set (a real target) or both
        null (someday, no target yet). Returns (quarter, year, error)."""
        q, y = body.get("target_quarter"), body.get("target_year")
        if q in (None, ""):
            q = None
        else:
            try:
                q = int(q)
            except (TypeError, ValueError):
                return None, None, "invalid_target_quarter"
            if q not in (1, 2, 3, 4):
                return None, None, "invalid_target_quarter"
        if y in (None, ""):
            y = None
        else:
            try:
                y = int(y)
            except (TypeError, ValueError):
                return None, None, "invalid_target_year"
        if (q is None) != (y is None):
            return None, None, "target_quarter_and_year_go_together"
        return q, y, None

    @app.post("/api/vision_notes")
    @require_booker
    def create_vision_note():
        body = request.get_json(silent=True) or {}
        content = (body.get("content") or "").strip()
        if not content:
            return jsonify(error="content_required"), 400
        quarter, year, err = _parse_quarter_year(body)
        if err:
            return jsonify(error=err), 400
        cur = g.db.cursor()
        cur.execute(
            "INSERT INTO vision_notes (content, target_quarter, target_year, created_by) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (content, quarter, year, g.viewer.id),
        )
        note_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "vision_note", note_id, "create",
                     {"content": content, "target_quarter": quarter, "target_year": year})
        return jsonify(id=note_id), 201

    @app.patch("/api/vision_notes/<int:note_id>")
    @require_booker
    def update_vision_note(note_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM vision_notes WHERE id = %s", (note_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        body = request.get_json(silent=True) or {}
        updates = {}
        if "content" in body:
            content = (body["content"] or "").strip()
            if not content:
                return jsonify(error="content_required"), 400
            updates["content"] = content
        if "target_quarter" in body or "target_year" in body:
            quarter, year, err = _parse_quarter_year(body)
            if err:
                return jsonify(error=err), 400
            updates["target_quarter"] = quarter
            updates["target_year"] = year
        if not updates:
            return jsonify(error="no_fields_to_update"), 400
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(f"UPDATE vision_notes SET {set_clause}, updated_at = now() WHERE id = %s",
                    list(updates.values()) + [note_id])
        audit.record(g.db, g.viewer, "vision_note", note_id, "update",
                     {k: audit.jsonable(v) for k, v in updates.items()})
        return jsonify(ok=True)

    @app.delete("/api/vision_notes/<int:note_id>")
    @require_booker
    def delete_vision_note(note_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM vision_notes WHERE id = %s", (note_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        cur.execute("DELETE FROM vision_notes WHERE id = %s", (note_id,))
        audit.record(g.db, g.viewer, "vision_note", note_id, "delete", None)
        return jsonify(ok=True)

    @app.get("/api/ma_offers")
    @require_booker
    def list_ma_offers():
        cur = g.db.cursor()
        cur.execute(
            "SELECT o.id, o.title, o.artist_id, a.name AS artist_name, o.notes, o.link, "
            "o.submitted_date, o.follow_up_date, o.status "
            "FROM ma_offers o LEFT JOIN artists a ON a.id = o.artist_id "
            "ORDER BY (o.status = 'open') DESC, o.follow_up_date NULLS LAST, o.submitted_date DESC"
        )
        cols = [c.name for c in cur.description]
        return jsonify(offers=[dict(zip(cols, row)) for row in cur.fetchall()])

    @app.post("/api/ma_offers")
    @require_booker
    def create_ma_offer():
        body = request.get_json(silent=True) or {}
        title = (body.get("title") or "").strip()
        if not title:
            return jsonify(error="title_required"), 400
        submitted_raw = body.get("submitted_date")
        try:
            submitted = date.fromisoformat(submitted_raw) if submitted_raw else date.today()
        except ValueError:
            return jsonify(error="invalid_submitted_date"), 400
        follow_up_raw = body.get("follow_up_date")
        try:
            follow_up = date.fromisoformat(follow_up_raw) if follow_up_raw else None
        except ValueError:
            return jsonify(error="invalid_follow_up_date"), 400
        cur = g.db.cursor()
        cur.execute(
            "INSERT INTO ma_offers (title, artist_id, notes, link, submitted_date, follow_up_date, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (title, body.get("artist_id") or None, body.get("notes") or None, body.get("link") or None,
             submitted, follow_up, g.viewer.id),
        )
        offer_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "ma_offer", offer_id, "create", {"title": title})
        return jsonify(id=offer_id), 201

    @app.patch("/api/ma_offers/<int:offer_id>")
    @require_booker
    def update_ma_offer(offer_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM ma_offers WHERE id = %s", (offer_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        body = request.get_json(silent=True) or {}
        updates = {}
        if "title" in body:
            title = (body["title"] or "").strip()
            if not title:
                return jsonify(error="title_required"), 400
            updates["title"] = title
        if "artist_id" in body:
            updates["artist_id"] = body["artist_id"] or None
        if "notes" in body:
            updates["notes"] = body["notes"] or None
        if "link" in body:
            updates["link"] = body["link"] or None
        if "status" in body:
            if body["status"] not in ("open", "closed"):
                return jsonify(error="invalid_status"), 400
            updates["status"] = body["status"]
        if "submitted_date" in body:
            val = body["submitted_date"]
            if not val:
                return jsonify(error="submitted_date_required"), 400
            try:
                updates["submitted_date"] = date.fromisoformat(val)
            except ValueError:
                return jsonify(error="invalid_submitted_date"), 400
        if "follow_up_date" in body:
            val = body["follow_up_date"]
            if val in (None, ""):
                updates["follow_up_date"] = None
            else:
                try:
                    updates["follow_up_date"] = date.fromisoformat(val)
                except ValueError:
                    return jsonify(error="invalid_follow_up_date"), 400
        if not updates:
            return jsonify(error="no_fields_to_update"), 400
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(f"UPDATE ma_offers SET {set_clause}, updated_at = now() WHERE id = %s",
                    list(updates.values()) + [offer_id])
        audit.record(g.db, g.viewer, "ma_offer", offer_id, "update",
                     {k: audit.jsonable(v) for k, v in updates.items()})
        return jsonify(ok=True)

    @app.delete("/api/ma_offers/<int:offer_id>")
    @require_booker
    def delete_ma_offer(offer_id):
        cur = g.db.cursor()
        cur.execute("SELECT 1 FROM ma_offers WHERE id = %s", (offer_id,))
        if cur.fetchone() is None:
            return jsonify(error="not_found"), 404
        cur.execute("DELETE FROM ma_offers WHERE id = %s", (offer_id,))
        audit.record(g.db, g.viewer, "ma_offer", offer_id, "delete", None)
        return jsonify(ok=True)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
