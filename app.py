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

load_dotenv()  # local dev only — a no-op if .env doesn't exist (Railway sets real env vars directly)

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

    @app.post("/api/events")
    @require_booker
    def create_event():
        body = request.get_json(silent=True) or {}
        venue_id = body.get("venue_id")
        headliner = (body.get("headliner") or "").strip()
        show_date_raw = body.get("show_date")
        status = body.get("status", "hold1")

        if not venue_id or not headliner or not show_date_raw:
            return jsonify(error="venue_id, headliner, and show_date are required"), 400
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

        artist_id = artists_module.find_or_create(g.db, headliner)

        cur.execute(
            """INSERT INTO events (venue_id, artist_id, show_date, status, created_by)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (venue_id, artist_id, show_date, status, g.viewer.id),
        )
        event_id = cur.fetchone()[0]
        audit.record(g.db, g.viewer, "event", event_id, "create",
                     {"venue_id": venue_id, "headliner": headliner,
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
            """SELECT venue_id, artist_id, support, show_date, doors, show_time, status,
                      deal_type, guarantee, backend_pct, deal_notes, announce_date, onsale_date, notes
               FROM events WHERE id = %s""",
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not_found"), 404
        before_cols = ["venue_id", "artist_id", "support", "show_date", "doors", "show_time", "status",
                       "deal_type", "guarantee", "backend_pct", "deal_notes", "announce_date",
                       "onsale_date", "notes"]
        before = dict(zip(before_cols, row))

        updates = {}
        if "headliner" in body:
            name = (body["headliner"] or "").strip()
            if not name:
                return jsonify(error="headliner_cannot_be_empty"), 400
            updates["artist_id"] = artists_module.find_or_create(g.db, name)
        if "venue_id" in body:
            cur.execute("SELECT 1 FROM venues WHERE id = %s", (body["venue_id"],))
            if cur.fetchone() is None:
                return jsonify(error="unknown_venue"), 400
            updates["venue_id"] = body["venue_id"]
        if "status" in body:
            if body["status"] not in ALL_STATUSES:
                return jsonify(error="invalid_status"), 400
            updates["status"] = body["status"]
        for key in ("support", "deal_type", "deal_notes", "notes"):
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

    return app


app = create_app()

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
