"""Real database, real Flask test client: the day-by-day ticket sales
log -- separate from settlements.tickets_sold (the one final number
entered after the show), this is for watching sales velocity while
tickets are still on sale.
"""
import unittest

import app as app_module
import auth
import db


class TicketSales(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"ticketsales-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"ticketsales-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues LIMIT 1")
        self.venue_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO events (venue_id, show_date, status, created_by)
               VALUES (%s, '2027-07-01', 'confirmed', %s) RETURNING id""",
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

    def test_logging_a_sale_shows_up_on_the_event(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post(f"/api/events/{self.event_id}/ticket_sales", json={
            "sale_date": "2027-06-20", "tickets_sold": 45,
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(mine["ticket_sales"], [{"sale_date": "2027-06-20", "tickets_sold": 45, "source": "manual"}])
        self.assertEqual(mine["latest_ticket_count"], 45)

    def test_relogging_the_same_date_corrects_it_not_duplicates(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        client.post(f"/api/events/{self.event_id}/ticket_sales", json={"sale_date": "2027-06-20", "tickets_sold": 45})
        client.post(f"/api/events/{self.event_id}/ticket_sales", json={"sale_date": "2027-06-20", "tickets_sold": 60})
        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(len(mine["ticket_sales"]), 1)
        self.assertEqual(mine["ticket_sales"][0]["tickets_sold"], 60)

    def test_latest_ticket_count_is_the_most_recent_date_not_highest_count(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        client.post(f"/api/events/{self.event_id}/ticket_sales", json={"sale_date": "2027-06-25", "tickets_sold": 80})
        client.post(f"/api/events/{self.event_id}/ticket_sales", json={"sale_date": "2027-06-20", "tickets_sold": 45})
        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(mine["latest_ticket_count"], 80)

    def test_deleting_an_entry(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        client.post(f"/api/events/{self.event_id}/ticket_sales", json={"sale_date": "2027-06-20", "tickets_sold": 45})
        r = client.delete(f"/api/events/{self.event_id}/ticket_sales/2027-06-20")
        self.assertEqual(r.status_code, 200)
        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(mine["ticket_sales"], [])
        self.assertIsNone(mine["latest_ticket_count"])

    def test_negative_count_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post(f"/api/events/{self.event_id}/ticket_sales", json={"sale_date": "2027-06-20", "tickets_sold": -5})
        self.assertEqual(r.status_code, 400)

    def test_invalid_date_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post(f"/api/events/{self.event_id}/ticket_sales", json={"sale_date": "not-a-date", "tickets_sold": 5})
        self.assertEqual(r.status_code, 400)

    def test_crew_cannot_log_or_see_ticket_sales(self):
        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        r = crew_client.post(f"/api/events/{self.event_id}/ticket_sales", json={
            "sale_date": "2027-06-20", "tickets_sold": 45,
        })
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
