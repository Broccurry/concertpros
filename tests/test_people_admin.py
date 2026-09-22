"""Real database, real Flask test client: creating accounts, the
elevation rule (a booker can only touch crew, never another booker or the
owner), resetting passwords, and setting job-role coverage.
"""
import unittest

import app as app_module
import auth
import db


class PeopleAdmin(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"
        self._made_person_ids = []

        self.booker_id = self._make_person(cur, "Test Booker", "booker")
        self.crew_id = self._make_person(cur, "Test Crew", "crew")
        self.owner_id = self._make_person(cur, "Test Owner", "owner")
        cur.execute("SELECT id FROM roles WHERE name IN ('Sound', 'Door') ORDER BY name")
        self.sound_id, self.door_id = [r[0] for r in cur.fetchall()]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.owner_email = self._email(self.owner_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True

    def _make_person(self, cur, name, level):
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (name, f"padmin-{level}-{id(self)}@example.invalid", auth.hash_password(self.password), level),
        )
        pid = cur.fetchone()[0]
        self._made_person_ids.append(pid)
        return pid

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r

    def tearDown(self):
        cur = self.conn.cursor()
        # anyone created BY a test (new hires) plus the fixture people
        cur.execute("SELECT id FROM people WHERE email LIKE %s", (f"%{id(self)}%",))
        all_ids = [r[0] for r in cur.fetchall()] + self._made_person_ids
        all_ids = list(set(all_ids))
        if all_ids:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'person' AND entity_id = ANY(%s)", (all_ids,))
            cur.execute("DELETE FROM person_roles WHERE person_id = ANY(%s)", (all_ids,))
            cur.execute("DELETE FROM sessions WHERE person_id = ANY(%s)", (all_ids,))
            cur.execute("DELETE FROM people WHERE id = ANY(%s)", (all_ids,))
        self.conn.commit()
        self.conn.close()

    def test_booker_can_create_and_manage_a_crew_member(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)

        r = client.post("/api/people", json={
            "name": "New Hire", "email": f"new-hire-{id(self)}@example.invalid",
            "access_level": "crew", "phone": "419-555-0100",
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        new_id = r.get_json()["id"]
        temp_password = r.get_json()["temp_password"]
        self.assertTrue(temp_password)

        listed = client.get("/api/people").get_json()["people"]
        mine = next(p for p in listed if p["id"] == new_id)
        self.assertEqual(mine["access_level"], "crew")
        self.assertEqual(mine["roles"], [])

        roles_resp = client.put(f"/api/people/{new_id}/roles", json={"role_ids": [self.sound_id, self.door_id]})
        self.assertEqual(roles_resp.status_code, 200)
        listed = client.get("/api/people").get_json()["people"]
        mine = next(p for p in listed if p["id"] == new_id)
        self.assertEqual({r["name"] for r in mine["roles"]}, {"Sound", "Door"})

        reset = client.post(f"/api/people/{new_id}/reset-password")
        self.assertEqual(reset.status_code, 200)
        new_temp = reset.get_json()["temp_password"]
        self.assertNotEqual(new_temp, temp_password)
        # the new temp password actually works — round-trip through login
        other_client = self.app.test_client()
        login = other_client.post("/api/login", json={
            "email": self._email(new_id), "password": new_temp,
        })
        self.assertEqual(login.status_code, 200)

        deact = client.patch(f"/api/people/{new_id}", json={"active": False})
        self.assertEqual(deact.status_code, 200)
        listed = client.get("/api/people").get_json()["people"]
        self.assertFalse(next(p for p in listed if p["id"] == new_id)["active"])

    def test_booker_cannot_elevate_or_touch_higher_rank(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)

        create_booker = client.post("/api/people", json={
            "name": "Should Not Exist", "email": f"nope-{id(self)}@example.invalid", "access_level": "booker",
        })
        self.assertEqual(create_booker.status_code, 403)

        edit_owner = client.patch(f"/api/people/{self.owner_id}", json={"name": "Renamed"})
        self.assertEqual(edit_owner.status_code, 403)

        promote_crew = client.patch(f"/api/people/{self.crew_id}", json={"access_level": "booker"})
        self.assertEqual(promote_crew.status_code, 403)

        reset_owner = client.post(f"/api/people/{self.owner_id}/reset-password")
        self.assertEqual(reset_owner.status_code, 403)

    def test_owner_can_create_and_promote_bookers(self):
        client = self.app.test_client()
        self._login(client, self.owner_email)

        create_booker = client.post("/api/people", json={
            "name": "New Booker", "email": f"new-booker-{id(self)}@example.invalid", "access_level": "booker",
        })
        self.assertEqual(create_booker.status_code, 201, create_booker.get_json())

        promote = client.patch(f"/api/people/{self.crew_id}", json={"access_level": "booker"})
        self.assertEqual(promote.status_code, 200)
        cur = self.conn.cursor()
        cur.execute("SELECT access_level FROM people WHERE id = %s", (self.crew_id,))
        self.assertEqual(cur.fetchone()[0], "booker")

    def test_crew_is_forbidden_from_people_admin(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        self.assertEqual(client.get("/api/people").status_code, 403)
        self.assertEqual(
            client.post("/api/people", json={"name": "X", "email": "x@example.invalid"}).status_code, 403)

    def test_duplicate_email_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/people", json={"name": "Dup", "email": self.crew_email, "access_level": "crew"})
        self.assertEqual(r.status_code, 400)

    def test_unknown_role_id_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.put(f"/api/people/{self.crew_id}/roles", json={"role_ids": [999999]})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
