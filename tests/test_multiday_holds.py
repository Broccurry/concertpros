"""Real database, real Flask test client: multi-day holds — several
candidate dates for one prospective show, each with its own status,
sharing one bill/deal/tasks until one of them confirms and the rest
disappear outright (Broc's call: no "didn't work out" trail for them).
"""
import unittest

import app as app_module
import auth
import db


class MultiDayHolds(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"multiday-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.artist_name = f"Multiday Test Band {id(self)}"
        self.event_ids = []  # every event id created this test, cleaned up in tearDown

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client):
        r = client.post("/api/login", json={"email": self.booker_email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def tearDown(self):
        cur = self.conn.cursor()
        if self.event_ids:
            fmt = ",".join(["%s"] * len(self.event_ids))
            cur.execute(f"DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id IN ({fmt})", self.event_ids)
            cur.execute(f"DELETE FROM events WHERE id IN ({fmt})", self.event_ids)
        cur.execute("DELETE FROM artists WHERE name = %s", (self.artist_name,))
        cur.execute("DELETE FROM sessions WHERE person_id = %s", (self.booker_id,))
        cur.execute("DELETE FROM people WHERE id = %s", (self.booker_id,))
        self.conn.commit()
        self.conn.close()

    def _create_multiday(self, client, extra_dates):
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": [self.artist_name], "show_date": "2027-12-01",
            "status": "hold1", "extra_dates": extra_dates,
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["id"]

    def _mine(self, client, event_id):
        events = client.get("/api/events").get_json()["events"]
        return next(e for e in events if e["id"] == event_id)

    def test_creating_a_multiday_hold_makes_one_row_per_date(self):
        client = self.app.test_client()
        self._login(client)
        anchor_id = self._create_multiday(client, ["2027-12-02", "2027-12-03"])

        anchor = self._mine(client, anchor_id)
        self.event_ids = [m["id"] for m in anchor["group_members"]]
        self.assertEqual(len(anchor["group_members"]), 3)
        dates = sorted(m["show_date"] for m in anchor["group_members"])
        self.assertEqual(dates, ["2027-12-01", "2027-12-02", "2027-12-03"])
        # every date starts on the same status the primary was created with
        self.assertTrue(all(m["status"] == "hold1" for m in anchor["group_members"]))

    def test_every_date_shares_the_same_bill_and_can_have_its_own_status(self):
        client = self.app.test_client()
        self._login(client)
        anchor_id = self._create_multiday(client, ["2027-12-02"])
        anchor = self._mine(client, anchor_id)
        self.event_ids = [m["id"] for m in anchor["group_members"]]
        other_id = next(m["id"] for m in anchor["group_members"] if m["id"] != anchor_id)

        # the non-anchor date shows the SAME acts, borrowed from the anchor
        other = self._mine(client, other_id)
        self.assertEqual([a["name"] for a in other["artists"]], [self.artist_name])

        # give the non-anchor date its own status
        other_version = other["version"]
        r = client.patch(f"/api/events/{other_id}", json={"status": "hold2", "version": other_version})
        self.assertEqual(r.status_code, 200, r.get_json())

        anchor_after = self._mine(client, anchor_id)
        statuses = {m["id"]: m["status"] for m in anchor_after["group_members"]}
        self.assertEqual(statuses[anchor_id], "hold1", "the anchor's own status must not have moved")
        self.assertEqual(statuses[other_id], "hold2")

    def test_editing_a_shared_field_on_a_non_anchor_date_updates_everyone(self):
        client = self.app.test_client()
        self._login(client)
        anchor_id = self._create_multiday(client, ["2027-12-02"])
        anchor = self._mine(client, anchor_id)
        self.event_ids = [m["id"] for m in anchor["group_members"]]
        other_id = next(m["id"] for m in anchor["group_members"] if m["id"] != anchor_id)
        other = self._mine(client, other_id)

        r = client.patch(f"/api/events/{other_id}", json={
            "notes": "agent wants a Friday", "version": other["version"],
        })
        self.assertEqual(r.status_code, 200, r.get_json())

        anchor_after = self._mine(client, anchor_id)
        self.assertEqual(anchor_after["notes"], "agent wants a Friday",
                          "a shared field edited from any date must land on the whole group")

    def test_adding_and_removing_dates_on_an_existing_show(self):
        client = self.app.test_client()
        self._login(client)
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": [self.artist_name], "show_date": "2027-12-01",
        })
        event_id = r.get_json()["id"]
        self.event_ids = [event_id]

        add = client.post(f"/api/events/{event_id}/hold_dates", json={"dates": ["2027-12-05", "2027-12-06"]})
        self.assertEqual(add.status_code, 201, add.get_json())
        self.event_ids += add.get_json()["ids"]

        mine = self._mine(client, event_id)
        self.assertEqual(len(mine["group_members"]), 3)

        remove_id = add.get_json()["ids"][0]
        rem = client.delete(f"/api/events/{remove_id}/hold_date")
        self.assertEqual(rem.status_code, 200, rem.get_json())
        mine = self._mine(client, event_id)
        self.assertEqual(len(mine["group_members"]), 2)

    def test_cannot_remove_the_last_date(self):
        client = self.app.test_client()
        self._login(client)
        r = client.post("/api/events", json={
            "venue_id": self.venue_id, "acts": [self.artist_name], "show_date": "2027-12-01",
        })
        event_id = r.get_json()["id"]
        self.event_ids = [event_id]
        rem = client.delete(f"/api/events/{event_id}/hold_date")
        self.assertEqual(rem.status_code, 400)

    def test_confirming_a_non_anchor_date_migrates_data_and_deletes_the_rest(self):
        client = self.app.test_client()
        self._login(client)
        anchor_id = self._create_multiday(client, ["2027-12-02", "2027-12-03"])
        anchor = self._mine(client, anchor_id)
        self.event_ids = [m["id"] for m in anchor["group_members"]]
        members = anchor["group_members"]
        confirmed_id = next(m["id"] for m in members if m["id"] != anchor_id)
        confirmed_version = next(m["version"] for m in members if m["id"] == confirmed_id)

        r = client.patch(f"/api/events/{confirmed_id}", json={"status": "confirmed", "version": confirmed_version})
        self.assertEqual(r.status_code, 200, r.get_json())

        events = client.get("/api/events").get_json()["events"]
        ids_left = [e["id"] for e in events if e["id"] in self.event_ids]
        self.assertEqual(ids_left, [confirmed_id], "only the confirmed date should survive")

        confirmed = next(e for e in events if e["id"] == confirmed_id)
        self.assertEqual([a["name"] for a in confirmed["artists"]], [self.artist_name],
                          "the confirmed date must inherit the bill")
        self.assertIsNone(confirmed["hold_group_id"], "a confirmed date's group should be dissolved")
        self.assertEqual(confirmed["status"], "confirmed")

    def test_confirming_the_anchor_date_deletes_the_others(self):
        client = self.app.test_client()
        self._login(client)
        anchor_id = self._create_multiday(client, ["2027-12-02"])
        anchor = self._mine(client, anchor_id)
        self.event_ids = [m["id"] for m in anchor["group_members"]]

        r = client.patch(f"/api/events/{anchor_id}", json={"status": "confirmed", "version": anchor["version"]})
        self.assertEqual(r.status_code, 200, r.get_json())

        events = client.get("/api/events").get_json()["events"]
        ids_left = [e["id"] for e in events if e["id"] in self.event_ids]
        self.assertEqual(ids_left, [anchor_id])
        confirmed = next(e for e in events if e["id"] == anchor_id)
        self.assertEqual([a["name"] for a in confirmed["artists"]], [self.artist_name])


if __name__ == "__main__":
    unittest.main()
