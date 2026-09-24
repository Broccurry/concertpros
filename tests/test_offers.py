"""Real database, real Flask test client: the Offers board (MAO -> Needed
-> Sent -> Confirmed), its own tab and its own table as of 2026-09-24 --
deliberately separate from the Vision Board (see test_vision_board.py)
since offers run at a much higher volume with a much higher fizzle rate
than an idea worth chasing. Covers card CRUD, the column allowlist,
drag-reorder persistence, the "didn't work out" flag, and keeping crew
out entirely.
"""
import unittest

import app as app_module
import auth
import db


class Offers(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"offer-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"offer-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO artists (name) VALUES (%s) RETURNING id",
            (f"Offer Test Band {id(self)}",),
        )
        self.artist_id = cur.fetchone()[0]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.offer_ids = []

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def _create(self, client, **extra):
        payload = {"title": "MAO: some agent's routing invite"}
        payload.update(extra)
        r = client.post("/api/offers", json=payload)
        self.assertEqual(r.status_code, 201, r.get_json())
        offer_id = r.get_json()["id"]
        self.offer_ids.append(offer_id)
        return offer_id

    def tearDown(self):
        cur = self.conn.cursor()
        for offer_id in self.offer_ids:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'offer' AND entity_id = %s", (offer_id,))
            cur.execute("DELETE FROM offers WHERE id = %s", (offer_id,))
        cur.execute("DELETE FROM audit_log WHERE entity_type = 'artist' AND entity_id = %s", (self.artist_id,))
        cur.execute("DELETE FROM artists WHERE id = %s", (self.artist_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def test_a_new_offer_defaults_to_the_mao_column(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        offer_id = self._create(client)
        offers = client.get("/api/offers").get_json()["offers"]
        mine = next(o for o in offers if o["id"] == offer_id)
        self.assertEqual(mine["column_key"], "mao")
        self.assertFalse(mine["dead"])

    def test_an_offer_can_be_edited_and_moved_to_another_column(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        offer_id = self._create(client)
        r = client.patch(f"/api/offers/{offer_id}", json={
            "column_key": "sent", "artist_id": self.artist_id, "venue_id": self.venue_id,
            "notes": "sent $2000 guarantee", "link": "https://example.com/offer.pdf",
        })
        self.assertEqual(r.status_code, 200, r.get_json())
        offers = client.get("/api/offers").get_json()["offers"]
        mine = next(o for o in offers if o["id"] == offer_id)
        self.assertEqual(mine["column_key"], "sent")
        self.assertEqual(mine["artist_id"], self.artist_id)
        self.assertEqual(mine["venue_id"], self.venue_id)
        self.assertEqual(mine["notes"], "sent $2000 guarantee")

    def test_didnt_work_out_is_a_flag_not_a_column(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        offer_id = self._create(client, column_key="needed")
        r = client.patch(f"/api/offers/{offer_id}", json={"dead": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        offers = client.get("/api/offers").get_json()["offers"]
        mine = next(o for o in offers if o["id"] == offer_id)
        self.assertTrue(mine["dead"])
        self.assertEqual(mine["column_key"], "needed")  # dead doesn't move it off its board

    def test_invalid_column_is_rejected_on_create_and_update(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/offers", json={"title": "x", "column_key": "someday"})
        self.assertEqual(r.status_code, 400)
        offer_id = self._create(client)
        r2 = client.patch(f"/api/offers/{offer_id}", json={"column_key": "someday"})
        self.assertEqual(r2.status_code, 400)

    def test_title_is_required(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/offers", json={"notes": "no title"})
        self.assertEqual(r.status_code, 400)

    def test_reordering_persists_column_and_sequence(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        a = self._create(client, title="Offer A")
        b = self._create(client, title="Offer B")
        c = self._create(client, title="Offer C")

        r = client.post("/api/offers/reorder", json={"column_key": "confirmed", "offer_ids": [c, a, b]})
        self.assertEqual(r.status_code, 200, r.get_json())

        offers = client.get("/api/offers").get_json()["offers"]
        moved = [x for x in offers if x["id"] in (a, b, c)]
        by_id = {x["id"]: x for x in moved}
        self.assertTrue(all(x["column_key"] == "confirmed" for x in moved))
        self.assertEqual(by_id[c]["sort_order"], 0)
        self.assertEqual(by_id[a]["sort_order"], 1)
        self.assertEqual(by_id[b]["sort_order"], 2)

    def test_reordering_with_an_invalid_column_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        offer_id = self._create(client)
        r = client.post("/api/offers/reorder", json={"column_key": "nope", "offer_ids": [offer_id]})
        self.assertEqual(r.status_code, 400)

    def test_deleting_an_offer(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        offer_id = self._create(client)
        r = client.delete(f"/api/offers/{offer_id}")
        self.assertEqual(r.status_code, 200)
        # Leave offer_id in self.offer_ids -- tearDown's cleanup still needs
        # to delete the "delete" action's own audit_log row for it (the
        # offer row itself is already gone, so that DELETE is just a
        # harmless no-op).
        offers = client.get("/api/offers").get_json()["offers"]
        self.assertNotIn(offer_id, [o["id"] for o in offers])

    def test_crew_cannot_see_or_touch_offers_at_all(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        offer_id = self._create(client)

        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        self.assertEqual(crew_client.get("/api/offers").status_code, 403)
        self.assertEqual(crew_client.post("/api/offers", json={"title": "x"}).status_code, 403)
        self.assertEqual(crew_client.patch(f"/api/offers/{offer_id}", json={"dead": True}).status_code, 403)
        self.assertEqual(crew_client.delete(f"/api/offers/{offer_id}").status_code, 403)


if __name__ == "__main__":
    unittest.main()
