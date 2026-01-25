import json
import re
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import requests
from playwright.async_api import async_playwright, Response, Browser, Playwright

LDJSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

class ZaraBrowserManager:
    _instance = None
    
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def ensure_browser(self):
        async with self._lock:
            if self.browser is None:
                self.playwright = await async_playwright().start()
                # Headless=False is critical for Zara
                self.browser = await self.playwright.chromium.launch(headless=False)

    async def close(self):
        async with self._lock:
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None

    async def fetch_document_html(self, url: str, wait_ms: int = 6000) -> str:
        await self.ensure_browser()
        if not self.browser:
            raise RuntimeError("Browser could not be started")

        doc_html: Optional[str] = None
        
        # Create a new context for isolation (cookies, storage) per request
        context = await self.browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        # Gereksiz kaynakları engelle (Hızlandırma)
        async def route_handler(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet", "other"]:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", route_handler)

        async def on_response(resp: Response):
            nonlocal doc_html
            try:
                if doc_html is not None:
                    return
                if resp.request.resource_type != "document":
                    return
                if resp.status != 200:
                    return
                ct = (resp.headers.get("content-type") or "").lower()
                if "text/html" not in ct:
                    return
                doc_html = await resp.text()
            except Exception:
                return

        page.on("response", on_response)

        try:
            # Sadece DOM içeriğinin yüklenmesini bekle (load state gereksiz zaman kaybı)
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Verinin (ld+json) geldiğinden emin olmak için kısa bir bekleme (isteğe bağlı)
            # Genelde domcontentloaded yeterlidir, ancak garanti olsun diye script tagini bekleyebiliriz.
            try:
                await page.wait_for_selector('script[type="application/ld+json"]', state="attached", timeout=5000)
            except Exception:
                pass 

        finally:
            # We only close the context/page, keep the browser open
            await context.close()

        if not doc_html:
            raise RuntimeError("Document HTML yakalanamadı (deny/redirect olabilir).")
        return doc_html

# Singleton instance
zara_browser = ZaraBrowserManager.get_instance()

def safe_json_loads(s: str) -> Optional[Any]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None

def extract_ldjson_from_html(html: str) -> List[Any]:
    out: List[Any] = []
    for m in LDJSON_RE.finditer(html):
        raw = (m.group(1) or "").strip()
        parsed = safe_json_loads(raw)
        if parsed is None:
            continue
        if isinstance(parsed, list):
            out.extend(parsed)
        else:
            out.append(parsed)
    return out

def _norm_availability(av: Optional[str]) -> str:
    if not av or not isinstance(av, str):
        return "unknown"
    av = av.strip()
    av = av.rsplit("/", 1)[-1]
    av = re.sub(r"[^a-zA-Z]", "", av).lower()
    return av or "unknown"

def _is_stock_positive(av_norm: str) -> bool:
    if av_norm in {"outofstock", "soldout", "discontinued"}:
        return False
    if av_norm == "unknown":
        return False
    return True

async def check_size_status_async(url: str, target_size: str) -> Tuple[str, str, Dict[str, Any]]:
    """
    Async version of check_size_status.
    """
    html = await zara_browser.fetch_document_html(url)
    ld = extract_ldjson_from_html(html)
    products = [o for o in ld if isinstance(o, dict) and o.get("@type") == "Product"]

    tsize = target_size.upper()
    target = None
    for p in products:
        if str(p.get("size", "")).upper() == tsize:
            target = p
            break

    if not target:
        return "NOT_FOUND", "unknown", {"url": url, "size": tsize}

    offers = target.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    av_raw = offers.get("availability")
    av_norm = _norm_availability(av_raw)

    if av_norm == "unknown":
        status = "UNKNOWN"
    elif _is_stock_positive(av_norm):
        status = "POSITIVE"
    else:
        status = "NEGATIVE"

    details = {
        "name": target.get("name"),
        "brand": target.get("brand"),
        "color": target.get("color"),
        "size": tsize,
        "sku": target.get("sku"),
        "price": offers.get("price"),
        "currency": offers.get("priceCurrency"),
        "availability_raw": av_raw,
        "availability_norm": av_norm,
        "url": offers.get("url") or url,
        "image": target.get("image"),
    }
    return status, av_norm, details

def send_telegram(token: str, chat_id: str, text: str) -> None:
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(api, json={"chat_id": chat_id, "text": text}, timeout=20)
    r.raise_for_status()