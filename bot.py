import asyncio
import json
import logging
import os
import random
import threading
import time
from itertools import cycle
from pathlib import Path
from typing import Any, Optional

import discord
from discord.ext import commands
from flask import Flask, jsonify
import cloudscraper
from curl_cffi import requests as curl_requests

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

try:
    import undetected_chromedriver as uc
    UC_AVAILABLE = True
except ImportError:
    UC_AVAILABLE = False

try:
    from capsolver import Capsolver
    CAPSOLVER_AVAILABLE = True
except ImportError:
    CAPSOLVER_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("vinted-bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN fehlt in den Render Environment Variables."
    )

CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "").strip()
TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY", "").strip()
CAPMONSTER_API_KEY = os.getenv("CAPMONSTER_API_KEY", "").strip()
ANTICAPTCHA_API_KEY = os.getenv("ANTICAPTCHA_API_KEY", "").strip()
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "").strip()
PROXY_LIST_RAW = os.getenv("PROXY_LIST", "").strip()

PROXIES: list = []
if PROXY_LIST_RAW:
    for entry in PROXY_LIST_RAW.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if not entry.startswith("http"):
            entry = "http://" + entry
        PROXIES.append(entry)

proxy_pool = cycle(PROXIES) if PROXIES else None

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
SEEN_FILE = DATA_DIR / "seen.json"

DEFAULT_CONFIG = {
    "brands": [
        "Nike",
        "Ralph Lauren",
        "Adidas",
        "Tommy Hilfiger",
        "Lacoste",
        "Carhartt",
    ],
    "channel_id": None,
    "interval": 300,
    "max_price": None,
    "results_per_brand": 20,
}

file_lock = threading.Lock()


def load_json(path: Path, default: Any) -> Any:
    with file_lock:
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.exception("Konnte %s nicht lesen.", path)
            return default


def save_json(path: Path, data: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with file_lock:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp.replace(path)


config = load_json(CONFIG_FILE, DEFAULT_CONFIG.copy())
seen_ids = set(str(x) for x in load_json(SEEN_FILE, []))

for key, value in DEFAULT_CONFIG.items():
    if key not in config:
        config[key] = value

if not isinstance(config.get("brands"), list):
    config["brands"] = DEFAULT_CONFIG["brands"].copy()

config["brands"] = [str(x).strip() for x in config["brands"] if str(x).strip()]

try:
    config["interval"] = max(60, int(config.get("interval", 300)))
except (TypeError, ValueError):
    config["interval"] = 300

if config.get("max_price") is not None:
    try:
        config["max_price"] = float(config["max_price"])
    except (TypeError, ValueError):
        config["max_price"] = None

save_json(CONFIG_FILE, config)
save_json(SEEN_FILE, sorted(seen_ids))


VINTED_DOMAIN = "de"
VINTED_BASE_URL = f"https://www.vinted.{VINTED_DOMAIN}"
VINTED_403_COOLDOWN = 5 * 60
VINTED_429_COOLDOWN = 15 * 60
VINTED_REQUEST_TIMEOUT = 30
VINTED_MAX_RETRIES_PER_REQUEST = 3

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) "
    "Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

IMPERSONATIONS = [
    "chrome124",
    "chrome123",
    "chrome120",
    "chrome119",
    "chrome116",
    "safari17_0",
    "edge101",
]

ACCEPT_LANGUAGES = [
    "de-DE,de;q=0.9,en;q=0.8",
    "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "de,en-US;q=0.9,en;q=0.8",
]


def pick_proxy() -> Optional[dict]:
    if proxy_pool is None:
        return None
    proxy = next(proxy_pool)
    return {"http": proxy, "https": proxy}


def build_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "DNT": "1",
        "Connection": "keep-alive",
    }


class VintedBlockedError(Exception):
    pass


class CloudscraperLayer:
    def __init__(self) -> None:
        self.scraper = None
        self._build()

    def _build(self) -> None:
        self.scraper = cloudscraper.create_scraper(
            browser={
                "browser": "chrome",
                "platform": "windows",
                "mobile": False,
                "desktop": True,
            },
            interpreter="native",
            delay=random.uniform(3, 7),
            enable_stealth=True,
        )
        self.scraper.headers.update(build_headers())

    def get(self, url: str, params: Optional[dict] = None) -> Any:
        proxy = pick_proxy()
        for attempt in range(1, VINTED_MAX_RETRIES_PER_REQUEST + 1):
            try:
                resp = self.scraper.get(
                    url,
                    params=params,
                    timeout=VINTED_REQUEST_TIMEOUT,
                    proxies=proxy,
                )
                if resp.status_code in (403, 429):
                    return resp
                if resp.status_code >= 500:
                    time.sleep(random.uniform(3, 7))
                    self._build()
                    proxy = pick_proxy()
                    continue
                return resp
            except Exception:
                time.sleep(random.uniform(2, 6))
                self._build()
                proxy = pick_proxy()
        raise RuntimeError("cloudscraper-Layer erschöpft.")


class CurlCffiLayer:
    def __init__(self) -> None:
        self.session = None
        self._build()

    def _build(self) -> None:
        self.session = curl_requests.Session(
            impersonate=random.choice(IMPERSONATIONS),
            timeout=VINTED_REQUEST_TIMEOUT,
        )
        self.session.headers.update(build_headers())

    def get(self, url: str, params: Optional[dict] = None) -> Any:
        proxy = pick_proxy()
        for attempt in range(1, VINTED_MAX_RETRIES_PER_REQUEST + 1):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    proxies=proxy,
                )
                if resp.status_code in (403, 429):
                    return resp
                if resp.status_code >= 500:
                    time.sleep(random.uniform(3, 7))
                    self._build()
                    proxy = pick_proxy()
                    continue
                return resp
            except Exception:
                time.sleep(random.uniform(2, 6))
                self._build()
                proxy = pick_proxy()
        raise RuntimeError("curl_cffi-Layer erschöpft.")


class FlareSolverrLayer:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def get(self, url: str) -> Optional[str]:
        if not self.endpoint:
            return None
        import requests as r

        proxy = pick_proxy()
        proxy_url = None
        if proxy:
            proxy_url = proxy.get("https") or proxy.get("http")

        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": 90000,
        }
        if proxy_url:
            payload["proxy"] = {"url": proxy_url}

        try:
            resp = r.post(
                self.endpoint, json=payload, timeout=120
            ).json()
            if resp.get("status") == "ok":
                return resp["solution"]["response"]
            return None
        except Exception:
            return None


class BrowserLayer:
    def __init__(self) -> None:
        self.driver = None
        self._build()

    def _build(self) -> None:
        if not UC_AVAILABLE and not SELENIUM_AVAILABLE:
            self.driver = None
            return

        proxy = pick_proxy()
        proxy_str = None
        if proxy:
            proxy_str = proxy.get("https") or proxy.get("http")

        try:
            if UC_AVAILABLE:
                options = uc.ChromeOptions()
            else:
                options = ChromeOptions()

            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument(
                "--disable-blink-features=AutomationControlled"
            )
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--lang=de-DE")
            options.add_argument(
                f"--user-agent={random.choice(USER_AGENTS)}"
            )
            if proxy_str:
                options.add_argument(f"--proxy-server={proxy_str}")

            if UC_AVAILABLE:
                self.driver = uc.Chrome(
                    options=options,
                    headless=True,
                    use_subprocess=True,
                )
            else:
                self.driver = webdriver.Chrome(options=options)

            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """
                        Object.defineProperty(navigator, 'webdriver',
                            {get: () => undefined});
                        Object.defineProperty(navigator, 'plugins',
                            {get: () => [1,2,3,4,5]});
                        Object.defineProperty(navigator, 'languages',
                            {get: () => ['de-DE','de','en']});
                        window.chrome = { runtime: {} };
                        const originalQuery =
                            window.navigator.permissions.query;
                        window.navigator.permissions.query = (p) =>
                            p.name === 'notifications'
                                ? Promise.resolve(
                                    {state: Notification.permission})
                                : originalQuery(p);
                    """
                },
            )
        except Exception:
            self.driver = None

    def fetch(self, url: str, wait_seconds: int = 20) -> Optional[str]:
        if self.driver is None:
            return None
        try:
            self.driver.get(url)
            WebDriverWait(self.driver, wait_seconds).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            if (
                "Just a moment" in self.driver.title
                or "Checking your browser" in self.driver.page_source
                or "cf-challenge" in self.driver.page_source
            ):
                logger.info(
                    "Cloudflare-Challenge erkannt, warte auf Lösung..."
                )
                for _ in range(45):
                    time.sleep(1)
                    if "Just a moment" not in self.driver.title:
                        break

            self._solve_captcha_if_present()
            return self.driver.page_source
        except Exception:
            self._rebuild()
            return None

    def _rebuild(self) -> None:
        try:
            if self.driver is not None:
                self.driver.quit()
        except Exception:
            pass
        self.driver = None
        self._build()

    def _solve_captcha_if_present(self) -> None:
        if self.driver is None:
            return
        page = self.driver.page_source

        if "g-recaptcha" in page or "recaptcha/api.js" in page:
            sitekey = self._extract_sitekey("data-sitekey")
            if sitekey:
                token = self._solve_recaptcha(
                    sitekey, self.driver.current_url
                )
                if token:
                    self._inject_token(token, "g-recaptcha-response")
        elif "h-captcha" in page:
            sitekey = self._extract_sitekey("data-sitekey")
            if sitekey:
                token = self._solve_hcaptcha(
                    sitekey, self.driver.current_url
                )
                if token:
                    self._inject_token(token, "h-captcha-response")
        elif "cf-turnstile" in page:
            sitekey = self._extract_sitekey("data-sitekey")
            if sitekey:
                token = self._solve_turnstile(
                    sitekey, self.driver.current_url
                )
                if token:
                    self._inject_token(
                        token, "cf-turnstile-response"
                    )

    def _extract_sitekey(self, attr: str) -> Optional[str]:
        try:
            elem = self.driver.find_element(
                By.CSS_SELECTOR, f"[{attr}]"
            )
            return elem.get_attribute(attr)
        except Exception:
            return None

    def _solve_recaptcha(
        self, sitekey: str, url: str
    ) -> Optional[str]:
        if CAPSOLVER_AVAILABLE and CAPSOLVER_API_KEY:
            try:
                client = Capsolver(api_key=CAPSOLVER_API_KEY)
                return client.solve_recaptcha_v2(
                    sitekey=sitekey, url=url
                )
            except Exception:
                pass
        if TWOCAPTCHA_API_KEY:
            return self._solve_2captcha(
                "userrecaptcha", sitekey, url
            )
        if CAPMONSTER_API_KEY:
            return self._solve_capmonster(
                "RecaptchaV2TaskProxyless", sitekey, url
            )
        if ANTICAPTCHA_API_KEY:
            return self._solve_anticaptcha(
                "RecaptchaV2TaskProxyless", sitekey, url
            )
        return None

    def _solve_hcaptcha(
        self, sitekey: str, url: str
    ) -> Optional[str]:
        if CAPSOLVER_AVAILABLE and CAPSOLVER_API_KEY:
            try:
                client = Capsolver(api_key=CAPSOLVER_API_KEY)
                return client.solve_hcaptcha(
                    sitekey=sitekey, url=url
                )
            except Exception:
                pass
        if TWOCAPTCHA_API_KEY:
            return self._solve_2captcha("hcaptcha", sitekey, url)
        if CAPMONSTER_API_KEY:
            return self._solve_capmonster(
                "HCaptchaTaskProxyless", sitekey, url
            )
        if ANTICAPTCHA_API_KEY:
            return self._solve_anticaptcha(
                "HCaptchaTaskProxyless", sitekey, url
            )
        return None

    def _solve_turnstile(
        self, sitekey: str, url: str
    ) -> Optional[str]:
        if CAPSOLVER_AVAILABLE and CAPSOLVER_API_KEY:
            try:
                client = Capsolver(api_key=CAPSOLVER_API_KEY)
                return client.solve_turnstile(
                    sitekey=sitekey, url=url
                )
            except Exception:
                pass
        return None

    def _solve_2captcha(
        self, method: str, sitekey: str, url: str
    ) -> Optional[str]:
        import requests as r

        payload = {
            "key": TWOCAPTCHA_API_KEY,
            "method": method,
            "googlekey" if method == "userrecaptcha"
            else "sitekey": sitekey,
            "pageurl": url,
            "json": 1,
        }
        try:
            resp = r.post(
                "http://2captcha.com/in.php",
                data=payload,
                timeout=30,
            ).json()
            if resp.get("status") != 1:
                return None
            captcha_id = resp["request"]
            for _ in range(60):
                time.sleep(5)
                result = r.get(
                    f"http://2captcha.com/res.php"
                    f"?key={TWOCAPTCHA_API_KEY}"
                    f"&action=get&id={captcha_id}&json=1",
                    timeout=30,
                ).json()
                if result.get("status") == 1:
                    return result["request"]
            return None
        except Exception:
            return None

    def _solve_capmonster(
        self, task_type: str, sitekey: str, url: str
    ) -> Optional[str]:
        import requests as r

        try:
            create = r.post(
                "https://api.capmonster.cloud/createTask",
                json={
                    "clientKey": CAPMONSTER_API_KEY,
                    "task": {
                        "type": task_type,
                        "websiteURL": url,
                        "websiteKey": sitekey,
                    },
                },
                timeout=30,
            ).json()
            task_id = create.get("taskId")
            if not task_id:
                return None
            for _ in range(60):
                time.sleep(5)
                result = r.post(
                    "https://api.capmonster.cloud/getTaskResult",
                    json={
                        "clientKey": CAPMONSTER_API_KEY,
                        "taskId": task_id,
                    },
                    timeout=30,
                ).json()
                if result.get("status") == "ready":
                    sol = result["solution"]
                    return sol.get("gRecaptchaResponse") or sol.get(
                        "token"
                    )
            return None
        except Exception:
            return None

    def _solve_anticaptcha(
        self, task_type: str, sitekey: str, url: str
    ) -> Optional[str]:
        import requests as r

        try:
            create = r.post(
                "https://api.anti-captcha.com/createTask",
                json={
                    "clientKey": ANTICAPTCHA_API_KEY,
                    "task": {
                        "type": task_type,
                        "websiteURL": url,
                        "websiteKey": sitekey,
                    },
                },
                timeout=30,
            ).json()
            task_id = create.get("taskId")
            if not task_id:
                return None
            for _ in range(60):
                time.sleep(5)
                result = r.post(
                    "https://api.anti-captcha.com/getTaskResult",
                    json={
                        "clientKey": ANTICAPTCHA_API_KEY,
                        "taskId": task_id,
                    },
                    timeout=30,
                ).json()
                if result.get("status") == "ready":
                    sol = result["solution"]
                    return sol.get("gRecaptchaResponse") or sol.get(
                        "token"
                    )
            return None
        except Exception:
            return None

    def _inject_token(self, token: str, field_name: str) -> None:
        try:
            self.driver.execute_script(
                """
                const token = arguments[0];
                const fieldName = arguments[1];
                let el = document.getElementById(fieldName)
                    || document.querySelector(`[name="${fieldName}"]`);
                if (!el) {
                    el = document.createElement('textarea');
                    el.id = fieldName;
                    el.name = fieldName;
                    el.style.display = 'none';
                    document.body.appendChild(el);
                }
                el.value = token;
                el.innerHTML = token;
                if (typeof ___grecaptcha_cfg !== 'undefined') {
                    Object.values(___grecaptcha_cfg.clients).forEach(c => {
                        if (c && c.callback) {
                            try { c.callback(token); } catch(e){}
                        }
                    });
                }
                if (typeof grecaptcha !== 'undefined'
                    && grecaptcha.getResponse) {
                    try {
                        grecaptcha.getResponse = () => token;
                    } catch(e){}
                }
                document.dispatchEvent(new Event('change'));
                """,
                token,
                field_name,
            )
        except Exception:
            pass

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


class VintedSession:
    def __init__(self) -> None:
        self.cloudscraper_layer = CloudscraperLayer()
        self.curl_layer = CurlCffiLayer()
        self.flare_layer = FlareSolverrLayer(FLARESOLVERR_URL)
        self.browser_layer: Optional[BrowserLayer] = None

    def _ensure_browser(self) -> BrowserLayer:
        if self.browser_layer is None:
            self.browser_layer = BrowserLayer()
        return self.browser_layer

    def warmup(self) -> None:
        try:
            self.cloudscraper_layer.get(f"{VINTED_BASE_URL}/")
        except Exception:
            pass
        time.sleep(random.uniform(1.5, 4.0))

    def search(
        self,
        query: str,
        order: str = "newest_first",
        per_page: int = 20,
        price_to: Optional[float] = None,
    ) -> dict:
        params = {
            "search_text": query,
            "order": order,
            "per_page": int(per_page),
        }
        if price_to is not None:
            params["price_to"] = price_to

        url = f"{VINTED_BASE_URL}/api/v2/catalog/items"

        try:
            resp = self.cloudscraper_layer.get(url, params=params)
            data = self._handle_response(resp)
            if data is not None:
                return data
        except Exception:
            pass

        try:
            resp = self.curl_layer.get(url, params=params)
            data = self._handle_response(resp)
            if data is not None:
                return data
        except Exception:
            pass

        if FLARESOLVERR_URL:
            query_string = "&".join(
                f"{k}={v}" for k, v in params.items()
            )
            full_url = f"{url}?{query_string}"
            html = self.flare_layer.get(full_url)
            if html:
                data = self._parse_json(html)
                if data is not None:
                    return data

        browser = self._ensure_browser()
        query_string = "&".join(
            f"{k}={v}" for k, v in params.items()
        )
        full_url = f"{url}?{query_string}"
        html = browser.fetch(full_url)
        if html:
            data = self._parse_json(html)
            if data is not None:
                return data

        err = RuntimeError(
            "Alle Umgehungsschichten fehlgeschlagen (Vinted blockiert)."
        )
        raise err

    def _handle_response(self, resp: Any) -> Optional[dict]:
        if resp is None:
            return None
        if resp.status_code in (403, 429):
            err = RuntimeError(f"Vinted HTTP {resp.status_code}")
            setattr(err, "response", resp)
            raise err
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                return self._parse_json(resp.text)
        err = RuntimeError(f"Vinted HTTP {resp.status_code}")
        setattr(err, "response", resp)
        raise err

    def _parse_json(self, text: str) -> Optional[dict]:
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start == -1 or end <= 0:
                return None
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                return None


vinted: Optional[VintedSession] = None
vinted_blocked_until = 0.0
vinted_block_logged = False
vinted_lock = threading.Lock()


def exception_status_code(exc: BaseException) -> Optional[int]:
    response = getattr(exc, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if status_code is not None:
            try:
                return int(status_code)
            except (TypeError, ValueError):
                pass
    message = str(exc)
    if "403" in message:
        return 403
    if "429" in message:
        return 429
    return None


def create_vinted_session() -> VintedSession:
    logger.info("Erstelle neue Vinted-Session (mehrschichtige Umgehung)...")
    session = VintedSession()
    session.warmup()
    logger.info("Neue Vinted-Session bereit.")
    return session


def reset_vinted_session() -> None:
    global vinted
    with vinted_lock:
        if vinted is not None:
            try:
                if vinted.browser_layer is not None:
                    vinted.browser_layer.close()
            except Exception:
                pass
        vinted = None
    logger.info("Vinted-Session wurde verworfen.")


def mark_vinted_403() -> None:
    global vinted_blocked_until
    global vinted_block_logged
    vinted_blocked_until = time.time() + VINTED_403_COOLDOWN
    if not vinted_block_logged:
        logger.warning(
            "Vinted HTTP 403. Session verworfen. "
            "Nächster Versuch in %d Minuten.",
            VINTED_403_COOLDOWN // 60,
        )
        vinted_block_logged = True


def mark_vinted_429() -> None:
    global vinted_blocked_until
    global vinted_block_logged
    vinted_blocked_until = time.time() + VINTED_429_COOLDOWN
    if not vinted_block_logged:
        logger.warning(
            "Vinted HTTP 429. Nächster Versuch in %d Minuten.",
            VINTED_429_COOLDOWN // 60,
        )
        vinted_block_logged = True


def clear_vinted_block() -> None:
    global vinted_blocked_until
    global vinted_block_logged
    vinted_blocked_until = 0.0
    vinted_block_logged = False


def vinted_is_blocked() -> bool:
    return time.time() < vinted_blocked_until


def vinted_block_remaining() -> int:
    return max(0, int(vinted_blocked_until - time.time()))


def vinted_search_sync(brand: str) -> dict:
    global vinted

    if vinted_is_blocked():
        raise VintedBlockedError(
            f"Vinted pausiert noch {vinted_block_remaining()}s."
        )

    with vinted_lock:
        if vinted is None:
            try:
                vinted = create_vinted_session()
            except Exception as exc:
                status = exception_status_code(exc)
                if status == 403:
                    mark_vinted_403()
                    raise VintedBlockedError(
                        "Vinted verweigert die neue Session "
                        "mit HTTP 403."
                    ) from exc
                raise
        session = vinted

    try:
        result = session.search(
            query=brand,
            order="newest_first",
            per_page=int(config.get("results_per_brand", 20)),
            price_to=config.get("max_price"),
        )
        clear_vinted_block()
        return result
    except Exception as exc:
        status = exception_status_code(exc)
        if status == 403:
            reset_vinted_session()
            mark_vinted_403()
            raise VintedBlockedError("Vinted HTTP 403.") from exc
        if status == 429:
            reset_vinted_session()
            mark_vinted_429()
            raise VintedBlockedError("Vinted HTTP 429.") from exc
        raise


async def search_brand(brand: str) -> list:
    result = await asyncio.to_thread(vinted_search_sync, brand)
    return get_items(result)


def get_value(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def get_items(result: Any) -> list:
    if result is None:
        return []
    direct = get_value(result, "items", default=None)
    if isinstance(direct, list):
        return direct
    dtos = get_value(result, "dtos", default=None)
    if dtos is not None:
        items = get_value(dtos, "items", default=None)
        if isinstance(items, list):
            return items
    return []


def item_id(item: Any) -> Optional[str]:
    value = get_value(item, "id", default=None)
    if value is None:
        return None
    return str(value)


def item_title(item: Any) -> str:
    return str(
        get_value(item, "title", "name", default="Vinted-Angebot")
    )


def item_price(item: Any) -> str:
    price = get_value(item, "price", default=None)
    if isinstance(price, dict):
        amount = get_value(price, "amount", "value", default=None)
        currency = get_value(
            price, "currency_code", "currency", default="EUR"
        )
    else:
        amount = price
        currency = get_value(
            item, "currency_code", "currency", default="EUR"
        )
    if amount is None:
        return "Preis unbekannt"
    return f"{amount} {currency}"


def item_url(item: Any) -> Optional[str]:
    return get_value(item, "url", "item_url", "web_url", default=None)


def item_photo(item: Any) -> Optional[str]:
    photo = get_value(
        item, "photo", "photo_url", "image_url", default=None
    )
    if isinstance(photo, dict):
        return get_value(
            photo, "url", "full_size_url", "full_size", default=None
        )
    return photo


def item_brand(item: Any) -> Optional[str]:
    return get_value(item, "brand_title", "brand", default=None)


intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)


def get_target_channel():
    channel_id = config.get("channel_id")
    if not channel_id:
        return None
    try:
        channel_id = int(channel_id)
    except (TypeError, ValueError):
        return None
    return bot.get_channel(channel_id)


def build_item_embed(item: Any, brand: str) -> discord.Embed:
    title = item_title(item)
    price = item_price(item)
    url = item_url(item)
    photo = item_photo(item)
    actual_brand = item_brand(item) or brand

    embed = discord.Embed(
        title=title[:256],
        description=(
            f"**Marke:** {actual_brand}\n" f"**Preis:** {price}"
        ),
    )
    if isinstance(url, str) and url.startswith("http"):
        embed.url = url
    if isinstance(photo, str) and photo.startswith("http"):
        embed.set_thumbnail(url=photo)
    embed.set_footer(text="Vinted Monitor")
    return embed


async def check_brand(brand: str) -> None:
    if vinted_is_blocked():
        return

    channel = get_target_channel()
    if channel is None:
        logger.warning(
            "Kein Discord-Zielkanal gesetzt. "
            "Nutze !config channel #kanal."
        )
        return

    try:
        items = await search_brand(brand)
    except VintedBlockedError as exc:
        logger.warning(
            "Vinted-Suche für %s pausiert: %s", brand, exc
        )
        return
    except Exception:
        logger.exception("Fehler bei Vinted-Suche für %s.", brand)
        return

    new_items = []
    for item in items:
        iid = item_id(item)
        if iid is None:
            continue
        if iid not in seen_ids:
            seen_ids.add(iid)
            new_items.append(item)

    if new_items:
        save_json(SEEN_FILE, sorted(seen_ids))

    for item in reversed(new_items):
        try:
            await channel.send(embed=build_item_embed(item, brand))
        except discord.Forbidden:
            logger.error(
                "Keine Berechtigung, in den Zielkanal zu schreiben."
            )
            break
        except discord.HTTPException:
            logger.exception("Discord-Fehler beim Senden.")
        await asyncio.sleep(0.5)

    if new_items:
        logger.info(
            "%d neue Angebote für %s gefunden.",
            len(new_items),
            brand,
        )


automatic_task: Optional[asyncio.Task] = None


async def automatic_finder() -> None:
    await bot.wait_until_ready()
    logger.info("Automatischer Vinted-Finder gestartet.")

    while not bot.is_closed():
        interval = max(60, int(config.get("interval", 300)))

        if vinted_is_blocked():
            remaining = vinted_block_remaining()
            logger.info(
                "Vinted pausiert noch %d Sekunden. "
                "Überspringe kompletten Scan.",
                remaining,
            )
            await asyncio.sleep(min(60, max(1, remaining)))
            if not vinted_is_blocked():
                logger.info(
                    "Vinted-Cooldown beendet. "
                    "Beim nächsten Scan wird eine neue Session "
                    "erstellt."
                )
            continue

        brands = list(config.get("brands", []))
        if not brands:
            logger.warning("Keine Marken konfiguriert.")
        else:
            for brand in brands:
                if bot.is_closed():
                    return
                if vinted_is_blocked():
                    logger.warning(
                        "Scan abgebrochen wegen Vinted-Sperre. "
                        "Restliche Marken werden übersprungen."
                    )
                    break
                await check_brand(brand)
                await asyncio.sleep(1)

        logger.info(
            "Scan beendet. Nächster Scan in %d Sekunden.", interval
        )
        await asyncio.sleep(interval)


@bot.event
async def on_ready() -> None:
    global automatic_task
    logger.info(
        "Discord verbunden als %s (%s).",
        bot.user,
        bot.user.id if bot.user else "?",
    )
    if automatic_task is None or automatic_task.done():
        automatic_task = asyncio.create_task(automatic_finder())


def admin_only():
    async def predicate(ctx: commands.Context) -> bool:
        if not ctx.guild:
            return False
        if ctx.author.guild_permissions.manage_guild:
            return True
        await ctx.send(
            "❌ Du brauchst die Berechtigung **Server verwalten**."
        )
        return False

    return commands.check(predicate)


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    embed = discord.Embed(
        title="Vinted Monitor",
        description=(
            "`!config show`\n"
            "`!config brands`\n"
            "`!config brand add <Marke>`\n"
            "`!config brand remove <Marke>`\n"
            "`!config channel #kanal`\n"
            "`!config interval <Sekunden>`\n"
            "`!config maxprice <Preis>`\n"
            "`!config maxprice off`\n"
            "`!search <Marke>`\n"
            "`!search all`\n"
            "`!status`"
        ),
    )
    await ctx.send(embed=embed)


@bot.group(name="config", invoke_without_command=True)
@admin_only()
async def config_group(ctx: commands.Context) -> None:
    await ctx.send(
        "Nutze `!config show`, `!config brands` oder `!help`."
    )


@config_group.command(name="show")
@admin_only()
async def config_show(ctx: commands.Context) -> None:
    channel_id = config.get("channel_id")
    channel_text = "nicht gesetzt"
    if channel_id:
        try:
            channel = bot.get_channel(int(channel_id))
            channel_text = (
                channel.mention if channel else str(channel_id)
            )
        except (TypeError, ValueError):
            pass

    max_price = config.get("max_price")
    max_price_text = (
        "aus" if max_price is None else f"{max_price:.2f} €"
    )

    embed = discord.Embed(
        title="Vinted-Konfiguration",
        description=(
            f"**Marken:** "
            f"{', '.join(config['brands']) or 'keine'}\n"
            f"**Kanal:** {channel_text}\n"
            f"**Intervall:** {config['interval']} Sekunden\n"
            f"**Max. Preis:** {max_price_text}\n"
            f"**Angebote/Marke:** {config['results_per_brand']}\n"
            f"**Proxies:** {len(PROXIES)}\n"
            f"**FlareSolverr:** "
            f"{'aktiv' if FLARESOLVERR_URL else 'aus'}\n"
            f"**CAPTCHA-Solver:** "
            f"{'Capsolver' if CAPSOLVER_API_KEY else ''}"
            f"{' 2Captcha' if TWOCAPTCHA_API_KEY else ''}"
            f"{' CapMonster' if CAPMONSTER_API_KEY else ''}"
            f"{' AntiCaptcha' if ANTICAPTCHA_API_KEY else ''}"
            f"{' keiner' if not any([CAPSOLVER_API_KEY, TWOCAPTCHA_API_KEY, CAPMONSTER_API_KEY, ANTICAPTCHA_API_KEY]) else ''}"
        ),
    )
    await ctx.send(embed=embed)


@config_group.command(name="brands")
@admin_only()
async def config_brands(ctx: commands.Context) -> None:
    brands = config.get("brands", [])
    if not brands:
        await ctx.send("Keine Marken konfiguriert.")
        return
    await ctx.send(
        "**Aktive Marken:**\n"
        + "\n".join(f"• {brand}" for brand in brands)
    )


@config_group.group(name="brand", invoke_without_command=True)
@admin_only()
async def config_brand(ctx: commands.Context) -> None:
    await ctx.send(
        "Nutze `!config brand add <Marke>` "
        "oder `!config brand remove <Marke>`."
    )


@config_brand.command(name="add")
@admin_only()
async def config_brand_add(
    ctx: commands.Context, *, brand: str
) -> None:
    brand = brand.strip()
    if not brand:
        await ctx.send("❌ Keine Marke angegeben.")
        return

    existing = {x.lower() for x in config["brands"]}
    if brand.lower() in existing:
        await ctx.send("❌ Diese Marke ist bereits vorhanden.")
        return

    config["brands"].append(brand)
    save_json(CONFIG_FILE, config)
    await ctx.send(f"✅ **{brand}** wurde hinzugefügt.")


@config_brand.command(name="remove")
@admin_only()
async def config_brand_remove(
    ctx: commands.Context, *, brand: str
) -> None:
    brand = brand.strip()
    old = config["brands"]
    new = [x for x in old if x.lower() != brand.lower()]
    if len(new) == len(old):
        await ctx.send("❌ Diese Marke ist nicht konfiguriert.")
        return

    config["brands"] = new
    save_json(CONFIG_FILE, config)
    await ctx.send(f"✅ **{brand}** wurde entfernt.")


@config_group.command(name="channel")
@admin_only()
async def config_channel(
    ctx: commands.Context, channel: discord.TextChannel
) -> None:
    config["channel_id"] = channel.id
    save_json(CONFIG_FILE, config)
    await ctx.send(f"✅ Zielkanal ist jetzt {channel.mention}.")


@config_group.command(name="interval")
@admin_only()
async def config_interval(
    ctx: commands.Context, seconds: int
) -> None:
    if seconds < 60:
        await ctx.send("❌ Minimum: **60 Sekunden**.")
        return
    if seconds > 86400:
        await ctx.send("❌ Maximum: **86400 Sekunden**.")
        return

    config["interval"] = seconds
    save_json(CONFIG_FILE, config)
    await ctx.send(f"✅ Prüfintervall: **{seconds} Sekunden**.")


@config_group.command(name="maxprice")
@admin_only()
async def config_maxprice(
    ctx: commands.Context, value: str
) -> None:
    if value.lower() == "off":
        config["max_price"] = None
        save_json(CONFIG_FILE, config)
        await ctx.send("✅ Preislimit deaktiviert.")
        return

    try:
        price = float(value.replace(",", "."))
    except ValueError:
        await ctx.send("❌ Ungültiger Preis.")
        return

    if price <= 0:
        await ctx.send("❌ Der Preis muss größer als 0 sein.")
        return

    config["max_price"] = price
    save_json(CONFIG_FILE, config)
    await ctx.send(f"✅ Maximalpreis: **{price:.2f} €**.")


@bot.command(name="search")
@admin_only()
async def manual_search(
    ctx: commands.Context, *, brand: str
) -> None:
    brand = brand.strip()
    if not brand:
        await ctx.send("❌ Beispiel: `!search Nike`")
        return

    if vinted_is_blocked():
        remaining = vinted_block_remaining()
        await ctx.send(
            "⚠️ Vinted ist momentan pausiert. "
            f"Neuer Versuch in **{remaining // 60}m "
            f"{remaining % 60}s**."
        )
        return

    if brand.lower() == "all":
        brands = list(config.get("brands", []))
    else:
        brands = [brand]

    await ctx.send("🔎 Suche nach aktuellen Vinted-Angeboten...")

    total = 0
    for current_brand in brands:
        if vinted_is_blocked():
            await ctx.send(
                "⚠️ Vinted hat die Session gesperrt. "
                "Suche abgebrochen, Bot pausiert für "
                f"**{vinted_block_remaining() // 60} Minuten**."
            )
            return
        try:
            items = await search_brand(current_brand)
        except VintedBlockedError as exc:
            await ctx.send(
                "⚠️ Vinted ist momentan nicht verfügbar.\n"
                f"`{exc}`"
            )
            return
        except Exception:
            logger.exception("Manuelle Suche fehlgeschlagen.")
            await ctx.send(f"❌ Fehler bei **{current_brand}**.")
            continue

        if not items:
            await ctx.send(
                f"Keine Angebote für **{current_brand}** gefunden."
            )
            continue

        for item in items[:10]:
            try:
                await ctx.send(
                    embed=build_item_embed(item, current_brand)
                )
                total += 1
            except discord.HTTPException:
                logger.exception("Discord-Fehler.")
                break
            await asyncio.sleep(0.4)

    await ctx.send(f"✅ Suche beendet. Angezeigt: **{total}**.")


@bot.command(name="status")
async def status_command(ctx: commands.Context) -> None:
    if vinted_is_blocked():
        remaining = vinted_block_remaining()
        minutes = remaining // 60
        seconds = remaining % 60
        vinted_status = (
            f"⏸️ pausiert – neue Session in "
            f"{minutes}m {seconds}s"
        )
    elif vinted is None:
        vinted_status = "🟡 neue Session beim nächsten Scan"
    else:
        vinted_status = "🟢 Session aktiv"

    channel_id = config.get("channel_id")
    channel = None
    if channel_id:
        try:
            channel = bot.get_channel(int(channel_id))
        except (TypeError, ValueError):
            pass

    embed = discord.Embed(
        title="Bot-Status",
        description=(
            f"**Discord:** 🟢 online\n"
            f"**Vinted:** {vinted_status}\n"
            f"**Marken:** {len(config.get('brands', []))}\n"
            f"**Gesehene Angebote:** {len(seen_ids)}\n"
            f"**Intervall:** {config.get('interval', 300)}s\n"
            f"**Proxies:** {len(PROXIES)}\n"
            f"**Zielkanal:** "
            f"{channel.mention if channel else 'nicht gesetzt'}"
        ),
    )
    await ctx.send(embed=embed)


@bot.event
async def on_command_error(
    ctx: commands.Context, error: commands.CommandError
) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Argument fehlt. Nutze `!help`.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("❌ Ungültiges Argument. Nutze `!help`.")
        return
    if isinstance(error, commands.CheckFailure):
        return

    logger.exception("Unhandled command error: %s", error)
    await ctx.send("❌ Bei dem Befehl ist ein Fehler aufgetreten.")


app = Flask(__name__)


@app.get("/")
def home():
    return jsonify(
        {
            "status": "online",
            "service": "vinted-discord-bot",
            "discord": bot.is_ready(),
            "vinted_paused": vinted_is_blocked(),
            "vinted_pause_remaining": vinted_block_remaining(),
            "brands": config.get("brands", []),
            "proxies": len(PROXIES),
            "flaresolverr": bool(FLARESOLVERR_URL),
            "captcha_solver": bool(
                CAPSOLVER_API_KEY
                or TWOCAPTCHA_API_KEY
                or CAPMONSTER_API_KEY
                or ANTICAPTCHA_API_KEY
            ),
        }
    )


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "discord_ready": bot.is_ready(),
            "vinted_paused": vinted_is_blocked(),
            "vinted_pause_remaining": vinted_block_remaining(),
        }
    )


def run_flask() -> None:
    port = int(os.getenv("PORT", "10000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )


def main() -> None:
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Render Healthserver gestartet.")
    logger.info("Starte Discord-Bot...")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
