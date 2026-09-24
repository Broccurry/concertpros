"""Real database, real Flask test client: a logged-in person changing
their own password. Added alongside a Settings UI for this -- the
endpoint already existed but nothing in the frontend called it, which
is exactly how Broc got locked out with no way back in.
"""
import unittest

import app as app_module
import auth
import db


class ChangePassword(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Person', %s, %s, 'crew') RETURNING id""",
            (f"changepw-test-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.person_id = cur.fetchone()[0]
        self.conn.commit()
        self.email = self._email()
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True

    def _email(self):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (self.person_id,))
        return cur.fetchone()[0]

    def _login(self, client):
        r = client.post("/api/login", json={"email": self.email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def tearDown(self):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM sessions WHERE person_id = %s", (self.person_id,))
        cur.execute("DELETE FROM people WHERE id = %s", (self.person_id,))
        self.conn.commit()
        self.conn.close()

    def test_changing_to_a_new_password_lets_you_log_in_with_it(self):
        client = self.app.test_client()
        self._login(client)
        r = client.post("/api/change-password", json={
            "current_password": self.password, "new_password": "a whole new passphrase",
        })
        self.assertEqual(r.status_code, 200, r.get_json())

        fresh = self.app.test_client()
        old = fresh.post("/api/login", json={"email": self.email, "password": self.password})
        self.assertEqual(old.status_code, 401)
        new = fresh.post("/api/login", json={"email": self.email, "password": "a whole new passphrase"})
        self.assertEqual(new.status_code, 200)

    def test_wrong_current_password_is_rejected(self):
        client = self.app.test_client()
        self._login(client)
        r = client.post("/api/change-password", json={
            "current_password": "not it", "new_password": "a whole new passphrase",
        })
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["error"], "wrong_current_password")

    def test_too_short_new_password_is_rejected(self):
        client = self.app.test_client()
        self._login(client)
        r = client.post("/api/change-password", json={
            "current_password": self.password, "new_password": "short",
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "password_too_short")

    def test_must_be_logged_in(self):
        client = self.app.test_client()
        r = client.post("/api/change-password", json={
            "current_password": self.password, "new_password": "a whole new passphrase",
        })
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
