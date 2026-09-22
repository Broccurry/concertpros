"""Proves the crew/booker boundary in permissions.py against a REAL database
— not a mock. CLAUDE.md requires this test to exist; this is it.

Runs inside one transaction that's always rolled back in tearDown, so it
never leaves test rows behind in a real (dev or prod) database. stdlib
unittest only, no extra test-runner dependency.

Usage: DATABASE_URL=... python -m unittest tests.test_permissions -v
"""
import unittest
from datetime import date

import db
import permissions
from permissions import Viewer


class CrewCannotSeeHoldsOrMoney(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        self.conn.autocommit = False
        cur = self.conn.cursor()

        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.venue_id = cur.fetchone()[0]

        self.artist_name = f"Test Artist {id(self)}"  # unique-ish per test run
        cur.execute(
            "INSERT INTO artists (name) VALUES (%s) RETURNING id",
            (self.artist_name,),
        )
        self.artist_id = cur.fetchone()[0]

        cur.execute(
            """INSERT INTO people (name, email, access_level)
               VALUES ('Test Crew', %s, 'crew') RETURNING id""",
            (f"test-crew-{id(self)}@example.invalid",),
        )
        self.crew_person_id = cur.fetchone()[0]

        cur.execute(
            """INSERT INTO people (name, email, access_level)
               VALUES ('Test Booker', %s, 'booker') RETURNING id""",
            (f"test-booker-{id(self)}@example.invalid",),
        )
        self.booker_person_id = cur.fetchone()[0]

        # A hold — crew must never see this.
        cur.execute(
            """INSERT INTO events (venue_id, artist_id, show_date, status,
                                    guarantee, deal_notes)
               VALUES (%s, %s, %s, 'hold1', 5000, 'top secret deal terms')
               RETURNING id""",
            (self.venue_id, self.artist_id, date(2026, 12, 1)),
        )
        self.hold_event_id = cur.fetchone()[0]

        # A confirmed show — crew should see the public fields, nothing else.
        cur.execute(
            """INSERT INTO events (venue_id, artist_id, show_date, status,
                                    guarantee, deal_notes)
               VALUES (%s, %s, %s, 'confirmed', 9999, 'also secret')
               RETURNING id""",
            (self.venue_id, self.artist_id, date(2026, 12, 15)),
        )
        self.confirmed_event_id = cur.fetchone()[0]

        cur.execute(
            """INSERT INTO settlements (event_id, gross, artist_payout)
               VALUES (%s, 12345.67, 4000)""",
            (self.confirmed_event_id,),
        )

        cur.execute("SELECT id FROM roles WHERE name = 'Sound'")
        role_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO assignments (event_id, role_id, person_id) VALUES (%s, %s, %s)",
            (self.confirmed_event_id, role_id, self.crew_person_id),
        )
        self.conn.commit()

        self.crew_viewer = Viewer(id=self.crew_person_id, access_level="crew")
        self.booker_viewer = Viewer(id=self.booker_person_id, access_level="booker")

    def tearDown(self):
        # Explicit cleanup (not just rollback) since setUp already committed,
        # to keep the crew-visibility assertions honest against a durable
        # commit rather than an uncommitted transaction a real query might
        # not even see under some isolation levels.
        cur = self.conn.cursor()
        cur.execute("DELETE FROM events WHERE id IN (%s, %s)",
                    (self.hold_event_id, self.confirmed_event_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)",
                    (self.crew_person_id, self.booker_person_id))
        cur.execute("DELETE FROM artists WHERE id = %s", (self.artist_id,))
        self.conn.commit()
        self.conn.close()

    def test_crew_never_receives_the_hold(self):
        events = permissions.events_for(self.conn, self.crew_viewer)
        ids = [e["id"] for e in events]
        self.assertNotIn(self.hold_event_id, ids,
                          "a crew viewer received a hold — this must never happen")

    def test_crew_sees_the_confirmed_show_but_not_the_money(self):
        events = permissions.events_for(self.conn, self.crew_viewer)
        mine = next(e for e in events if e["id"] == self.confirmed_event_id)
        self.assertEqual(mine["headliner"], self.artist_name)
        self.assertEqual(mine["my_roles"], ["Sound"])
        for forbidden in ("guarantee", "backend_pct", "deal_notes", "settlement",
                          "ticket_tiers", "staff", "notes", "announce_date", "onsale_date"):
            self.assertNotIn(forbidden, mine,
                              f"crew's event dict leaked '{forbidden}'")

    def test_booker_sees_both_the_hold_and_the_money(self):
        events = permissions.events_for(self.conn, self.booker_viewer)
        ids = {e["id"]: e for e in events}
        self.assertIn(self.hold_event_id, ids, "booker must see holds")
        self.assertIn(self.confirmed_event_id, ids)
        confirmed = ids[self.confirmed_event_id]
        self.assertEqual(float(confirmed["guarantee"]), 9999.0)
        self.assertIsNotNone(confirmed["settlement"])
        self.assertAlmostEqual(float(confirmed["settlement"]["gross"]), 12345.67)


if __name__ == "__main__":
    unittest.main()
