"""Real database, real Flask test client: a show's event type(s) (concert,
dance party, rental, private event -- can be more than one) and its
promoter(s) (Innovation Concerts by default, or Kickstand/Bravo/a custom
"Other" name, again possibly more than one for a co-pro show).
"""
import unittest

import app as app_module
import auth
import db


class EventTypesAndPromoters(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()

        self.password = "correct horse battery staple"
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"evtype-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"evtype-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
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
        cur.execute(
            "DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id IN "
            "(SELECT id FROM events WHERE created_by IN (%s, %s))",
            (self.booker_id, self.crew_id),
        )
        cur.execute("DELETE FROM events WHERE created_by IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM artists WHERE name LIKE %s", (f"%Event Type Test Band {id(self)}%",))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def _book(self, client, **extra):
        payload = {
            "venue_id": self.venue_id, "acts": [f"Event Type Test Band {id(self)}"], "show_date": "2027-11-02",
        }
        payload.update(extra)
        return client.post("/api/events", json=payload)

    def _get_event(self, client, event_id):
        events = client.get("/api/events").get_json()["events"]
        return next(e for e in events if e["id"] == event_id)

    def test_defaults_when_not_specified(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = self._book(client)
        self.assertEqual(r.status_code, 201, r.get_json())
        ev = self._get_event(client, r.get_json()["id"])
        self.assertEqual(ev["event_types"], [])
        self.assertEqual(ev["promoters"], ["Innovation Concerts"])

    def test_can_set_multiple_event_types_and_promoters_at_booking(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = self._book(client, event_types=["concert", "private_event"],
                       promoters=["Innovation Concerts", "Kickstand", "The Loud Room"])
        self.assertEqual(r.status_code, 201, r.get_json())
        ev = self._get_event(client, r.get_json()["id"])
        self.assertEqual(ev["event_types"], ["concert", "private_event"])
        self.assertEqual(ev["promoters"], ["Innovation Concerts", "Kickstand", "The Loud Room"])

    def test_invalid_event_type_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = self._book(client, event_types=["concert", "block_party"])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "invalid_event_types")

    def test_duplicate_event_types_are_deduped(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = self._book(client, event_types=["concert", "concert", "rental"])
        self.assertEqual(r.status_code, 201, r.get_json())
        ev = self._get_event(client, r.get_json()["id"])
        self.assertEqual(ev["event_types"], ["concert", "rental"])

    def test_blank_and_duplicate_promoters_are_cleaned_up(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = self._book(client, promoters=["Bravo", "  ", "bravo", "Kickstand"])
        self.assertEqual(r.status_code, 201, r.get_json())
        ev = self._get_event(client, r.get_json()["id"])
        self.assertEqual(ev["promoters"], ["Bravo", "Kickstand"])

    def test_updating_event_types_and_promoters_after_booking(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = self._book(client)
        event_id = r.get_json()["id"]
        ev = self._get_event(client, event_id)
        u = client.patch(f"/api/events/{event_id}", json={
            "version": ev["version"], "event_types": ["dance_party"], "promoters": ["Bravo"],
        })
        self.assertEqual(u.status_code, 200, u.get_json())
        ev2 = self._get_event(client, event_id)
        self.assertEqual(ev2["event_types"], ["dance_party"])
        self.assertEqual(ev2["promoters"], ["Bravo"])

    def test_crew_never_receives_event_types_or_promoters(self):
        """Non-financial, but still a booker/owner field for now -- new
        fields default invisible to crew until someone decides otherwise
        (see permissions.py's own stated rule)."""
        booker_client = self.app.test_client()
        self._login(booker_client, self.booker_email)
        r = self._book(booker_client, event_types=["concert"], promoters=["Kickstand"])
        self.assertEqual(r.status_code, 201, r.get_json())
        u = booker_client.patch(f"/api/events/{r.get_json()['id']}", json={
            "version": 1, "status": "confirmed",
        })
        self.assertEqual(u.status_code, 200, u.get_json())

        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        events = crew_client.get("/api/events").get_json()["events"]
        ev = next(e for e in events if e["id"] == r.get_json()["id"])
        self.assertNotIn("event_types", ev)
        self.assertNotIn("promoters", ev)


if __name__ == "__main__":
    unittest.main()
