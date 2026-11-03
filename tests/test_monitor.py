import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gacha_alert.monitor import (MonitorError, PRODUCT_URL, Snapshot, check_once,
                                parse_page, parse_stores, product_code_from_page,
                                fetch_stores, send_discord)

FIXTURE = (Path(__file__).parent / "fixtures/example.html").read_text()
MATCHING_STORES = {"status": True, "gplus_data": [
    {"shop_title": "ガチャガチャの森　荻窪店"}, {"shop_title": "歌舞伎町タワー namco TOKYO"}]}
EMPTY_STORES = {"status": True, "gplus_data": []}


def page(text):
    return ('<section id="shopArea">' + text
            + '<a href="../shop/gplus_list.php?product_code=4582770069150">一覧で見る</a></section>')


class ParsingTests(unittest.TestCase):
    def test_timestamp_and_both_shops_with_spacing_and_fullwidth(self):
        snapshot = parse_page(FIXTURE.replace("09:30", "０９：３０"))
        self.assertEqual(snapshot.updated_at, "2026-11-02T09:30:00+09:00")
        self.assertEqual(snapshot.shops, ())  # HTML alone cannot establish shop presence.
        self.assertEqual(len(parse_stores(MATCHING_STORES)[0]), 2)

    def test_no_target_is_valid(self):
        self.assertEqual(parse_page(page("更新日：2026年11月2日 <p>別の店舗</p>")).shops, ())

    def test_script_and_template_do_not_match_shops(self):
        for tag in ["script", "style", "template", "noscript"]:
            with self.subTest(tag=tag):
                snapshot = parse_page(page(f"更新日時:2026/11/02 10:00 <{tag}>namcoTOKYO</{tag}>"))
                self.assertEqual(snapshot.shops, ())

    def test_missing_ambiguous_or_invalid_date_is_failure(self):
        for html in ["店舗名 namcoTOKYO", "更新日:2026/11/02 更新日:2026/11/03", "更新日:2026/02/30"]:
            with self.subTest(html=html), self.assertRaises(MonitorError):
                parse_page(page(html))

    def test_unrelated_shop_names_do_not_match(self):
        self.assertEqual(parse_stores({"status": True, "gplus_data": [
            {"shop_title": "ガチャガチャの森新宿店"}, {"shop_title": "namco池袋店"}]}), ((), 2))

    def test_timestamp_outside_shop_section_is_ignored(self):
        snapshot = parse_page("更新日:2025/01/01" + page("最終更新日時：2026年11月04日04時00分"))
        self.assertEqual(snapshot.updated_at, "2026-11-04T04:00:00+09:00")
        with self.assertRaises(MonitorError):
            parse_page("更新日:2026/11/04")

    def test_invalid_api_response_is_not_treated_as_no_shops(self):
        for data in [None, [], {}, {"status": False, "gplus_data": []},
                     {"status": True}, {"status": True, "gplus_data": [{}]},
                     {"status": True, "gplus_data": [{"shop_title": None}]}]:
            with self.subTest(data=data), self.assertRaises(MonitorError):
                parse_stores(data)

    def test_wrong_product_or_missing_list_link_is_rejected(self):
        self.assertEqual(product_code_from_page(FIXTURE, PRODUCT_URL), "4582770069150")
        for html in ["<p>no link</p>", FIXTURE.replace("4582770069150", "0000000000000")]:
            with self.assertRaises(MonitorError):
                product_code_from_page(html, PRODUCT_URL)

    def test_real_page_and_api_excerpts(self):
        root = Path(__file__).parent / "fixtures"
        html = (root / "gashapon_excerpt.html").read_text()
        data = json.loads((root / "gashapon_stores.json").read_text())
        self.assertEqual(parse_page(html).updated_at, "2026-11-04T04:00:00+09:00")
        self.assertEqual(product_code_from_page(html, PRODUCT_URL), "4582770069150")
        self.assertEqual(parse_stores(data), ((), 13))


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "state.json"
        self.notify = Mock()

    def check(self, html=FIXTURE, **kwargs):
        stores = kwargs.pop("stores", MATCHING_STORES)
        return check_once(self.state, "not-a-real-secret", fetch=lambda _: html,
                          notify=self.notify, fetch_shop_data=lambda _: stores, **kwargs)

    def test_first_match_notifies_and_identical_timestamp_deduplicates(self):
        self.assertEqual(self.check()["notification"], "sent")
        self.assertEqual(self.check()["notification"], "not_needed")
        self.assertEqual(self.notify.call_count, 1)

    def test_new_update_without_target_saves_then_matching_update_notifies(self):
        self.check(page("更新日時:2026/11/01 09:00"), stores=EMPTY_STORES)
        self.notify.assert_not_called()
        self.assertEqual(self.check()["notification"], "sent")

    def test_new_update_with_same_shops_notifies_again(self):
        self.check()
        self.check(FIXTURE.replace("09:30", "10:30"))
        self.assertEqual(self.notify.call_count, 2)

    def test_shops_change_without_timestamp_change_does_not_notify(self):
        self.check(stores=EMPTY_STORES)
        self.check()
        self.notify.assert_not_called()

    def test_previously_notified_timestamp_is_not_sent_on_rollback(self):
        self.check()
        self.check(FIXTURE.replace("09:30", "10:30"))
        self.check()
        self.assertEqual(self.notify.call_count, 2)

    def test_discord_failure_does_not_advance_state_and_is_retried(self):
        self.check(page("更新日時:2026/11/01 09:00"), stores=EMPTY_STORES)
        old = self.state.read_bytes()
        self.notify.side_effect = MonitorError("simulated failure")
        with self.assertRaises(MonitorError):
            self.check()
        self.assertEqual(self.state.read_bytes(), old)
        self.notify.side_effect = None
        self.assertEqual(self.check()["notification"], "sent")

    def test_fetch_and_parse_failure_preserve_state(self):
        self.check()
        old = self.state.read_bytes()
        with self.assertRaises(MonitorError):
            self.check("<h1>Maintenance</h1>")
        with self.assertRaises(MonitorError):
            check_once(self.state, "secret", fetch=Mock(side_effect=MonitorError("offline")))
        self.assertEqual(self.state.read_bytes(), old)

    def test_dry_run_does_not_notify_or_create_state(self):
        self.assertEqual(self.check(dry_run=True)["notification"], "would_send")
        self.assertFalse(self.state.exists())
        self.notify.assert_not_called()

    def test_corrupt_or_wrong_product_state_fails_closed(self):
        for contents in ["broken", "[]", '{"version": 2}', json.dumps({"version": 1, "url": "other", "notified": [], "updated_at": "x"})]:
            self.state.write_text(contents)
            with self.assertRaises(MonitorError):
                self.check()
        self.notify.assert_not_called()

    def test_api_failure_preserves_state(self):
        self.check()
        old = self.state.read_bytes()
        with self.assertRaises(MonitorError):
            self.check(stores={"status": False})
        self.assertEqual(self.state.read_bytes(), old)

    def test_page_changes_during_api_request_does_not_save_or_notify(self):
        with self.assertRaises(MonitorError):
            check_once(self.state, "secret", fetch=Mock(side_effect=[FIXTURE, FIXTURE.replace("09:30", "10:30")]),
                       fetch_shop_data=lambda _: MATCHING_STORES, notify=self.notify)
        self.assertFalse(self.state.exists())
        self.notify.assert_not_called()


class StoreRequestTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_request_uses_the_product_pages_search_parameters(self, urlopen):
        from urllib.parse import parse_qs
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = json.dumps(MATCHING_STORES).encode()
        self.assertEqual(fetch_stores("4582770069150"), MATCHING_STORES)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://gashapon.jp/shop/leaflet/getShopProducts.php")
        params = parse_qs(request.data.decode(), keep_blank_values=True)
        self.assertEqual(params["product_code"], ["4582770069150"])
        self.assertEqual(params["map_distance_flg"], ["false"])
        self.assertEqual(params["pref"], [""])
        self.assertEqual(params["free"], [""])


class DiscordTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_explicit_test_notification_does_not_claim_store_availability(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'{"id":"123"}'
        send_discord("https://discord.com/api/webhooks/123/test-token", Snapshot("", ()), PRODUCT_URL, test_only=True)
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertIn("テスト通知", payload["content"])
        self.assertNotIn("検知しました", payload["content"])
        self.assertEqual(payload["embeds"][0]["fields"], [])

    @patch("urllib.request.urlopen")
    def test_payload_disables_mentions_and_waits_for_acknowledgement(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'{"id":"123"}'
        send_discord("https://discord.com/api/webhooks/123/test-token", Snapshot("2026-11-02", ("対象店舗",)), PRODUCT_URL)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("?wait=true"))
        self.assertEqual(json.loads(request.data)["allowed_mentions"], {"parse": []})

    @patch("urllib.request.urlopen")
    def test_missing_acknowledgement_is_failure(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'{}'
        with self.assertRaises(MonitorError):
            send_discord("https://discord.com/api/webhooks/123/test-token", Snapshot("x", ("shop",)), PRODUCT_URL)

    @patch("urllib.request.urlopen")
    def test_non_discord_webhook_is_rejected(self, urlopen):
        for webhook in ["https://example.com/api/webhooks/123/secret",
                        "https://discord.com:invalid/api/webhooks/123/secret"]:
            with self.subTest(webhook=webhook), self.assertRaises(MonitorError) as raised:
                send_discord(webhook, Snapshot("x", ()), PRODUCT_URL)
            self.assertNotIn("secret", str(raised.exception))
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
