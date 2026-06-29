import json
import os
import tempfile
import unittest
from types import SimpleNamespace
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
        self.original_media_cache_dir = config.MEDIA_CACHE_DIR
        config.DB_PATH = os.path.join(self.tmp.name, "test.db")
        config.WEBAPP_SECRET = "media-test-secret"
        config.MEDIA_CACHE_DIR = os.path.join(self.tmp.name, "media-cache")
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
                "https://cdn.discordapp.com/attachments/20/11/old.mp4?ex=1&is=1&hm=1",
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
        config.MEDIA_CACHE_DIR = self.original_media_cache_dir
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

    def test_compat_video_serves_cached_h264_version(self):
        compatible_path = os.path.join(self.tmp.name, "compatible.mp4")
        with open(compatible_path, "wb") as media:
            media.write(b"compatible-video")
        fresh_url = (
            "https://cdn.discordapp.com/attachments/20/11/fresh.mp4"
            "?ex=2&is=1&hm=abc"
        )

        with (
            mock.patch.object(
                server, "fetch_current_media_url", return_value=fresh_url
            ),
            mock.patch.object(
                server, "_compatible_video_path", return_value=compatible_path
            ) as transcode,
            self.app.test_client() as client,
        ):
            response = client.get(f"/daily/media?t={self.token}&compat=1")

        response_body = response.data
        response.close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response_body, b"compatible-video")
        self.assertEqual(response.content_type, "video/mp4")
        transcode.assert_called_once_with(1, today_str(), 10, fresh_url)

    def test_compatible_video_is_transcoded_to_browser_safe_codecs(self):
        fresh_url = (
            "https://cdn.discordapp.com/attachments/20/11/fresh.mov"
            "?ex=2&is=1&hm=abc"
        )

        def fake_download(_url, destination):
            with open(destination, "wb") as source:
                source.write(b"source-video")

        def fake_ffmpeg(command, **_kwargs):
            with open(command[-1], "wb") as output:
                output.write(b"h264-video")
            return SimpleNamespace(returncode=0, stderr="")

        with (
            mock.patch.object(server.shutil, "which", return_value="/usr/bin/ffmpeg"),
            mock.patch.object(
                server,
                "_download_media_for_transcode",
                side_effect=fake_download,
            ),
            mock.patch.object(
                server.subprocess,
                "run",
                side_effect=fake_ffmpeg,
            ) as ffmpeg,
        ):
            path = server._compatible_video_path(
                1, today_str(), 10, fresh_url
            )

        self.assertEqual(path, server._media_cache_path(1, today_str(), 10))
        with open(path, "rb") as transcoded:
            self.assertEqual(transcoded.read(), b"h264-video")
        command = ffmpeg.call_args.args[0]
        self.assertIn("libx264", command)
        self.assertIn("yuv420p", command)
        self.assertIn("aac", command)
        self.assertIn("+faststart", command)

    def test_compat_video_returns_clear_error_without_transcoder(self):
        fresh_url = (
            "https://cdn.discordapp.com/attachments/20/11/fresh.mp4"
            "?ex=2&is=1&hm=abc"
        )
        with (
            mock.patch.object(
                server, "fetch_current_media_url", return_value=fresh_url
            ),
            mock.patch.object(
                server, "_compatible_video_path", return_value=None
            ),
            self.app.test_client() as client,
        ):
            response = client.get(f"/daily/media?t={self.token}&compat=1")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json()["error"],
            "compatible_video_unavailable",
        )

    def test_hardcore_timer_adds_and_locks_video_duration(self):
        with self.app.test_client() as client:
            first = client.post(
                "/daily/start",
                json={
                    "token": self.token,
                    "difficulty": "hardcore",
                    "media_duration_ms": 24500,
                },
            )
            second = client.post(
                "/daily/start",
                json={
                    "token": self.token,
                    "difficulty": "hardcore",
                    "media_duration_ms": 90000,
                },
            )

        expected_ms = config.HARDCORE_TIME_LIMIT * 1000 + 24500
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["hardcore_limit_ms"], expected_ms)
        self.assertEqual(second.get_json()["hardcore_limit_ms"], expected_ms)
        self.assertEqual(
            database.get_daily_time_bonus_seconds(
                1, today_str(), 30, database.MODE_MEDIA
            ),
            24.5,
        )

    def test_normal_mode_ignores_video_duration(self):
        with self.app.test_client() as client:
            response = client.post(
                "/daily/start",
                json={
                    "token": self.token,
                    "difficulty": "normal",
                    "media_duration_ms": 24500,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["hardcore_limit_ms"],
            config.HARDCORE_TIME_LIMIT * 1000,
        )

    def test_video_bonus_is_used_by_server_timeout(self):
        with self.app.test_client() as client:
            client.post(
                "/daily/start",
                json={
                    "token": self.token,
                    "difficulty": "hardcore",
                    "media_duration_ms": 24500,
                },
            )
            database.get_conn().execute(
                "UPDATE daily_start SET started_at = datetime('now', '-20 seconds') "
                "WHERE guild_id = 1 AND date = ? AND user_id = 30 AND mode = ?",
                (today_str(), database.MODE_MEDIA),
            )
            database.get_conn().commit()
            response = client.post(
                "/daily/answer",
                json={"token": self.token, "guessed_id": 30},
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["timed_out"])
        self.assertTrue(response.get_json()["correct"])


if __name__ == "__main__":
    unittest.main()
