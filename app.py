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

import auth
import db
import permissions
from permissions import Viewer

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

    return app


app = create_app()

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
