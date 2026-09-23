"""Real database, real Flask test client: file storage (offers, contracts,
flyers). B2 itself is mocked out — these tests check the DB bookkeeping and
the permission gate, not that Backblaze is reachable.
"""
import unittest
from unittest.mock import patch

import app as app_module
import auth
import db


class Files(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()
        self.password = "correct horse battery staple"

        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"files-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Crew', %s, %s, 'crew') RETURNING id""",
            (f"files-test-crew-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.crew_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM venues LIMIT 1")
        self.venue_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO events (venue_id, show_date, status, created_by)
               VALUES (%s, '2027-05-01', 'confirmed', %s) RETURNING id""",
            (self.venue_id, self.booker_id),
        )
        self.event_id = cur.fetchone()[0]
        self.conn.commit()

        self.booker_email = self._email(self.booker_id)
        self.crew_email = self._email(self.crew_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.file_ids = []

        self.presign_upload_patcher = patch("storage.presign_upload", return_value="https://upload.example/put")
        self.presign_download_patcher = patch("storage.presign_download", return_value="https://download.example/get")
        self.delete_object_patcher = patch("storage.delete_object", return_value=None)
        self.presign_upload_patcher.start()
        self.presign_download_patcher.start()
        self.mock_delete_object = self.delete_object_patcher.start()

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def tearDown(self):
        self.presign_upload_patcher.stop()
        self.presign_download_patcher.stop()
        self.delete_object_patcher.stop()
        cur = self.conn.cursor()
        for file_id in self.file_ids:
            cur.execute("DELETE FROM audit_log WHERE entity_type IN ('file', 'event') AND entity_id = %s", (file_id,))
            cur.execute("DELETE FROM event_files WHERE id = %s", (file_id,))
        cur.execute("DELETE FROM audit_log WHERE entity_type = 'event' AND entity_id = %s", (self.event_id,))
        cur.execute("DELETE FROM events WHERE id = %s", (self.event_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.booker_id, self.crew_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.booker_id, self.crew_id))
        self.conn.commit()
        self.conn.close()

    def _upload(self, client, **extra):
        payload = {"filename": "contract.pdf", "content_type": "application/pdf"}
        payload.update(extra)
        r = client.post("/api/files/upload-url", json=payload)
        self.assertEqual(r.status_code, 201, r.get_json())
        body = r.get_json()
        self.file_ids.append(body["id"])
        return body

    def test_booker_can_request_an_upload_url(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        body = self._upload(client, event_id=self.event_id)
        self.assertEqual(body["upload_url"], "https://upload.example/put")
        files = client.get(f"/api/files?event_id={self.event_id}").get_json()["files"]
        mine = next(f for f in files if f["id"] == body["id"])
        self.assertEqual(mine["filename"], "contract.pdf")
        self.assertEqual(mine["uploaded_by_name"], "Test Booker")

    def test_filename_is_required(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/files/upload-url", json={"content_type": "application/pdf"})
        self.assertEqual(r.status_code, 400)

    def test_event_id_must_exist(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.post("/api/files/upload-url", json={"filename": "x.pdf", "event_id": 999999})
        self.assertEqual(r.status_code, 404)

    def test_a_file_with_no_event_id_lives_in_the_general_library(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        body = self._upload(client, filename="w9.pdf")
        files = client.get("/api/files").get_json()["files"]
        self.assertIn(body["id"], [f["id"] for f in files])
        self.assertIsNone(next(f for f in files if f["id"] == body["id"])["event_id"])

    def test_listing_filters_by_event(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        for_show = self._upload(client, filename="flyer.png", event_id=self.event_id)
        general = self._upload(client, filename="w9.pdf")

        show_files = client.get(f"/api/files?event_id={self.event_id}").get_json()["files"]
        self.assertEqual([f["id"] for f in show_files], [for_show["id"]])

        library_files = client.get("/api/files").get_json()["files"]
        library_ids = [f["id"] for f in library_files]
        self.assertIn(general["id"], library_ids)
        self.assertNotIn(for_show["id"], library_ids)

    def test_crew_cannot_list_or_upload_files(self):
        crew_client = self.app.test_client()
        self._login(crew_client, self.crew_email)
        r = crew_client.get("/api/files")
        self.assertEqual(r.status_code, 403)
        r = crew_client.post("/api/files/upload-url", json={"filename": "x.pdf"})
        self.assertEqual(r.status_code, 403)

    def test_download_url_for_unknown_file_is_404(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.get("/api/files/999999/download-url")
        self.assertEqual(r.status_code, 404)

    def test_getting_a_download_url(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        body = self._upload(client)
        r = client.get(f"/api/files/{body['id']}/download-url")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["url"], "https://download.example/get")

    def test_deleting_a_file_removes_it_and_the_storage_object(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        body = self._upload(client)
        r = client.delete(f"/api/files/{body['id']}")
        self.assertEqual(r.status_code, 200)
        self.mock_delete_object.assert_called_once()
        files = client.get("/api/files").get_json()["files"]
        self.assertNotIn(body["id"], [f["id"] for f in files])


if __name__ == "__main__":
    unittest.main()
