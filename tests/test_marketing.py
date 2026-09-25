"""Real database, real Flask test client: the Marketing tab -- contacts
import/dedupe, the one shared audience-filter function, and campaign
sending (mocked at the resend_client boundary, same split as the Etix
tests: mock only the network call, run the real audience/campaign logic
against the real DB).
"""
import unittest
from unittest.mock import patch

import app as app_module
import auth
import db


class MarketingContactsAndAudience(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"mkt-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"mkt-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Frankies'")
        self.frankies_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues WHERE name = 'Ottawa Tavern'")
        self.ottawa_id = cur.fetchone()[0]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.contact_emails = []

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def tearDown(self):
        cur = self.conn.cursor()
        if self.contact_emails:
            cur.execute("DELETE FROM marketing_contacts WHERE email = ANY(%s)", (self.contact_emails,))
        cur.execute(
            "DELETE FROM audit_log WHERE entity_type IN ('marketing_contacts', 'marketing_campaign') "
            "AND person_id IN (%s, %s)",
            (self.booker_id, self.crew_id),
        )
        cur.execute(
            "DELETE FROM marketing_campaigns WHERE created_by IN (%s, %s)", (self.booker_id, self.crew_id)
        )
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def _import(self, client, contacts, **extra):
        self.contact_emails.extend(
            c["email"] if isinstance(c, dict) else c for c in contacts if isinstance(c, (dict, str))
        )
        payload = {"contacts": contacts}
        payload.update(extra)
        return client.post("/api/marketing/contacts/import", json=payload)

    def test_crew_cannot_see_or_import_contacts(self):
        client = self.app.test_client()
        self._login(client, self.crew_email)
        r = client.get("/api/marketing/contacts")
        self.assertEqual(r.status_code, 403)
        r = self._import(client, ["nope@example.invalid"])
        self.assertEqual(r.status_code, 403)

    def test_importing_pasted_emails_with_names(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = self._import(client, [
            {"email": f"mkt-a-{id(self)}@example.invalid", "name": "Alice"},
            f"mkt-b-{id(self)}@example.invalid",
        ])
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(r.get_json(), {"added": 2, "skipped": 0})
        contacts = client.get("/api/marketing/contacts").get_json()["contacts"]
        alice = next(c for c in contacts if c["name"] == "Alice")
        self.assertEqual(alice["source"], "manual")

    def test_duplicate_and_invalid_entries_are_skipped_not_erroring(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        email = f"mkt-dupe-{id(self)}@example.invalid"
        r = self._import(client, [email, email, "not-an-email", "  "])
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(r.get_json(), {"added": 1, "skipped": 3})

    def test_reimporting_the_same_email_is_a_noop_not_a_duplicate_row(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        email = f"mkt-repeat-{id(self)}@example.invalid"
        self._import(client, [email])
        r2 = self._import(client, [email])
        self.assertEqual(r2.get_json(), {"added": 0, "skipped": 1})
        contacts = [c for c in client.get("/api/marketing/contacts").get_json()["contacts"] if c["email"] == email]
        self.assertEqual(len(contacts), 1)

    def test_audience_filters_by_venue(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._import(client, [f"mkt-frankies-{id(self)}@example.invalid"], venue_id=self.frankies_id)
        self._import(client, [f"mkt-ottawa-{id(self)}@example.invalid"], venue_id=self.ottawa_id)
        r = client.get(f"/api/marketing/audience?venue_id={self.frankies_id}")
        self.assertEqual(r.status_code, 200)
        emails = [s["email"] for s in r.get_json()["sample"]]
        self.assertIn(f"mkt-frankies-{id(self)}@example.invalid", emails)
        self.assertNotIn(f"mkt-ottawa-{id(self)}@example.invalid", emails)

    def test_opted_out_contact_is_never_in_the_audience(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        email = f"mkt-optout-{id(self)}@example.invalid"
        self._import(client, [email])
        cur = self.conn.cursor()
        cur.execute("UPDATE marketing_contacts SET email_opt_out = TRUE WHERE email = %s", (email,))
        self.conn.commit()
        r = client.get("/api/marketing/audience")
        emails = [s["email"] for s in r.get_json()["sample"]]
        self.assertNotIn(email, emails)

    def test_deleting_a_contact(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        email = f"mkt-del-{id(self)}@example.invalid"
        self._import(client, [email])
        contact = next(c for c in client.get("/api/marketing/contacts").get_json()["contacts"] if c["email"] == email)
        d = client.delete(f"/api/marketing/contacts/{contact['id']}")
        self.assertEqual(d.status_code, 200)
        self.contact_emails.remove(email)
        contacts = client.get("/api/marketing/contacts").get_json()["contacts"]
        self.assertFalse(any(c["email"] == email for c in contacts))


class MarketingSending(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"mktsend-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        self.conn.commit()
        cur.execute("SELECT email FROM people WHERE id = %s", (self.booker_id,))
        self.booker_email = cur.fetchone()[0]
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.contact_emails = []
        self.campaign_ids = []

    def tearDown(self):
        cur = self.conn.cursor()
        if self.contact_emails:
            cur.execute("DELETE FROM marketing_contacts WHERE email = ANY(%s)", (self.contact_emails,))
        if self.campaign_ids:
            cur.execute("DELETE FROM marketing_campaigns WHERE id = ANY(%s)", (self.campaign_ids,))
        # Covers both the campaign-send audit rows AND the contacts-import
        # audit row each test writes on the way in (entity_type
        # 'marketing_contacts', a fixed entity_id of 0 -- not worth
        # tracking individually since person_id alone identifies them all).
        cur.execute("DELETE FROM audit_log WHERE person_id = %s", (self.booker_id,))
        cur.execute("DELETE FROM sessions WHERE person_id = %s", (self.booker_id,))
        cur.execute("DELETE FROM people WHERE id = %s", (self.booker_id,))
        self.conn.commit()
        self.conn.close()

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_sending_when_email_not_configured_still_records_the_campaign(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        email = f"mktsend-a-{id(self)}@example.invalid"
        self.contact_emails.append(email)
        client.post("/api/marketing/contacts/import", json={"contacts": [email]})
        with patch("resend_client.is_configured", return_value=False):
            r = client.post("/api/marketing/send", json={"subject": "Hi", "body": "Hello {{name}}"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["error"], "email_not_configured")
        self.campaign_ids.append(r.get_json()["id"])
        campaigns = client.get("/api/marketing/campaigns").get_json()["campaigns"]
        self.assertTrue(any(c["id"] == r.get_json()["id"] and c["sent_at"] is None for c in campaigns))

    def test_sending_when_configured_calls_resend_per_recipient(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        email = f"mktsend-b-{id(self)}@example.invalid"
        self.contact_emails.append(email)
        client.post("/api/marketing/contacts/import", json={"contacts": [{"email": email, "name": "Bob"}]})
        with patch("resend_client.is_configured", return_value=True), \
             patch("resend_client.send", return_value=(True, None)) as mock_send:
            r = client.post("/api/marketing/send", json={"subject": "Hi", "body": "Hello {{name}}"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.campaign_ids.append(r.get_json()["id"])
        self.assertEqual(r.get_json()["sent_count"], 1)
        mock_send.assert_called_once()
        self.assertIn("Hello Bob", mock_send.call_args[0][2])

    def test_test_send_requires_email_configured(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        with patch("resend_client.is_configured", return_value=False):
            r = client.post("/api/marketing/test", json={"to": "x@example.invalid", "subject": "Hi", "body": "hi"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "email_not_configured")


if __name__ == "__main__":
    unittest.main()
