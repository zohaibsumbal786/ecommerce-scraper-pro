"""
WooCommerce Scraper — 3-method approach (no API keys required for public stores)

Method 1: WooCommerce Store API  /wp-json/wc/store/v1/products  (PUBLIC, no auth)
Method 2: WooCommerce REST API   /wp-json/wc/v3/products        (needs Consumer Key+Secret)
Method 3: HTML sitemap scraping  parses /sitemap.xml + product pages (fallback)
"""

import asyncio
import httpx
import re
import os
import json
from datetime import datetime
from urllib.parse import urlparse, urljoin
from pathlib import Path
from html.parser import HTMLParser

from database import SessionLocal
from models import Store, Product, Variant, Image

IMAGES_BASE = os.environ.get("IMAGES_PATH", "images")
woo_jobs: dict[str, dict] = {}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


# ── HTML helpers ──────────────────────────────────────────────────────────────

class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.fed = []
    def handle_data(self, d):
        self.fed.append(d)
    def get_data(self):
        return " ".join(self.fed).strip()


def strip_html(html: str) -> str:
    if not html:
        return ""
    s = HTMLStripper()
    try:
        s.feed(html)
    except Exception:
        pass
    return s.get_data()


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    return url


def get_domain(url: str) -> str:
    return urlparse(url).netloc.lower().replace("www.", "")


def get_base(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def get_image_filename(url: str) -> str:
    name = os.path.basename(urlparse(url).path)
    return re.sub(r"\?.*", "", name) or "image.jpg"


# ── Image downloader ──────────────────────────────────────────────────────────

async def download_images(client, sem, tasks):
    async def _one(src, path):
        async with sem:
            try:
                r = await client.get(src, timeout=15, follow_redirects=True)
                if r.status_code == 200:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(r.content)
                    return True
            except Exception:
                pass
            return False
    results = await asyncio.gather(*[_one(s, p) for s, p in tasks], return_exceptions=True)
    return sum(1 for r in results if r is True)


# ── METHOD 1: WooCommerce Public Store API (no auth needed) ──────────────────

async def scrape_via_store_api(base_url: str, client: httpx.AsyncClient) -> tuple[bool, list]:
    """
    Uses /wp-json/wc/store/v1/products — fully public, no API keys needed.
    Available on WooCommerce 3.6+.
    """
    all_products = []
    page = 1
    per_page = 100

    while True:
        url = f"{base_url}/wp-json/wc/store/v1/products?per_page={per_page}&page={page}&orderby=date&order=desc"
        try:
            for attempt in range(3):
                r = await client.get(url, timeout=20)
                if r.status_code == 429:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                break

            if r.status_code != 200:
                break

            data = r.json()
            if not isinstance(data, list) or not data:
                break

            all_products.extend(data)
            total_pages = int(r.headers.get("X-WP-TotalPages", 1))
            if page >= total_pages:
                break
            page += 1
            await asyncio.sleep(0.5)

        except Exception:
            break

    return len(all_products) > 0, all_products


def parse_store_api_product(p: dict) -> dict:
    """Parse wc/store/v1 product format."""
    raw_desc = p.get("description", "") or p.get("short_description", "") or ""
    description = strip_html(raw_desc)

    # Price
    try:
        price_str = p.get("prices", {}).get("price", "0") or "0"
        # Store API returns prices in minor units (cents) — divide by 100
        currency_minor = int(p.get("prices", {}).get("currency_minor_unit", 2))
        price = float(price_str) / (10 ** currency_minor)
    except (ValueError, TypeError):
        price = 0.0

    try:
        reg_str = p.get("prices", {}).get("regular_price", "0") or "0"
        reg_price = float(reg_str) / (10 ** currency_minor)
    except (ValueError, TypeError):
        reg_price = price

    # Images
    images_data = []
    for i, img in enumerate(p.get("images", [])):
        src = img.get("src", "") or img.get("url", "")
        if src:
            images_data.append({
                "position": i + 1,
                "alt_text": img.get("alt", "") or "",
                "original_src": src.split("?")[0],
            })

    # Tags & categories
    cats = [c.get("name", "") for c in p.get("categories", []) if c.get("name")]
    tags = ", ".join(t.get("name", "") for t in p.get("tags", []) if t.get("name"))

    # Variations — store/v1 only gives simple info
    in_stock = p.get("is_in_stock", True)
    variants_data = [{
        "shopify_variant_id": str(p.get("id", "")),
        "title": "Default",
        "sku": p.get("sku", "") or "",
        "price": price,
        "compare_at_price": reg_price if reg_price > price else None,
        "available": in_stock,
        "option1": "Default",
        "option2": None,
        "option3": None,
    }]

    return {
        "shopify_id": str(p.get("id", "")),
        "title": p.get("name", "") or "Untitled",
        "handle": p.get("slug", "") or str(p.get("id", "")),
        "product_type": cats[0] if cats else "",
        "vendor": "",
        "tags": tags,
        "description": description,
        "description_html": raw_desc,
        "option1_name": "Title",
        "option2_name": "",
        "option3_name": "",
        "price_min": price,
        "price_max": reg_price if reg_price > price else price,
        "created_at_shopify": "",
        "updated_at_shopify": "",
        "variants": variants_data,
        "images": images_data,
    }


# ── METHOD 2: WooCommerce REST API (needs Consumer Key + Secret) ─────────────

async def scrape_via_rest_api(base_url: str, client: httpx.AsyncClient,
                               ck: str, cs: str) -> tuple[bool, list]:
    """Uses /wp-json/wc/v3/products with Basic Auth."""
    all_products = []
    page = 1
    per_page = 100
    auth = (ck, cs)

    for api_ver in ["wc/v3", "wc/v2"]:
        all_products = []
        page = 1
        while True:
            url = f"{base_url}/wp-json/{api_ver}/products?per_page={per_page}&page={page}&status=publish"
            try:
                for attempt in range(3):
                    r = await client.get(url, auth=auth, timeout=20)
                    if r.status_code == 429:
                        await asyncio.sleep(3 * (attempt + 1))
                        continue
                    break

                if r.status_code not in (200, 201):
                    break

                data = r.json()
                if not isinstance(data, list) or not data:
                    break

                all_products.extend(data)
                total_pages = int(r.headers.get("X-WP-TotalPages", 1))
                if page >= total_pages:
                    break
                page += 1
                await asyncio.sleep(0.5)

            except Exception:
                break

        if all_products:
            return True, all_products

    return False, []


def parse_rest_api_product(p: dict) -> dict:
    """Parse wc/v3 product format."""
    raw_html = p.get("description", "") or p.get("short_description", "") or ""
    description = strip_html(raw_html)

    try:
        price = float(p.get("price", 0) or 0)
    except (ValueError, TypeError):
        price = 0.0
    try:
        reg_price = float(p.get("regular_price", 0) or 0)
        sale_price = float(p.get("sale_price", 0) or 0)
    except (ValueError, TypeError):
        reg_price = price
        sale_price = 0.0

    images_data = []
    for i, img in enumerate(p.get("images", [])):
        src = img.get("src", "")
        if src:
            images_data.append({
                "position": i + 1,
                "alt_text": img.get("alt", "") or img.get("name", "") or "",
                "original_src": src.split("?")[0],
            })

    cats = [c.get("name", "") for c in p.get("categories", []) if c.get("name")]
    tags = ", ".join(t.get("name", "") for t in p.get("tags", []) if t.get("name"))
    in_stock = p.get("stock_status", "instock") == "instock"

    variants_data = [{
        "shopify_variant_id": str(p.get("id", "")),
        "title": "Default",
        "sku": p.get("sku", "") or "",
        "price": sale_price if sale_price > 0 else price,
        "compare_at_price": reg_price if sale_price > 0 and reg_price > sale_price else None,
        "available": in_stock,
        "option1": "Default",
        "option2": None,
        "option3": None,
    }]

    return {
        "shopify_id": str(p.get("id", "")),
        "title": p.get("name", "") or "Untitled",
        "handle": p.get("slug", "") or str(p.get("id", "")),
        "product_type": cats[0] if cats else "",
        "vendor": "",
        "tags": tags,
        "description": description,
        "description_html": raw_html,
        "option1_name": "Title",
        "option2_name": "",
        "option3_name": "",
        "price_min": sale_price if sale_price > 0 else price,
        "price_max": reg_price if reg_price > 0 else price,
        "created_at_shopify": p.get("date_created", ""),
        "updated_at_shopify": p.get("date_modified", ""),
        "variants": variants_data,
        "images": images_data,
    }


# ── METHOD 3: HTML sitemap scraping (pure fallback — no API needed) ──────────

async def scrape_via_html(base_url: str, client: httpx.AsyncClient,
                           update_fn) -> tuple[bool, list]:
    """
    Scrapes WooCommerce product pages by:
    1. Finding product URLs from /sitemap.xml, /product-sitemap.xml, or /shop page
    2. Extracting JSON-LD schema.org/Product structured data from each page
    """
    product_urls = set()

    # Step A — find product URLs from sitemaps
    sitemap_candidates = [
        f"{base_url}/wp-sitemap-posts-product-1.xml",
        f"{base_url}/product-sitemap.xml",
        f"{base_url}/sitemap_index.xml",
        f"{base_url}/sitemap.xml",
    ]
    for sm_url in sitemap_candidates:
        try:
            r = await client.get(sm_url, timeout=15)
            if r.status_code == 200 and "xml" in r.headers.get("content-type", ""):
                # Extract all <loc> URLs that look like product pages
                locs = re.findall(r"<loc>(https?://[^<]+)</loc>", r.text)
                for loc in locs:
                    # If this is a sitemap index, note sub-sitemaps
                    if "sitemap" in loc.lower() and loc.endswith(".xml"):
                        try:
                            sub = await client.get(loc, timeout=10)
                            if sub.status_code == 200:
                                sub_locs = re.findall(r"<loc>(https?://[^<]+)</loc>", sub.text)
                                for sl in sub_locs:
                                    if "/product/" in sl or "?product=" in sl:
                                        product_urls.add(sl)
                        except Exception:
                            pass
                    elif "/product/" in loc or "?product=" in loc:
                        product_urls.add(loc)
                if product_urls:
                    break
        except Exception:
            continue

    # Step B — if sitemap didn't work, scrape the /shop page for product links
    if not product_urls:
        for shop_path in ["/shop", "/store", "/products"]:
            try:
                r = await client.get(f"{base_url}{shop_path}", timeout=15)
                if r.status_code == 200:
                    found = re.findall(
                        rf'href="({re.escape(base_url)}/product/[^"]+)"', r.text
                    )
                    for f in found:
                        product_urls.add(f.split("?")[0])
                    # Try next page too
                    for pg in range(2, 6):
                        r2 = await client.get(f"{base_url}{shop_path}/page/{pg}/", timeout=10)
                        if r2.status_code != 200:
                            break
                        found2 = re.findall(
                            rf'href="({re.escape(base_url)}/product/[^"]+)"', r2.text
                        )
                        for f in found2:
                            product_urls.add(f.split("?")[0])
                        if not found2:
                            break
                if product_urls:
                    break
            except Exception:
                continue

    if not product_urls:
        return False, []

    update_fn("scraping", f"Found {len(product_urls)} product URLs via HTML. Scraping pages...", pct=18)

    # Step C — extract JSON-LD from each product page
    products = []
    for i, purl in enumerate(list(product_urls)[:500]):
        try:
            r = await client.get(purl, timeout=15)
            if r.status_code != 200:
                continue

            # Extract JSON-LD schema.org/Product
            jsonld_blocks = re.findall(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                r.text, re.DOTALL
            )
            for block in jsonld_blocks:
                try:
                    data = json.loads(block.strip())
                    # Handle @graph arrays
                    if isinstance(data, dict) and data.get("@graph"):
                        data = next((x for x in data["@graph"] if x.get("@type") == "Product"), None)
                    if not data:
                        continue
                    if isinstance(data, list):
                        data = next((x for x in data if x.get("@type") == "Product"), None)
                    if not data or data.get("@type") != "Product":
                        continue

                    # Extract product info from JSON-LD
                    name = data.get("name", "") or ""
                    desc = strip_html(data.get("description", "") or "")
                    slug = purl.rstrip("/").split("/")[-1]

                    # Price
                    price = 0.0
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    try:
                        price = float(offers.get("price", 0) or 0)
                    except (ValueError, TypeError):
                        price = 0.0

                    # Image
                    img_src = ""
                    img_data = data.get("image", "")
                    if isinstance(img_data, str):
                        img_src = img_data
                    elif isinstance(img_data, list):
                        img_src = img_data[0] if img_data else ""
                    elif isinstance(img_data, dict):
                        img_src = img_data.get("url", "") or img_data.get("@id", "")

                    images = []
                    if img_src:
                        images = [{"position": 1, "alt_text": name, "original_src": img_src.split("?")[0]}]

                    in_stock = str(offers.get("availability", "InStock")).lower() not in ("outofstock", "https://schema.org/outofstock")

                    products.append({
                        "shopify_id": str(i),
                        "title": name,
                        "handle": slug,
                        "product_type": "",
                        "vendor": str(data.get("brand", {}).get("name", "") if isinstance(data.get("brand"), dict) else ""),
                        "tags": "",
                        "description": desc,
                        "description_html": data.get("description", "") or "",
                        "option1_name": "Title",
                        "option2_name": "",
                        "option3_name": "",
                        "price_min": price,
                        "price_max": price,
                        "created_at_shopify": "",
                        "updated_at_shopify": "",
                        "variants": [{
                            "shopify_variant_id": str(i),
                            "title": "Default",
                            "sku": "",
                            "price": price,
                            "compare_at_price": None,
                            "available": in_stock,
                            "option1": "Default",
                            "option2": None,
                            "option3": None,
                        }],
                        "images": images,
                    })
                    break  # found a product schema, move on
                except Exception:
                    continue

        except Exception:
            continue

        await asyncio.sleep(0)

    return len(products) > 0, products


# ── Main WooCommerce scrape job ───────────────────────────────────────────────

async def run_woo_scrape_job(job_id: str, url: str, ck: str = "", cs: str = ""):
    job = woo_jobs[job_id]
    db = SessionLocal()

    def update(status, message, page=0, products=0, products_total=0,
               images_done=0, images_total=0, pct=0.0, current_product=""):
        job["events"].append({
            "status": status,
            "message": message,
            "page": page,
            "products_found": products,
            "products_total": products_total,
            "images_downloaded": images_done,
            "images_total": images_total,
            "percentage": round(pct, 1),
            "current_product": current_product,
        })

    domain = ""
    try:
        # Step 1 — resolve URL
        update("scraping", "Resolving store URL...", pct=1)
        norm_url = normalize_url(url)
        base_url = get_base(norm_url)
        domain = get_domain(norm_url)
        update("scraping", f"Detected domain: {domain}", pct=3)

        async with httpx.AsyncClient(
            follow_redirects=True, headers=HEADERS, timeout=30
        ) as client:

            # Step 2 — try all 3 methods
            raw_products = []
            parser_fn = None
            method_used = ""

            # Method 1: Public Store API (no auth)
            update("scraping", "Trying WooCommerce Store API (no auth required)...", pct=6)
            ok, raw_products = await scrape_via_store_api(base_url, client)
            if ok:
                parser_fn = parse_store_api_product
                method_used = "WooCommerce Store API (public)"
            else:
                # Method 2: REST API with keys (if provided)
                if ck and cs:
                    update("scraping", "Trying WooCommerce REST API with provided keys...", pct=10)
                    ok, raw_products = await scrape_via_rest_api(base_url, client, ck, cs)
                    if ok:
                        parser_fn = parse_rest_api_product
                        method_used = "WooCommerce REST API (with keys)"

                # Method 3: HTML sitemap scraping
                if not ok:
                    update("scraping", "Trying HTML product page scraping...", pct=12)
                    ok, raw_products = await scrape_via_html(base_url, client, update)
                    if ok:
                        parser_fn = lambda p: p  # already in our format
                        method_used = "HTML page scraping"

            if not ok or not raw_products:
                update("error",
                       "Could not scrape this store. Possible reasons:\n"
                       "1. Not a WooCommerce store\n"
                       "2. Store API is disabled\n"
                       "3. No product pages found in sitemap\n"
                       "Tip: Try providing Consumer Key + Secret in the API Keys section.")
                job["done"] = True
                db.close()
                return

        total_products = len(raw_products)
        update("scraping",
               f"Found {total_products} products via {method_used}. Saving...",
               products_total=total_products, pct=20)

        # Step 3 — clear old data
        existing = db.query(Store).filter(Store.domain == domain).first()
        if existing:
            img_folder = Path(f"{IMAGES_BASE}/{domain}")
            if img_folder.exists():
                import shutil
                shutil.rmtree(img_folder)
            db.delete(existing)
            db.commit()

        store = Store(domain=domain, name=domain, original_url=url, scrape_status="scraping")
        db.add(store)
        db.commit()
        db.refresh(store)
        store_id = store.id
        job["store_id"] = store_id

        # Step 4 — save products
        PROG_START, PROG_END = 20.0, 97.0
        step = (PROG_END - PROG_START) / max(total_products, 1)
        images_total_count = sum(len(p.get("images", [])) for p in raw_products)

        for idx, raw_p in enumerate(raw_products):
            if job.get("stopped"):
                update("stopped", f"Stopped after {idx} products.",
                       products=idx, products_total=total_products,
                       images_total=images_total_count,
                       pct=PROG_START + idx * step)
                break

            parsed = parser_fn(raw_p)
            title = parsed["title"]
            pct = PROG_START + idx * step

            update("scraping", f"[{idx+1}/{total_products}] {title}",
                   products=idx+1, products_total=total_products,
                   images_total=images_total_count,
                   pct=pct, current_product=title)

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
                db.add(Image(
                    product_id=product.id,
                    position=imgd["position"],
                    alt_text=imgd["alt_text"],
                    original_src=src.split("?")[0],
                    local_path=None,  # CDN URL only — no download
                ))

            db.commit()

            if (idx + 1) % 25 == 0:
                s = db.query(Store).filter(Store.id == store_id).first()
                if s:
                    s.total_products = idx + 1
                    db.commit()

            await asyncio.sleep(0)

        # Step 5 — finalize
        from sqlalchemy import func
        from models import Variant as V, Image as Img
        fp = db.query(func.count(Product.id)).filter(Product.store_id == store_id).scalar() or 0
        fv = db.query(func.count(V.id)).join(Product).filter(Product.store_id == store_id).scalar() or 0
        fi = db.query(func.count(Img.id)).join(Product).filter(Product.store_id == store_id).scalar() or 0

        was_stopped = job.get("stopped", False)
        s = db.query(Store).filter(Store.id == store_id).first()
        s.total_products = fp
        s.total_variants = fv
        s.total_images = fi
        s.scrape_status = "stopped" if was_stopped else "complete"
        s.scraped_at = datetime.utcnow()
        s.name = domain
        db.commit()

        msg = (f"{'Stopped' if was_stopped else 'Done'}! "
               f"{fp} products · {fv} variants · {fi} image URLs via {method_used}")
        update("stopped" if was_stopped else "complete", msg,
               products=fp, products_total=fp,
               images_total=images_total_count,
               pct=100 if not was_stopped else round(PROG_START + fp * step, 1))

    except Exception as e:
        update("error", f"Scrape failed: {str(e)}")
        try:
            if domain:
                s = db.query(Store).filter(Store.domain == domain).first()
                if s:
                    s.scrape_status = "error"
                    db.commit()
        except Exception:
            pass
    finally:
        job["done"] = True
        db.close()
