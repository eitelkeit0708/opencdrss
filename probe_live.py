import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import opencd_free_rss as app


def load_env(path: str) -> None:
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def is_login_page(html: str) -> bool:
    lower = html.lower()
    return "takelogin" in lower or "login.php" in lower or "\u767b\u5f55" in html


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="test.env")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--delay", type=float, default=None)
    parser.add_argument("--add", action="store_true", help="push matched free/2xfree torrents to Transmission")
    args = parser.parse_args()

    load_env(args.env)
    config = app.load_config()
    delay = config.request_delay if args.delay is None else args.delay
    items = app.parse_rss(app.fetch_text(config.rss_url))
    print(f"RSS items: {len(items)}")

    tr = app.Transmission(
        config.transmission_url,
        config.transmission_username,
        config.transmission_password,
        config.download_dir,
        config.paused,
    )

    found = 0
    for index, item in enumerate(items[: args.limit]):
        if index and delay > 0:
            time.sleep(delay)
        torrent_id = (parse_qs(urlparse(item.detail_url).query).get("id") or ["?"])[0]
        html = app.fetch_text(item.detail_url, config.site_cookie)
        promo = app.find_promotion(html)
        print(
            f"id={torrent_id} promo={promo or 'none'} login={is_login_page(html)} "
            f"bytes={len(html)} title={title(html)[:80]}",
            flush=True,
        )
        if promo in {"free", "2xfree"}:
            found += 1
            if args.add:
                print(f"  transmission={tr.add(item.torrent_url)}", flush=True)

    print(f"Promos found: {found}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
