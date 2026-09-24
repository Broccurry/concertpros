"""Real database, real Flask test client: pulling a live ticket-count
snapshot from Etix into ticket_sales (source='etix'). Two layers, same
split as test_pull_ticket_link.py:

- EtixPullEndpoint mocks etix.pull_and_store_snapshot itself, checking
  the endpoint's permission gate and error handling, not that Etix's API
  is reachable.
- EtixSnapshotStorage mocks only the network calls inside etix.py
  (_find_public_event / get_snapshot), so the actual upsert-into-
  ticket_sales logic runs for real against the real database — this is
  the one place both the manual "Pull from Etix" button and the daily
  scheduled script (scripts/pull_etix_daily_sales.py) get their write
  behavior from, so it's worth testing directly rather than only through
  the mocked endpoint layer.
"""
import unittest
from datetime import date
from unittest.mock import patch

import app as app_module
import auth
import db
import etix


class EtixPullEndpoint(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"etixtix-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"etixtix-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
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
        cur.execute("DELETE FROM ticket_sales WHERE event_id = %s", (self.event_id,))
        cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def test_a_match_stores_the_count_and_returns_it(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        with patch("app.etix.pull_and_store_snapshot", return_value=42) as mock_pull:
            r = client.post(f"/api/events/{self.event_id}/ticket_sales/pull_etix")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["tickets_sold"], 42)
        self.assertEqual(mock_pull.call_args[0][3], date(2027, 8, 1))

    def test_no_match_is_a_404_not_a_crash(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        with patch("app.etix.pull_and_store_snapshot", return_value=None):
            r = client.post(f"/api/events/{self.event_id}/ticket_sales/pull_etix")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"], "no_matching_etix_event")

    def test_etix_failure_is_a_502_not_a_crash(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        with patch("app.etix.pull_and_store_snapshot", side_effect=RuntimeError("boom")):
            r = client.post(f"/api/events/{self.event_id}/ticket_sales/pull_etix")
        self.assertEqual(r.status_code, 502)

    def test_unknown_event_is_404(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/events/999999999/ticket_sales/pull_etix")
        self.assertEqual(r.status_code, 404)

    def test_crew_cannot_pull_ticket_sales(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        with patch("app.etix.pull_and_store_snapshot", return_value=42):
            r = client.post(f"/api/events/{self.event_id}/ticket_sales/pull_etix")
        self.assertEqual(r.status_code, 403)


class EtixSnapshotStorage(unittest.TestCase):
    """The real upsert-into-ticket_sales logic, network calls mocked."""

    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name FROM venues WHERE name = 'Frankies'")
        self.venue_id, self.venue_name = cur.fetchone()
        cur.execute(
            "INSERT INTO events (venue_id, show_date, status) VALUES (%s, '2027-08-15', 'confirmed') RETURNING id",
            (self.venue_id,),
        )
        self.event_id = cur.fetchone()[0]
        self.conn.commit()

    def tearDown(self):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM ticket_sales WHERE event_id = %s", (self.event_id,))
        cur.execute("DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id = %s", (self.event_id,))
        cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        self.conn.commit()
        self.conn.close()

    def test_a_real_match_upserts_a_ticket_sales_row(self):
        with patch("etix._find_public_event", return_value={"id": 999}), \
             patch("etix.get_snapshot", return_value={"revenueProducingTickets": 77}):
            result = etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        self.conn.commit()
        self.assertEqual(result, 77)
        cur = self.conn.cursor()
        cur.execute("SELECT tickets_sold, source FROM ticket_sales WHERE event_id = %s AND sale_date = %s",
                    (self.event_id, date(2027, 1, 1)))
        row = cur.fetchone()
        self.assertEqual(row, (77, "etix"))

    def test_no_match_returns_none_and_writes_nothing(self):
        with patch("etix._find_public_event", return_value=None):
            result = etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        self.assertIsNone(result)
        cur = self.conn.cursor()
        cur.execute("SELECT count(*) FROM ticket_sales WHERE event_id = %s", (self.event_id,))
        self.assertEqual(cur.fetchone()[0], 0)

    def test_re_pulling_the_same_date_corrects_it_not_duplicates(self):
        with patch("etix._find_public_event", return_value={"id": 999}), \
             patch("etix.get_snapshot", return_value={"revenueProducingTickets": 10}):
            etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        with patch("etix._find_public_event", return_value={"id": 999}), \
             patch("etix.get_snapshot", return_value={"revenueProducingTickets": 25}):
            etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        self.conn.commit()
        cur = self.conn.cursor()
        cur.execute("SELECT count(*), max(tickets_sold) FROM ticket_sales WHERE event_id = %s", (self.event_id,))
        self.assertEqual(cur.fetchone(), (1, 25))

    def test_a_prior_manual_entry_gets_relabeled_etix_on_repull(self):
        """A booker logged a manual count for today, then a same-day Etix
        pull happens (or the daily job runs) -- the row should end up
        attributed to Etix, not silently keep saying 'manual' for a count
        Etix just overwrote."""
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO ticket_sales (event_id, sale_date, tickets_sold, source) VALUES (%s, %s, %s, 'manual')",
            (self.event_id, date(2027, 1, 1), 5),
        )
        self.conn.commit()
        with patch("etix._find_public_event", return_value={"id": 999}), \
             patch("etix.get_snapshot", return_value={"revenueProducingTickets": 12}):
            etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        self.conn.commit()
        cur.execute("SELECT tickets_sold, source FROM ticket_sales WHERE event_id = %s AND sale_date = %s",
                    (self.event_id, date(2027, 1, 1)))
        self.assertEqual(cur.fetchone(), (12, "etix"))


if __name__ == "__main__":
    unittest.main()
