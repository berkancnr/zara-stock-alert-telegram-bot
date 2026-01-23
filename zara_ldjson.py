import json
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
from playwright.sync_api import sync_playwright, Response

LDJSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

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
    # “stokta olmaması dışındaki” = outofstock/soldout/discontinued HARİÇ
    if av_norm in {"outofstock", "soldout", "discontinued"}:
        return False
    if av_norm == "unknown":
        return False  # istersen True yaparız (spam riski artar)
    return True

def fetch_document_html_headful(url: str, wait_ms: int = 6000) -> str:
    doc_html: Optional[str] = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # Zara için kritik
        context = browser.new_context(
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        def on_response(resp: Response):
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
                doc_html = resp.text()
            except Exception:
                return

        page.on("response", on_response)

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)

        context.close()
        browser.close()

    if not doc_html:
        raise RuntimeError("Document HTML yakalanamadı (deny/redirect olabilir).")
    return doc_html

def check_size_status(url: str, target_size: str) -> Tuple[str, str, Dict[str, Any]]:
    """
    Dönenler:
      - status: "POSITIVE" | "NEGATIVE" | "UNKNOWN" | "NOT_FOUND"
      - availability_norm: instock/limitedavailability/outofstock/unknown...
      - details: mesajda kullanmak için özet (name, price, currency, color, sku, availability_raw, size, url)
    """
    html = fetch_document_html_headful(url)
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
