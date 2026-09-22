"""Real database, real Flask test client: a show's message thread —
posting, listing, and the delete rule (author or owner only).
"""
import unittest

import app as app_module
import auth
import db


class EventMessages(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Owner', %s, %s, 'owner') RETURNING id""",
            (f"msg-test-owner-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.owner_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"msg-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"msg-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
        self.conn.commit()

        self.owner_email = self._email(self.owner_id)
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

    def _book_show(self, client):
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": [f"Message Test Band {id(self)}"], "show_date": "2027-11-01",
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        self.event_id = r.get_json()["id"]

    def tearDown(self):
        cur = self.conn.cursor()
        if self.event_id:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id = %s", (self.event_id,))
            cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))  # cascades event_artists first
            cur.execute("DELETE FROM artists WHERE name = %s", (f"Message Test Band {id(self)}",))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s, %s)",
                    (self.owner_id, self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s, %s)",
                    (self.owner_id, self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def test_posting_and_listing_messages(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book_show(client)

        r = client.post(f"/api/events/{self.event_id}/messages", json={"body": "Confirmed the guarantee with the agent"})
        self.assertEqual(r.status_code, 201, r.get_json())

        messages = client.get(f"/api/events/{self.event_id}/messages").get_json()["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["body"], "Confirmed the guarantee with the agent")
        self.assertEqual(messages[0]["person_name"], "Test Booker")

    def test_empty_message_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book_show(client)
        r = client.post(f"/api/events/{self.event_id}/messages", json={"body": "   "})
        self.assertEqual(r.status_code, 400)

    def test_author_can_delete_their_own_message(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book_show(client)
        r = client.post(f"/api/events/{self.event_id}/messages", json={"body": "oops typo"})
        message_id = r.get_json()["id"]

        d = client.delete(f"/api/messages/{message_id}")
        self.assertEqual(d.status_code, 200)
        messages = client.get(f"/api/events/{self.event_id}/messages").get_json()["messages"]
        self.assertEqual(messages, [])

    def test_owner_can_delete_anyones_message_but_a_second_booker_cannot(self):
        booker_client = self.app.test_client()
        self._login(booker_client, self.booker_email)
        self._book_show(booker_client)

        r = booker_client.post(f"/api/events/{self.event_id}/messages", json={"body": "from the booker"})
        message_id = r.get_json()["id"]
        owner_client = self.app.test_client()
        self._login(owner_client, self.owner_email)
        allowed = owner_client.delete(f"/api/messages/{message_id}")
        self.assertEqual(allowed.status_code, 200, "the owner must be able to delete anyone's message")

        r2 = booker_client.post(f"/api/events/{self.event_id}/messages", json={"body": "another one"})
        message_id_2 = r2.get_json()["id"]
        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Second Booker', %s, %s, 'booker') RETURNING id""",
            (f"msg-test-booker2-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        second_booker_id = cur.fetchone()[0]
        self.conn.commit()
        try:
            second_client = self.app.test_client()
            self._login(second_client, self._email(second_booker_id))
            blocked = second_client.delete(f"/api/messages/{message_id_2}")
            self.assertEqual(blocked.status_code, 403, "a booker who isn't the author and isn't owner must be blocked")
        finally:
            cur.execute("DELETE FROM sessions WHERE person_id = %s", (second_booker_id,))
            cur.execute("DELETE FROM people WHERE id = %s", (second_booker_id,))
            self.conn.commit()

    def test_crew_cannot_see_or_post_messages(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        self.assertEqual(client.get("/api/events/1/messages").status_code, 403)
        self.assertEqual(client.post("/api/events/1/messages", json={"body": "x"}).status_code, 403)

    def test_mentioning_someone_notifies_them(self):
        booker_client = self.app.test_client()
        self._login(booker_client, self.booker_email)
        self._book_show(booker_client)
        r = booker_client.post(f"/api/events/{self.event_id}/messages",
                                json={"body": "@Test Owner can you check the guarantee on this one?"})
        self.assertEqual(r.status_code, 201, r.get_json())

        owner_client = self.app.test_client()
        self._login(owner_client, self.owner_email)
        me = owner_client.get("/api/me").get_json()
        self.assertEqual(me["mention_count"], 1)

        mentions = owner_client.get("/api/my_mentions").get_json()["mentions"]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0]["event_id"], self.event_id)
        self.assertIn("guarantee", mentions[0]["body"])

    def test_opening_the_thread_marks_the_mention_read(self):
        booker_client = self.app.test_client()
        self._login(booker_client, self.booker_email)
        self._book_show(booker_client)
        booker_client.post(f"/api/events/{self.event_id}/messages", json={"body": "@Test Owner take a look"})

        owner_client = self.app.test_client()
        self._login(owner_client, self.owner_email)
        self.assertEqual(owner_client.get("/api/me").get_json()["mention_count"], 1)

        owner_client.get(f"/api/events/{self.event_id}/messages")  # opening the thread reads it

        self.assertEqual(owner_client.get("/api/me").get_json()["mention_count"], 0)
        self.assertEqual(owner_client.get("/api/my_mentions").get_json()["mentions"], [])

    def test_mentioning_yourself_does_not_notify_you(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book_show(client)
        client.post(f"/api/events/{self.event_id}/messages", json={"body": "@Test Booker note to self"})
        self.assertEqual(client.get("/api/me").get_json()["mention_count"], 0)

    def test_crew_are_never_mentionable(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book_show(client)
        r = client.post(f"/api/events/{self.event_id}/messages", json={"body": "@Test Crew heads up"})
        message_id = r.get_json()["id"]
        cur = self.conn.cursor()
        cur.execute("SELECT count(*) FROM event_message_mentions WHERE message_id = %s", (message_id,))
        self.assertEqual(cur.fetchone()[0], 0, "crew is never a valid @mention target")


if __name__ == "__main__":
    unittest.main()
