import json
import os
import tempfile
import unittest

import config
import database
import tokens
from cogs.daily import today_str
from webapp import server


class RealtimeUpdatesTests(unittest.TestCase):
    GUILD_ID = 99
    CORRECT_ID = 100
    WRONG_ID = 200

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = config.DB_PATH
        self.original_secret = config.WEBAPP_SECRET
        config.DB_PATH = os.path.join(self.tmp.name, "test.db")
        config.WEBAPP_SECRET = "realtime-test-secret"
        if database._conn is not None:
            database._conn.close()
        database._conn = None
        database.init_db()
        with server._realtime_lock:
            server._realtime_subscribers.clear()

        self.date = today_str()
        options = json.dumps([
            [self.CORRECT_ID, "Bonne réponse"],
            [self.WRONG_ID, "Mauvaise réponse"],
        ])
        conn = database.get_conn()
        conn.execute(
            "INSERT INTO daily "
            "(guild_id, date, message_id, channel_id, author_id, author_name, "
            " content, options) VALUES (?, ?, 1, 2, ?, 'Bonne réponse', "
            " 'Message mystère', ?)",
            (self.GUILD_ID, self.date, self.CORRECT_ID, options),
        )
        conn.commit()

        self.app = server.create_app()
        self.app.testing = True

    def tearDown(self):
        with server._realtime_lock:
            server._realtime_subscribers.clear()
        if database._conn is not None:
            database._conn.close()
        database._conn = None
        config.DB_PATH = self.original_db_path
        config.WEBAPP_SECRET = self.original_secret
        self.tmp.cleanup()

    def _token(self, user_id, mode=database.MODE_AUTHOR):
        return tokens.make_token(
            {
                "g": self.GUILD_ID,
                "u": user_id,
                "d": self.date,
                "n": f"Joueur {user_id}",
                "a": "",
                "m": mode,
            },
            config.WEBAPP_SECRET,
        )

    def _record_attempt(self, user_id, guessed_id=None):
        guessed_id = self.CORRECT_ID if guessed_id is None else guessed_id
        is_correct = guessed_id == self.CORRECT_ID
        name = f"Joueur {user_id}"
        database.upsert_user(self.GUILD_ID, user_id, name, "")
        self.assertTrue(database.record_daily_attempt(
            self.GUILD_ID,
            self.date,
            user_id,
            name,
            guessed_id,
            is_correct,
            time_taken_ms=1200,
        ))
        database.update_streak(
            self.GUILD_ID, user_id, self.date, is_correct
        )
        database.record_answer(
            self.GUILD_ID, user_id, name, is_correct
        )

    def test_state_and_stream_are_locked_until_viewer_has_played(self):
        token = self._token(10)
        with self.app.test_client() as client:
            state = client.get(f"/.proxy/daily/state?t={token}")
            stream = client.get(f"/.proxy/daily/stream?t={token}")

        self.assertEqual(state.status_code, 403)
        self.assertEqual(state.get_json()["error"], "answer_required")
        self.assertEqual(stream.status_code, 403)
        self.assertEqual(stream.get_json()["error"], "answer_required")

    def test_state_contains_personalized_results_and_leaderboard(self):
        self._record_attempt(10)
        token = self._token(10)

        with self.app.test_client() as client:
            response = client.get(f"/.proxy/daily/state?t={token}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        data = response.get_json()
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["user_name"], "Joueur 10")
        self.assertTrue(data["leaderboard"][0]["is_me"])

    def test_stream_receives_update_and_unsubscribes_on_close(self):
        self._record_attempt(10)
        token = self._token(10)
        key = (self.GUILD_ID, self.date, database.MODE_AUTHOR)

        with self.app.test_client() as client:
            response = client.get(
                f"/.proxy/daily/stream?t={token}",
                buffered=False,
            )
            iterator = iter(response.response)
            initial = next(iterator).decode("utf-8")
            self.assertTrue(initial.startswith("data: "))
            self.assertIn(key, server._realtime_subscribers)

            self._record_attempt(20, self.WRONG_ID)
            server._publish_realtime(key)
            update = next(iterator).decode("utf-8")
            payload = json.loads(update.removeprefix("data: ").strip())
            self.assertEqual(len(payload["results"]), 2)
            response.close()

        self.assertNotIn(key, server._realtime_subscribers)

    def test_publish_is_isolated_by_mode(self):
        author_key = (self.GUILD_ID, self.date, database.MODE_AUTHOR)
        media_key = (self.GUILD_ID, self.date, database.MODE_MEDIA)
        subscriber = server._subscribe_realtime(author_key)
        try:
            server._publish_realtime(media_key)
            self.assertTrue(subscriber.empty())
            server._publish_realtime(author_key)
            self.assertIsNone(subscriber.get_nowait())
        finally:
            server._unsubscribe_realtime(author_key, subscriber)

    def test_answer_endpoint_publishes_an_update(self):
        key = (self.GUILD_ID, self.date, database.MODE_AUTHOR)
        subscriber = server._subscribe_realtime(key)
        try:
            with self.app.test_client() as client:
                response = client.post(
                    "/daily/answer",
                    json={
                        "token": self._token(30),
                        "guessed_id": self.CORRECT_ID,
                        "time_taken_ms": 900,
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertIsNone(subscriber.get_nowait())
        finally:
            server._unsubscribe_realtime(key, subscriber)


if __name__ == "__main__":
    unittest.main()
