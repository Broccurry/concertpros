"""Real database, real Flask test client: general ops to-dos ("order
ticket stock", "fix the marquee sign") — separate from a show's own
per-event checklist. Crew sees and can act on only what's assigned to
them; booker/owner see and assign everything.
"""
import unittest

import app as app_module
import auth
import db


class Todos(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"todo-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"todo-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Other Crew', %s, %s, 'crew') RETURNING id""",
            (f"todo-test-othercrew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.other_crew_id = cur.fetchone()[0]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.other_crew_email = self._email(self.other_crew_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.todo_ids = []

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def tearDown(self):
        cur = self.conn.cursor()
        for todo_id in self.todo_ids:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'todo' AND entity_id = %s", (todo_id,))
            cur.execute("DELETE FROM todos WHERE id = %s", (todo_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s, %s)",
                    (self.booker_id, self.crew_id, self.other_crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s, %s)",
                    (self.booker_id, self.crew_id, self.other_crew_id))
        self.conn.commit()
        self.conn.close()

    def _create(self, client, **extra):
        payload = {"title": "Order more ticket stock"}
        payload.update(extra)
        r = client.post("/api/todos", json=payload)
        self.assertEqual(r.status_code, 201, r.get_json())
        todo_id = r.get_json()["id"]
        self.todo_ids.append(todo_id)
        return todo_id

    def test_booker_can_create_and_assign_a_task(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        todo_id = self._create(client, assigned_to=self.crew_id, due_date="2027-01-15")
        todos = client.get("/api/todos").get_json()["todos"]
        mine = next(t for t in todos if t["id"] == todo_id)
        self.assertEqual(mine["assigned_to"], self.crew_id)
        self.assertEqual(mine["assigned_to_name"], "Test Crew")
        self.assertEqual(mine["due_date"], "2027-01-15")
        self.assertFalse(mine["done"])

    def test_an_unassigned_task_is_fine(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        todo_id = self._create(client)
        todos = client.get("/api/todos").get_json()["todos"]
        mine = next(t for t in todos if t["id"] == todo_id)
        self.assertIsNone(mine["assigned_to"])

    def test_title_is_required(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/todos", json={"notes": "no title"})
        self.assertEqual(r.status_code, 400)

    def test_assigning_to_an_unknown_person_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/todos", json={"title": "x", "assigned_to": 999999})
        self.assertEqual(r.status_code, 400)

    def test_crew_sees_only_their_own_tasks(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        mine_id = self._create(client, title="Mine", assigned_to=self.crew_id)
        self._create(client, title="Not mine", assigned_to=self.other_crew_id)
        self._create(client, title="Unassigned")

        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        todos = crew_client.get("/api/todos").get_json()["todos"]
        self.assertEqual([t["id"] for t in todos], [mine_id])

    def test_crew_can_mark_their_own_task_done(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        todo_id = self._create(client, assigned_to=self.crew_id)

        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        r = crew_client.patch(f"/api/todos/{todo_id}", json={"done": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        todos = client.get("/api/todos").get_json()["todos"]
        mine = next(t for t in todos if t["id"] == todo_id)
        self.assertTrue(mine["done"])

    def test_crew_cannot_mark_someone_elses_task_done(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        todo_id = self._create(client, assigned_to=self.other_crew_id)

        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        r = crew_client.patch(f"/api/todos/{todo_id}", json={"done": True})
        self.assertEqual(r.status_code, 403)

    def test_crew_cannot_edit_fields_other_than_done(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        todo_id = self._create(client, assigned_to=self.crew_id)

        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        r = crew_client.patch(f"/api/todos/{todo_id}", json={"title": "hijacked"})
        self.assertEqual(r.status_code, 403)

    def test_crew_cannot_create_a_task(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        r = client.post("/api/todos", json={"title": "x"})
        self.assertEqual(r.status_code, 403)

    def test_crew_cannot_delete_a_task(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        todo_id = self._create(client, assigned_to=self.crew_id)

        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        r = crew_client.delete(f"/api/todos/{todo_id}")
        self.assertEqual(r.status_code, 403)

    def test_booker_can_edit_and_reassign_a_task(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        todo_id = self._create(client, assigned_to=self.crew_id)
        r = client.patch(f"/api/todos/{todo_id}", json={
            "title": "Order MORE ticket stock", "assigned_to": self.other_crew_id, "notes": "rush it",
        })
        self.assertEqual(r.status_code, 200, r.get_json())
        todos = client.get("/api/todos").get_json()["todos"]
        mine = next(t for t in todos if t["id"] == todo_id)
        self.assertEqual(mine["title"], "Order MORE ticket stock")
        self.assertEqual(mine["assigned_to"], self.other_crew_id)
        self.assertEqual(mine["notes"], "rush it")

    def test_deleting_a_task(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        todo_id = self._create(client)
        r = client.delete(f"/api/todos/{todo_id}")
        self.assertEqual(r.status_code, 200)
        todos = client.get("/api/todos").get_json()["todos"]
        self.assertNotIn(todo_id, [t["id"] for t in todos])


if __name__ == "__main__":
    unittest.main()
