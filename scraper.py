"""
High-performance Shopify scraper.
Phase 1 - Fetch all product pages (parallel with 429 fallback to sequential)
Phase 2 - Bulk save products/variants/image-URLs to DB (no image downloads)
Images use original CDN URLs in the CSV for direct Shopify import.
"""
import asyncio
import httpx
import re
import os
import json
from datetime import datetime
from urllib.parse import urlparse
from html.parser import HTMLParser
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Store, Product, Variant, Image

IMAGES_BASE = os.environ.get("IMAGES_PATH", "images")
jobs: dict[str, dict] = {}

# ── Headers ───────────────────────────────────────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "DNT": "1",
}
API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "DNT": "1",
}
CF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

MAX_FETCH_WORKERS = 8
DB_BATCH_SIZE = 100
PAGE_DELAY = 0.2


# ── HTML helper ───────────────────────────────────────────────────────────────

class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__(); self.reset(); self.fed = []
    def handle_data(self, d): self.fed.append(d)
    def get_data(self): return " ".join(self.fed).strip()


def strip_html(html: str) -> str:
    if not html: return ""
    s = HTMLStripper()
    try: s.feed(html)
    except Exception: pass
    return s.get_data()


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"): url = "https://" + url
    return url


def get_image_filename(url: str) -> str:
    name = os.path.basename(urlparse(url).path)
    return re.sub(r"\?.*", "", name) or "image.jpg"


# ── Domain resolution ─────────────────────────────────────────────────────────

async def resolve_shop_app_domain(handle: str) -> str | None:
    candidates = [f"{handle}.com", f"www.{handle}.com"]
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers=BROWSER_HEADERS) as c:
            r = await c.get(f"https://shop.app/m/{handle}")
            m = re.search(r"([\w-]+\.myshopify\.com)", r.text)
            if m: return m.group(1).lower()
            m = re.search(r'"(?:storeDomain|domain)"\s*:\s*"([^"]+\.[a-z]{2,})"', r.text)
            if m:
                d = m.group(1).lower().replace("www.", "")
                if "shop.app" not in d: return d
    except Exception: pass
    async with httpx.AsyncClient(follow_redirects=True, timeout=10, headers=API_HEADERS) as c:
        for cand in candidates:
            try:
                r = await c.get(f"https://{cand}/products.json?limit=1", timeout=8)
                if r.status_code == 200 and "products" in r.json():
                    return cand.replace("www.", "")
            except Exception: continue
    return None


async def resolve_domain(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() in ("shop.app", "www.shop.app"):
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "m":
            handle = parts[1].split("?")[0]
            real = await resolve_shop_app_domain(handle)
            return real or f"{handle}.com"
    # For normal URLs — just parse the domain directly, no HTTP call needed
    netloc = parsed.netloc.lower().replace("www.", "")
    if netloc and "." in netloc:
        return netloc
    # Fallback: follow redirects (only if needed for short/redirect URLs)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10, headers=BROWSER_HEADERS) as c:
            r = await c.get(url)
            domain = urlparse(str(r.url)).netloc.lower().replace("www.", "")
            if "shop.app" in domain:
                m = re.search(r"([\w-]+\.myshopify\.com)", r.text)
                if m: return m.group(1).lower()
            return domain
    except Exception:
        return parsed.netloc.lower().replace("www.", "")


async def check_store_accessible(domain: str) -> tuple[bool, str]:
    """Quick check — one attempt per header set, short timeouts."""
    products_url = f"https://{domain}/products.json?limit=1"
    last_error = ""
    for headers in [API_HEADERS, BROWSER_HEADERS, CF_HEADERS]:
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=20, headers=headers,
            ) as client:
                req_headers = dict(headers)
                req_headers["Referer"] = f"https://{domain}/"
                r = await client.get(products_url, headers=req_headers, timeout=18)

            if r.status_code == 200:
                try:
                    data = r.json()
                    if "products" in data:
                        return True, "ok"
                except Exception:
                    pass
            elif r.status_code == 429:
                await asyncio.sleep(8)
                # retry same headers once
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=20, headers=headers,
                ) as client2:
                    r2 = await client2.get(products_url, timeout=18)
                if r2.status_code == 200 and "products" in r2.json():
                    return True, "ok"
                last_error = "Rate limited"
            elif r.status_code in (401, 403):
                return False, f"Store is password protected (HTTP {r.status_code})."
            elif r.status_code == 404:
                return False, "Not a Shopify store (404)."
            elif r.status_code in (503, 502, 521, 522, 523, 524):
                last_error = f"Store server error (HTTP {r.status_code})"
                await asyncio.sleep(2)
            else:
                last_error = f"Unexpected HTTP {r.status_code}"
        except httpx.TimeoutException:
            last_error = "Request timed out"
        except Exception as e:
            last_error = f"Connection failed: {str(e)[:60]}"

    return False, (
        last_error or "Could not access store."
    ) + " — May be Cloudflare-protected or temporarily down. Try again in 1-2 minutes."


# ── Page fetcher ──────────────────────────────────────────────────────────────

async def fetch_page(
    client: httpx.AsyncClient,
    domain: str,
    page: int,
    sem: asyncio.Semaphore,
) -> tuple[int, list[dict] | None]:
    """Returns (page, products) or (page, None) on 429."""
    url = f"https://{domain}/products.json?limit=250&page={page}"
    headers = dict(API_HEADERS)
    headers["Referer"] = f"https://{domain}/"
    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(url, headers=headers, timeout=25)
                if r.status_code == 429:
                    await asyncio.sleep(2 * (attempt + 1))
                    if attempt == 2:
                        return page, None
                    continue
                if r.status_code in (400, 404):
                    return page, []  # end of catalog or not found
                if r.status_code != 200:
                    return page, []
                products = r.json().get("products", [])
                await asyncio.sleep(PAGE_DELAY)
                return page, products
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 2: return page, []
                await asyncio.sleep(2 ** attempt)
            except Exception:
                return page, []
    return page, []


# ── Catalog fetcher ───────────────────────────────────────────────────────────

async def fetch_all_catalog(domain: str, update_fn) -> list[dict]:
    """
    Fetch ALL product pages SEQUENTIALLY (one by one) to avoid rate limiting.
    Progress: 5% → 50% during fetch phase.
    """
    api_headers = dict(API_HEADERS)
    api_headers["Referer"] = f"https://{domain}/"

    all_products: list[dict] = []
    current_page = 1
    consecutive_errors = 0
    delay_between_pages = 0.8  # Start with 0.8 second delay
    rate_limit_count = 0
    
    await update_fn("scraping", f"Starting page-by-page fetch from {domain}...", pct=5)
    
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers=api_headers) as client:
        while True:
            page_url = f"https://{domain}/products.json?limit=250&page={current_page}"
            
            try:
                # Fetch this page
                r = await client.get(page_url, timeout=20)
                
                # Handle rate limiting with exponential backoff (but max 8s)
                if r.status_code == 429:
                    rate_limit_count += 1
                    wait_time = min(4 * (2 ** min(rate_limit_count - 1, 1)), 8)  # 4s, 8s max
                    await update_fn("scraping", 
                        f"Rate limit hit, brief pause ({wait_time}s)... Page {current_page}",
                        products=len(all_products), pct=min(5 + (current_page * 0.3), 45))
                    await asyncio.sleep(wait_time)
                    delay_between_pages = min(delay_between_pages * 1.3, 3.0)  # Increase delay slightly
                    continue  # Retry same page
                
                # Reset rate limit counter on success
                if r.status_code == 200:
                    rate_limit_count = max(0, rate_limit_count - 1)  # Gradual recovery
                
                # Handle errors
                if r.status_code in (401, 403):
                    await update_fn("error", f"Store is password protected (HTTP {r.status_code}).")
                    return []
                
                if r.status_code == 404 or r.status_code == 400:
                    # End of catalog
                    if current_page == 1:
                        await update_fn("error", "Not a Shopify store — /products.json returned 404.")
                        return []
                    break  # Normal end of catalog
                
                if r.status_code != 200:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        await update_fn("error", f"Store returned HTTP {r.status_code} on 3 consecutive pages.")
                        return []
                    await asyncio.sleep(2)
                    continue
                
                # Parse products from this page
                try:
                    products = r.json().get("products", [])
                except Exception:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        break
                    await asyncio.sleep(2)
                    continue
                
                # Check if we got products
                if not products:
                    break  # End of catalog
                
                # Add to collection
                all_products.extend(products)
                consecutive_errors = 0  # Reset error counter
                
                # Calculate progress (5% to 50% range)
                # Realistic progress based on actual page counts
                progress_pct = min(5 + (current_page * 0.4), 50)
                
                await update_fn("scraping",
                    f"Page {current_page}: {len(products)} products | Total so far: {len(all_products):,}",
                    products=len(all_products), pct=progress_pct)
                
                # Check if this was the last page (less than 250 products)
                if len(products) < 250:
                    break
                
                # Move to next page
                current_page += 1
                
                # Delay between pages to avoid rate limiting
                await asyncio.sleep(delay_between_pages)
                
                # Gradually reduce delay if no issues (but stay above 0.5s minimum)
                if rate_limit_count == 0:
                    delay_between_pages = max(0.5, delay_between_pages * 0.95)
                
            except httpx.TimeoutException:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    await update_fn("error", f"Timeout on page {current_page} after 3 attempts.")
                    return []
                await update_fn("scraping", f"Timeout on page {current_page}, retrying...", 
                    products=len(all_products), pct=min(5 + (current_page * 0.4), 45))
                await asyncio.sleep(3)
                continue
                
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    await update_fn("error", f"Error on page {current_page}: {str(e)[:80]}")
                    return []
                await asyncio.sleep(2)
                continue
    
    await update_fn("scraping", 
        f"✓ Fetch complete: {len(all_products):,} products from {current_page} pages",
        products=len(all_products), pct=50)
    
    return all_products


# ── Product parser ────────────────────────────────────────────────────────────

def parse_product(p: dict) -> dict:
    prices = []
    variants_data = []
    options = p.get("options", [])
    option1_name = options[0].get("name", "Title") if options else "Title"
    option2_name = options[1].get("name", "") if len(options) > 1 else ""
    option3_name = options[2].get("name", "") if len(options) > 2 else ""
    if len(options) == 1 and options[0].get("name", "").lower() == "title":
        option1_name = "Title"

    for v in p.get("variants", []):
        try: price = float(v.get("price", 0) or 0)
        except: price = 0.0
        try: compare_at = float(v.get("compare_at_price", 0) or 0)
        except: compare_at = None
        prices.append(price)
        variants_data.append({
            "shopify_variant_id": str(v.get("id", "")),
            "title": v.get("title", ""),
            "sku": v.get("sku", ""),
            "price": price,
            "compare_at_price": compare_at,
            "available": v.get("available", True),
            "option1": v.get("option1"),
            "option2": v.get("option2"),
            "option3": v.get("option3"),
        })

    images_data = [
        {
            "position": img.get("position", i + 1),
            "alt_text": img.get("alt", ""),
            "original_src": img.get("src", ""),
        }
        for i, img in enumerate(p.get("images", []))
    ]
    tags = p.get("tags", "")
    if isinstance(tags, list): tags = ", ".join(tags)
    raw_html = p.get("body_html", "") or ""

    return {
        "shopify_id": str(p.get("id", "")),
        "title": p.get("title", ""),
        "handle": p.get("handle", ""),
        "product_type": p.get("product_type", ""),
        "vendor": p.get("vendor", ""),
        "tags": tags,
        "description": strip_html(raw_html),
        "description_html": raw_html,
        "option1_name": option1_name,
        "option2_name": option2_name,
        "option3_name": option3_name,
        "price_min": min(prices) if prices else 0.0,
        "price_max": max(prices) if prices else 0.0,
        "created_at_shopify": p.get("created_at", ""),
        "updated_at_shopify": p.get("updated_at", ""),
        "variants": variants_data,
        "images": images_data,
    }


# ── Bulk DB save (no image download) ─────────────────────────────────────────

async def bulk_save_products(db: Session, store_id: int, domain: str,
                             raw_products: list[dict], update_fn, job: dict) -> None:
    """Save all products to DB. Image URLs stored as original CDN URLs (no download)."""
    total = len(raw_products)

    for batch_start in range(0, total, DB_BATCH_SIZE):
        if job.get("stopped"):
            break

        batch = raw_products[batch_start: batch_start + DB_BATCH_SIZE]

        for raw_p in batch:
            parsed = parse_product(raw_p)
            product = Product(
                store_id=store_id,
                shopify_id=parsed["shopify_id"],
                title=parsed["title"],
                handle=parsed["handle"],
                product_type=parsed["product_type"],
                vendor=parsed["vendor"],
                tags=parsed["tags"],
                description=parsed["description"],
                description_html=parsed["description_html"],
                option1_name=parsed["option1_name"],
                option2_name=parsed["option2_name"],
                option3_name=parsed["option3_name"],
                price_min=parsed["price_min"],
                price_max=parsed["price_max"],
                created_at_shopify=parsed["created_at_shopify"],
                updated_at_shopify=parsed["updated_at_shopify"],
            )
            db.add(product)
            db.flush()

            for vd in parsed["variants"]:
                db.add(Variant(product_id=product.id, **vd))

            for imgd in parsed["images"]:
                src = imgd["original_src"]
                if not src:
                    continue
                db.add(Image(
                    product_id=product.id,
                    position=imgd["position"],
                    alt_text=imgd["alt_text"],
                    original_src=src.split("?")[0],  # clean CDN URL
                    local_path=None,                  # not downloaded
                ))

        db.commit()

        saved = min(batch_start + len(batch), total)
        pct = 50 + saved / total * 45
        await update_fn("scraping",
                  f"Saved {saved:,} / {total:,} products...",
                  products=saved, products_total=total, pct=pct)

        # Yield to event loop so SSE can flush progress to browser
        await asyncio.sleep(0)


# ── Main job ──────────────────────────────────────────────────────────────────

async def run_scrape_job(job_id: str, url: str):
    job = jobs[job_id]
    db: Session = SessionLocal()

    async def update(status, message, page=0, products=0, products_total=0,
               images_done=0, images_total=0, pct=0.0, current_product=""):
        job["events"].append({
            "status": status, "message": message,
            "page": page, "products_found": products,
            "products_total": products_total,
            "images_downloaded": images_done,
            "images_total": images_total,
            "percentage": round(pct, 1),
            "current_product": current_product,
        })
        await asyncio.sleep(0)  # Yield to event loop immediately

    domain = ""
    try:
        await update("scraping", "Starting scrape job...", pct=0)
        
        await update("scraping", "Resolving store URL...", pct=1)
        
        norm_url = normalize_url(url)
        domain = await resolve_domain(norm_url)
        if not domain:
            await update("error", "Could not resolve store domain.")
            job["done"] = True; db.close(); return

        await update("scraping", f"Resolved: {domain} — connecting to store...", pct=3)

        existing = db.query(Store).filter(Store.domain == domain).first()
        if existing:
            await update("scraping", "Clearing previous data...", pct=4)
            db.delete(existing); db.commit()

        store = Store(domain=domain, name=domain, original_url=url, scrape_status="scraping")
        db.add(store); db.commit(); db.refresh(store)
        store_id = store.id
        job["store_id"] = store_id

        # Small delay before starting to avoid triggering rate limits immediately
        await asyncio.sleep(0.5)
        
        await update("scraping", "Fetching full product catalog...", pct=5)
        all_raw = await fetch_all_catalog(domain, update)
        
        print(f"✓ {domain} — scraped {len(all_raw)} products successfully")

        if not all_raw:
            await update("complete", "Store has no products.", pct=100)
            s = db.query(Store).filter(Store.id == store_id).first()
            s.scrape_status = "complete"; s.total_products = 0
            s.scraped_at = datetime.utcnow(); db.commit()
            job["done"] = True; db.close(); return

        total_products = len(all_raw)
        images_total = sum(len(p.get("images", [])) for p in all_raw)
        await update("scraping",
               f"Found {total_products:,} products ({images_total:,} image URLs). Saving to database...",
               products=total_products, products_total=total_products,
               images_total=images_total, pct=50)

        await bulk_save_products(db, store_id, domain, all_raw, update, job)

        from sqlalchemy import func
        from models import Variant as V, Image as Img

        fp = db.query(func.count(Product.id)).filter(Product.store_id == store_id).scalar() or 0
        fv = db.query(func.count(V.id)).join(Product).filter(Product.store_id == store_id).scalar() or 0
        fi = db.query(func.count(Img.id)).join(Product).filter(Product.store_id == store_id).scalar() or 0

        was_stopped = job.get("stopped", False)
        s = db.query(Store).filter(Store.id == store_id).first()
        s.total_products = fp; s.total_variants = fv; s.total_images = fi
        s.scrape_status = "stopped" if was_stopped else "complete"
        s.scraped_at = datetime.utcnow(); s.name = domain
        db.commit()

        msg = (
            f"{'Stopped' if was_stopped else 'Done!'} "
            f"{fp:,} products | {fv:,} variants | {fi:,} image URLs saved. "
            f"Export CSV for Shopify import."
        )
        await update("stopped" if was_stopped else "complete", msg,
               products=fp, products_total=fp,
               images_done=fi, images_total=images_total,
               pct=100 if not was_stopped else 80)

    except Exception as e:
        await update("error", f"Scrape failed: {str(e)}")
        try:
            if domain:
                s = db.query(Store).filter(Store.domain == domain).first()
                if s: s.scrape_status = "error"; db.commit()
        except Exception:
            pass
    finally:
        job["done"] = True
        db.close()
