"""Real database, real Flask test client: the public website. The
Anthropic call is mocked out -- these tests check the DB bookkeeping,
the permission gate, and that the public routes never leak an
unpublished show, not that Anthropic's API is reachable.
"""
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import app as app_module
import auth
import db
from app import _anthropic_complete


class AnthropicCompleteParsing(unittest.TestCase):
    """A real response can put a "thinking" block before the text block
    (hit live while building the September example site -- content[0]
    isn't reliably the text)."""

    def _mock_response(self, payload):
        body = json.dumps(payload).encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value = io.BytesIO(body)
        cm.__exit__.return_value = False
        return cm

    def test_finds_the_text_block_after_a_leading_thinking_block(self):
        payload = {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "Come on out."}]}
        with patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            self.assertEqual(_anthropic_complete("fake-key", "prompt"), "Come on out.")

    def test_works_when_text_is_already_first(self):
        payload = {"content": [{"type": "text", "text": "Come on out."}]}
        with patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            self.assertEqual(_anthropic_complete("fake-key", "prompt"), "Come on out.")

    def test_raises_a_clear_error_when_no_text_block_exists(self):
        payload = {"content": [{"type": "thinking", "thinking": "..."}]}
        with patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            with self.assertRaises(ValueError):
                _anthropic_complete("fake-key", "prompt")


class Website(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"website-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"website-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues LIMIT 1")
        self.venue_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO events (venue_id, show_date, status, created_by)
               VALUES (%s, '2027-06-01', 'confirmed', %s) RETURNING id""",
            (self.venue_id, self.booker_id),
        )
        self.event_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO event_files (event_id, filename, storage_key, content_type)
               VALUES (%s, 'flyer.jpg', %s, 'image/jpeg') RETURNING id""",
            (self.event_id, f"website-test-{id(self)}-flyer.jpg"),
        )
        self.file_id = cur.fetchone()[0]
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
        cur.execute("DELETE FROM event_files WHERE event_id = %s", (self.event_id,))
        cur.execute("DELETE FROM event_website WHERE event_id = %s", (self.event_id,))
        cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def test_website_defaults_when_nothing_set(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.get(f"/api/events/{self.event_id}/website")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"hero_file_id": None, "blurb": None, "published": False})

    def test_setting_website_fields_round_trips(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.put(f"/api/events/{self.event_id}/website", json={
            "hero_file_id": self.file_id, "blurb": "A great show.", "published": True,
        })
        self.assertEqual(r.status_code, 200, r.get_json())
        got = client.get(f"/api/events/{self.event_id}/website").get_json()
        self.assertEqual(got["hero_file_id"], self.file_id)
        self.assertEqual(got["blurb"], "A great show.")
        self.assertTrue(got["published"])

    def test_hero_file_from_another_event_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO event_files (filename, storage_key, content_type) "
            "VALUES ('other.jpg', %s, 'image/jpeg') RETURNING id",
            (f"website-test-{id(self)}-other.jpg",),
        )
        other_file_id = cur.fetchone()[0]
        self.conn.commit()
        try:
            r = client.put(f"/api/events/{self.event_id}/website", json={"hero_file_id": other_file_id})
            self.assertEqual(r.status_code, 400)
        finally:
            cur.execute("DELETE FROM event_files WHERE id = %s", (other_file_id,))
            self.conn.commit()

    def test_crew_cannot_touch_website_settings(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        self.assertEqual(client.get(f"/api/events/{self.event_id}/website").status_code, 403)
        self.assertEqual(client.put(f"/api/events/{self.event_id}/website", json={"published": True}).status_code, 403)

    def test_generating_a_blurb_needs_a_headliner(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        with patch("app._anthropic_complete", return_value="Should not be called"):
            r = client.post(f"/api/events/{self.event_id}/website/blurb")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "no_headliner")

    def test_generating_a_blurb_uses_the_headliner_and_venue(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": ["Test Headliner", "Test Support"], "show_date": "2027-06-02",
        })
        events = client.get("/api/events").get_json()["events"]
        headliner_event = next(e for e in events if e["show_date"] == "2027-06-02")
        with patch("app._anthropic_complete", return_value="A can't-miss night of music.") as mock_complete:
            r = client.post(f"/api/events/{headliner_event['id']}/website/blurb")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["blurb"], "A can't-miss night of music.")
        prompt = mock_complete.call_args[0][1]
        self.assertIn("Test Headliner", prompt)
        self.assertIn("Test Support", prompt)
        cur = self.conn.cursor()
        cur.execute("DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id = %s", (headliner_event["id"],))
        cur.execute("DELETE FROM events WHERE id = %s", (headliner_event["id"],))
        self.conn.commit()

    def test_public_site_never_shows_an_unpublished_show(self):
        client = self.app.test_client()
        r = client.get("/site")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(f"/site/{self.event_id}", r.get_data(as_text=True))

        r2 = client.get(f"/site/{self.event_id}")
        self.assertEqual(r2.status_code, 404)

    def test_public_site_shows_a_published_show_with_no_login(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        client.put(f"/api/events/{self.event_id}/website", json={
            "hero_file_id": self.file_id, "blurb": "Come on out.", "published": True,
        })
        anon = self.app.test_client()  # no session cookie at all
        r = anon.get("/site")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"/site/{self.event_id}", r.get_data(as_text=True))

        r2 = anon.get(f"/site/{self.event_id}")
        self.assertEqual(r2.status_code, 200)
        body = r2.get_data(as_text=True)
        self.assertIn("Come on out.", body)
        # A public page must never surface booker-only fields even by
        # accident -- there's no template variable for these at all, but
        # assert it anyway so a future template change can't quietly leak one.
        self.assertNotIn("guarantee", body.lower())
        self.assertNotIn("settlement", body.lower())


if __name__ == "__main__":
    unittest.main()
