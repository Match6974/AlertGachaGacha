import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gacha_alert.monitor import (MonitorError, PRODUCT_URL, Snapshot, check_once,
                                parse_page, parse_stores, product_code_from_page,
                                fetch_stores, send_discord, load_targets, targets_key, TARGETS, main)

FIXTURE = (Path(__file__).parent / "fixtures/example.html").read_text()
NO_LISTING = (Path(__file__).parent / "fixtures/gashapon_no_listing.html").read_text()
MATCHING_STORES = {"status": True, "gplus_data": [
    {"shop_title": "ガチャガチャの森　荻窪店"}, {"shop_title": "歌舞伎町タワー namco TOKYO"}]}
EMPTY_STORES = {"status": True, "gplus_data": []}


class TargetConfigurationTests(unittest.TestCase):
    def test_unset_variable_keeps_existing_shops_and_empty_array_disables(self):
        for value in [None, "", " \n"]:
            self.assertEqual(load_targets(value), TARGETS)
        self.assertEqual(load_targets("[]"), {})
        self.assertEqual(parse_stores(MATCHING_STORES, {}), ((), 2))

    def test_custom_list_replaces_defaults_and_matches_normalized_names(self):
        targets = load_targets('["カプセルラボ 土岐店", "ＮＡＭＣＯ ｔｏｋｙｏ"]')
        payload = {"status": True, "gplus_data": MATCHING_STORES["gplus_data"] + [
            {"shop_title": "カプセルラボ土岐店"}, {"shop_title": "カプセルラボ別店舗"}]}
        self.assertEqual(parse_stores(payload, targets), (("カプセルラボ 土岐店", "ＮＡＭＣＯ ｔｏｋｙｏ"), 4))

    def test_duplicate_and_reordered_patterns_share_identity(self):
        first = load_targets('["NAMCO TOKYO", "店舗A", "namcoＴＯＫＹＯ"]')
        second = load_targets('["店舗A", "namcoTOKYO"]')
        self.assertEqual(len(first), 2)
        self.assertEqual(targets_key(first), targets_key(second))

    def test_invalid_configuration_fails_without_echoing_its_value(self):
        for raw in ['not-json', '{}', 'null', '"店舗A"', '[null]', '[1]', '[" "]',
                    '["店\\n舗"]', json.dumps(["店"] * 26), json.dumps(["店" * 121])]:
            with self.subTest(raw=raw), self.assertRaises(MonitorError):
                load_targets(raw)

    @patch("gacha_alert.monitor.check_once")
    def test_cli_passes_environment_variable_to_monitor(self, check):
        check.return_value = {}
        with patch.dict("os.environ", {"TARGET_SHOPS": '["追加店舗"]'}), patch("sys.argv", ["monitor", "--dry-run"]):
            self.assertEqual(main(), 0)
        self.assertEqual(check.call_args.kwargs["targets"], {"追加店舗": ("追加店舗",)})

    @patch("gacha_alert.monitor.check_once")
    def test_cli_rejects_invalid_config_before_fetching(self, check):
        with patch.dict("os.environ", {"TARGET_SHOPS": '{invalid'}), patch("sys.argv", ["monitor", "--dry-run"]):
            self.assertEqual(main(), 1)
        check.assert_not_called()


def page(text):
    return ('<section id="shopArea">' + text
            + '<a href="../shop/gplus_list.php?product_code=4582770069150">一覧で見る</a></section>')


class ParsingTests(unittest.TestCase):
    def test_timestamp_and_both_shops_with_spacing_and_fullwidth(self):
        snapshot = parse_page(FIXTURE.replace("09:30", "０９：３０"))
        self.assertEqual(snapshot.updated_at, "2026-11-02T09:30:00+09:00")
        self.assertEqual(snapshot.shops, ())  # HTML本文だけで店舗の掲載を判定しない。
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

    def test_confirmed_product_without_shop_section_has_no_listing(self):
        self.assertEqual(parse_page(NO_LISTING), Snapshot(None, (), False))

    def test_missing_map_without_product_evidence_is_still_an_error(self):
        for html in ["", "<h1>メンテナンス中</h1>",
                     NO_LISTING.replace('data-shop="0"', 'data-shop="1"'),
                     NO_LISTING.replace('data-shop="0"', ''),
                     NO_LISTING.replace("4582770069150000", "0000000000000000"),
                     NO_LISTING.replace('pg-detail__data', 'unknown'),
                     NO_LISTING.replace('pg-heading', 'unknown'),
                     NO_LISTING.replace('property="og:url"', 'property="unknown"'),
                     NO_LISTING.split('</section>')[0],
                     NO_LISTING.replace('新フィギュアシリーズ！', '現在メンテナンス中です。')]:
            with self.subTest(html=html[:100]), self.assertRaises(MonitorError):
                parse_page(html)

    def test_conflicting_shop_flags_are_not_treated_as_no_listing(self):
        for html in [NO_LISTING.replace('</body>', '<section id="shopArea"></section></body>'),
                     NO_LISTING.replace('</body>', '<section class="pg-wrap" data-shop="1"></section></body>')]:
            with self.assertRaises(MonitorError):
                parse_page(html)


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

    def test_added_target_is_evaluated_even_without_timestamp_change(self):
        self.check(stores=EMPTY_STORES)
        targets = load_targets('["新しい店舗"]')
        stores = {"status": True, "gplus_data": [{"shop_title": "新しい店舗"}]}
        result = self.check(targets=targets, stores=stores)
        self.assertFalse(result["changed"])
        self.assertTrue(result["targets_changed"])
        self.assertEqual(result["notification"], "sent")
        self.assertEqual(self.check(targets=targets, stores=stores)["notification"], "not_needed")
        self.assertEqual(self.notify.call_count, 1)

    def test_config_change_dry_run_and_notification_failure_preserve_state(self):
        self.check(stores=EMPTY_STORES)
        old = self.state.read_bytes()
        targets = load_targets('["namcoTOKYO"]')
        self.assertEqual(self.check(targets=targets, dry_run=True)["notification"], "would_send")
        self.assertEqual(self.state.read_bytes(), old)
        self.notify.side_effect = MonitorError("offline")
        with self.assertRaises(MonitorError):
            self.check(targets=targets)
        self.assertEqual(self.state.read_bytes(), old)
        self.notify.side_effect = None
        self.assertEqual(self.check(targets=targets)["notification"], "sent")

    def test_empty_targets_never_notify_and_restoring_custom_list_deduplicates(self):
        targets = load_targets('["namcoTOKYO"]')
        self.check(targets=targets)
        self.assertEqual(self.check(targets={})["notification"], "not_needed")
        self.assertEqual(self.check(targets=targets)["notification"], "not_needed")
        self.assertEqual(self.notify.call_count, 1)

    def test_legacy_state_preserves_original_deduplication(self):
        self.check()
        legacy = json.loads(self.state.read_text())
        legacy.pop("targets_key")
        legacy.pop("listing_available")
        legacy.pop("listing_epoch")
        self.state.write_text(json.dumps(legacy))
        self.assertEqual(self.check()["notification"], "not_needed")
        self.assertEqual(self.notify.call_count, 1)

    def test_page_changes_during_api_request_does_not_save_or_notify(self):
        with self.assertRaises(MonitorError):
            check_once(self.state, "secret", fetch=Mock(side_effect=[FIXTURE, FIXTURE.replace("09:30", "10:30")]),
                       fetch_shop_data=lambda _: MATCHING_STORES, notify=self.notify)
        self.assertFalse(self.state.exists())
        self.notify.assert_not_called()

    def test_no_listing_is_successful_without_store_api_or_notification(self):
        api = Mock(side_effect=AssertionError("非掲載時にAPIを呼んではいけない"))
        result = check_once(self.state, "secret", fetch=lambda _: NO_LISTING,
                            fetch_shop_data=api, notify=self.notify)
        self.assertFalse(result["listing_available"])
        self.assertIsNone(result["updated_at"])
        self.assertEqual(result["store_count"], 0)
        self.assertEqual(result["notification"], "not_needed")
        api.assert_not_called()
        self.notify.assert_not_called()
        self.assertEqual(self.check(NO_LISTING)["notification"], "not_needed")

    def test_disappearance_keeps_history_and_reappearance_notifies_once(self):
        self.check()
        old_history = json.loads(self.state.read_text())["notified"]
        self.check(NO_LISTING)
        absent = self.state.read_bytes()
        self.assertEqual(json.loads(absent)["notified"], old_history)
        self.check(NO_LISTING)
        self.assertEqual(self.state.read_bytes(), absent)
        # 更新日時が前回と同じでも、掲載なしを挟んだ復帰時は再判定する。
        self.assertEqual(self.check()["notification"], "sent")
        self.assertEqual(self.check()["notification"], "not_needed")
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(json.loads(self.state.read_text())["listing_epoch"], 1)

    def test_initial_absence_then_listing_and_multiple_restock_cycles(self):
        self.check(NO_LISTING)
        self.assertEqual(self.check()["notification"], "sent")
        self.check(NO_LISTING)
        self.assertEqual(self.check()["notification"], "sent")
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(json.loads(self.state.read_text())["listing_epoch"], 2)

    def test_reappearance_without_target_does_not_notify(self):
        self.check(NO_LISTING)
        result = self.check(stores=EMPTY_STORES)
        self.assertTrue(result["listing_available"])
        self.assertEqual(result["notification"], "not_needed")
        self.notify.assert_not_called()

    def test_reappearance_notification_failure_keeps_absent_state_for_retry(self):
        self.check(NO_LISTING)
        old = self.state.read_bytes()
        self.notify.side_effect = MonitorError("模擬的な送信失敗")
        with self.assertRaises(MonitorError):
            self.check()
        self.assertEqual(self.state.read_bytes(), old)
        self.notify.side_effect = None
        self.assertEqual(self.check()["notification"], "sent")
        self.assertEqual(self.check()["notification"], "not_needed")

    def test_no_listing_dry_run_does_not_create_or_modify_state(self):
        self.check(NO_LISTING, dry_run=True)
        self.assertFalse(self.state.exists())
        self.check()
        old = self.state.read_bytes()
        self.check(NO_LISTING, dry_run=True)
        self.assertEqual(self.state.read_bytes(), old)

    def test_listing_transition_during_fetch_preserves_state(self):
        for before, after in [(FIXTURE, NO_LISTING), (NO_LISTING, FIXTURE)]:
            self.check(NO_LISTING)
            old = self.state.read_bytes()
            with self.assertRaises(MonitorError):
                check_once(self.state, "secret", fetch=Mock(side_effect=[before, after]),
                           fetch_shop_data=lambda _: MATCHING_STORES, notify=self.notify)
            self.assertEqual(self.state.read_bytes(), old)
        self.notify.assert_not_called()

    def test_parse_failure_during_absence_keeps_state_and_future_recovery(self):
        self.check(NO_LISTING)
        old = self.state.read_bytes()
        with self.assertRaises(MonitorError):
            self.check("<h1>メンテナンス中</h1>")
        self.assertEqual(self.state.read_bytes(), old)
        self.assertEqual(self.check()["notification"], "sent")

    def test_invalid_listing_state_is_rejected(self):
        self.check(NO_LISTING)
        state = json.loads(self.state.read_text())
        for change in [{"listing_available": "false"}, {"listing_epoch": -1},
                       {"listing_epoch": True}, {"listing_available": True}]:
            self.state.write_text(json.dumps(state | change))
            with self.assertRaises(MonitorError):
                self.check()


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
