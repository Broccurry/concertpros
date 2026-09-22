"""Real database, real Flask test client: a show can be deleted any time
before it's Complete (a hold that never went anywhere, a confirmed date
that fell through, a dead one someone wants off the books) — but never
once it's Complete, since real settlement history is attached to it by
then.
"""
import unittest

import app as app_module
import auth
import db


class DeletingAShow(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"delete-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"delete-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
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

    def _book(self, client, status="hold1", band=None):
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": [band or f"Delete Test Band {id(self)}"],
            "show_date": "2027-12-01", "status": status,
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        self.event_id = r.get_json()["id"]
        return self.event_id

    def tearDown(self):
        cur = self.conn.cursor()
        if self.event_id:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id = %s", (self.event_id,))
            cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM artists WHERE name LIKE %s", (f"%Delete Test Band {id(self)}%",))
        # A deleted event's own audit_log rows survive it on purpose (the
        # forensic trail outlives the row it's about) — clean those up by
        # person_id too, not just by the now-gone entity_id, before the
        # people row they reference can be removed.
        cur.execute("DELETE FROM audit_log WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def test_a_hold_can_be_deleted(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client, status="hold1")
        r = client.delete(f"/api/events/{self.event_id}")
        self.assertEqual(r.status_code, 200, r.get_json())
        events = client.get("/api/events").get_json()["events"]
        self.assertNotIn(self.event_id, [e["id"] for e in events])
        self.event_id = None  # already gone, nothing for tearDown to clean up

    def test_a_confirmed_show_can_be_deleted(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client, status="confirmed")
        r = client.delete(f"/api/events/{self.event_id}")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.event_id = None

    def test_a_completed_show_cannot_be_deleted(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client, status="confirmed")
        version = client.get("/api/events").get_json()["events"]
        version = next(e for e in version if e["id"] == self.event_id)["version"]
        client.patch(f"/api/events/{self.event_id}", json={"version": version, "status": "complete"})

        r = client.delete(f"/api/events/{self.event_id}")
        self.assertEqual(r.status_code, 400)
        events = client.get("/api/events").get_json()["events"]
        self.assertIn(self.event_id, [e["id"] for e in events])

    def test_deleting_removes_its_bill_and_messages(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client, status="hold1")
        client.post(f"/api/events/{self.event_id}/messages", json={"body": "a note"})
        event_id = self.event_id

        r = client.delete(f"/api/events/{event_id}")
        self.assertEqual(r.status_code, 200)
        self.event_id = None

        cur = self.conn.cursor()
        cur.execute("SELECT count(*) FROM event_artists WHERE event_id = %s", (event_id,))
        self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("SELECT count(*) FROM event_messages WHERE event_id = %s", (event_id,))
        self.assertEqual(cur.fetchone()[0], 0)

    def test_crew_cannot_delete_a_show(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client, status="hold1")

        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        r = crew_client.delete(f"/api/events/{self.event_id}")
        self.assertEqual(r.status_code, 403)

    def test_deleting_an_unknown_event_is_a_404(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.delete("/api/events/999999")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
