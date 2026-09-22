"""Real database, real Flask test client: settling a show's finances,
proving a partial save (just ticking 'settled') doesn't wipe out figures
saved earlier, and that crew can't reach this at all.
"""
import unittest

import app as app_module
import auth
import db


class SettlingAShow(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"settle-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"settle-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO artists (name) VALUES (%s) RETURNING id",
            (f"Settlement Test Band {id(self)}",),
        )
        self.artist_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO events (venue_id, show_date, status, created_by)
               VALUES (%s, '2027-02-01', 'complete', %s) RETURNING id""",
            (self.venue_id, self.booker_id),
        )
        self.event_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO event_artists (event_id, artist_id, confirmed) VALUES (%s, %s, TRUE)",
            (self.event_id, self.artist_id),
        )
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
        cur.execute("DELETE FROM settlements WHERE event_id = %s", (self.event_id,))
        cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM artists WHERE id = %s", (self.artist_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def test_crew_cannot_touch_the_settlement(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        r = client.put(f"/api/events/{self.event_id}/settlement", json={"gross": 1000})
        self.assertEqual(r.status_code, 403)

    def test_booker_records_finals_and_they_show_up(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)

        r = client.put(f"/api/events/{self.event_id}/settlement", json={
            "tickets_sold": 200, "gross": 4000, "expenses": 800, "artist_payout": 2000,
        })
        self.assertEqual(r.status_code, 200, r.get_json())

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        s = mine["settlement"]
        self.assertEqual(s["tickets_sold"], 200)
        self.assertAlmostEqual(float(s["gross"]), 4000.0)
        self.assertAlmostEqual(float(s["artist_payout"]), 2000.0)
        self.assertFalse(s["settled"])

        cur = self.conn.cursor()
        cur.execute(
            "SELECT action, detail FROM audit_log WHERE entity_type = 'event' AND entity_id = %s AND action = 'settle'",
            (self.event_id,),
        )
        action, detail = cur.fetchone()
        self.assertEqual(detail["after"]["gross"], 4000.0)

    def test_a_partial_save_does_not_null_out_earlier_figures(self):
        """Ticking 'settled' later must not wipe the gross/expenses saved
        in an earlier call — this is the whole point of merging with what's
        already stored rather than overwriting the full row."""
        client = self.app.test_client()
        self._login(client, self.booker_email)

        client.put(f"/api/events/{self.event_id}/settlement", json={
            "tickets_sold": 150, "gross": 3000, "expenses": 500, "artist_payout": 1200,
        })
        r2 = client.put(f"/api/events/{self.event_id}/settlement", json={"settled": True})
        self.assertEqual(r2.status_code, 200)

        events = client.get("/api/events").get_json()["events"]
        s = next(e for e in events if e["id"] == self.event_id)["settlement"]
        self.assertTrue(s["settled"])
        self.assertEqual(s["tickets_sold"], 150, "the earlier tickets_sold must survive a partial save")
        self.assertAlmostEqual(float(s["gross"]), 3000.0, msg="the earlier gross must survive a partial save")
        self.assertAlmostEqual(float(s["artist_payout"]), 1200.0)

    def test_invalid_number_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.put(f"/api/events/{self.event_id}/settlement", json={"gross": "not a number"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
