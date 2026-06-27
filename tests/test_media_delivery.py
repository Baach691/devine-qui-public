import json
import os
import tempfile
import unittest
from unittest import mock

import config
import database
import tokens
from cogs.daily import today_str
from webapp import server


class _FakeUpstream:
    status = 206
    headers = {
        "Content-Type": "image/jpeg",
        "Content-Length": "4",
        "Content-Range": "bytes 0-3/4",
        "Accept-Ranges": "bytes",
    }

    def __init__(self):
        self._chunks = [b"test", b""]
        self.closed = False

    def read(self, _size):
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


class MediaDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = config.DB_PATH
        self.original_secret = config.WEBAPP_SECRET
        config.DB_PATH = os.path.join(self.tmp.name, "test.db")
        config.WEBAPP_SECRET = "media-test-secret"
        if database._conn is not None:
            database._conn.close()
        database._conn = None
        database.init_db()

        today = today_str()
        database.get_conn().execute(
            "INSERT INTO media_daily "
            "(guild_id, date, message_id, channel_id, author_id, author_name, "
            " content, options) VALUES (1, ?, 10, 20, 30, 'Player One', ?, ?)",
            (
                today,
                "https://cdn.discordapp.com/attachments/20/11/old.jpg?ex=1&is=1&hm=1",
                json.dumps([[30, "Player One"], [31, "Player Two"]]),
            ),
        )
        database.get_conn().commit()
        self.token = tokens.make_token(
            {
                "g": 1,
                "u": 30,
                "d": today,
                "n": "Player One",
                "a": "",
                "m": database.MODE_MEDIA,
            },
            config.WEBAPP_SECRET,
        )
        self.app = server.create_app()
        self.app.testing = True

    def tearDown(self):
        if database._conn is not None:
            database._conn.close()
        database._conn = None
        config.DB_PATH = self.original_db_path
        config.WEBAPP_SECRET = self.original_secret
        self.tmp.cleanup()

    def test_media_is_refreshed_and_streamed_from_same_origin(self):
        fresh_url = (
            "https://cdn.discordapp.com/attachments/20/11/fresh.jpg"
            "?ex=2&is=1&hm=abc"
        )
        upstream = _FakeUpstream()
        with (
            mock.patch.object(
                server, "fetch_current_media_url", return_value=fresh_url
            ) as refresh,
            mock.patch.object(
                server.urllib.request, "urlopen", return_value=upstream
            ) as urlopen,
            self.app.test_client() as client,
        ):
            response = client.get(
                f"/daily/media?t={self.token}",
                headers={"Range": "bytes=0-3"},
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"test")
        self.assertEqual(response.content_type, "image/jpeg")
        self.assertEqual(response.headers["Content-Range"], "bytes 0-3/4")
        self.assertTrue(upstream.closed)
        refresh.assert_called_once_with(None, 20, 10)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, fresh_url)
        self.assertEqual(request.headers["Range"], "bytes=0-3")

    def test_non_media_token_is_rejected(self):
        payload = tokens.verify_token(self.token, config.WEBAPP_SECRET)
        payload.pop("m")
        author_token = tokens.make_token(payload, config.WEBAPP_SECRET)
        with self.app.test_client() as client:
            response = client.get(f"/daily/media?t={author_token}")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "not_media_mode")


if __name__ == "__main__":
    unittest.main()
