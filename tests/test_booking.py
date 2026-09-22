"""Real database, real Flask test client: proves booking a show works,
crew is structurally forbidden from creating one, and booking the same
artist twice (even with different capitalization/whitespace) reuses one
artist record instead of creating a duplicate.
"""
import unittest

import app as app_module
import auth
import db


class BookingAShow(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()

        self.password = "correct horse battery staple"
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"booking-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"booking-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
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
        cur.execute("DELETE FROM artists WHERE name LIKE %s", (f"%Booking Test Band {id(self)}%",))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def test_crew_cannot_book_a_show(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": ["Should Never Exist"], "show_date": "2027-06-01",
        })
        self.assertEqual(r.status_code, 403)

    def test_unauthenticated_cannot_book_a_show(self):
        client = self.app.test_client()
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": ["Should Never Exist"], "show_date": "2027-06-01",
        })
        self.assertEqual(r.status_code, 401)

    def test_booker_can_book_a_show_and_it_shows_up(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        band = f"Booking Test Band {id(self)}"
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": [band], "show_date": "2027-06-01", "status": "hold1",
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        event_id = r.get_json()["id"]

        listed = client.get("/api/events").get_json()["events"]
        mine = next(e for e in listed if e["id"] == event_id)
        self.assertEqual([a["name"] for a in mine["artists"]], [band])
        self.assertFalse(mine["artists"][0]["confirmed"])
        self.assertEqual(mine["status"], "hold1")

        cur = self.conn.cursor()
        cur.execute(
            "SELECT action, person_id FROM audit_log WHERE entity_type = 'event' AND entity_id = %s",
            (event_id,),
        )
        action, person_id = cur.fetchone()
        self.assertEqual(action, "create")
        self.assertEqual(person_id, self.booker_id)

    def test_booking_the_same_artist_twice_reuses_one_artist_row(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        band = f"  Booking Test Band {id(self)}  "  # deliberately messy whitespace
        r1 = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": [band], "show_date": "2027-06-01",
        })
        r2 = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": [band.strip().upper()], "show_date": "2027-06-08",
        })
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)

        cur = self.conn.cursor()
        cur.execute("SELECT artist_id FROM event_artists WHERE event_id IN (%s, %s)",
                    (r1.get_json()["id"], r2.get_json()["id"]))
        artist_ids = {row[0] for row in cur.fetchall()}
        self.assertEqual(len(artist_ids), 1, "booking the same band twice created two artist rows")

    def test_booking_two_acts_creates_two_separate_artists_never_one_merged_name(self):
        """The exact scenario Broc described: typing 'Foo Fighters' then
        adding 'Nirvana' must create two acts on the bill, never one artist
        named 'Foo Fighters Nirvana'."""
        client = self.app.test_client()
        self._login(client, self.booker_email)
        band1 = f"Booking Test Band {id(self)} One"
        band2 = f"Booking Test Band {id(self)} Two"
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": [band1, band2], "show_date": "2027-06-01",
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        event_id = r.get_json()["id"]

        listed = client.get("/api/events").get_json()["events"]
        mine = next(e for e in listed if e["id"] == event_id)
        names = [a["name"] for a in mine["artists"]]
        self.assertEqual(names, [band1, band2], "acts must stay separate, in order, never concatenated")

        cur = self.conn.cursor()
        cur.execute("SELECT name FROM artists WHERE name = %s", (band1 + " " + band2,))
        self.assertIsNone(cur.fetchone(), "a single merged artist row must never be created")


if __name__ == "__main__":
    unittest.main()
