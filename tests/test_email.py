"""Real database, real Flask test client: each booker/owner connecting
their OWN real mailbox (email_accounts, one row per person) -- reading and
sending through it via IMAP/SMTP (email_client.py), and keeping crew out
entirely.

The real IMAP/SMTP conversation itself is mocked out here, the same way
test_files.py mocks B2 -- these tests check the permission gate, the
request validation, and that the password is actually encrypted at rest
and never handed back to the browser, not that Zoho is reachable. The
connection code's ability to actually talk to a real IMAP/SMTP server was
verified separately, by hand, against Zoho's real (production) server --
see the session notes; a wrong password there came back as a real
[AUTHENTICATIONFAILED] from Zoho itself, not a mock.
"""
import unittest
from unittest.mock import patch

import app as app_module
import auth
import crypto
import db
import email_client


class EmailAccounts(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"email-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"email-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True

        self.test_connection_patcher = patch("email_client.test_connection", return_value=True)
        self.mock_test_connection = self.test_connection_patcher.start()

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def tearDown(self):
        self.test_connection_patcher.stop()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM audit_log WHERE entity_type = 'email_account' AND entity_id IN (%s, %s)",
                    (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM email_accounts WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def _connect(self, client, **extra):
        payload = {"email_address": "booker@innovationconcerts.com", "password": "some-app-password"}
        payload.update(extra)
        r = client.post("/api/email/account", json=payload)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r

    def test_connecting_saves_defaults_to_zoho_and_never_returns_the_password(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._connect(client)

        r = client.get("/api/email/account")
        account = r.get_json()["account"]
        self.assertEqual(account["email_address"], "booker@innovationconcerts.com")
        self.assertEqual(account["imap_host"], "imap.zoho.com")
        self.assertEqual(account["imap_port"], 993)
        self.assertEqual(account["smtp_host"], "smtp.zoho.com")
        self.assertEqual(account["smtp_port"], 465)
        self.assertNotIn("password", account)
        self.assertNotIn("encrypted_password", account)

    def test_the_stored_password_is_actually_encrypted_and_decrypts_back(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._connect(client, password="a real app password")

        cur = self.conn.cursor()
        cur.execute("SELECT encrypted_password FROM email_accounts WHERE person_id = %s", (self.booker_id,))
        stored = cur.fetchone()[0]
        self.assertNotEqual(stored, "a real app password")
        self.assertEqual(crypto.decrypt(stored), "a real app password")

    def test_a_failed_connection_test_saves_nothing_and_reports_why(self):
        self.mock_test_connection.side_effect = email_client.EmailAuthError("IMAP login failed: bad creds")
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/email/account", json={"email_address": "x@example.com", "password": "wrong"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("bad creds", r.get_json()["detail"])
        self.assertIsNone(client.get("/api/email/account").get_json()["account"])

    def test_email_and_password_are_required(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/email/account", json={"email_address": "x@example.com"})
        self.assertEqual(r.status_code, 400)
        r2 = client.post("/api/email/account", json={"password": "x"})
        self.assertEqual(r2.status_code, 400)

    def test_custom_imap_smtp_host_and_port_are_honored(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._connect(client, imap_host="imap.example.com", imap_port=143, smtp_host="smtp.example.com", smtp_port=587)
        account = client.get("/api/email/account").get_json()["account"]
        self.assertEqual(account["imap_host"], "imap.example.com")
        self.assertEqual(account["imap_port"], 143)
        self.assertEqual(account["smtp_host"], "smtp.example.com")
        self.assertEqual(account["smtp_port"], 587)

    def test_reconnecting_replaces_the_existing_account_not_a_second_row(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._connect(client, email_address="first@innovationconcerts.com")
        self._connect(client, email_address="second@innovationconcerts.com")
        cur = self.conn.cursor()
        cur.execute("SELECT count(*) FROM email_accounts WHERE person_id = %s", (self.booker_id,))
        self.assertEqual(cur.fetchone()[0], 1)
        account = client.get("/api/email/account").get_json()["account"]
        self.assertEqual(account["email_address"], "second@innovationconcerts.com")

    def test_disconnecting_removes_the_account(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._connect(client)
        r = client.delete("/api/email/account")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(client.get("/api/email/account").get_json()["account"])

    def test_inbox_requires_a_connected_account(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.get("/api/email/inbox")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "not_connected")

    def test_inbox_returns_messages_from_the_real_mailbox_call(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._connect(client)
        with patch("email_client.list_messages", return_value=[{"uid": "1", "from": "a@b.com", "subject": "Hi", "date": "today"}]) as mock_list:
            r = client.get("/api/email/inbox")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["messages"][0]["subject"], "Hi")
        # The decrypted password actually reached email_client, not a stub or the ciphertext.
        called_account = mock_list.call_args[0][0]
        self.assertEqual(called_account["password"], "some-app-password")

    def test_inbox_surfaces_a_broken_connection_instead_of_a_generic_500(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._connect(client)
        with patch("email_client.list_messages", side_effect=email_client.EmailAuthError("IMAP login failed")):
            r = client.get("/api/email/inbox")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json()["error"], "connection_failed")

    def test_send_requires_to_and_subject(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._connect(client)
        r = client.post("/api/email/send", json={"subject": "Hi", "body": "hello"})
        self.assertEqual(r.status_code, 400)
        r2 = client.post("/api/email/send", json={"to": "x@y.com", "body": "hello"})
        self.assertEqual(r2.status_code, 400)

    def test_send_requires_a_connected_account(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/email/send", json={"to": "x@y.com", "subject": "Hi", "body": "hello"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "not_connected")

    def test_sending_calls_the_real_send_with_the_right_account_and_message(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._connect(client)
        with patch("email_client.send_message", return_value=None) as mock_send:
            r = client.post("/api/email/send", json={"to": "band@example.com", "subject": "Load-in time", "body": "6pm"})
        self.assertEqual(r.status_code, 200, r.get_json())
        args = mock_send.call_args[0]
        self.assertEqual(args[0]["email_address"], "booker@innovationconcerts.com")
        self.assertEqual(args[1], "band@example.com")
        self.assertEqual(args[2], "Load-in time")
        self.assertEqual(args[3], "6pm")

    def test_crew_cannot_touch_email_at_all(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._connect(client)

        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        self.assertEqual(crew_client.get("/api/email/account").status_code, 403)
        self.assertEqual(crew_client.post("/api/email/account", json={"email_address": "x@y.com", "password": "x"}).status_code, 403)
        self.assertEqual(crew_client.delete("/api/email/account").status_code, 403)
        self.assertEqual(crew_client.get("/api/email/inbox").status_code, 403)
        self.assertEqual(crew_client.get("/api/email/message/1").status_code, 403)
        self.assertEqual(crew_client.post("/api/email/send", json={"to": "x@y.com", "subject": "x"}).status_code, 403)

    def test_a_booker_cannot_reach_another_bookers_mailbox(self):
        """Nothing in these routes takes a person_id from the request --
        every query is scoped to the logged-in viewer -- so there is no way
        to even ask for someone else's inbox, not just a check that denies
        it."""
        client = self.app.test_client()
        self._login(client, self.booker_email)
        self._connect(client, email_address="mine@innovationconcerts.com")

        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Other Booker', %s, %s, 'booker') RETURNING id""",
            (f"email-test-other-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        other_id = cur.fetchone()[0]
        self.conn.commit()
        try:
            other_email = self._email(other_id)
            other_client = self.app.test_client()
            self._login(other_client, other_email)
            account = other_client.get("/api/email/account").get_json()["account"]
            self.assertIsNone(account)
        finally:
            cur.execute("DELETE FROM sessions WHERE person_id = %s", (other_id,))
            cur.execute("DELETE FROM people WHERE id = %s", (other_id,))
            self.conn.commit()


if __name__ == "__main__":
    unittest.main()
