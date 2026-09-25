"""Real database, real Flask test client: the Hive Mind suggestion board
-- the one board in this app open to every access level, since it holds
no pricing/hold/deal data for rule 1 to gate. Posting, listing (newest
idea first, replies nested oldest-first), and the delete rule (author or
owner only, same shape as event_messages).
"""
import unittest

import app as app_module
import auth
import db


class Hivemind(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Owner', %s, %s, 'owner') RETURNING id""",
            (f"hivemind-test-owner-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.owner_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"hivemind-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"hivemind-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        self.conn.commit()

        self.owner_email = self._email(self.owner_id)
        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.idea_ids = []

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def tearDown(self):
        cur = self.conn.cursor()
        if self.idea_ids:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'hivemind' AND entity_id = ANY(%s)",
                        (self.idea_ids,))
            cur.execute("DELETE FROM hivemind_ideas WHERE id = ANY(%s)", (self.idea_ids,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s, %s)",
                    (self.owner_id, self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s, %s)",
                    (self.owner_id, self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def _post(self, client, body, parent_idea_id=None):
        payload = {"body": body}
        if parent_idea_id is not None:
            payload["parent_idea_id"] = parent_idea_id
        r = client.post("/api/hivemind", json=payload)
        if r.status_code == 201:
            self.idea_ids.append(r.get_json()["id"])
        return r

    def test_crew_can_post_and_see_ideas(self):
        """The whole point of this board -- crew are locked out of Vision
        Board, Offers, and every dollar figure, but Hive Mind has none of
        that, so they get full read/write here."""
        client = self.app.test_client()
        self._login(client, self.crew_email)
        r = self._post(client, "We should book a Wheatus cover night")
        self.assertEqual(r.status_code, 201, r.get_json())

        ideas = client.get("/api/hivemind").get_json()["ideas"]
        self.assertEqual(len(ideas), 1)
        self.assertEqual(ideas[0]["body"], "We should book a Wheatus cover night")
        self.assertEqual(ideas[0]["person_name"], "Test Crew")

    def test_empty_idea_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = self._post(client, "   ")
        self.assertEqual(r.status_code, 400)

    def test_newest_idea_is_listed_first(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._post(client, "first idea")
        self._post(client, "second idea")
        ideas = client.get("/api/hivemind").get_json()["ideas"]
        self.assertEqual([i["body"] for i in ideas], ["second idea", "first idea"])

    def test_a_reply_nests_under_its_idea_not_as_its_own_top_level_row(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        idea = self._post(client, "book more metal shows").get_json()
        self._post(client, "yes please", parent_idea_id=idea["id"])

        ideas = client.get("/api/hivemind").get_json()["ideas"]
        self.assertEqual(len(ideas), 1)
        self.assertEqual(len(ideas[0]["replies"]), 1)
        self.assertEqual(ideas[0]["replies"][0]["body"], "yes please")

    def test_replying_with_an_unknown_parent_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = self._post(client, "a reply to nothing", parent_idea_id=999999999)
        self.assertEqual(r.status_code, 400)

    def test_author_can_delete_their_own_idea(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        idea = self._post(client, "oops").get_json()

        d = client.delete(f"/api/hivemind/{idea['id']}")
        self.assertEqual(d.status_code, 200)
        # Deliberately NOT removing idea["id"] from self.idea_ids here --
        # the row itself is already gone, but tearDown still needs that id
        # to clean up the audit_log row this delete wrote (audit_log rows
        # aren't cascade-deleted with the idea), or the later DELETE FROM
        # people hits a leftover foreign-key reference.
        ideas = client.get("/api/hivemind").get_json()["ideas"]
        self.assertEqual(ideas, [])

    def test_owner_can_delete_anyones_idea_but_a_second_booker_cannot(self):
        booker_client = self.app.test_client()
        self._login(booker_client, self.booker_email)
        idea = self._post(booker_client, "my idea").get_json()

        other_booker_id = None
        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Second Booker', %s, %s, 'booker') RETURNING id""",
            (f"hivemind-test-booker2-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        other_booker_id = cur.fetchone()[0]
        self.conn.commit()
        other_booker_email = self._email(other_booker_id)
        try:
            other_client = self.app.test_client()
            self._login(other_client, other_booker_email)
            r = other_client.delete(f"/api/hivemind/{idea['id']}")
            self.assertEqual(r.status_code, 403)

            owner_client = self.app.test_client()
            self._login(owner_client, self.owner_email)
            r2 = owner_client.delete(f"/api/hivemind/{idea['id']}")
            self.assertEqual(r2.status_code, 200)
            # idea["id"] deliberately stays in self.idea_ids -- see the
            # comment in test_author_can_delete_their_own_idea.
        finally:
            cur.execute("DELETE FROM sessions WHERE person_id = %s", (other_booker_id,))
            cur.execute("DELETE FROM people WHERE id = %s", (other_booker_id,))
            self.conn.commit()

    def test_deleting_not_found_idea_is_404(self):
        client = self.app.test_client()
        self._login(client, self.owner_email)
        r = client.delete("/api/hivemind/999999999")
        self.assertEqual(r.status_code, 404)

    def test_logged_out_cannot_read_or_post(self):
        client = self.app.test_client()
        r = client.get("/api/hivemind")
        self.assertEqual(r.status_code, 401)
        r = client.post("/api/hivemind", json={"body": "sneaky"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
