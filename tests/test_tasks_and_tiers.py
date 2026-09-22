"""Real database, real Flask test client: the task checklist auto-created
on booking, toggling/adding/removing a task, and saving ticket tiers
(including proving the tier's own id is now exposed, which was missing
before this pass — the same class of bug caught earlier on assignments).
"""
import unittest

import app as app_module
import auth
import db


class TasksAndTiers(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"tt-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"tt-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
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
        self.artist_name = f"Tasks Tiers Band {id(self)}"

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
            cur.execute("DELETE FROM event_tasks WHERE event_id = %s", (self.event_id,))
            cur.execute("DELETE FROM ticket_tiers WHERE event_id = %s", (self.event_id,))
            cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM artists WHERE name = %s", (self.artist_name,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def _mine(self, client):
        """The database is the real, live one now that this app is deployed
        — never assume events[0] is this test's own row. Always filter by
        the id this test actually created."""
        events = client.get("/api/events").get_json()["events"]
        return next(e for e in events if e["id"] == self.event_id)

    def _book_show(self, client):
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "headliner": self.artist_name, "show_date": "2027-09-01",
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        self.event_id = r.get_json()["id"]

    def test_booking_a_show_creates_the_template_checklist(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book_show(client)

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        labels = [t["label"] for t in mine["tasks"]]
        self.assertEqual(labels, ["Website", "Marketing", "Offer", "Contract"])
        self.assertTrue(all(t["done"] is False for t in mine["tasks"]))

    def test_toggle_add_and_remove_a_task(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book_show(client)
        task_id = self._mine(client)["tasks"][0]["id"]

        toggle = client.patch(f"/api/tasks/{task_id}", json={"done": True})
        self.assertEqual(toggle.status_code, 200)
        mine = self._mine(client)
        self.assertTrue(next(t for t in mine["tasks"] if t["id"] == task_id)["done"])

        add = client.post(f"/api/events/{self.event_id}/tasks", json={"label": "Load-in plan"})
        self.assertEqual(add.status_code, 201, add.get_json())
        new_task_id = add.get_json()["id"]
        mine = self._mine(client)
        self.assertIn("Load-in plan", [t["label"] for t in mine["tasks"]])
        self.assertEqual(len(mine["tasks"]), 5)

        deleted = client.delete(f"/api/tasks/{new_task_id}")
        self.assertEqual(deleted.status_code, 200)
        mine = self._mine(client)
        self.assertEqual(len(mine["tasks"]), 4)

    def test_crew_cannot_touch_tasks(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        self.assertEqual(
            client.post("/api/events/1/tasks", json={"label": "x"}).status_code, 403)
        self.assertEqual(client.patch("/api/tasks/1", json={"done": True}).status_code, 403)
        self.assertEqual(client.delete("/api/tasks/1").status_code, 403)

    def test_setting_ticket_tiers_exposes_each_tiers_own_id(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book_show(client)

        r = client.put(f"/api/events/{self.event_id}/ticket_tiers", json={
            "tiers": [{"label": "Advance", "price": 20}, {"label": "Day of show", "price": 25}],
        })
        self.assertEqual(r.status_code, 200, r.get_json())

        mine = self._mine(client)
        tiers = mine["ticket_tiers"]
        self.assertEqual(len(tiers), 2)
        self.assertTrue(all("id" in t and t["id"] for t in tiers),
                         "each ticket tier must expose its own id")
        self.assertEqual(tiers[0]["label"], "Advance")
        self.assertAlmostEqual(float(tiers[0]["price"]), 20.0)

        # re-saving replaces the whole set, including a tier with no price
        r2 = client.put(f"/api/events/{self.event_id}/ticket_tiers", json={
            "tiers": [{"label": "GA", "price": None}],
        })
        self.assertEqual(r2.status_code, 200)
        mine = self._mine(client)
        self.assertEqual(len(mine["ticket_tiers"]), 1)
        self.assertEqual(mine["ticket_tiers"][0]["label"], "GA")
        self.assertIsNone(mine["ticket_tiers"][0]["price"])

    def test_crew_cannot_set_ticket_tiers(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        r = client.put("/api/events/1/ticket_tiers", json={"tiers": []})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
