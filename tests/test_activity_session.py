import os
import json
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

import config
import database
import tokens
from cogs.daily import _build_daily_link, today_str
from webapp import server


class ActivitySessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = {
            "DB_PATH": config.DB_PATH,
            "DISCORD_CLIENT_ID": config.DISCORD_CLIENT_ID,
            "ALLOWED_ROLE_IDS": config.ALLOWED_ROLE_IDS,
            "WEBAPP_SECRET": config.WEBAPP_SECRET,
        }
        config.DB_PATH = os.path.join(self.tmp.name, "test.db")
        config.DISCORD_CLIENT_ID = "1234"
        config.ALLOWED_ROLE_IDS = []
        config.WEBAPP_SECRET = "activity-test-secret"
        if database._conn is not None:
            database._conn.close()
        database._conn = None
        database.init_db()
        self.app = server.create_app()
        self.app.testing = True

    def tearDown(self):
        if database._conn is not None:
            database._conn.close()
        database._conn = None
        for key, value in self.original.items():
            setattr(config, key, value)
        self.tmp.cleanup()

    @staticmethod
    def _discord_response(path, authorization):
        if path == "/oauth2/@me":
            return {
                "application": {"id": "1234"},
                "user": {
                    "id": "42",
                    "username": "player_one",
                    "global_name": "Player One",
                    "avatar": None,
                },
            }
        if path == "/users/@me/guilds":
            return [{"id": "99", "name": "Serveur de test"}]
        raise AssertionError(f"Appel Discord inattendu: {path} ({authorization})")

    def test_valid_session_returns_signed_activity_daily_url(self):
        with mock.patch.object(
            server, "_discord_api_get", side_effect=self._discord_response
        ):
            with self.app.test_client() as client:
                response = client.post(
                    "/api/activity/session",
                    json={"access_token": "oauth-token", "guild_id": "99"},
                )

        self.assertEqual(response.status_code, 200)
        url = response.get_json()["url"]
        self.assertTrue(url.startswith("/.proxy/daily?t="))
        token = parse_qs(urlparse(url).query)["t"][0]
        payload = tokens.verify_token(token, config.WEBAPP_SECRET)
        self.assertEqual(payload["g"], 99)
        self.assertEqual(payload["u"], 42)
        self.assertEqual(payload["d"], today_str())
        self.assertEqual(payload["x"], "activity")

    def test_session_rejects_a_guild_the_user_does_not_belong_to(self):
        with mock.patch.object(
            server, "_discord_api_get", side_effect=self._discord_response
        ):
            with self.app.test_client() as client:
                response = client.post(
                    "/api/activity/session",
                    json={"access_token": "oauth-token", "guild_id": "100"},
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "not_a_guild_member")

    def test_activity_links_stay_inside_discord_proxy(self):
        url = _build_daily_link(
            99,
            42,
            today_str(),
            "Player One",
            "https://example.test/avatar.png",
            mode=database.MODE_MEDIA,
            activity=True,
        )

        self.assertTrue(url.startswith("/.proxy/daily?t="))
        token = parse_qs(urlparse(url).query)["t"][0]
        payload = tokens.verify_token(token, config.WEBAPP_SECRET)
        self.assertEqual(payload["m"], database.MODE_MEDIA)
        self.assertEqual(payload["x"], "activity")

    def test_activity_daily_renders_all_three_internal_tabs(self):
        today = today_str()
        conn = database.get_conn()
        author_options = json.dumps([[42, "Player One"], [43, "Player Two"]])
        phrase_options = json.dumps([
            [100, "La bonne phrase", 42],
            [101, "Une autre phrase", 43],
        ])
        conn.execute(
            "INSERT INTO daily "
            "(guild_id, date, message_id, channel_id, author_id, author_name, "
            " content, options) VALUES (99, ?, 1, 2, 42, 'Player One', 'Message', ?)",
            (today, author_options),
        )
        conn.execute(
            "INSERT INTO phrase_daily "
            "(guild_id, date, target_author_id, target_author_name, "
            " correct_message_id, channel_id, content, options) "
            "VALUES (99, ?, 42, 'Player One', 100, 2, 'La bonne phrase', ?)",
            (today, phrase_options),
        )
        conn.execute(
            "INSERT INTO media_daily "
            "(guild_id, date, message_id, channel_id, author_id, author_name, "
            " content, options) VALUES (99, ?, 3, 2, 42, 'Player One', "
            "'https://example.test/media.png', ?)",
            (today, author_options),
        )
        conn.commit()
        token = tokens.make_token(
            {
                "g": 99,
                "u": 42,
                "d": today,
                "n": "Player One",
                "a": "",
                "x": "activity",
            },
            config.WEBAPP_SECRET,
        )

        with self.app.test_client() as client:
            response = client.get(f"/.proxy/daily?t={token}")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Qui a écrit ça ?", html)
        self.assertIn("Devine la phrase", html)
        self.assertIn("Devine le média", html)
        self.assertEqual(html.count('href="/.proxy/daily?t='), 3)


if __name__ == "__main__":
    unittest.main()
