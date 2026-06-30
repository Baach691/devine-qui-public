import os
import tempfile
import unittest
from unittest import mock

import config
import database
import tokens
from cogs.daily import today_str
from webapp import server


class DailyResultShareTests(unittest.TestCase):
    GUILD_ID = 1
    USER_ID = 30
    CHANNEL_ID = 900

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = config.DB_PATH
        self.original_secret = config.WEBAPP_SECRET
        config.DB_PATH = os.path.join(self.tmp.name, "test.db")
        config.WEBAPP_SECRET = "share-test-secret"
        if database._conn is not None:
            database._conn.close()
        database._conn = None
        database.init_db()

        self.date = today_str()
        database.mark_daily_announced(
            self.GUILD_ID,
            self.date,
            channel_id=self.CHANNEL_ID,
        )
        self.bot = object()
        self.app = server.create_app(bot=self.bot)
        self.app.testing = True
        self.token = tokens.make_token(
            {
                "g": self.GUILD_ID,
                "u": self.USER_ID,
                "d": self.date,
                "n": "Joueur Test",
                "a": "",
            },
            config.WEBAPP_SECRET,
        )

    def tearDown(self):
        if database._conn is not None:
            database._conn.close()
        database._conn = None
        config.DB_PATH = self.original_db_path
        config.WEBAPP_SECRET = self.original_secret
        self.tmp.cleanup()

    def _record_all_modes(self):
        for mode, guessed_id, correct in (
            (database.MODE_AUTHOR, 10, True),
            (database.MODE_PHRASE, 20, False),
            (database.MODE_MEDIA, 30, True),
        ):
            self.assertTrue(database.record_daily_attempt(
                self.GUILD_ID,
                self.date,
                self.USER_ID,
                "Joueur Test",
                guessed_id,
                correct,
                time_taken_ms=1200,
                mode=mode,
            ))
        self.assertTrue(database.record_sequence_attempt(
            self.GUILD_ID,
            self.date,
            self.USER_ID,
            "Joueur Test",
            [1, 2, 3, 5, 4],
            3,
            False,
            time_taken_ms=2400,
        ))

    def test_share_requires_all_four_modes(self):
        database.record_daily_attempt(
            self.GUILD_ID,
            self.date,
            self.USER_ID,
            "Joueur Test",
            10,
            True,
            mode=database.MODE_AUTHOR,
        )
        with (
            mock.patch.object(server, "send_daily_result") as send,
            self.app.test_client() as client,
        ):
            response = client.post(
                "/daily/share",
                json={"token": self.token},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "daily_not_complete")
        send.assert_not_called()

    def test_share_posts_spoiler_free_emojis_once_in_announce_channel(self):
        self._record_all_modes()
        with (
            mock.patch.object(
                server,
                "send_daily_result",
                return_value=123456,
            ) as send,
            self.app.test_client() as client,
        ):
            first = client.post("/daily/share", json={"token": self.token})
            second = client.post("/daily/share", json={"token": self.token})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["error"], "already_shared")
        send.assert_called_once()
        bot, channel_id, content = send.call_args.args
        self.assertIs(bot, self.bot)
        self.assertEqual(channel_id, self.CHANNEL_ID)
        self.assertIn("🌞 ✅", content)
        self.assertIn("✍️ ❌", content)
        self.assertIn("🖼️ ✅", content)
        self.assertIn("🔀 3️⃣/5", content)
        self.assertIn("2/4 modes réussis", content)
        self.assertNotIn("guessed", content)
        self.assertTrue(database.has_daily_result_share(
            self.GUILD_ID,
            self.date,
            self.USER_ID,
        ))

    def test_failed_send_releases_reservation_for_retry(self):
        self._record_all_modes()
        with (
            mock.patch.object(server, "send_daily_result", return_value=None),
            self.app.test_client() as client,
        ):
            response = client.post("/daily/share", json={"token": self.token})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"], "share_failed")
        self.assertFalse(database.has_daily_result_share(
            self.GUILD_ID,
            self.date,
            self.USER_ID,
        ))
        self.assertTrue(database.reserve_daily_result_share(
            self.GUILD_ID,
            self.date,
            self.USER_ID,
            self.CHANNEL_ID,
        ))

    def test_concurrent_share_reports_in_progress(self):
        self._record_all_modes()
        self.assertTrue(database.reserve_daily_result_share(
            self.GUILD_ID,
            self.date,
            self.USER_ID,
            self.CHANNEL_ID,
        ))

        with self.app.test_client() as client:
            response = client.post("/daily/share", json={"token": self.token})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "share_in_progress")

    def test_live_progress_exposes_share_only_on_completed_own_row(self):
        self._record_all_modes()
        progress = server._daily_progress_view(
            self.GUILD_ID,
            self.date,
            self.USER_ID,
        )
        own = next(player for player in progress if player["is_me"])
        self.assertTrue(own["can_share"])
        self.assertFalse(own["shared"])

        database.reserve_daily_result_share(
            self.GUILD_ID,
            self.date,
            self.USER_ID,
            self.CHANNEL_ID,
        )
        database.complete_daily_result_share(
            self.GUILD_ID,
            self.date,
            self.USER_ID,
            123456,
        )
        refreshed = server._daily_progress_view(
            self.GUILD_ID,
            self.date,
            self.USER_ID,
        )
        own = next(player for player in refreshed if player["is_me"])
        self.assertTrue(own["shared"])


if __name__ == "__main__":
    unittest.main()
