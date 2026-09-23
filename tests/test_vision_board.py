"""Real database, real Flask test client: the vision board — a Trello-
style pipeline (Idea -> Reaching Out -> Offer Sent -> Booked) that replaced
the old separate vision_notes/ma_offers tables. Covers card CRUD, the
column allowlist, drag-reorder persistence, linking a card to the show
that created the opportunity, and keeping crew out entirely.
"""
import unittest

import app as app_module
import auth
import db


class VisionBoard(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"visioncard-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"visioncard-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Other Booker', %s, %s, 'booker') RETURNING id""",
            (f"visioncard-test-otherbooker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.other_booker_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.other_booker_email = self._email(self.other_booker_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.card_ids = []  # every card created this test, cleaned up in tearDown
        self.event_id = None
        self.artist_ids = []  # any real artist rows find_or_create made along the way

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def tearDown(self):
        cur = self.conn.cursor()
        for card_id in self.card_ids:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'vision_card' AND entity_id = %s", (card_id,))
            cur.execute("DELETE FROM vision_cards WHERE id = %s", (card_id,))
        if self.event_id:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id = %s", (self.event_id,))
            cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        if self.artist_ids:
            # find_or_create makes real artist rows for the bill (Harbor
            # Divide, The Browning) — deleting the event doesn't touch
            # those, so they'd otherwise leak into the real roster forever.
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'artist' AND entity_id = ANY(%s)", (self.artist_ids,))
            cur.execute("DELETE FROM artists WHERE id = ANY(%s)", (self.artist_ids,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s, %s)",
                    (self.booker_id, self.crew_id, self.other_booker_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s, %s)",
                    (self.booker_id, self.crew_id, self.other_booker_id))
        self.conn.commit()
        self.conn.close()

    def _create(self, client, **extra):
        payload = {"title": "Ask Harbor Divide's agent about a follow-up date"}
        payload.update(extra)
        r = client.post("/api/vision_cards", json=payload)
        self.assertEqual(r.status_code, 201, r.get_json())
        card_id = r.get_json()["id"]
        self.card_ids.append(card_id)
        return card_id

    def test_a_new_card_defaults_to_the_idea_column(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        # Real cards may already be on the board (this isn't a clean-slate
        # column), so a new one just needs to land at the end, not at 0.
        existing_max = max([c["sort_order"] for c in client.get("/api/vision_cards").get_json()["cards"]
                             if c["column_key"] == "idea"], default=-1)
        card_id = self._create(client)
        cards = client.get("/api/vision_cards").get_json()["cards"]
        mine = next(c for c in cards if c["id"] == card_id)
        self.assertEqual(mine["column_key"], "idea")
        self.assertGreater(mine["sort_order"], existing_max)

    def test_a_card_can_be_edited_and_moved_to_another_column(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        card_id = self._create(client)
        r = client.patch(f"/api/vision_cards/{card_id}", json={
            "column_key": "offer_sent", "notes": "left a voicemail", "follow_up_date": "2027-01-05",
        })
        self.assertEqual(r.status_code, 200, r.get_json())
        cards = client.get("/api/vision_cards").get_json()["cards"]
        mine = next(c for c in cards if c["id"] == card_id)
        self.assertEqual(mine["column_key"], "offer_sent")
        self.assertEqual(mine["notes"], "left a voicemail")
        self.assertEqual(mine["follow_up_date"], "2027-01-05")

    def test_booked_is_not_a_column_a_booked_show_lives_on_the_calendar(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/vision_cards", json={"title": "x", "column_key": "booked"})
        self.assertEqual(r.status_code, 400)

    def test_reaching_out_is_no_longer_a_column_mao_is_one_stage(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/vision_cards", json={"title": "x", "column_key": "reaching_out"})
        self.assertEqual(r.status_code, 400)

    def test_invalid_column_is_rejected_on_create_and_update(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/vision_cards", json={"title": "x", "column_key": "someday"})
        self.assertEqual(r.status_code, 400)
        card_id = self._create(client)
        r2 = client.patch(f"/api/vision_cards/{card_id}", json={"column_key": "someday"})
        self.assertEqual(r2.status_code, 400)

    def test_title_is_required(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/vision_cards", json={"notes": "no title"})
        self.assertEqual(r.status_code, 400)

    def test_reordering_persists_column_and_sequence(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        a = self._create(client, title="Card A")
        b = self._create(client, title="Card B")
        c = self._create(client, title="Card C")

        r = client.post("/api/vision_cards/reorder", json={"column_key": "offer_sent", "card_ids": [c, a, b]})
        self.assertEqual(r.status_code, 200, r.get_json())

        cards = client.get("/api/vision_cards").get_json()["cards"]
        moved = [x for x in cards if x["id"] in (a, b, c)]
        by_id = {x["id"]: x for x in moved}
        self.assertTrue(all(x["column_key"] == "offer_sent" for x in moved))
        self.assertEqual(by_id[c]["sort_order"], 0)
        self.assertEqual(by_id[a]["sort_order"], 1)
        self.assertEqual(by_id[b]["sort_order"], 2)

    def test_reordering_with_an_invalid_column_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        card_id = self._create(client)
        r = client.post("/api/vision_cards/reorder", json={"column_key": "nope", "card_ids": [card_id]})
        self.assertEqual(r.status_code, 400)

    def test_a_card_can_link_back_to_the_show_that_created_the_opportunity(self):
        """Broc's example: a local band opens a packed show and gets real
        exposure, so the follow-up card should carry that context with it."""
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": ["Harbor Divide", "The Browning"], "show_date": "2027-09-17",
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        self.event_id = r.get_json()["id"]

        card_id = self._create(client, title="Harbor Divide follow-up", trigger_event_id=self.event_id)
        cards = client.get("/api/vision_cards").get_json()["cards"]
        mine = next(c for c in cards if c["id"] == card_id)
        self.assertEqual(mine["trigger_event_id"], self.event_id)
        self.assertEqual(mine["trigger_venue"], "Frankies")
        self.assertEqual(len(mine["trigger_bill_artist_ids"]), 2, "both acts on the triggering show's bill")
        self.artist_ids = mine["trigger_bill_artist_ids"]  # find_or_create made real rows; tearDown removes them by id

    def test_deleting_a_card(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        card_id = self._create(client)
        d = client.delete(f"/api/vision_cards/{card_id}")
        self.assertEqual(d.status_code, 200)
        cards = client.get("/api/vision_cards").get_json()["cards"]
        self.assertNotIn(card_id, [c["id"] for c in cards])

    def test_crew_cannot_see_or_touch_the_vision_board(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        self.assertEqual(client.get("/api/vision_cards").status_code, 403)
        self.assertEqual(client.post("/api/vision_cards", json={"title": "x"}).status_code, 403)

    def test_a_follow_up_can_be_assigned_to_a_booker(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        card_id = self._create(client, follow_up_date="2027-01-05", follow_up_person_id=self.other_booker_id)
        cards = client.get("/api/vision_cards").get_json()["cards"]
        mine = next(c for c in cards if c["id"] == card_id)
        self.assertEqual(mine["follow_up_person_id"], self.other_booker_id)
        self.assertEqual(mine["follow_up_person_name"], "Other Booker")

    def test_assigning_a_follow_up_to_an_unknown_person_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/vision_cards", json={"title": "x", "follow_up_person_id": 999999})
        self.assertEqual(r.status_code, 400)

    def test_assigning_a_follow_up_to_crew_is_rejected(self):
        """Crew can't see the vision board at all, so assigning one a
        follow-up would be a notification nobody could act on."""
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/vision_cards", json={"title": "x", "follow_up_person_id": self.crew_id})
        self.assertEqual(r.status_code, 400)

    def test_my_followups_only_shows_due_or_overdue_cards_assigned_to_me(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        overdue_id = self._create(client, title="Overdue one", follow_up_date="2020-01-01",
                                   follow_up_person_id=self.other_booker_id)
        self._create(client, title="Not due yet", follow_up_date="2099-01-01",
                     follow_up_person_id=self.other_booker_id)
        self._create(client, title="Someone else's", follow_up_date="2020-01-01",
                     follow_up_person_id=self.booker_id)

        other_client = self.app.test_client()
        self._login(other_client, self.other_booker_email)
        r = other_client.get("/api/my_followups")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([f["id"] for f in r.get_json()["followups"]], [overdue_id])

        me = other_client.get("/api/me").get_json()
        self.assertEqual(me["followup_count"], 1)


if __name__ == "__main__":
    unittest.main()
