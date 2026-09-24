"""Real database, real Flask test client: the owner-curated genre list
(only the owner can add/remove a genre; booker/crew can still read it) and
the artist genre field only accepting a value that's actually on the list.
"""
import unittest

import app as app_module
import auth
import db


class Genres(unittest.TestCase):
    def setUp(self):
        self.conn = db.get_connection()
        cur = self.conn.cursor()

        self.password = "correct horse battery staple"
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Owner', %s, %s, 'owner') RETURNING id""",
            (f"genre-test-owner-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.owner_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO people (name, email, password_hash, access_level)
               VALUES ('Test Booker', %s, %s, 'booker') RETURNING id""",
            (f"genre-test-booker-{id(self)}@example.invalid", auth.hash_password(self.password)),
        )
        self.booker_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO genres (name) VALUES (%s) RETURNING id",
            (f"Genre Test Existing {id(self)}",),
        )
        self.genre_id = cur.fetchone()[0]
        self.genre_name = f"Genre Test Existing {id(self)}"
        cur.execute(
            "INSERT INTO artists (name) VALUES (%s) RETURNING id",
            (f"Genre Test Band {id(self)}",),
        )
        self.artist_id = cur.fetchone()[0]
        self.conn.commit()

        self.owner_email = self._email(self.owner_id)
        self.booker_email = self._email(self.booker_id)
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.new_genre_ids = []

    def _email(self, person_id):
        cur = self.conn.cursor()
        cur.execute("SELECT email FROM people WHERE id = %s", (person_id,))
        return cur.fetchone()[0]

    def _login(self, client, email):
        r = client.post("/api/login", json={"email": email, "password": self.password})
        self.assertEqual(r.status_code, 200, r.get_json())

    def tearDown(self):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM audit_log WHERE entity_type IN ('artist', 'genre')")
        cur.execute("DELETE FROM artists WHERE id = %s", (self.artist_id,))
        if self.new_genre_ids:
            cur.execute("DELETE FROM genres WHERE id = ANY(%s)", (self.new_genre_ids,))
        cur.execute("DELETE FROM genres WHERE id = %s", (self.genre_id,))
        cur.execute("DELETE FROM sessions WHERE person_id IN (%s, %s)", (self.owner_id, self.booker_id))
        cur.execute("DELETE FROM people WHERE id IN (%s, %s)", (self.owner_id, self.booker_id))
        self.conn.commit()
        self.conn.close()

    def test_owner_can_add_and_remove_a_genre(self):
        client = self.app.test_client()
        self._login(client, self.owner_email)
        r = client.post("/api/genres", json={"name": f"New Genre {id(self)}"})
        self.assertEqual(r.status_code, 201, r.get_json())
        new_id = r.get_json()["id"]
        self.new_genre_ids.append(new_id)

        names = [g["name"] for g in client.get("/api/genres").get_json()["genres"]]
        self.assertIn(f"New Genre {id(self)}", names)

        r = client.delete(f"/api/genres/{new_id}")
        self.assertEqual(r.status_code, 200)
        self.new_genre_ids.remove(new_id)
        names = [g["name"] for g in client.get("/api/genres").get_json()["genres"]]
        self.assertNotIn(f"New Genre {id(self)}", names)

    def test_duplicate_genre_name_is_rejected(self):
        client = self.app.test_client()
        self._login(client, self.owner_email)
        r = client.post("/api/genres", json={"name": self.genre_name})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "genre_already_exists")

    def test_booker_can_read_but_not_write_the_genre_list(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.get("/api/genres")
        self.assertEqual(r.status_code, 200)

        r = client.post("/api/genres", json={"name": f"Booker Genre {id(self)}"})
        self.assertEqual(r.status_code, 403)
        r = client.delete(f"/api/genres/{self.genre_id}")
        self.assertEqual(r.status_code, 403)

    def test_artist_genre_must_be_on_the_list(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.patch(f"/api/artists/{self.artist_id}", json={"genre": "Not A Real Genre"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "unknown_genre")

        r = client.patch(f"/api/artists/{self.artist_id}", json={"genre": self.genre_name})
        self.assertEqual(r.status_code, 200)
        artists = client.get("/api/artists").get_json()["artists"]
        mine = next(a for a in artists if a["id"] == self.artist_id)
        self.assertEqual(mine["genre"], self.genre_name)

    def test_sub_genre_is_free_text(self):
        client = self.app.test_client()
        self._login(client, self.booker_email)
        r = client.patch(f"/api/artists/{self.artist_id}", json={"sub_genre": "Whatever I Feel Like"})
        self.assertEqual(r.status_code, 200, r.get_json())
        artists = client.get("/api/artists").get_json()["artists"]
        mine = next(a for a in artists if a["id"] == self.artist_id)
        self.assertEqual(mine["sub_genre"], "Whatever I Feel Like")


if __name__ == "__main__":
    unittest.main()
