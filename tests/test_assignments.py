"""Real database, real Flask test client: assigning and unassigning staff
on a show, and proving crew stays locked out of both the roster and the
write endpoints.
"""
import unittest

import app as app_module
import auth
import db


class StaffAssignments(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()

        self.password = "correct horse battery staple"
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"assign-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew Booking', %s, %s, 'crew') RETURNING id""",
            (f"assign-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        # A second crew person to actually assign to a show (distinct from
        # the one used to test the crew-is-forbidden boundary).
        cur.execute(
            """INSERT INTO people (name, email, access_level)
               VALUES ('Sound Person', %s, 'crew') RETURNING id""",
            (f"assign-test-sound-{id(self)}@example.invalid",),
        )
        self.sound_person_id = cur.fetchone()[0]

        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM roles WHERE name = 'Sound'")
        self.sound_role_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO artists (name) VALUES (%s) RETURNING id",
            (f"Assignment Test Band {id(self)}",),
        )
        self.artist_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO events (venue_id, show_date, status, created_by)
               VALUES (%s, '2027-08-01', 'confirmed', %s) RETURNING id""",
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
        cur.execute("DELETE FROM assignments WHERE event_id = %s", (self.event_id,))
        cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM artists WHERE id = %s", (self.artist_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s, %s)",
                    (self.booker_id, self.crew_id, self.sound_person_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s, %s)",
                    (self.booker_id, self.crew_id, self.sound_person_id))
        self.conn.commit()
        self.conn.close()

    def test_crew_cannot_see_the_roster_or_create_assignments(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        self.assertEqual(client.get("/api/people").status_code, 403)
        self.assertEqual(client.get("/api/roles").status_code, 403)
        r = client.post(f"/api/events/{self.event_id}/assignments",
                         json={"role_id": self.sound_role_id, "person_id": self.sound_person_id})
        self.assertEqual(r.status_code, 403)

    def test_booker_assigns_someone_and_it_shows_up_on_the_event(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)

        r = client.post(f"/api/events/{self.event_id}/assignments",
                         json={"role_id": self.sound_role_id, "person_id": self.sound_person_id})
        self.assertEqual(r.status_code, 201, r.get_json())
        assignment_id = r.get_json()["id"]

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(len(mine["staff"]), 1)
        self.assertEqual(mine["staff"][0]["role"], "Sound")
        self.assertEqual(mine["staff"][0]["person_id"], self.sound_person_id)
        self.assertEqual(mine["staff"][0]["id"], assignment_id,
                          "the assignment's own id must be exposed so the UI can remove it")

        cur = self.conn.cursor()
        cur.execute(
            "SELECT action FROM audit_log WHERE entity_type = 'event' AND entity_id = %s AND action = 'assign'",
            (self.event_id,),
        )
        self.assertIsNotNone(cur.fetchone())

        # now unassign
        d = client.delete(f"/api/assignments/{assignment_id}")
        self.assertEqual(d.status_code, 200)
        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(mine["staff"], [])

    def test_assigning_to_an_unknown_role_or_person_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r1 = client.post(f"/api/events/{self.event_id}/assignments",
                          json={"role_id": 999999, "person_id": self.sound_person_id})
        self.assertEqual(r1.status_code, 400)
        r2 = client.post(f"/api/events/{self.event_id}/assignments",
                          json={"role_id": self.sound_role_id, "person_id": 999999})
        self.assertEqual(r2.status_code, 400)


if __name__ == "__main__":
    unittest.main()
