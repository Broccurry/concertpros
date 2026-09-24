"""Real database, real Flask test client: pulling a ticket purchase link
from Etix's public event feed. etix.find_ticket_link itself is mocked --
these tests check the endpoint's permission gate, matching, and DB
write, not that Etix's API is reachable (see etix.py for that).
"""
import unittest
from unittest.mock import patch

import app as app_module
import auth
import db


class PullTicketLink(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"pulltix-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"pulltix-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues LIMIT 1")
        self.venue_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO events (venue_id, show_date, status, created_by)
               VALUES (%s, '2027-08-01', 'confirmed', %s) RETURNING id""",
            (self.venue_id, self.booker_id),
        )
        self.event_id = cur.fetchone()[0]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def tearDown(self):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id = %s", (self.event_id,))
        cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def test_a_match_saves_the_link_and_returns_it(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        with patch("app.etix.find_ticket_link", return_value="https://tickets.etix.com/ticket/p/123") as mock_find:
            r = client.post(f"/api/events/{self.event_id}/pull_ticket_link")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["ticket_link"], "https://tickets.etix.com/ticket/p/123")
        self.assertEqual(mock_find.call_args[0][1], __import__("datetime").date(2027, 8, 1))

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(mine["ticket_link"], "https://tickets.etix.com/ticket/p/123")

    def test_no_match_leaves_the_link_alone(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        with patch("app.etix.find_ticket_link", return_value=None):
            r = client.post(f"/api/events/{self.event_id}/pull_ticket_link")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"], "no_matching_etix_event")

    def test_etix_lookup_failure_is_a_502_not_a_crash(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        with patch("app.etix.find_ticket_link", side_effect=RuntimeError("boom")):
            r = client.post(f"/api/events/{self.event_id}/pull_ticket_link")
        self.assertEqual(r.status_code, 502)

    def test_unknown_event_is_404(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/events/999999999/pull_ticket_link")
        self.assertEqual(r.status_code, 404)

    def test_crew_cannot_pull_a_ticket_link(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        with patch("app.etix.find_ticket_link", return_value="https://tickets.etix.com/ticket/p/123"):
            r = client.post(f"/api/events/{self.event_id}/pull_ticket_link")
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
