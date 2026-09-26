"""Real database, real Flask test client: the Offers v2 ledger build
(2026-09-26) -- deal terms, budgeted expense lines, capacity-based ticket
tiers, PDF export, and linking a confirmed offer to a real show so a
settlement can pre-seed itself from it. Separate from test_offers.py,
which covers the original pipeline-card CRUD (title/column/artist/venue)
that this build extends rather than replaces.
"""
import unittest
from unittest.mock import patch

import app as app_module
import auth
import db


class OfferLedger(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"offerv2-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"offerv2-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO artists (name) VALUES (%s) RETURNING id",
            (f"Offer Ledger Test Band {id(self)}",),
        )
        self.artist_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO offers (title, artist_id, venue_id, created_by) VALUES (%s, %s, %s, %s) RETURNING id",
            (f"Test offer {id(self)}", self.artist_id, self.venue_id, self.booker_id),
        )
        self.offer_id = cur.fetchone()[0]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.event_id = None

        self.put_bytes_patcher = patch("storage.put_bytes", return_value=None)
        self.mock_put_bytes = self.put_bytes_patcher.start()
        self.presign_patcher = patch("storage.presign_download", return_value="https://example.invalid/fake.pdf")
        self.mock_presign = self.presign_patcher.start()

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def _get_offer(self, client):
        offers = client.get("/api/offers").get_json()["offers"]
        return next(o for o in offers if o["id"] == self.offer_id)

    def tearDown(self):
        self.put_bytes_patcher.stop()
        self.presign_patcher.stop()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM audit_log WHERE entity_type = 'offer' AND entity_id = %s", (self.offer_id,))
        if self.event_id:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id = %s", (self.event_id,))
            cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM offers WHERE id = %s", (self.offer_id,))
        cur.execute("DELETE FROM audit_log WHERE entity_type = 'artist' AND entity_id = %s", (self.artist_id,))
        cur.execute("DELETE FROM artists WHERE id = %s", (self.artist_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    # -- deal terms ------------------------------------------------------

    def test_deal_terms_round_trip(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.patch(f"/api/offers/{self.offer_id}", json={
            "deal_type": "Guarantee vs %", "guarantee": 1000, "backend_pct": 70, "template": "detailed",
        })
        self.assertEqual(r.status_code, 200, r.get_json())
        mine = self._get_offer(client)
        self.assertEqual(mine["deal_type"], "Guarantee vs %")
        self.assertAlmostEqual(float(mine["guarantee"]), 1000.0)
        self.assertAlmostEqual(float(mine["backend_pct"]), 70.0)
        self.assertEqual(mine["template"], "detailed")

    def test_invalid_template_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.patch(f"/api/offers/{self.offer_id}", json={"template": "fancy"})
        self.assertEqual(r.status_code, 400)

    def test_invalid_guarantee_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.patch(f"/api/offers/{self.offer_id}", json={"guarantee": "not a number"})
        self.assertEqual(r.status_code, 400)

    # -- expenses ----------------------------------------------------------

    def test_expenses_round_trip_and_replace_whole_set(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.put(f"/api/offers/{self.offer_id}/expenses", json={"lines": [
            {"label": "Security", "budget": 300}, {"label": "Marketing", "budget": 200},
        ]})
        self.assertEqual(r.status_code, 200, r.get_json())
        mine = self._get_offer(client)
        self.assertEqual([e["label"] for e in mine["expenses"]], ["Security", "Marketing"])

        r2 = client.put(f"/api/offers/{self.offer_id}/expenses", json={"lines": [
            {"label": "Security", "budget": 300},
        ]})
        self.assertEqual(r2.status_code, 200, r2.get_json())
        mine2 = self._get_offer(client)
        self.assertEqual([e["label"] for e in mine2["expenses"]], ["Security"])

    def test_a_blank_expense_label_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.put(f"/api/offers/{self.offer_id}/expenses", json={"lines": [
            {"label": "", "budget": 100},
        ]})
        self.assertEqual(r.status_code, 400)

    # -- ticket tiers (capacity, not sold) ---------------------------------

    def test_ticket_tiers_round_trip_with_capacity(self):
        """Broc's own worked example: 100 seats @ $20 + 100 GA @ $10 ->
        200 capacity, $3,000 possible gross at sellout."""
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.put(f"/api/offers/{self.offer_id}/ticket_tiers", json={"tiers": [
            {"label": "Seated", "price": 20, "capacity": 100},
            {"label": "GA", "price": 10, "capacity": 100},
        ]})
        self.assertEqual(r.status_code, 200, r.get_json())
        mine = self._get_offer(client)
        tiers = mine["ticket_tiers"]
        self.assertEqual([t["label"] for t in tiers], ["Seated", "GA"])
        total_capacity = sum(t["capacity"] for t in tiers)
        possible_gross = sum(float(t["price"]) * t["capacity"] for t in tiers)
        self.assertEqual(total_capacity, 200)
        self.assertAlmostEqual(possible_gross, 3000.0)

    def test_a_blank_tier_label_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.put(f"/api/offers/{self.offer_id}/ticket_tiers", json={"tiers": [
            {"label": "", "price": 20, "capacity": 100},
        ]})
        self.assertEqual(r.status_code, 400)

    def test_invalid_capacity_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.put(f"/api/offers/{self.offer_id}/ticket_tiers", json={"tiers": [
            {"label": "Seated", "price": 20, "capacity": "a lot"},
        ]})
        self.assertEqual(r.status_code, 400)

    # -- PDF ----------------------------------------------------------------

    def test_pdf_is_a_real_pdf(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        client.patch(f"/api/offers/{self.offer_id}", json={"deal_type": "Guarantee", "guarantee": 500})
        client.put(f"/api/offers/{self.offer_id}/expenses", json={"lines": [{"label": "Security", "budget": 100}]})
        client.put(f"/api/offers/{self.offer_id}/ticket_tiers", json={"tiers": [
            {"label": "GA", "price": 15, "capacity": 200},
        ]})
        r = client.get(f"/api/offers/{self.offer_id}/pdf")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["url"], "https://example.invalid/fake.pdf")
        pdf_bytes = self.mock_put_bytes.call_args[0][1]
        content_type = self.mock_put_bytes.call_args[0][2]
        self.assertEqual(pdf_bytes[:4], b"%PDF", "must be a real PDF, not a stub")
        self.assertEqual(content_type, "application/pdf")

    def test_pdf_for_unknown_offer_is_404(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.get("/api/offers/999999999/pdf")
        self.assertEqual(r.status_code, 404)

    # -- linking to a real show ----------------------------------------------

    def test_linking_to_a_show_sets_event_id_and_files_a_real_pdf(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        client.patch(f"/api/offers/{self.offer_id}", json={"deal_type": "Guarantee", "guarantee": 750})

        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO events (venue_id, show_date, status, created_by) VALUES (%s, '2027-04-01', 'confirmed', %s) RETURNING id",
            (self.venue_id, self.booker_id),
        )
        self.event_id = cur.fetchone()[0]
        self.conn.commit()

        r = client.post(f"/api/offers/{self.offer_id}/link_event", json={"event_id": self.event_id})
        self.assertEqual(r.status_code, 200, r.get_json())

        mine = self._get_offer(client)
        self.assertEqual(mine["event_id"], self.event_id)

        pdf_bytes = self.mock_put_bytes.call_args[0][1]
        self.assertEqual(pdf_bytes[:4], b"%PDF")
        cur.execute(
            "SELECT count(*), content_type FROM event_files WHERE event_id = %s AND filename = 'Offer Sheet.pdf' "
            "GROUP BY content_type",
            (self.event_id,),
        )
        count, content_type = cur.fetchone()
        self.assertEqual(count, 1)
        self.assertEqual(content_type, "application/pdf")

        # Re-linking (or re-saving after linking) updates the same file,
        # not a second copy -- same convention as the settlement PDF.
        r2 = client.post(f"/api/offers/{self.offer_id}/link_event", json={"event_id": self.event_id})
        self.assertEqual(r2.status_code, 200, r2.get_json())
        cur.execute(
            "SELECT count(*) FROM event_files WHERE event_id = %s AND filename = 'Offer Sheet.pdf'",
            (self.event_id,),
        )
        self.assertEqual(cur.fetchone()[0], 1)

    def test_linking_to_an_unknown_event_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post(f"/api/offers/{self.offer_id}/link_event", json={"event_id": 999999999})
        self.assertEqual(r.status_code, 400)

    def test_a_linked_offer_shows_up_on_the_events_endpoint(self):
        """This is what a fresh settlement pre-seeds itself from client-
        side -- confirms the plumbing (permissions.py) actually attaches
        it, not just that the offer's own endpoint knows about the link."""
        client = self.app.test_client()
        self._login(client, self.booker_email)
        client.patch(f"/api/offers/{self.offer_id}", json={"deal_type": "Door split", "backend_pct": 60})
        client.put(f"/api/offers/{self.offer_id}/expenses", json={"lines": [{"label": "Security", "budget": 100}]})
        client.put(f"/api/offers/{self.offer_id}/ticket_tiers", json={"tiers": [
            {"label": "GA", "price": 15, "capacity": 200},
        ]})

        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO events (venue_id, show_date, status, created_by) VALUES (%s, '2027-04-02', 'confirmed', %s) RETURNING id",
            (self.venue_id, self.booker_id),
        )
        self.event_id = cur.fetchone()[0]
        self.conn.commit()
        client.post(f"/api/offers/{self.offer_id}/link_event", json={"event_id": self.event_id})

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertIsNotNone(mine["offer"])
        self.assertEqual(mine["offer"]["deal_type"], "Door split")
        self.assertAlmostEqual(float(mine["offer"]["backend_pct"]), 60.0)
        self.assertEqual([e["label"] for e in mine["offer"]["expenses"]], ["Security"])
        self.assertEqual([t["label"] for t in mine["offer"]["ticket_tiers"]], ["GA"])

    def test_an_event_with_no_linked_offer_has_a_null_offer(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO events (venue_id, show_date, status, created_by) VALUES (%s, '2027-04-03', 'confirmed', %s) RETURNING id",
            (self.venue_id, self.booker_id),
        )
        self.event_id = cur.fetchone()[0]
        self.conn.commit()
        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertIsNone(mine["offer"])

    # -- crew boundary --------------------------------------------------------

    def test_crew_cannot_touch_offer_ledger_endpoints(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        self.assertEqual(client.put(f"/api/offers/{self.offer_id}/expenses", json={"lines": []}).status_code, 403)
        self.assertEqual(client.put(f"/api/offers/{self.offer_id}/ticket_tiers", json={"tiers": []}).status_code, 403)
        self.assertEqual(client.get(f"/api/offers/{self.offer_id}/pdf").status_code, 403)
        self.assertEqual(client.post(f"/api/offers/{self.offer_id}/link_event", json={"event_id": 1}).status_code, 403)

    def test_crew_never_receives_the_offer_field(self):
        """A crew viewer's /api/events call never includes 'offer' at
        all -- crew's query never joins the offers table in the first
        place (same boundary as settlement/guarantee/etc)."""
        client = self.app.test_client()
        self._login(client, self.crew_email)
        r = client.get("/api/events")
        self.assertEqual(r.status_code, 200)
        for e in r.get_json()["events"]:
            self.assertNotIn("offer", e)


if __name__ == "__main__":
    unittest.main()
