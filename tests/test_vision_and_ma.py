"""Real database, real Flask test client: the vision board (rough,
editable ideas with an optional target quarter) and the mutually-agreeable
offer tracker (open-ended offers with a follow-up date).
"""
import unittest
from datetime import date

import app as app_module
import auth
import db


class VisionBoardAndMAOffers(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"vision-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"vision-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.note_id = None
        self.offer_id = None

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def tearDown(self):
        cur = self.conn.cursor()
        if self.note_id:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'vision_note' AND entity_id = %s", (self.note_id,))
            cur.execute("DELETE FROM vision_notes WHERE id = %s", (self.note_id,))
        if self.offer_id:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'ma_offer' AND entity_id = %s", (self.offer_id,))
            cur.execute("DELETE FROM ma_offers WHERE id = %s", (self.offer_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def test_booker_can_create_and_edit_a_vision_note(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/vision_notes", json={
            "content": "Ask the Blue Fields agent about a spring residency",
            "target_quarter": 2, "target_year": 2027,
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        self.note_id = r.get_json()["id"]

        notes = client.get("/api/vision_notes").get_json()["notes"]
        mine = next(n for n in notes if n["id"] == self.note_id)
        self.assertEqual(mine["target_quarter"], 2)
        self.assertEqual(mine["target_year"], 2027)

        edit = client.patch(f"/api/vision_notes/{self.note_id}", json={"target_quarter": None, "target_year": None})
        self.assertEqual(edit.status_code, 200, edit.get_json())
        notes = client.get("/api/vision_notes").get_json()["notes"]
        mine = next(n for n in notes if n["id"] == self.note_id)
        self.assertIsNone(mine["target_quarter"])
        self.assertIsNone(mine["target_year"])

    def test_quarter_and_year_must_travel_together(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/vision_notes", json={"content": "x", "target_quarter": 1})
        self.assertEqual(r.status_code, 400)
        r2 = client.post("/api/vision_notes", json={"content": "x", "target_year": 2027})
        self.assertEqual(r2.status_code, 400)

    def test_crew_cannot_see_or_touch_the_vision_board(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        self.assertEqual(client.get("/api/vision_notes").status_code, 403)
        self.assertEqual(client.post("/api/vision_notes", json={"content": "x"}).status_code, 403)

    def test_deleting_a_vision_note(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/vision_notes", json={"content": "temp idea"})
        note_id = r.get_json()["id"]
        d = client.delete(f"/api/vision_notes/{note_id}")
        self.assertEqual(d.status_code, 200)
        notes = client.get("/api/vision_notes").get_json()["notes"]
        self.assertNotIn(note_id, [n["id"] for n in notes])

    def test_booker_can_log_and_follow_up_an_ma_offer(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/ma_offers", json={
            "title": "Foo Fighters 27", "notes": "sent via agent, no deadline given",
            "submitted_date": "2026-09-20", "follow_up_date": "2026-10-01",
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        self.offer_id = r.get_json()["id"]

        offers = client.get("/api/ma_offers").get_json()["offers"]
        mine = next(o for o in offers if o["id"] == self.offer_id)
        self.assertEqual(mine["status"], "open")
        self.assertEqual(mine["submitted_date"], "2026-09-20")
        self.assertEqual(mine["follow_up_date"], "2026-10-01")

        close = client.patch(f"/api/ma_offers/{self.offer_id}", json={"status": "closed"})
        self.assertEqual(close.status_code, 200, close.get_json())
        offers = client.get("/api/ma_offers").get_json()["offers"]
        mine = next(o for o in offers if o["id"] == self.offer_id)
        self.assertEqual(mine["status"], "closed")

    def test_ma_offer_title_is_required(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/ma_offers", json={"notes": "no title"})
        self.assertEqual(r.status_code, 400)

    def test_crew_cannot_see_or_touch_ma_offers(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        self.assertEqual(client.get("/api/ma_offers").status_code, 403)
        self.assertEqual(client.post("/api/ma_offers", json={"title": "x"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
