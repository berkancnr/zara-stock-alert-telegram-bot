import json
import re
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import requests
from playwright.async_api import async_playwright, Response, Browser, Playwright

LDJSON_RE = re.compile(
    r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
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
                self.browser = await self.playwright.chromium.launch(headless=False)

    async def close(self):
        async with self._lock:
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None

    async def fetch_document_html(self, url: str) -> str:
        await self.ensure_browser()
        if not self.browser:
            raise RuntimeError("Browser could not be started")

        doc_html: Optional[str] = None
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

        async def route_handler(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet", "other"]:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", route_handler)

        async def on_response(resp: Response):
            nonlocal doc_html
            try:
                if doc_html is not None: return
                if resp.request.resource_type != "document": return
                if resp.status != 200: return
                if "text/html" not in (resp.headers.get("content-type") or "").lower(): return
                doc_html = await resp.text()
            except Exception: pass

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_selector('script[type="application/ld+json"]', state="attached", timeout=5000)
            except Exception: pass 
        finally:
            await context.close()

        if not doc_html:
            raise RuntimeError("Document HTML yakalanamadı.")
        return doc_html

zara_browser = ZaraBrowserManager.get_instance()

def safe_json_loads(s: str) -> Optional[Any]:
    s = (s or "").strip()
    if not s: return None
    try: return json.loads(s)
    except Exception: return None

def extract_ldjson_from_html(html: str) -> List[Any]:
    out: List[Any] = []
    for m in LDJSON_RE.finditer(html):
        raw = (m.group(1) or "").strip()
        parsed = safe_json_loads(raw)
        if parsed is None: continue
        if isinstance(parsed, list): out.extend(parsed)
        else: out.append(parsed)
    return out

def _norm_availability(av: Optional[str]) -> str:
    if not av or not isinstance(av, str): return "unknown"
    return re.sub(r"[^a-zA-Z]", "", av.rsplit("/", 1)[-1]).lower() or "unknown"

def _is_stock_positive(av_norm: str) -> bool:
    return av_norm not in {"outofstock", "soldout", "discontinued", "unknown"}

async def get_zara_products_async(url: str) -> List[Dict[str, Any]]:
    """Fetch and return all product variants from LD+JSON."""
    html = await zara_browser.fetch_document_html(url)
    ld = extract_ldjson_from_html(html)
    return [o for o in ld if isinstance(o, dict) and o.get("@type") == "Product"]

def evaluate_size_status(products: List[Dict[str, Any]], target_size: str, url: str) -> Tuple[str, str, Dict[str, Any]]:
    """Evaluate status for a specific size based on provided products list."""
    tsize = target_size.upper()

    if tsize == "ANY":
        found_sizes = []
        base_product = None
        for p in products:
            p_size = str(p.get("size", "")).strip()
            if not p_size: continue
            if base_product is None: base_product = p
            offers = p.get("offers", [{}])[0] if isinstance(p.get("offers"), list) else p.get("offers", {})
            av_norm = _norm_availability(offers.get("availability"))
            if _is_stock_positive(av_norm): found_sizes.append(p_size)
        
        if not base_product: return "NOT_FOUND", "unknown", {"url": url, "size": "ANY"}
        offers = base_product.get("offers", [{}])[0] if isinstance(base_product.get("offers"), list) else base_product.get("offers", {})
        av_norm = "instock" if found_sizes else "outofstock"
        return ("POSITIVE" if found_sizes else "NEGATIVE"), av_norm, {
            "name": base_product.get("name"), "color": base_product.get("color"),
            "size": "ANY", "found_sizes": found_sizes, "sku": base_product.get("sku"),
            "price": offers.get("price"), "currency": offers.get("priceCurrency"),
            "availability_norm": av_norm, "url": url
        }

    target = next((p for p in products if str(p.get("size", "")).upper() == tsize), None)
    if not target: return "NOT_FOUND", "unknown", {"url": url, "size": tsize}

    offers = target.get("offers", [{}])[0] if isinstance(target.get("offers"), list) else target.get("offers", {})
    av_norm = _norm_availability(offers.get("availability"))
    status = "POSITIVE" if _is_stock_positive(av_norm) else "NEGATIVE"
    return status, av_norm, {
        "name": target.get("name"), "color": target.get("color"), "size": tsize,
        "sku": target.get("sku"), "price": offers.get("price"),
        "currency": offers.get("priceCurrency"), "availability_norm": av_norm, "url": url
    }


def send_telegram(token: str, chat_id: str, text: str) -> None:
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(api, json={"chat_id": chat_id, "text": text}, timeout=20).raise_for_status()
