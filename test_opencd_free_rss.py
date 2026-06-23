import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import opencd_free_rss as app


class PromoTests(unittest.TestCase):
    def test_detects_free_and_2xfree_markers(self):
        self.assertEqual(app.find_promotion('<img src="/pic/free.png" title="Free">'), "free")
        self.assertEqual(app.find_promotion('<img alt="2X Free" src="/pic/2xfree.png">'), "2xfree")
        self.assertEqual(app.find_promotion("<td>\u4fc3\u9500</td><td>2\u500d\u514d\u8d39</td>"), "2xfree")

    def test_ignores_non_free_promotions(self):
        self.assertIsNone(app.find_promotion('<img src="/pic/halfdown.png" title="50%">'))

    def test_derives_detail_url_from_download_link(self):
        url = app.detail_url_from_download("https://open.cd/download.php?id=123&passkey=secret")

        parsed = urlparse(url)
        self.assertEqual(parsed.path, "/details.php")
        self.assertEqual(parse_qs(parsed.query), {"id": ["123"]})


class HttpTests(unittest.TestCase):
    def test_fetch_text_uses_custom_user_agent(self):
        captured = {}

        class FakeResponse:
            headers = type("Headers", (), {"get_content_charset": lambda self: "utf-8"})()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"ok"

        def fake_urlopen(request, timeout):
            captured["ua"] = request.headers.get("User-agent") or request.headers.get("User-Agent")
            return FakeResponse()

        with patch.object(app, "urlopen", fake_urlopen):
            self.assertEqual(app.fetch_text("https://example.invalid", user_agent="Browser UA"), "ok")

        self.assertEqual(captured["ua"], "Browser UA")


class RssTests(unittest.TestCase):
    def test_parses_rss_item_urls(self):
        xml = """<?xml version="1.0"?>
        <rss><channel><item>
          <title>Album</title>
          <link>https://open.cd/download.php?id=7&amp;passkey=secret</link>
          <comments>https://open.cd/details.php?id=7</comments>
        </item></channel></rss>
        """

        items = app.parse_rss(xml)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Album")
        self.assertEqual(items[0].torrent_url, "https://open.cd/download.php?id=7&passkey=secret")
        self.assertEqual(items[0].detail_url, "https://open.cd/details.php?id=7")

    def test_prefers_enclosure_as_torrent_url(self):
        xml = """<?xml version="1.0"?>
        <rss><channel><item>
          <title>Album</title>
          <link>https://open.cd/details.php?id=7</link>
          <comments>https://open.cd/details.php?id=7&amp;cmtpage=0#startcomments</comments>
          <enclosure url="https://open.cd/download.php?id=7&amp;passkey=secret" type="application/x-bittorrent" />
        </item></channel></rss>
        """

        items = app.parse_rss(xml)

        self.assertEqual(items[0].torrent_url, "https://open.cd/download.php?id=7&passkey=secret")
        self.assertEqual(items[0].detail_url, "https://open.cd/details.php?id=7&cmtpage=0#startcomments")


class TransmissionTests(unittest.TestCase):
    def test_builds_torrent_add_request(self):
        body = json.loads(app.transmission_add_body("https://open.cd/download.php?id=7&passkey=secret"))

        self.assertEqual(body["method"], "torrent-add")
        self.assertEqual(body["arguments"]["filename"], "https://open.cd/download.php?id=7&passkey=secret")


class RunOnceTests(unittest.TestCase):
    def test_item_error_does_not_stop_the_rest_of_the_feed(self):
        xml = """<rss><channel>
        <item><title>Bad</title><link>https://open.cd/details.php?id=1</link><enclosure url="https://open.cd/download.php?id=1&amp;passkey=x" /></item>
        <item><title>Good</title><link>https://open.cd/details.php?id=2</link><enclosure url="https://open.cd/download.php?id=2&amp;passkey=x" /></item>
        </channel></rss>"""

        class FakeTransmission:
            def __init__(self):
                self.added = []

            def add(self, url):
                self.added.append(url)
                return "success"

        with tempfile.TemporaryDirectory() as tmp:
            config = app.Config(
                rss_url="https://example.invalid/rss",
                transmission_url="",
                transmission_username="",
                transmission_password="",
                site_cookie="cookie",
                state_file=app.Path(tmp) / "seen.json",
                interval=600,
                download_dir="",
                paused=False,
                request_delay=0,
                max_detail_checks=3,
            )
            transmission = FakeTransmission()
            with patch.object(app, "fetch_text", side_effect=[xml, TimeoutError("slow"), '<img src="/pic/free.png">']):
                app.run_once(config, transmission)

            self.assertEqual(transmission.added, ["https://open.cd/download.php?id=2&passkey=x"])
            self.assertEqual(json.loads(config.state_file.read_text(encoding="utf-8")), {"2": {"checks": 1, "added": True}})

    def test_saves_seen_after_each_checked_item(self):
        xml = """<rss><channel>
        <item><title>One</title><link>https://open.cd/details.php?id=1</link><enclosure url="https://open.cd/download.php?id=1&amp;passkey=x" /></item>
        <item><title>Two</title><link>https://open.cd/details.php?id=2</link><enclosure url="https://open.cd/download.php?id=2&amp;passkey=x" /></item>
        </channel></rss>"""

        class FakeTransmission:
            def add(self, url):
                return "success"

        with tempfile.TemporaryDirectory() as tmp:
            config = app.Config(
                rss_url="https://example.invalid/rss",
                transmission_url="",
                transmission_username="",
                transmission_password="",
                site_cookie="cookie",
                state_file=app.Path(tmp) / "seen.json",
                interval=600,
                download_dir="",
                paused=False,
                request_delay=1,
                max_detail_checks=3,
            )
            with patch.object(app, "fetch_text", side_effect=[xml, "no promo"]), patch.object(app.time, "sleep", side_effect=RuntimeError("stop")):
                with self.assertRaises(RuntimeError):
                    app.run_once(config, FakeTransmission())

            self.assertEqual(json.loads(config.state_file.read_text(encoding="utf-8")), {"1": {"checks": 1, "added": False}})


class StateTests(unittest.TestCase):
    def test_load_state_migrates_legacy_seen_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            path.write_text(json.dumps(["1", "2"]), encoding="utf-8")

            state = app.load_state(path)

        self.assertEqual(state, {"1": {"checks": 1, "added": False}, "2": {"checks": 1, "added": False}})

    def test_should_skip_added_or_checked_three_times(self):
        state = {
            "1": {"checks": 1, "added": True},
            "2": {"checks": 3, "added": False},
            "3": {"checks": 2, "added": False},
        }

        self.assertTrue(app.should_skip(state, "1", 3))
        self.assertTrue(app.should_skip(state, "2", 3))
        self.assertFalse(app.should_skip(state, "3", 3))

    def test_successful_non_promo_check_increments_count(self):
        state = {}

        app.mark_checked(state, "1", added=False)

        self.assertEqual(state, {"1": {"checks": 1, "added": False}})

    def test_added_torrent_is_marked_added(self):
        state = {"1": {"checks": 1, "added": False}}

        app.mark_checked(state, "1", added=True)

        self.assertEqual(state, {"1": {"checks": 2, "added": True}})


class CloudflareTests(unittest.TestCase):
    def test_detects_cloudflare_challenge(self):
        self.assertTrue(app.is_cloudflare_challenge("<title>Just a moment...</title><script>cf-chl</script>"))
        self.assertTrue(app.is_cloudflare_challenge("<html>cf_clearance required by Cloudflare</html>"))
        self.assertFalse(app.is_cloudflare_challenge("<html>normal torrent detail</html>"))

    def test_cloudflare_challenge_does_not_count_as_check(self):
        xml = """<rss><channel>
        <item><title>Blocked</title><link>https://open.cd/details.php?id=1</link><enclosure url="https://open.cd/download.php?id=1&amp;passkey=x" /></item>
        </channel></rss>"""

        class FakeTransmission:
            def add(self, url):
                raise AssertionError("should not add")

        with tempfile.TemporaryDirectory() as tmp:
            config = app.Config(
                rss_url="https://example.invalid/rss",
                transmission_url="",
                transmission_username="",
                transmission_password="",
                site_cookie="cookie",
                state_file=app.Path(tmp) / "seen.json",
                interval=600,
                download_dir="",
                paused=False,
                request_delay=0,
            )
            with patch.object(app, "fetch_text", side_effect=[xml, "Just a moment... cf-chl"]), patch.object(app, "notify") as notify:
                app.run_once(config, FakeTransmission())

            self.assertFalse(config.state_file.exists())
            notify.assert_called_once()


class CookieCloudTests(unittest.TestCase):
    def test_cookiecloud_cookie_header_filters_host(self):
        payload = {
            "cookie_data": {
                "open.cd": [
                    {"name": "a", "value": "1", "domain": "open.cd"},
                    {"name": "b", "value": "2", "domain": ".open.cd"},
                ],
                "example.com": [{"name": "x", "value": "y", "domain": "example.com"}],
            }
        }

        self.assertEqual(app.cookiecloud_cookie_header(payload, "open.cd"), "a=1; b=2")

    def test_cookiecloud_key_matches_official_formula(self):
        self.assertEqual(app.cookiecloud_key("uuid", "password"), "c1afd3d73880531d")


class NotificationTests(unittest.TestCase):
    def test_telegram_body(self):
        body = json.loads(app.telegram_body("chat", "hello"))

        self.assertEqual(body, {"chat_id": "chat", "text": "hello"})

    def test_notify_returns_false_when_disabled(self):
        config = app.Config(
            rss_url="https://example.invalid/rss",
            transmission_url="",
            transmission_username="",
            transmission_password="",
            site_cookie="",
            state_file=app.Path("seen.json"),
            interval=600,
            download_dir="",
            paused=False,
            request_delay=10,
        )

        self.assertIs(app.notify(config, "hello"), False)

    def test_notify_returns_true_when_sent(self):
        config = app.Config(
            rss_url="https://example.invalid/rss",
            transmission_url="",
            transmission_username="",
            transmission_password="",
            site_cookie="",
            state_file=app.Path("seen.json"),
            interval=600,
            download_dir="",
            paused=False,
            request_delay=10,
            telegram_bot_token="token",
            telegram_chat_id="chat",
        )

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch.object(app, "urlopen", return_value=FakeResponse()):
            self.assertIs(app.notify(config, "hello"), True)


class StatusTests(unittest.TestCase):
    def test_status_summary_counts_added_pending_and_exhausted(self):
        state = {
            "1": {"checks": 1, "added": True},
            "2": {"checks": 2, "added": False},
            "3": {"checks": 3, "added": False},
        }

        self.assertEqual(
            app.status_summary(state, 3),
            "total=3 added=1 pending=1 exhausted=1 checks=1:1,2:1,3+:1",
        )

    def test_cookie_summary_does_not_print_cookie_values(self):
        self.assertEqual(app.cookie_summary("a=1; b=2"), "cookie=ok pairs=2")
        self.assertEqual(app.cookie_summary(""), "cookie=missing pairs=0")


class LogTests(unittest.TestCase):
    def test_trim_log_keeps_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_bytes(b"0123456789abcdef")

            app.trim_log(path, 8)

            self.assertEqual(path.read_bytes(), b"cdef")


class ConfigTests(unittest.TestCase):
    def test_loads_request_delay(self):
        with patch.dict("os.environ", {"RSS_URL": "https://example.invalid/rss", "REQUEST_DELAY_SECONDS": "1.5"}, clear=True):
            config = app.load_config()

        self.assertEqual(config.request_delay, 1.5)

    def test_direct_python_defaults_are_slow_enough(self):
        with patch.dict("os.environ", {"RSS_URL": "https://example.invalid/rss"}, clear=True):
            config = app.load_config()

        self.assertEqual(config.interval, 600)
        self.assertEqual(config.request_delay, 10)
        self.assertEqual(config.state_file, app.Path("seen.json"))
        self.assertEqual(config.transmission_url, "http://127.0.0.1:9091/transmission/rpc")
        self.assertEqual(config.max_detail_checks, 3)
        self.assertEqual(config.log_max_bytes, 2 * 1024 * 1024)
        self.assertEqual(config.user_agent, "opencd-free-rss/1.0")
        self.assertEqual(app.config_summary(config), "started poll=600s delay=10s max_checks=3 cookiecloud=off telegram=off ua=default")


if __name__ == "__main__":
    unittest.main()
