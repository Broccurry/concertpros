"""Real database, real Flask test client: proves editing a show works,
bumps its version, writes an audit entry, rejects a stale version instead
of silently overwriting another booker's edit, and keeps crew out.
"""
import unittest

import app as app_module
import auth
import db


class UpdatingAShow(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()

        self.password = "correct horse battery staple"
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"update-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"update-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO artists (name) VALUES (%s) RETURNING id",
            (f"Update Test Band {id(self)}",),
        )
        self.artist_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO events (venue_id, show_date, status, created_by)
               VALUES (%s, '2027-07-01', 'hold1', %s) RETURNING id, version""",
            (self.venue_id, self.booker_id),
        )
        self.event_id, self.initial_version = cur.fetchone()
        cur.execute(
            "INSERT INTO event_artists (event_id, artist_id) VALUES (%s, %s)",
            (self.event_id, self.artist_id),
        )
        self.new_artist_id = None  # set by test_replacing_the_bill_relinks_and_supports_multiple_acts
        self.second_artist_id = None
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
        cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))  # must go before either artist
        cur.execute("DELETE FROM artists WHERE id = %s", (self.artist_id,))
        if self.new_artist_id is not None:
            cur.execute("DELETE FROM artists WHERE id = %s", (self.new_artist_id,))
        if self.second_artist_id is not None:
            cur.execute("DELETE FROM artists WHERE id = %s", (self.second_artist_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def test_crew_cannot_edit_a_show(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        r = client.patch(f"/api/events/{self.event_id}", json={
            "version": self.initial_version, "status": "confirmed",
        })
        self.assertEqual(r.status_code, 403)

    def test_booker_can_confirm_a_hold_and_version_bumps(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.patch(f"/api/events/{self.event_id}", json={
            "version": self.initial_version, "status": "confirmed", "guarantee": 2500,
        })
        self.assertEqual(r.status_code, 200, r.get_json())
        new_version = r.get_json()["version"]
        self.assertEqual(new_version, self.initial_version + 1)

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(mine["status"], "confirmed")
        self.assertEqual(float(mine["guarantee"]), 2500.0)
        self.assertEqual(mine["version"], new_version)

        cur = self.conn.cursor()
        cur.execute(
            "SELECT action, person_id, detail FROM audit_log WHERE entity_type = 'event' AND entity_id = %s",
            (self.event_id,),
        )
        action, person_id, detail = cur.fetchone()
        self.assertEqual(action, "update")
        self.assertEqual(person_id, self.booker_id)
        self.assertEqual(detail["after"]["status"], "confirmed")

    def test_stale_version_is_rejected_not_silently_overwritten(self):
        """Cody and Christian both open the same show; Cody saves first."""
        client = self.app.test_client()
        self._login(client, self.booker_email)

        first = client.patch(f"/api/events/{self.event_id}", json={
            "version": self.initial_version, "notes": "Cody's edit",
        })
        self.assertEqual(first.status_code, 200)

        # Christian's client still holds the OLD version number.
        second = client.patch(f"/api/events/{self.event_id}", json={
            "version": self.initial_version, "notes": "Christian's edit — should not land",
        })
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["current_version"], self.initial_version + 1)

        cur = self.conn.cursor()
        cur.execute("SELECT notes FROM events WHERE id = %s", (self.event_id,))
        self.assertEqual(cur.fetchone()[0], "Cody's edit", "the second, stale write must not land")

    def test_replacing_the_bill_relinks_and_supports_multiple_acts(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        new_name = f"Renamed Band {id(self)}"
        second_name = f"Second Act {id(self)}"
        r = client.put(f"/api/events/{self.event_id}/artists", json={
            "artists": [{"name": new_name, "confirmed": True}, {"name": second_name, "confirmed": False}],
        })
        self.assertEqual(r.status_code, 200, r.get_json())

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual([a["name"] for a in mine["artists"]], [new_name, second_name])
        self.assertTrue(mine["artists"][0]["confirmed"])
        self.assertFalse(mine["artists"][1]["confirmed"])

        cur = self.conn.cursor()
        cur.execute("SELECT artist_id FROM event_artists WHERE event_id = %s ORDER BY sort_order",
                    (self.event_id,))
        ids = [row[0] for row in cur.fetchall()]
        self.new_artist_id, self.second_artist_id = ids  # cleaned up in tearDown, after the event row is gone
        self.assertNotEqual(self.new_artist_id, self.artist_id)

    def test_double_click_confirm_toggles_one_act_immediately(self):
        """The confirm action doesn't wait for the main Save button — it
        persists the instant it's toggled, same as a task checkbox."""
        client = self.app.test_client()
        self._login(client, self.booker_email)
        cur = self.conn.cursor()
        cur.execute("SELECT id, confirmed FROM event_artists WHERE event_id = %s", (self.event_id,))
        event_artist_id, was_confirmed = cur.fetchone()
        self.assertFalse(was_confirmed)

        r = client.patch(f"/api/event_artists/{event_artist_id}", json={"confirmed": True})
        self.assertEqual(r.status_code, 200, r.get_json())

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertTrue(mine["artists"][0]["confirmed"])

    def test_crew_cannot_touch_the_bill(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        r1 = client.put(f"/api/events/{self.event_id}/artists", json={"artists": [{"name": "x"}]})
        self.assertEqual(r1.status_code, 403)

        cur = self.conn.cursor()
        cur.execute("SELECT id FROM event_artists WHERE event_id = %s", (self.event_id,))
        event_artist_id = cur.fetchone()[0]
        r2 = client.patch(f"/api/event_artists/{event_artist_id}", json={"confirmed": True})
        self.assertEqual(r2.status_code, 403)


if __name__ == "__main__":
    unittest.main()
