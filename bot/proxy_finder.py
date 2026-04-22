import os
import random
import logging
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

TELEGRAM_TEST_HOST = "149.154.167.50"
TELEGRAM_TEST_PORT = 443

PROXY_SOURCES = [
    "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=socks5&timeout=5000&country=all&ssl=all&anonymity=all&simplified=true",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
]

PROXY_CACHE_FILE = ".proxy_cache"


def _fetch_proxies() -> list:
    proxies = []
    for url in PROXY_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
                for line in text.splitlines():
                    line = line.strip()
                    if ":" in line and not line.startswith("#"):
                        proxies.append(line)
        except Exception as e:
            logger.debug(f"Failed to fetch proxies from {url}: {e}")
    random.shuffle(proxies)
    return proxies


def _test_socks5_proxy(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        import socks
        s = socks.socksocket()
        s.set_proxy(socks.SOCKS5, host, port)
        s.settimeout(timeout)
        s.connect((TELEGRAM_TEST_HOST, TELEGRAM_TEST_PORT))
        s.close()
        return True
    except Exception:
        return False


def _load_cached_proxy() -> Optional[str]:
    if not os.path.exists(PROXY_CACHE_FILE):
        return None
    try:
        with open(PROXY_CACHE_FILE) as f:
            proxy = f.read().strip()
        if not proxy:
            return None
        parts = proxy.split(":")
        if len(parts) == 2 and _test_socks5_proxy(parts[0], int(parts[1]), timeout=5.0):
            return proxy
    except Exception:
        pass
    return None


def _save_cached_proxy(proxy: str):
    try:
        with open(PROXY_CACHE_FILE, "w") as f:
            f.write(proxy)
    except Exception:
        pass


def find_working_proxy() -> Optional[str]:
    if os.environ.get("PROXY_URL"):
        return None

    cached = _load_cached_proxy()
    if cached:
        print(f"🔌 Using cached proxy: {cached}")
        return cached

    print("🔍 Telegram may be blocked. Searching for a working proxy automatically...")

    proxies = _fetch_proxies()
    if not proxies:
        print("❌ Could not fetch proxy list. Will attempt direct connection.")
        return None

    print(f"🔍 Testing proxies ({len(proxies)} candidates)...")

    for i, proxy in enumerate(proxies[:150]):
        parts = proxy.strip().split(":")
        if len(parts) != 2:
            continue
        host, port_str = parts
        try:
            port = int(port_str)
        except ValueError:
            continue

        if _test_socks5_proxy(host, port, timeout=4.0):
            print(f"✅ Found working proxy after testing {i + 1} proxies: {host}:{port}")
            _save_cached_proxy(proxy)
            return proxy

    print("❌ No working proxy found. Will attempt direct connection.")
    return None
