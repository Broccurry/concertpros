"""Real database, real Flask test client: editing a band's card
(tier/genre/tags/location) and each act's own guarantee/paid/walkups
figures on a show's bill — the data that later shows up as that band's
show history.
"""
import unittest

import app as app_module
import auth
import db


class ArtistProfile(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"artist-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"artist-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
        self.artist_name = f"Profile Test Band {id(self)}"
        cur.execute("INSERT INTO artists (name) VALUES (%s) RETURNING id", (self.artist_name,))
        self.artist_id = cur.fetchone()[0]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.event_id = None

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def tearDown(self):
        cur = self.conn.cursor()
        if self.event_id:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id = %s", (self.event_id,))
            cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM audit_log WHERE entity_type = 'artist' AND entity_id = %s", (self.artist_id,))
        cur.execute("DELETE FROM artists WHERE id = %s", (self.artist_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def test_booker_can_edit_the_bands_card(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.patch(f"/api/artists/{self.artist_id}", json={
            "tier": "Local", "genre": "Metal", "location": "Bowling Green, OH",
            "tags": ["heavy", "HEAVY", "  loud  ", ""],
        })
        self.assertEqual(r.status_code, 200, r.get_json())

        artists = client.get("/api/artists").get_json()["artists"]
        mine = next(a for a in artists if a["id"] == self.artist_id)
        self.assertEqual(mine["tier"], "Local")
        self.assertEqual(mine["genre"], "Metal")
        self.assertEqual(mine["location"], "Bowling Green, OH")
        self.assertEqual(mine["tags"], ["heavy", "loud"], "tags must be trimmed, deduped case-insensitively")

    def test_invalid_tier_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.patch(f"/api/artists/{self.artist_id}", json={"tier": "Galactic"})
        self.assertEqual(r.status_code, 400)

    def test_crew_cannot_edit_a_bands_card(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        r = client.patch(f"/api/artists/{self.artist_id}", json={"tier": "Local"})
        self.assertEqual(r.status_code, 403)

    def test_crew_cannot_see_the_artist_roster_at_all(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        r = client.get("/api/artists")
        self.assertEqual(r.status_code, 403)

    def test_per_act_guarantee_paid_and_walkups_round_trip(self):
        """The exact fields that later become a band's show history."""
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": [self.artist_name], "show_date": "2027-10-01",
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        self.event_id = r.get_json()["id"]

        put = client.put(f"/api/events/{self.event_id}/artists", json={
            "artists": [{"name": self.artist_name, "confirmed": True,
                         "guarantee": 500, "paid": 550, "walkups": 8}],
        })
        self.assertEqual(put.status_code, 200, put.get_json())

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        act = mine["artists"][0]
        self.assertTrue(act["confirmed"])
        self.assertAlmostEqual(float(act["guarantee"]), 500.0)
        self.assertAlmostEqual(float(act["paid"]), 550.0)
        self.assertEqual(act["walkups"], 8)

    def test_invalid_act_figure_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/events", json={
            "venue_id": self.venue_id,
            "acts": [{"name": self.artist_name, "guarantee": "not-a-number"}],
            "show_date": "2027-10-01",
        })
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
