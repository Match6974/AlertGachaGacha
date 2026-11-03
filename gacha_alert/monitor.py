from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

PRODUCT_URL = "https://gashapon.jp/products/detail.php?jan_code=4582770069150000"
STORES_URL = "https://gashapon.jp/shop/leaflet/getShopProducts.php"
TARGETS = {
    "ガチャガチャの森 荻窪店": ("ガチャガチャの森荻窪店",),
    "歌舞伎町タワー namco TOKYO": ("namcoTOKYO", "ナムコトーキョー"),
}
DATE = r"(?P<year>20\d{2})\s*[年/.-]\s*(?P<month>\d{1,2})\s*[月/.-]\s*(?P<day>\d{1,2})\s*日?(?:\s*(?:\([^)]*\))?\s*(?P<hour>\d{1,2})\s*[:：時]\s*(?P<minute>\d{1,2})(?:\s*[:：分]\s*(?P<second>\d{1,2})\s*秒?)?)?"
UPDATED = re.compile(r"(?:最終\s*更新(?:\s*日時|\s*日)?|更新\s*日時|更新\s*日|情報\s*更新|データ\s*更新)\s*[:：]?\s*" + DATE)


class MonitorError(Exception):
    """An actionable failure, safe to display without revealing credentials."""


class VisibleText(HTMLParser):
    def __init__(self, scope_id=None):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0
        self.scope_id = scope_id
        self.section_depth = 0
        self.links: list[str] = []

    @property
    def in_scope(self):
        return self.scope_id is None or self.section_depth > 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section" and (self.section_depth or attrs.get("id") == self.scope_id):
            self.section_depth += 1
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden += 1
        if self.in_scope and not self.hidden and tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if self.in_scope and not self.hidden and tag in {"br", "p", "div", "li", "tr", "section", "h1", "h2"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        if self.in_scope and not self.hidden:
            self.parts.append(" ")
        if tag == "section" and self.section_depth:
            self.section_depth -= 1

    def handle_data(self, data):
        if self.in_scope and not self.hidden:
            self.parts.append(data)


def normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def load_targets(raw: str | None) -> dict[str, tuple[str, ...]]:
    """An unset/empty Actions variable preserves the original two shops."""
    if raw is None or not raw.strip():
        return TARGETS
    try:
        names = json.loads(raw)
    except ValueError:
        raise MonitorError('TARGET_SHOPS は JSON 配列で指定してください（例: ["店舗A", "店舗B"]）。') from None
    if (not isinstance(names, list) or len(names) > 25
            or any(not isinstance(name, str) or not normalize(name) or len(name) > 120
                   or any(unicodedata.category(c).startswith("C") for c in name) for name in names)):
        raise MonitorError("TARGET_SHOPS は空白でない店舗名の配列にしてください（最大25件、各120文字）。[] は対象店舗なしです。")
    targets = {}
    seen = set()
    for name in names:
        key = normalize(name)
        if key not in seen:
            targets[name.strip()] = (name.strip(),)
            seen.add(key)
    return targets


def targets_key(targets: dict[str, tuple[str, ...]]) -> str:
    # Display order, case and whitespace must not cause duplicate notifications.
    patterns = sorted({normalize(alias) for aliases in targets.values() for alias in aliases})
    return hashlib.sha256(json.dumps(patterns, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    updated_at: str
    shops: tuple[str, ...]


def parse_page(html: str) -> Snapshot:
    # Only the machine/shop section carries the timestamp we monitor.
    parser = VisibleText("shopArea")
    parser.feed(html)
    text = unicodedata.normalize("NFKC", "".join(parser.parts))
    dates = set()
    for match in UPDATED.finditer(text):
        values = match.groupdict()
        try:
            date = datetime(*(int(values[k]) for k in ("year", "month", "day")),
                            int(values["hour"] or 0), int(values["minute"] or 0),
                            int(values["second"] or 0), tzinfo=ZoneInfo("Asia/Tokyo"))
        except ValueError as error:
            raise MonitorError("ページの更新日時が不正です。HTML の構造を確認してください。") from error
        dates.add(date.isoformat() if values["hour"] else date.date().isoformat())
    if not dates:
        raise MonitorError("HTML から更新日時を取得できません。JavaScript・別 API・iframe による表示、またはページ構造の変更を確認してください。")
    if len(dates) != 1:
        raise MonitorError("異なる更新日時が複数見つかりました。監視対象の日時を特定する必要があります。")
    # Shop names are loaded through a separate API, never from page text.
    return Snapshot(dates.pop(), ())


def product_code_from_page(html: str, url: str) -> str:
    parser = VisibleText("shopArea")
    parser.feed(html)
    codes = set()
    for href in parser.links:
        link = urllib.parse.urlsplit(urllib.parse.urljoin(url, href))
        if (link.scheme == "https" and link.hostname == "gashapon.jp"
                and link.path == "/shop/gplus_list.php"):
            codes.update(urllib.parse.parse_qs(link.query).get("product_code", []))
    jan_codes = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("jan_code", [])
    if (len(codes) != 1 or len(jan_codes) != 1
            or not re.fullmatch(r"\d{16}", jan_codes[0])):
        raise MonitorError("商品ページの店舗一覧リンクまたは商品コードを特定できません。")
    code = codes.pop()
    if not re.fullmatch(r"\d{13}", code) or not jan_codes[0].startswith(code):
        raise MonitorError("店舗一覧の商品コードが監視対象の商品と一致しません。")
    return code


def parse_stores(payload: object, targets: dict[str, tuple[str, ...]] | None = None) -> tuple[tuple[str, ...], int]:
    if targets is None:
        targets = TARGETS
    if not isinstance(payload, dict) or payload.get("status") is not True:
        raise MonitorError("店舗検索 API が成功を返しませんでした。次回再試行します。")
    rows = payload.get("gplus_data")
    if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("shop_title"), str)
            or not row["shop_title"].strip() for row in rows):
        raise MonitorError("店舗検索 API のデータ形式が変わりました。状態は更新しません。")
    names = [normalize(row["shop_title"]) for row in rows]
    # Compare individual titles; unrelated fields and adjacent stores cannot match.
    shops = tuple(name for name, aliases in targets.items()
                  if any(normalize(alias) in title for title in names for alias in aliases))
    return shops, len(rows)


def fetch_stores(product_code: str) -> object:
    if not re.fullmatch(r"\d{13}", product_code):
        raise MonitorError("店舗検索の商品コードが不正です。")
    # Same parameters as the product page without geolocation permission.
    data = urllib.parse.urlencode({
        "pref": "", "free": "", "product_code": product_code, "gplus_type": "gplus",
        "center_lat": "35.728856", "center_lng": "139.720078", "map_distance_flg": "false",
    }).encode()
    request = urllib.request.Request(STORES_URL, data=data, headers={
        "User-Agent": "AlertGachaGacha/1.0 (personal availability monitor)",
        "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(5_000_001)
            if len(data) > 5_000_000:
                raise MonitorError("店舗検索 API の応答がサイズ上限を超えました。")
            return json.loads(data)
    except urllib.error.HTTPError as error:
        raise MonitorError(f"店舗検索 API の取得に失敗しました (HTTP {error.code})。") from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise MonitorError("店舗検索 API を取得・解析できません。次回再試行します。") from None


def fetch_page(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "gashapon.jp" or parsed.username or parsed.password:
        raise MonitorError("監視 URL は https://gashapon.jp/ 配下を指定してください。")
    request = urllib.request.Request(url, headers={
        "User-Agent": "AlertGachaGacha/1.0 (personal availability monitor)",
        "Accept": "text/html", "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise MonitorError("監視ページが HTML を返しませんでした。")
            data = response.read(5_000_001)
            if len(data) > 5_000_000:
                raise MonitorError("監視ページのサイズが上限を超えました。")
            return data.decode(response.headers.get_content_charset() or "utf-8")
    except urllib.error.HTTPError as error:
        raise MonitorError(f"監視ページの取得に失敗しました (HTTP {error.code})。") from None
    except (urllib.error.URLError, TimeoutError, UnicodeError, LookupError):
        raise MonitorError("監視ページの取得に失敗しました。ネットワークと文字コードを確認してください。") from None


def send_discord(webhook: str, snapshot: Snapshot, url: str, *, test_only: bool = False) -> None:
    try:
        parsed = urllib.parse.urlsplit(webhook)
        valid = (parsed.scheme == "https" and parsed.hostname == "discord.com"
                 and parsed.port in {None, 443} and not parsed.username and not parsed.password
                 and re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[\w-]+", parsed.path)
                 and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise MonitorError("DISCORD_WEBHOOK_URL に discord.com の HTTPS Webhook URL を設定してください。")
    payload = {
        "content": "テスト通知です。店舗の掲載検知ではありません。" if test_only else "対象店舗の掲載を検知しました。",
        "allowed_mentions": {"parse": []},
        "embeds": [{"title": "通知設定のテスト" if test_only else "ガシャポン掲載情報の更新", "url": url,
                    "description": "iPhone の Discord 通知を確認してください。" if test_only else "\n".join(snapshot.shops),
                    "color": 5763719,
                    "fields": [] if test_only else [
                        {"name": "ページの更新日時", "value": snapshot.updated_at},
                        {"name": "注意", "value": "掲載情報の通知です。在庫・販売状況は店舗で確認してください。"}]}],
    }
    # wait=true returns an acknowledgement rather than merely accepting the request.
    request = urllib.request.Request(webhook + "?wait=true", method="POST",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "AlertGachaGacha/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise MonitorError("Discord の送信確認に失敗しました。次回再試行します。")
            acknowledgement = json.loads(response.read(100_000))
            if not isinstance(acknowledgement, dict) or not acknowledgement.get("id"):
                raise MonitorError("Discord の送信確認を取得できませんでした。次回再試行します。")
    except urllib.error.HTTPError as error:
        raise MonitorError(f"Discord 通知に失敗しました (HTTP {error.code})。Webhook とレート制限を確認してください。") from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise MonitorError("Discord 通知の送信確認に失敗しました。次回再試行します。") from None


def read_state(path: Path, url: str) -> dict:
    if not path.exists():
        return {"version": 1, "url": url, "updated_at": None, "notified": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or state.get("version") != 1 or state.get("url") != url
                or not isinstance(state.get("notified"), list)
                or not all(isinstance(v, str) for v in state["notified"])
                or not isinstance(state.get("updated_at"), str)):
            raise ValueError
        return state
    except (ValueError, OSError):
        raise MonitorError("保存状態が不正、または監視 URL が異なります。状態ファイルを確認してください。") from None


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = output.name
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def check_once(state_path: Path, webhook: str | None, *, url: str = PRODUCT_URL,
               dry_run: bool = False, fetch: Callable = fetch_page, notify: Callable = send_discord,
               fetch_shop_data: Callable = fetch_stores, targets: dict[str, tuple[str, ...]] | None = None) -> dict:
    if targets is None:
        targets = TARGETS
    key = targets_key(targets)
    default_key = targets_key(TARGETS)
    state = read_state(state_path, url)
    html = fetch(url)
    page = parse_page(html)
    product_code = product_code_from_page(html, url)
    shops, store_count = parse_stores(fetch_shop_data(product_code), targets)
    # Avoid saving a mixed snapshot if the page refreshes during the API request.
    after_html = fetch(url)
    if (parse_page(after_html).updated_at != page.updated_at
            or product_code_from_page(after_html, url) != product_code):
        raise MonitorError("取得中に商品ページが更新されました。状態を保持し、次回再試行します。")
    snapshot = Snapshot(page.updated_at, shops)
    identity = url + "\n" + snapshot.updated_at
    # Preserve existing notification IDs for the original configuration.
    if key != default_key:
        identity += "\n" + key
    event_id = hashlib.sha256(identity.encode()).hexdigest()
    changed = state["updated_at"] != snapshot.updated_at
    targets_changed = state.get("targets_key", default_key) != key
    should_notify = bool((changed or targets_changed) and snapshot.shops and event_id not in state["notified"])
    result = {"updated_at": snapshot.updated_at, "shops": list(snapshot.shops), "store_count": store_count,
              "changed": changed, "targets_changed": targets_changed,
              "target_shops": list(targets), "notification": "not_needed"}
    if dry_run:
        result["notification"] = "would_send" if should_notify else "not_needed"
        return result
    if not webhook:
        raise MonitorError("DISCORD_WEBHOOK_URL が未設定です。GitHub Actions の Repository secret に設定してください。")
    if should_notify:
        notify(webhook, snapshot, url)
        state["notified"].append(event_id)
        result["notification"] = "sent"
    state["updated_at"] = snapshot.updated_at
    state["shops"] = list(snapshot.shops)
    state["targets_key"] = key
    write_state(state_path, state)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="更新日時と対象店舗を確認して Discord に通知します。")
    parser.add_argument("--state", type=Path, default=Path(".local/state.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="通知・状態保存を行わず取得と判定だけを実行")
    mode.add_argument("--test-notification", action="store_true", help="Discord にテスト通知を1通送信（監視・状態保存はしない）")
    args = parser.parse_args()
    try:
        if args.test_notification:
            webhook = os.environ.get("DISCORD_WEBHOOK_URL")
            if not webhook:
                raise MonitorError("DISCORD_WEBHOOK_URL を GitHub Actions の Repository secret に設定してください。")
            send_discord(webhook, Snapshot("", ()), PRODUCT_URL, test_only=True)
            print("Discord のテスト通知を送信しました。iPhone で着信を確認してください。")
            return 0
        targets = load_targets(os.environ.get("TARGET_SHOPS"))
        result = check_once(args.state, os.environ.get("DISCORD_WEBHOOK_URL"), dry_run=args.dry_run, targets=targets)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (MonitorError, OSError) as error:
        print(f"監視エラー: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
