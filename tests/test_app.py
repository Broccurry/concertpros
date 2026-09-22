"""End-to-end: real database, real Flask test client, real login flow.
Proves the whole chain (password hash -> login -> session cookie -> the
permission layer) actually works together, not just each piece alone.
"""
import unittest
from datetime import date

import app as app_module
import auth
import db


class LoginAndEventsEndToEnd(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()

        self.password = "correct horse battery staple"
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew E2E', %s, %s, 'crew') RETURNING id""",
            (f"e2e-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.person_id = cur.fetchone()[0]

        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        venue_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO artists (name) VALUES (%s) RETURNING id",
            (f"E2E Artist {id(self)}",),
        )
        artist_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO events (venue_id, artist_id, show_date, status)
               VALUES (%s, %s, %s, 'confirmed') RETURNING id""",
            (venue_id, artist_id, date(2027, 1, 10)),
        )
        self.event_id = cur.fetchone()[0]
        self.artist_id = artist_id
        self.conn.commit()

        self.email = self._get_email()
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _get_email(self):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (self.person_id,))
        return cur.fetchone()[0]

    def tearDown(self):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM artists WHERE id = %s", (self.artist_id,))
        cur.execute("DELETE FROM sessions WHERE person_id = %s", (self.person_id,))
        cur.execute("DELETE FROM people WHERE id = %s", (self.person_id,))
        self.conn.commit()
        self.conn.close()

    def test_wrong_password_is_rejected(self):
        resp = self.client.post("/api/login", json={"email": self.email, "password": "nope"})
        self.assertEqual(resp.status_code, 401)

    def test_unauthenticated_events_request_is_rejected(self):
        resp = self.client.get("/api/events")
        self.assertEqual(resp.status_code, 401)

    def test_full_login_then_fetch_events(self):
        resp = self.client.post("/api/login", json={"email": self.email, "password": self.password})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()["access_level"], "crew")

        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.get_json()["email"], self.email)

        events = self.client.get("/api/events")
        self.assertEqual(events.status_code, 200)
        ids = [e["id"] for e in events.get_json()["events"]]
        self.assertIn(self.event_id, ids)

        logout = self.client.post("/api/logout")
        self.assertEqual(logout.status_code, 200)

        after_logout = self.client.get("/api/events")
        self.assertEqual(after_logout.status_code, 401)


if __name__ == "__main__":
    unittest.main()
