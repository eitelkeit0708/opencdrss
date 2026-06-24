import argparse
import base64
import hashlib
import json
import os
import subprocess
import re
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

TWOXFREE_RE = re.compile(
    r"2\s*x\s*free|2xfree|2x-free|2upfree|two\s*up\s*free|2\s*\u500d\s*\u514d\u8d39|2x\s*\u514d\u8d39",
    re.I,
)
FREE_RE = re.compile(
    r"freeleech|pro[_-]?free|/free\.(?:png|gif|webp)|\bfree\b|(?:\u4fc3\u9500|\u4f18\u60e0).{0,80}(?:\u514d\u8d39|free)",
    re.I | re.S,
)


@dataclass(frozen=True)
class RssItem:
    title: str
    torrent_url: str
    detail_url: str

    @property
    def key(self) -> str:
        parsed = urlparse(self.detail_url or self.torrent_url)
        torrent_id = parse_qs(parsed.query).get("id", [""])[0]
        return torrent_id or self.torrent_url


def text_of(node: ElementTree.Element, tag: str) -> str:
    found = node.find(tag)
    return (found.text or "").strip() if found is not None else ""


def parse_rss(xml_text: str) -> list[RssItem]:
    root = ElementTree.fromstring(xml_text)
    items = []
    for node in root.findall(".//item"):
        title = text_of(node, "title")
        link_url = text_of(node, "link")
        enclosure = node.find("enclosure")
        enclosure_url = enclosure.get("url", "").strip() if enclosure is not None else ""
        torrent_url = enclosure_url or link_url
        if not torrent_url:
            continue

        detail_url = first_detail_url(text_of(node, "comments"), link_url, text_of(node, "guid"), torrent_url)
        items.append(RssItem(title=title or torrent_url, torrent_url=torrent_url, detail_url=detail_url))
    return items


def first_detail_url(*urls: str) -> str:
    for url in urls:
        if "details.php" in url:
            return url
    return detail_url_from_download(urls[-1])


def detail_url_from_download(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path.endswith("/details.php"):
        return url
    query = parse_qs(parsed.query)
    torrent_id = query.get("id", [""])[0]
    if not torrent_id:
        return url
    path = parsed.path.rsplit("/", 1)[0] + "/details.php"
    return urlunparse(parsed._replace(path=path, query=urlencode({"id": torrent_id}), fragment=""))


def find_promotion(html: str) -> str | None:
    raw = unescape(html)
    text = re.sub(r"<[^>]+>", " ", raw)
    haystack = f"{raw}\n{text}"
    if TWOXFREE_RE.search(haystack):
        return "2xfree"
    if FREE_RE.search(haystack):
        return "free"
    return None


def transmission_add_body(torrent_url: str, download_dir: str | None = None, paused: bool = False) -> str:
    args: dict[str, object] = {"filename": torrent_url}
    if download_dir:
        args["download-dir"] = download_dir
    if paused:
        args["paused"] = True
    return json.dumps({"method": "torrent-add", "arguments": args})


def qbittorrent_add_body(torrent_url: str, download_dir: str = "", paused: bool = False, boundary: str = "") -> tuple[str, bytes]:
    boundary = boundary or f"opencd{int(time.time() * 1000000):x}"
    fields = [("urls", torrent_url)]
    if download_dir:
        fields.append(("savepath", download_dir))
    if paused:
        fields.append(("paused", "true"))
    chunks: list[str] = []
    for name, value in fields:
        chunks.append(f"--{boundary}\r\n")
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
        chunks.append(f"{value}\r\n")
    chunks.append(f"--{boundary}--\r\n")
    return f"multipart/form-data; boundary={boundary}", "".join(chunks).encode("utf-8")


class Transmission:
    def __init__(
        self,
        url: str,
        username: str = "",
        password: str = "",
        download_dir: str = "",
        paused: bool = False,
        unlimit_upload_tracker: str = "open.cd",
    ):
        self.url = url
        self.auth = basic_auth(username, password)
        self.download_dir = download_dir
        self.paused = paused
        self.session_id = ""
        self.unlimit_upload_tracker = unlimit_upload_tracker.lower()

    def add(self, torrent_url: str) -> str:
        payload = self._rpc_payload(transmission_add_body(torrent_url, self.download_dir or None, self.paused).encode())
        result = self._result(payload)
        self._unlimit_added_upload(payload)
        return result

    def unlimit_upload_by_tracker(self) -> int:
        if not self.unlimit_upload_tracker:
            return 0
        fields = ["id", "name", "uploadLimited", "honorsSessionLimits", "trackers", "trackerStats"]
        payload = self._rpc("torrent-get", {"fields": fields})
        ids = []
        for torrent in payload.get("arguments", {}).get("torrents", []):
            if not self._matches_tracker(torrent):
                continue
            if torrent.get("uploadLimited") or torrent.get("honorsSessionLimits") is not False:
                ids.append(torrent["id"])
        self._unlimit_upload_ids(ids)
        return len(ids)

    def _unlimit_added_upload(self, payload: dict[str, object]) -> None:
        args = payload.get("arguments", {})
        if not isinstance(args, dict):
            return
        torrent = args.get("torrent-added") or args.get("torrent-duplicate")
        if isinstance(torrent, dict) and "id" in torrent:
            self._unlimit_upload_ids([torrent["id"]])

    def _unlimit_upload_ids(self, ids: list[object]) -> None:
        if ids:
            self._rpc("torrent-set", {"ids": ids, "uploadLimited": False, "honorsSessionLimits": False})

    def _matches_tracker(self, torrent: dict[str, object]) -> bool:
        data = json.dumps([torrent.get("trackers"), torrent.get("trackerStats")], ensure_ascii=False).lower()
        return self.unlimit_upload_tracker in data

    def _rpc(self, method: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
        return self._rpc_payload(json.dumps({"method": method, "arguments": arguments or {}}).encode())

    def _rpc_payload(self, body: bytes) -> dict[str, object]:
        headers = {"Content-Type": "application/json"}
        if self.session_id:
            headers["X-Transmission-Session-Id"] = self.session_id
        if self.auth:
            headers["Authorization"] = self.auth
        request = Request(self.url, data=body, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code != 409:
                raise
            self.session_id = error.headers["X-Transmission-Session-Id"]
            return self._rpc_payload(body)

    def _result(self, payload: dict[str, object]) -> str:
        result = str(payload.get("result", ""))
        if result not in {"success", "duplicate torrent"}:
            raise RuntimeError(f"Transmission rejected torrent: {result}")
        return result


class Qbittorrent:
    def __init__(self, url: str, username: str = "", password: str = "", download_dir: str = "", paused: bool = False):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.download_dir = download_dir
        self.paused = paused
        self.sid_cookie = ""

    def add(self, torrent_url: str) -> str:
        if self.username and not self.sid_cookie:
            self.login()
        content_type, body = qbittorrent_add_body(torrent_url, self.download_dir, self.paused)
        headers = {"Content-Type": content_type, "Referer": self.url}
        if self.sid_cookie:
            headers["Cookie"] = self.sid_cookie
        request = Request(self.url + "/api/v2/torrents/add", data=body, headers=headers)
        with urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8", errors="replace").strip()
        if text and text.lower().startswith("fails"):
            raise RuntimeError(f"qBittorrent rejected torrent: {text}")
        return "success"

    def login(self) -> None:
        data = urlencode({"username": self.username, "password": self.password}).encode()
        request = Request(
            self.url + "/api/v2/auth/login",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": self.url},
        )
        with urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8", errors="replace").strip()
            cookie = response.headers.get("Set-Cookie", "")
        if text != "Ok.":
            raise RuntimeError(f"qBittorrent login failed: {text or 'empty response'}")
        self.sid_cookie = cookie.split(";", 1)[0]
        if not self.sid_cookie:
            raise RuntimeError("qBittorrent login did not return SID")


Downloader = Transmission | Qbittorrent


def basic_auth(username: str, password: str) -> str:
    if not username:
        return ""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


@dataclass(frozen=True)
class Config:
    rss_url: str
    transmission_url: str
    transmission_username: str
    transmission_password: str
    site_cookie: str
    state_file: Path
    interval: int
    download_dir: str
    paused: bool
    request_delay: float
    max_detail_checks: int = 3
    cookiecloud_url: str = ""
    cookiecloud_uuid: str = ""
    cookiecloud_password: str = ""
    cookiecloud_host: str = "open.cd"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    log_file: Path = Path("opencd_free_rss.log")
    log_max_bytes: int = 2 * 1024 * 1024
    user_agent: str = DEFAULT_USER_AGENT
    download_client: str = "transmission"
    qbittorrent_url: str = "http://127.0.0.1:8080"
    qbittorrent_username: str = ""
    qbittorrent_password: str = ""
    transmission_unlimit_upload_tracker: str = "open.cd"


def load_env_file(path: str) -> None:
    file = Path(path)
    if not file.exists():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_default_env_file() -> None:
    explicit = os.getenv("ENV_FILE")
    if explicit:
        load_env_file(explicit)
        return
    load_env_file("test.env" if Path("test.env").exists() else ".env")


def load_config() -> Config:
    rss_url = os.getenv("RSS_URL", "").strip()
    if not rss_url:
        raise SystemExit("RSS_URL is required")
    return Config(
        rss_url=rss_url,
        transmission_url=os.getenv("TRANSMISSION_URL", "http://127.0.0.1:9091/transmission/rpc"),
        transmission_username=os.getenv("TRANSMISSION_USERNAME", ""),
        transmission_password=os.getenv("TRANSMISSION_PASSWORD", ""),
        site_cookie=os.getenv("SITE_COOKIE", ""),
        state_file=Path(os.getenv("STATE_FILE", "seen.json")),
        interval=int(os.getenv("POLL_SECONDS", "600")),
        download_dir=os.getenv("DOWNLOAD_DIR", ""),
        paused=os.getenv("ADD_PAUSED", "").lower() in {"1", "true", "yes"},
        request_delay=float(os.getenv("REQUEST_DELAY_SECONDS", "10")),
        max_detail_checks=int(os.getenv("MAX_DETAIL_CHECKS", "3")),
        cookiecloud_url=os.getenv("COOKIECLOUD_URL", os.getenv("COOKIE_CLOUD_HOST", "")),
        cookiecloud_uuid=os.getenv("COOKIECLOUD_UUID", os.getenv("COOKIE_CLOUD_UUID", "")),
        cookiecloud_password=os.getenv("COOKIECLOUD_PASSWORD", os.getenv("COOKIE_CLOUD_PASSWORD", "")),
        cookiecloud_host=os.getenv("COOKIECLOUD_HOST", "open.cd"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        log_file=Path(os.getenv("LOG_FILE", "opencd_free_rss.log")),
        log_max_bytes=int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024))),
        user_agent=os.getenv("USER_AGENT", DEFAULT_USER_AGENT),
        download_client=os.getenv("DOWNLOAD_CLIENT", "transmission").lower(),
        qbittorrent_url=os.getenv("QBITTORRENT_URL", os.getenv("QBIT_URL", "http://127.0.0.1:8080")),
        qbittorrent_username=os.getenv("QBITTORRENT_USERNAME", os.getenv("QBIT_USERNAME", "")),
        qbittorrent_password=os.getenv("QBITTORRENT_PASSWORD", os.getenv("QBIT_PASSWORD", "")),
        transmission_unlimit_upload_tracker=os.getenv("TRANSMISSION_UNLIMIT_UPLOAD_TRACKER", "open.cd").lower(),
    )


def trim_log(path: Path, max_bytes: int) -> None:
    if max_bytes <= 0 or not path.exists() or path.stat().st_size <= max_bytes:
        return
    keep = max_bytes // 2
    with path.open("r+b") as file:
        file.seek(-keep, os.SEEK_END)
        tail = file.read()
        file.seek(0)
        file.write(tail)
        file.truncate()


def config_summary(config: Config) -> str:
    cookiecloud = "on" if config.cookiecloud_url and config.cookiecloud_uuid and config.cookiecloud_password else "off"
    telegram = "on" if config.telegram_bot_token and config.telegram_chat_id else "off"
    ua = "default" if config.user_agent == DEFAULT_USER_AGENT else "custom"
    return (
        f"started client={config.download_client} poll={config.interval}s delay={config.request_delay:g}s "
        f"max_checks={config.max_detail_checks} cookiecloud={cookiecloud} telegram={telegram} ua={ua}"
    )


def cookie_pair_count(cookie: str) -> int:
    return sum(1 for part in cookie.split(";") if "=" in part.strip())


def cookie_summary(cookie: str) -> str:
    status = "ok" if cookie else "missing"
    return f"cookie={status} pairs={cookie_pair_count(cookie)}"


def telegram_body(chat_id: str, text: str) -> str:
    return json.dumps({"chat_id": chat_id, "text": text})


def notify(config: Config, text: str) -> bool:
    if not config.telegram_bot_token or not config.telegram_chat_id:
        return False
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    body = telegram_body(config.telegram_chat_id, text).encode()
    request = Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=15):
            pass
        return True
    except Exception as error:
        print(f"notify failed: {error}", flush=True)
        return False


def is_login_page(html: str) -> bool:
    lower = html.lower()
    return "takelogin" in lower or "login.php" in lower or "\u767b\u5f55" in html


def is_cloudflare_challenge(html: str) -> bool:
    lower = html.lower()
    return any(marker in lower for marker in (
        "just a moment",
        "cf-chl",
        "cf_clearance",
        "challenge-platform",
        "checking your browser",
        "cloudflare",
    ))


def cookiecloud_key(uuid: str, password: str) -> str:
    return hashlib.md5(f"{uuid}-{password}".encode()).hexdigest()[:16]


def decrypt_cookiecloud(encrypted: str, uuid: str, password: str) -> dict[str, object]:
    env = os.environ.copy()
    env["COOKIECLOUD_OPENSSL_PASS"] = cookiecloud_key(uuid, password)
    proc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-d", "-base64", "-A", "-md", "md5", "-pass", "env:COOKIECLOUD_OPENSSL_PASS"],
        input=encrypted,
        text=True,
        capture_output=True,
        timeout=15,
        env=env,
    )
    if proc.returncode:
        raise RuntimeError((proc.stderr or "CookieCloud decrypt failed").strip())
    return json.loads(proc.stdout)


def cookiecloud_cookie_header(payload: dict[str, object], host: str) -> str:
    cookie_data = payload.get("cookie_data", payload)
    if not isinstance(cookie_data, dict):
        return ""
    wanted = host.lstrip(".").lower()
    cookies: list[str] = []
    for domain_key, items in cookie_data.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or domain_key).lstrip(".").lower()
            if domain != wanted and not domain.endswith("." + wanted):
                continue
            name = item.get("name")
            value = item.get("value")
            if name is not None and value is not None:
                cookies.append(f"{name}={value}")
    return "; ".join(cookies)


def cookiecloud_cookie(config: Config) -> str:
    if not (config.cookiecloud_url and config.cookiecloud_uuid and config.cookiecloud_password):
        return ""
    url = config.cookiecloud_url.rstrip("/") + "/get/" + quote(config.cookiecloud_uuid)
    with urlopen(Request(url, headers={"User-Agent": config.user_agent}), timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict) and "encrypted" in payload:
        payload = decrypt_cookiecloud(str(payload["encrypted"]), config.cookiecloud_uuid, config.cookiecloud_password)
    return cookiecloud_cookie_header(payload, config.cookiecloud_host) if isinstance(payload, dict) else ""


def fetch_text(url: str, cookie: str = "", user_agent: str = DEFAULT_USER_AGENT) -> str:
    headers = {"User-Agent": user_agent}
    if cookie:
        headers["Cookie"] = cookie
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def normalize_state_item(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {"checks": int(value.get("checks", 0)), "added": bool(value.get("added", False))}
    if isinstance(value, bool):
        return {"checks": 1, "added": value}
    if isinstance(value, int):
        return {"checks": value, "added": False}
    return {"checks": 1, "added": False}


def status_summary(state: dict[str, dict[str, object]], max_checks: int) -> str:
    total = len(state)
    added = 0
    pending = 0
    exhausted = 0
    buckets: dict[str, int] = {}
    limit = max(1, max_checks)
    for value in state.values():
        item = normalize_state_item(value)
        checks = int(item["checks"])
        if item["added"]:
            added += 1
        elif checks >= max_checks:
            exhausted += 1
        else:
            pending += 1
        label = f"{limit}+" if checks >= limit else str(checks)
        buckets[label] = buckets.get(label, 0) + 1
    order = [str(i) for i in range(limit)] + [f"{limit}+"]
    checks_text = ",".join(f"{key}:{buckets[key]}" for key in order if key in buckets) or "none"
    return f"total={total} added={added} pending={pending} exhausted={exhausted} checks={checks_text}"


def load_state(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {str(key): {"checks": 1, "added": False} for key in data}
    if isinstance(data, dict):
        return {str(key): normalize_state_item(value) for key, value in data.items()}
    return {}


def save_state(path: Path, state: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    ordered = {key: state[key] for key in sorted(state)}
    tmp.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def should_skip(state: dict[str, dict[str, object]], key: str, max_checks: int) -> bool:
    item = state.get(key, {})
    return bool(item.get("added", False)) or int(item.get("checks", 0)) >= max_checks


def mark_checked(state: dict[str, dict[str, object]], key: str, added: bool) -> None:
    item = normalize_state_item(state.get(key, {"checks": 0, "added": False}))
    item["checks"] = int(item["checks"]) + 1
    item["added"] = bool(item["added"]) or added
    state[key] = item


def best_cookie(config: Config) -> str:
    try:
        return cookiecloud_cookie(config) or config.site_cookie
    except Exception as error:
        print(f"CookieCloud failed: {error}", flush=True)
        notify(config, f"CookieCloud failed: {error}")
        return config.site_cookie


def build_downloader(config: Config) -> Downloader:
    if config.download_client in {"transmission", "tr"}:
        return Transmission(
            config.transmission_url,
            config.transmission_username,
            config.transmission_password,
            config.download_dir,
            config.paused,
            config.transmission_unlimit_upload_tracker,
        )
    if config.download_client in {"qbittorrent", "qbit", "qb"}:
        return Qbittorrent(
            config.qbittorrent_url,
            config.qbittorrent_username,
            config.qbittorrent_password,
            config.download_dir,
            config.paused,
        )
    raise SystemExit(f"Unknown DOWNLOAD_CLIENT: {config.download_client}")


def run_once(config: Config, transmission: Downloader) -> None:
    state = load_state(config.state_file)
    xml_text = fetch_text(config.rss_url, user_agent=config.user_agent)
    cookie = best_cookie(config)
    first_detail = True
    for item in parse_rss(xml_text):
        if should_skip(state, item.key, config.max_detail_checks):
            continue
        if not first_detail and config.request_delay > 0:
            time.sleep(config.request_delay)
        first_detail = False
        try:
            html = fetch_text(item.detail_url, cookie, config.user_agent)
            if is_cloudflare_challenge(html):
                print(f"cloudflare challenge {item.key}", flush=True)
                notify(config, "OpenCD Cloudflare challenge detected")
                break
            if is_login_page(html):
                refreshed = best_cookie(config)
                if refreshed and refreshed != cookie:
                    cookie = refreshed
                    html = fetch_text(item.detail_url, cookie, config.user_agent)
                if is_cloudflare_challenge(html):
                    print(f"cloudflare challenge {item.key}", flush=True)
                    notify(config, "OpenCD Cloudflare challenge detected")
                    break
                if is_login_page(html):
                    print("cookie expired", flush=True)
                    notify(config, "OpenCD cookie expired")
                    break
            promotion = find_promotion(html)
            if promotion in {"free", "2xfree"}:
                try:
                    result = transmission.add(item.torrent_url)
                except Exception as error:
                    notify(config, f"Torrent add failed for {item.key}: {error}")
                    raise
                print(f"added {promotion}: {item.title} ({result})", flush=True)
                mark_checked(state, item.key, added=True)
            else:
                print(f"skip: {item.title}", flush=True)
                mark_checked(state, item.key, added=False)
            save_state(config.state_file, state)
        except Exception as error:
            print(f"error {item.key}: {error}", flush=True)
    if isinstance(transmission, Transmission):
        changed = transmission.unlimit_upload_by_tracker()
        if changed:
            print(f"unlimited upload for {changed} Transmission torrents matching {transmission.unlimit_upload_tracker}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch OpenCD RSS and add free torrents to a download client")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="show seen-state summary and exit")
    group.add_argument("--cookie-test", action="store_true", help="check CookieCloud/static cookie availability and exit")
    group.add_argument("--notify-test", action="store_true", help="send a Telegram test notification and exit")
    group.add_argument("--once", action="store_true", help="run one RSS check and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    load_default_env_file()
    config = load_config()
    if args.status:
        exists = "yes" if config.state_file.exists() else "no"
        print(f"state={config.state_file} exists={exists} {status_summary(load_state(config.state_file), config.max_detail_checks)}", flush=True)
        return
    if args.cookie_test:
        print(cookie_summary(best_cookie(config)), flush=True)
        return
    if args.notify_test:
        sent = notify(config, "opencd-free-rss notify test")
        print("notify=sent" if sent else "notify=disabled_or_failed", flush=True)
        return
    if args.once:
        trim_log(config.log_file, config.log_max_bytes)
        print(config_summary(config), flush=True)
        run_once(config, build_downloader(config))
        return

    trim_log(config.log_file, config.log_max_bytes)
    print(config_summary(config), flush=True)
    notify(config, "opencd-free-rss started")
    transmission = build_downloader(config)
    while True:
        try:
            run_once(config, transmission)
        except Exception as error:
            print(f"error: {error}", flush=True)
        time.sleep(config.interval)


if __name__ == "__main__":
    main()
