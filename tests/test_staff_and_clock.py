"""Real database, real Flask test client: TBA (unfilled) staffing slots,
a scheduled shift time on an assignment, and the crew self-service clock
in/out endpoint — the one write a crew viewer can make directly, scoped to
their own assignment row and nothing else about the show.
"""
import unittest

import app as app_module
import auth
import db


class StaffAndClock(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"clock-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Clock Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"clock-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Other Crew', %s, %s, 'crew') RETURNING id""",
            (f"clock-test-othercrew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.other_crew_id = cur.fetchone()[0]

        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM roles WHERE name = 'Security'")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO roles (name) VALUES ('Security') RETURNING id")
            row = cur.fetchone()
        self.role_id = row[0]
        cur.execute(
            """INSERT INTO events (venue_id, show_date, status, created_by)
               VALUES (%s, '2027-08-15', 'confirmed', %s) RETURNING id""",
            (self.venue_id, self.booker_id),
        )
        self.event_id = cur.fetchone()[0]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.other_crew_email = self._email(self.other_crew_id)
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
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s, %s)",
                    (self.booker_id, self.crew_id, self.other_crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s, %s)",
                    (self.booker_id, self.crew_id, self.other_crew_id))
        self.conn.commit()
        self.conn.close()

    def _assign(self, client, person_id=None, scheduled_time=None):
        body = {"role_id": self.role_id, "person_id": person_id}
        if scheduled_time:
            body["scheduled_time"] = scheduled_time
        r = client.post(f"/api/events/{self.event_id}/assignments", json=body)
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["id"]

    # -- TBA / open slots -------------------------------------------------

    def test_leaving_person_out_books_an_open_tba_slot(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._assign(client)

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(len(mine["staff"]), 1)
        self.assertIsNone(mine["staff"][0]["person_id"])

    def test_two_tba_slots_for_the_same_role_can_coexist(self):
        """"We need 2 security" — one row per body needed."""
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._assign(client)
        self._assign(client)

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(len(mine["staff"]), 2)
        self.assertTrue(all(s["person_id"] is None for s in mine["staff"]))

    def test_scheduled_time_round_trips(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._assign(client, person_id=self.crew_id, scheduled_time="18:30")

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(mine["staff"][0]["scheduled_time"], "18:30:00")

    def test_invalid_scheduled_time_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post(f"/api/events/{self.event_id}/assignments",
                         json={"role_id": self.role_id, "scheduled_time": "not-a-time"})
        self.assertEqual(r.status_code, 400)

    # -- clock in/out -------------------------------------------------------

    def test_crew_can_clock_themselves_in_and_out(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        assignment_id = self._assign(client, person_id=self.crew_id)

        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)

        r_in = crew_client.post(f"/api/assignments/{assignment_id}/clock", json={"action": "in"})
        self.assertEqual(r_in.status_code, 200, r_in.get_json())
        self.assertIsNotNone(r_in.get_json()["clocked_in_at"])
        self.assertIsNone(r_in.get_json()["clocked_out_at"])

        r_out = crew_client.post(f"/api/assignments/{assignment_id}/clock", json={"action": "out"})
        self.assertEqual(r_out.status_code, 200, r_out.get_json())
        self.assertIsNotNone(r_out.get_json()["clocked_out_at"])

    def test_crew_cannot_clock_in_someone_elses_assignment(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        assignment_id = self._assign(client, person_id=self.crew_id)

        other_client = self.app.test_client()
        self._login(other_client, self.other_crew_email)
        r = other_client.post(f"/api/assignments/{assignment_id}/clock", json={"action": "in"})
        self.assertEqual(r.status_code, 403)

    def test_booker_can_clock_in_on_someones_behalf(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        assignment_id = self._assign(client, person_id=self.crew_id)
        r = client.post(f"/api/assignments/{assignment_id}/clock", json={"action": "in"})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_cannot_clock_in_twice(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        assignment_id = self._assign(client, person_id=self.crew_id)
        client.post(f"/api/assignments/{assignment_id}/clock", json={"action": "in"})
        r = client.post(f"/api/assignments/{assignment_id}/clock", json={"action": "in"})
        self.assertEqual(r.status_code, 400)

    def test_cannot_clock_out_before_clocking_in(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        assignment_id = self._assign(client, person_id=self.crew_id)
        r = client.post(f"/api/assignments/{assignment_id}/clock", json={"action": "out"})
        self.assertEqual(r.status_code, 400)

    def test_cannot_clock_in_to_an_unfilled_slot(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        assignment_id = self._assign(client)
        r = client.post(f"/api/assignments/{assignment_id}/clock", json={"action": "in"})
        self.assertEqual(r.status_code, 400)

    # -- crew's own view of their assignment -------------------------------

    def test_crew_sees_only_their_own_assignment_with_clock_fields(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        assignment_id = self._assign(client, person_id=self.crew_id, scheduled_time="19:00")
        self._assign(client, person_id=self.other_crew_id)

        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        events = crew_client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(len(mine["my_assignments"]), 1)
        self.assertEqual(mine["my_assignments"][0]["id"], assignment_id)
        self.assertEqual(mine["my_assignments"][0]["scheduled_time"], "19:00:00")
        self.assertNotIn("staff", mine, "crew must never receive who ELSE is working")


if __name__ == "__main__":
    unittest.main()
