"""Real database, real Flask test client: per-act notes/declined/bill_role,
the contact log that stops two bookers from calling the same band, and the
event-level ticket_link field — plus proof that booking a NEW show actually
persists doors/show_time/deal/notes/ticket_link instead of silently
dropping them (a gap create_event had before this fixed it).
"""
import unittest

import app as app_module
import auth
import db


class ActDetailsAndContactLog(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"actdetail-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"actdetail-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
        self.artist_name = f"Act Detail Test Band {id(self)}"
        self.conn.commit()

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

    def _book(self, client, **extra):
        payload = {"venue_id": self.venue_id, "acts": [self.artist_name], "show_date": "2027-11-01"}
        payload.update(extra)
        r = client.post("/api/events", json=payload)
        self.assertEqual(r.status_code, 201, r.get_json())
        self.event_id = r.get_json()["id"]
        return r

    def tearDown(self):
        cur = self.conn.cursor()
        if self.event_id:
            cur.execute("DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id = %s", (self.event_id,))
            cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM artists WHERE name = %s", (self.artist_name,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    # -- per-act notes / declined / bill_role --------------------------

    def test_notes_declined_and_bill_role_round_trip(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client)

        put = client.put(f"/api/events/{self.event_id}/artists", json={
            "artists": [{"name": self.artist_name, "confirmed": False, "declined": True,
                         "notes": "Needs a vegan rider", "bill_role": "Direct Support"}],
        })
        self.assertEqual(put.status_code, 200, put.get_json())

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        act = mine["artists"][0]
        self.assertTrue(act["declined"])
        self.assertEqual(act["notes"], "Needs a vegan rider")
        self.assertEqual(act["bill_role"], "Direct Support")

    def test_invalid_bill_role_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client)
        put = client.put(f"/api/events/{self.event_id}/artists", json={
            "artists": [{"name": self.artist_name, "bill_role": "Galactic Headliner"}],
        })
        self.assertEqual(put.status_code, 400)

    def test_set_time_round_trips_and_survives_a_second_save(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client)

        put = client.put(f"/api/events/{self.event_id}/artists", json={
            "artists": [{"name": self.artist_name, "set_time": "21:30", "set_time_end": "22:15"}],
        })
        self.assertEqual(put.status_code, 200, put.get_json())
        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(mine["artists"][0]["set_time"], "21:30:00")
        self.assertEqual(mine["artists"][0]["set_time_end"], "22:15:00")

        # Same act, no set_time in the payload this time -- an existing act
        # is matched by artist_id and updated in place, so this should
        # clear it rather than leaving the old value silently stuck.
        put2 = client.put(f"/api/events/{self.event_id}/artists", json={
            "artists": [{"name": self.artist_name}],
        })
        self.assertEqual(put2.status_code, 200, put2.get_json())
        events2 = client.get("/api/events").get_json()["events"]
        mine2 = next(e for e in events2 if e["id"] == self.event_id)
        self.assertIsNone(mine2["artists"][0]["set_time"])
        self.assertIsNone(mine2["artists"][0]["set_time_end"])

    def test_invalid_set_time_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client)
        put = client.put(f"/api/events/{self.event_id}/artists", json={
            "artists": [{"name": self.artist_name, "set_time": "not-a-time"}],
        })
        self.assertEqual(put.status_code, 400)

    def test_an_acts_own_row_survives_repeated_saves(self):
        """Upsert-by-artist_id: saving the bill twice with the same act
        must not mint a new event_artists row (and so must not orphan its
        contact log) as long as the same artist stays on the bill."""
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client)
        events = client.get("/api/events").get_json()["events"]
        act_id_1 = next(e for e in events if e["id"] == self.event_id)["artists"][0]["id"]

        put = client.put(f"/api/events/{self.event_id}/artists", json={
            "artists": [{"name": self.artist_name, "notes": "second save"}],
        })
        self.assertEqual(put.status_code, 200)
        events = client.get("/api/events").get_json()["events"]
        act_id_2 = next(e for e in events if e["id"] == self.event_id)["artists"][0]["id"]
        self.assertEqual(act_id_1, act_id_2, "the same act's row must survive across saves")

    # -- contact log -----------------------------------------------------

    def test_logging_a_contact_records_method_and_person_and_it_lists_back(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client)
        events = client.get("/api/events").get_json()["events"]
        act_id = next(e for e in events if e["id"] == self.event_id)["artists"][0]["id"]

        r = client.post(f"/api/event_artists/{act_id}/contacts", json={"method": "phone"})
        self.assertEqual(r.status_code, 201, r.get_json())

        events = client.get("/api/events").get_json()["events"]
        act = next(e for e in events if e["id"] == self.event_id)["artists"][0]
        self.assertEqual(len(act["contacts"]), 1)
        self.assertEqual(act["contacts"][0]["method"], "phone")
        self.assertEqual(act["contacts"][0]["person_name"], "Test Booker")

    def test_a_contact_can_carry_its_own_note(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client)
        events = client.get("/api/events").get_json()["events"]
        act_id = next(e for e in events if e["id"] == self.event_id)["artists"][0]["id"]

        r = client.post(f"/api/event_artists/{act_id}/contacts",
                         json={"method": "text", "note": "Kevin — checking with band"})
        self.assertEqual(r.status_code, 201, r.get_json())

        events = client.get("/api/events").get_json()["events"]
        act = next(e for e in events if e["id"] == self.event_id)["artists"][0]
        self.assertEqual(act["contacts"][0]["note"], "Kevin — checking with band")

    def test_a_contact_without_a_note_is_fine(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client)
        events = client.get("/api/events").get_json()["events"]
        act_id = next(e for e in events if e["id"] == self.event_id)["artists"][0]["id"]
        r = client.post(f"/api/event_artists/{act_id}/contacts", json={"method": "phone"})
        self.assertEqual(r.status_code, 201)
        events = client.get("/api/events").get_json()["events"]
        act = next(e for e in events if e["id"] == self.event_id)["artists"][0]
        self.assertIsNone(act["contacts"][0]["note"])

    def test_messenger_is_a_valid_contact_method(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client)
        events = client.get("/api/events").get_json()["events"]
        act_id = next(e for e in events if e["id"] == self.event_id)["artists"][0]["id"]
        r = client.post(f"/api/event_artists/{act_id}/contacts", json={"method": "messenger"})
        self.assertEqual(r.status_code, 201, r.get_json())

    def test_invalid_contact_method_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client)
        events = client.get("/api/events").get_json()["events"]
        act_id = next(e for e in events if e["id"] == self.event_id)["artists"][0]["id"]
        r = client.post(f"/api/event_artists/{act_id}/contacts", json={"method": "carrier pigeon"})
        self.assertEqual(r.status_code, 400)

    def test_crew_cannot_log_a_contact(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client)
        events = client.get("/api/events").get_json()["events"]
        act_id = next(e for e in events if e["id"] == self.event_id)["artists"][0]["id"]

        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        r = crew_client.post(f"/api/event_artists/{act_id}/contacts", json={"method": "phone"})
        self.assertEqual(r.status_code, 403)

    # -- ticket_link -------------------------------------------------------

    def test_ticket_link_round_trips_on_update(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client)
        version = client.get("/api/events").get_json()["events"]
        version = next(e for e in version if e["id"] == self.event_id)["version"]

        r = client.patch(f"/api/events/{self.event_id}", json={
            "version": version, "ticket_link": "https://etix.com/ticket/12345",
        })
        self.assertEqual(r.status_code, 200, r.get_json())
        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(mine["ticket_link"], "https://etix.com/ticket/12345")

    # -- booking a NEW show must actually persist these fields -----------

    def test_booking_a_new_show_persists_doors_show_time_deal_and_notes(self):
        """create_event used to silently drop every optional field except
        venue/date/status/acts — a booker filling in doors, show time,
        deal terms, or notes while booking would have them vanish."""
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._book(client, doors="19:00", show_time="20:00", deal_type="Guarantee",
                   guarantee=1500, backend_pct=15, deal_notes="Net after $500 in expenses",
                   notes="Load in through the back", ticket_link="https://etix.com/ticket/999",
                   announce_date="2027-09-01", onsale_date="2027-09-08")

        events = client.get("/api/events").get_json()["events"]
        mine = next(e for e in events if e["id"] == self.event_id)
        self.assertEqual(mine["doors"], "19:00:00")
        self.assertEqual(mine["show_time"], "20:00:00")
        self.assertEqual(mine["deal_type"], "Guarantee")
        self.assertAlmostEqual(float(mine["guarantee"]), 1500.0)
        self.assertAlmostEqual(float(mine["backend_pct"]), 15.0)
        self.assertEqual(mine["deal_notes"], "Net after $500 in expenses")
        self.assertEqual(mine["notes"], "Load in through the back")
        self.assertEqual(mine["ticket_link"], "https://etix.com/ticket/999")
        self.assertEqual(mine["announce_date"], "2027-09-01")
        self.assertEqual(mine["onsale_date"], "2027-09-08")

    def test_invalid_doors_on_create_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": [self.artist_name], "show_date": "2027-11-01",
            "doors": "not-a-time",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(r.get_json().get("id"))


if __name__ == "__main__":
    unittest.main()
