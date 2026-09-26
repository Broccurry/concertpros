"""Real database, real Flask test client: pulling a live ticket-count
snapshot from Etix into ticket_sales (source='etix'). Two layers, same
split as test_pull_ticket_link.py:

- EtixPullEndpoint mocks etix.pull_and_store_snapshot itself, checking
  the endpoint's permission gate and error handling, not that Etix's API
  is reachable.
- EtixSnapshotStorage mocks only the network calls inside etix.py
  (_find_public_event / get_snapshot), so the actual upsert-into-
  ticket_sales logic runs for real against the real database — this is
  the one place both the manual "Pull from Etix" button and the daily
  scheduled script (scripts/pull_etix_daily_sales.py) get their write
  behavior from, so it's worth testing directly rather than only through
  the mocked endpoint layer.
"""
import json
import unittest
from datetime import date
from unittest.mock import patch

import app as app_module
import auth
import db
import etix


class EtixPullEndpoint(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"etixtix-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"etixtix-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues LIMIT 1")
        self.venue_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO events (venue_id, show_date, status, created_by)
               VALUES (%s, '2027-08-01', 'confirmed', %s) RETURNING id""",
            (self.venue_id, self.booker_id),
        )
        self.event_id = cur.fetchone()[0]
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
        cur.execute("DELETE FROM ticket_sales WHERE event_id = %s", (self.event_id,))
        cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def test_a_match_stores_the_count_and_returns_it(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        with patch("app.etix.pull_and_store_snapshot", return_value=42) as mock_pull:
            r = client.post(f"/api/events/{self.event_id}/ticket_sales/pull_etix")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["tickets_sold"], 42)
        self.assertEqual(mock_pull.call_args[0][3], date(2027, 8, 1))

    def test_no_match_is_a_404_not_a_crash(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        with patch("app.etix.pull_and_store_snapshot", return_value=None):
            r = client.post(f"/api/events/{self.event_id}/ticket_sales/pull_etix")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"], "no_matching_etix_event")

    def test_etix_failure_is_a_502_not_a_crash(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        with patch("app.etix.pull_and_store_snapshot", side_effect=RuntimeError("boom")):
            r = client.post(f"/api/events/{self.event_id}/ticket_sales/pull_etix")
        self.assertEqual(r.status_code, 502)

    def test_unknown_event_is_404(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/events/999999999/ticket_sales/pull_etix")
        self.assertEqual(r.status_code, 404)

    def test_crew_cannot_pull_ticket_sales(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        with patch("app.etix.pull_and_store_snapshot", return_value=42):
            r = client.post(f"/api/events/{self.event_id}/ticket_sales/pull_etix")
        self.assertEqual(r.status_code, 403)


class EtixSnapshotStorage(unittest.TestCase):
    """The real upsert-into-ticket_sales logic, network calls mocked."""

    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        cur.execute("SELECT id, name FROM venues WHERE name = 'Frankies'")
        self.venue_id, self.venue_name = cur.fetchone()
        cur.execute(
            "INSERT INTO events (venue_id, show_date, status) VALUES (%s, '2027-08-15', 'confirmed') RETURNING id",
            (self.venue_id,),
        )
        self.event_id = cur.fetchone()[0]
        self.conn.commit()

    def tearDown(self):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM ticket_sales WHERE event_id = %s", (self.event_id,))
        cur.execute("DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id = %s", (self.event_id,))
        cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        self.conn.commit()
        self.conn.close()

    def test_a_real_match_upserts_a_ticket_sales_row(self):
        with patch("etix._find_public_event", return_value={"id": 999}), \
             patch("etix.get_snapshot", return_value={"revenueProducingTickets": 77}), \
             patch("etix.get_daily_sales", return_value=[]):
            result = etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        self.conn.commit()
        self.assertEqual(result, 77)
        cur = self.conn.cursor()
        cur.execute("SELECT tickets_sold, source FROM ticket_sales WHERE event_id = %s AND sale_date = %s",
                    (self.event_id, date(2027, 1, 1)))
        row = cur.fetchone()
        self.assertEqual(row, (77, "etix"))

    def test_gross_is_summed_from_daily_sales_not_the_snapshot(self):
        """Confirmed against real production data (2026-09-24): the
        snapshot's own salesByCurrency includes the face value of pulled
        tickets (comps/kills), not just real sales -- one real show had 0
        revenueProducingTickets and a $320 snapshot total, entirely from
        40 pulled tickets, while its real daily-sales revenue was $0. So
        gross must come from get_daily_sales, and a snapshot total is
        ignored even when present."""
        with patch("etix._find_public_event", return_value={"id": 999}), \
             patch("etix.get_snapshot", return_value={
                 "revenueProducingTickets": 77,
                 "salesByCurrency": [{"currency": "USD", "price": 999999.00}],
             }), \
             patch("etix.get_daily_sales", return_value=[
                 {"date": "09/20/2026", "salesByCurrency": [{"currency": "USD", "price": 900.00}]},
                 {"date": "09/24/2026", "salesByCurrency": [{"currency": "USD", "price": 600.50}]},
             ]):
            etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        self.conn.commit()
        cur = self.conn.cursor()
        cur.execute("SELECT gross FROM ticket_sales WHERE event_id = %s AND sale_date = %s",
                    (self.event_id, date(2027, 1, 1)))
        self.assertEqual(cur.fetchone()[0], 1500.50)

    def test_no_daily_sales_yet_is_a_real_zero_not_null(self):
        with patch("etix._find_public_event", return_value={"id": 999}), \
             patch("etix.get_snapshot", return_value={"revenueProducingTickets": 0}), \
             patch("etix.get_daily_sales", return_value=[]):
            etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        self.conn.commit()
        cur = self.conn.cursor()
        cur.execute("SELECT gross FROM ticket_sales WHERE event_id = %s AND sale_date = %s",
                    (self.event_id, date(2027, 1, 1)))
        self.assertEqual(cur.fetchone()[0], 0)

    def test_no_match_returns_none_and_writes_nothing(self):
        with patch("etix._find_public_event", return_value=None), \
             patch("etix._find_private_event", return_value=None):
            result = etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        self.assertIsNone(result)
        cur = self.conn.cursor()
        cur.execute("SELECT count(*) FROM ticket_sales WHERE event_id = %s", (self.event_id,))
        self.assertEqual(cur.fetchone()[0], 0)

    def test_a_played_show_falls_back_to_the_private_lookup(self):
        """/public/events only lists on-sale/upcoming shows (confirmed
        2026-09-25 against real Frankies data -- a real past show had
        already dropped off it entirely), so a show that's already
        happened must be found through the private VIEW_VENUE endpoint
        instead. This is what makes a settlement's "pull from Etix"
        work for a show that played last night, not just an upcoming one."""
        with patch("etix._find_public_event", return_value=None), \
             patch("etix._find_private_event", return_value={"id": 999}) as mock_private, \
             patch("etix.get_snapshot", return_value={"revenueProducingTickets": 47}), \
             patch("etix.get_daily_sales", return_value=[]):
            result = etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        self.conn.commit()
        self.assertEqual(result, 47)
        mock_private.assert_called_once_with(self.venue_name, date(2027, 8, 15))

    def test_an_upcoming_show_never_needs_the_private_fallback(self):
        """The public feed is cheap and already covers on-sale shows --
        don't call the private endpoint when it's not needed."""
        with patch("etix._find_public_event", return_value={"id": 999}), \
             patch("etix._find_private_event") as mock_private, \
             patch("etix.get_snapshot", return_value={"revenueProducingTickets": 10}), \
             patch("etix.get_daily_sales", return_value=[]):
            etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        mock_private.assert_not_called()

    def test_re_pulling_the_same_date_corrects_it_not_duplicates(self):
        with patch("etix._find_public_event", return_value={"id": 999}), \
             patch("etix.get_snapshot", return_value={"revenueProducingTickets": 10}), \
             patch("etix.get_daily_sales", return_value=[]):
            etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        with patch("etix._find_public_event", return_value={"id": 999}), \
             patch("etix.get_snapshot", return_value={"revenueProducingTickets": 25}), \
             patch("etix.get_daily_sales", return_value=[]):
            etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        self.conn.commit()
        cur = self.conn.cursor()
        cur.execute("SELECT count(*), max(tickets_sold) FROM ticket_sales WHERE event_id = %s", (self.event_id,))
        self.assertEqual(cur.fetchone(), (1, 25))

    def test_a_prior_manual_entry_gets_relabeled_etix_on_repull(self):
        """A booker logged a manual count for today, then a same-day Etix
        pull happens (or the daily job runs) -- the row should end up
        attributed to Etix, not silently keep saying 'manual' for a count
        Etix just overwrote."""
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO ticket_sales (event_id, sale_date, tickets_sold, source) VALUES (%s, %s, %s, 'manual')",
            (self.event_id, date(2027, 1, 1), 5),
        )
        self.conn.commit()
        with patch("etix._find_public_event", return_value={"id": 999}), \
             patch("etix.get_snapshot", return_value={"revenueProducingTickets": 12}), \
             patch("etix.get_daily_sales", return_value=[]):
            etix.pull_and_store_snapshot(
                self.conn, self.event_id, self.venue_name, date(2027, 8, 15), sale_date=date(2027, 1, 1))
        self.conn.commit()
        cur.execute("SELECT tickets_sold, source FROM ticket_sales WHERE event_id = %s AND sale_date = %s",
                    (self.event_id, date(2027, 1, 1)))
        self.assertEqual(cur.fetchone(), (12, "etix"))


class PriceBreakdownFromOrders(unittest.TestCase):
    """etix.get_price_breakdown() -- per-tier ticket counts built from real
    order data (GET /organizations/{id}/orders), not Etix's Settlement API
    (that needs a scope this app's key doesn't have -- confirmed live,
    406 invalid_scope). Mocks only the HTTP call; the aggregation and
    comp-exclusion logic run for real. Shape below mirrors the real
    Merkules response exactly (confirmed 2026-09-26): 2 real online
    orders plus one internal box-office order carrying the comp tickets
    that must be excluded."""

    def _fake_response(self, payload):
        class FakeResponse:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return json.dumps(payload).encode()
        return FakeResponse()

    def test_internal_channel_orders_are_excluded_as_comps(self):
        payload = {
            "createdOrders": [
                {
                    "salesChannel": "SALES_CHANNEL.ONLINE",
                    "tickets": [
                        {"priceCode": "ADVANCED", "price": 30.0},
                        {"priceCode": "ADVANCED", "price": 30.0},
                        {"priceCode": "DAY OF", "price": 35.0},
                    ],
                },
                {
                    # Box-office-pulled comps -- must not count as sales.
                    "salesChannel": "SALES_CHANNEL.INTERNAL",
                    "tickets": [
                        {"priceCode": "ADVANCED", "price": 30.0},
                        {"priceCode": "ADVANCED", "price": 30.0},
                        {"priceCode": "ADVANCED", "price": 30.0},
                    ],
                },
            ],
        }
        with patch("urllib.request.urlopen", return_value=self._fake_response(payload)), \
             patch("etix._get_token", return_value="fake-token"):
            tiers = etix.get_price_breakdown(87567148)
        self.assertEqual(sorted(tiers, key=lambda t: t["label"]), [
            {"label": "ADVANCED", "price": 30.0, "sold": 2},
            {"label": "DAY OF", "price": 35.0, "sold": 1},
        ])

    def test_no_orders_is_an_empty_list_not_a_crash(self):
        with patch("urllib.request.urlopen", return_value=self._fake_response({"createdOrders": []})), \
             patch("etix._get_token", return_value="fake-token"):
            self.assertEqual(etix.get_price_breakdown(87567148), [])

    def test_a_price_code_with_more_than_one_real_price_splits_and_labels_each(self):
        """Confirmed 2026-09-26 against a real reserved-seating show
        (Crystal Bowersox, Cla-Zel): a single priceCode ("Floor D seats")
        is NOT always one uniform price -- it carried both $35 and $50
        tickets. Grouping by code name alone and reporting only the first
        price seen understated real revenue once the settlement sheet
        does price*sold (was off by hundreds of dollars on that real
        show). Each distinct price under a shared code becomes its own
        row, labeled to disambiguate."""
        payload = {
            "createdOrders": [
                {
                    "salesChannel": "SALES_CHANNEL.ONLINE",
                    "tickets": [
                        {"priceCode": "Floor D seats", "price": 35.0},
                        {"priceCode": "Floor D seats", "price": 35.0},
                        {"priceCode": "Floor D seats", "price": 50.0},
                        {"priceCode": "Floor A seats", "price": 35.0},
                    ],
                },
            ],
        }
        with patch("urllib.request.urlopen", return_value=self._fake_response(payload)), \
             patch("etix._get_token", return_value="fake-token"):
            tiers = etix.get_price_breakdown(87567148)
        by_label = {t["label"]: t for t in tiers}
        self.assertEqual(by_label["Floor D seats ($35)"], {"label": "Floor D seats ($35)", "price": 35.0, "sold": 2})
        self.assertEqual(by_label["Floor D seats ($50)"], {"label": "Floor D seats ($50)", "price": 50.0, "sold": 1})
        # A code with only one real price is left with its plain name.
        self.assertEqual(by_label["Floor A seats"], {"label": "Floor A seats", "price": 35.0, "sold": 1})
        total_gross = sum(t["sold"] * t["price"] for t in tiers)
        self.assertAlmostEqual(total_gross, 2 * 35.0 + 1 * 50.0 + 1 * 35.0)


if __name__ == "__main__":
    unittest.main()
