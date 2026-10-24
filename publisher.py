"""
Publisher Agent — pushes scraped products to a target Shopify store
via the Shopify Admin REST API.

Flow per product:
  1. Build product payload (title, body_html, vendor, product_type, tags, variants)
  2. POST /admin/api/2024-01/products.json
  3. For each image: POST /admin/api/2024-01/products/{id}/images.json
     (uses original CDN src so no re-upload needed)
  4. Respect Shopify rate limit: 2 req/sec (leaky bucket)
"""

import asyncio
import httpx
from datetime import datetime
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Store, Product, Variant, Image, PublishJob

# Global publish job store for SSE progress
publish_jobs: dict[str, dict] = {}

SHOPIFY_API_VERSION = "2024-01"
RATE_LIMIT_DELAY = 0.6   # seconds between requests (~1.6 req/s, safely under 2/s limit


async def _shopify_request(
    client: httpx.AsyncClient,
    method: str,
    shop_domain: str,
    access_token: str,
    path: str,
    payload: dict | None = None,
) -> tuple[int, dict]:
    """Make a single Shopify Admin API request with rate-limit awareness."""
    url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/{path}"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    for attempt in range(4):
        try:
            if method == "POST":
                resp = await client.post(url, json=payload, headers=headers, timeout=30)
            elif method == "PUT":
                resp = await client.put(url, json=payload, headers=headers, timeout=30)
            else:
                resp = await client.get(url, headers=headers, timeout=30)

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                await asyncio.sleep(retry_after)
                continue

            return resp.status_code, resp.json() if resp.content else {}
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == 3:
                return 0, {"error": "Network error after retries"}
            await asyncio.sleep(2 ** attempt)

    return 0, {"error": "Max retries exceeded"}


def _build_product_payload(product: Product, variants: list[Variant]) -> dict:
    """Build the Shopify product creation payload from DB records."""
    # Build variants list
    shopify_variants = []
    for v in variants:
        vd = {
            "price": str(round(v.price or 0, 2)),
            "inventory_management": None,
            "inventory_policy": "continue",
        }
        if v.compare_at_price and v.compare_at_price > 0:
            vd["compare_at_price"] = str(round(v.compare_at_price, 2))
        if v.sku:
            vd["sku"] = v.sku
        if v.title and v.title != "Default Title":
            vd["title"] = v.title
        if v.option1:
            vd["option1"] = v.option1
        if v.option2:
            vd["option2"] = v.option2
        if v.option3:
            vd["option3"] = v.option3
        shopify_variants.append(vd)

    payload = {
        "product": {
            "title": product.title or "Untitled Product",
            "body_html": product.description or "",
            "vendor": product.vendor or "",
            "product_type": product.product_type or "",
            "tags": product.tags or "",
            "status": "active",
            "variants": shopify_variants if shopify_variants else [{"price": "0.00"}],
        }
    }
    return payload


async def run_publish_job(job_key: str, job_id: int, store_id: int,
                          shop_domain: str, access_token: str):
    """Main publisher coroutine — pushes all products to target Shopify store."""
    job = publish_jobs[job_key]
    db: Session = SessionLocal()

    def update(status: str, message: str, published: int = 0, total: int = 0,
               failed: int = 0, pct: float = 0, current: str = ""):
        job["events"].append({
            "status": status,
            "message": message,
            "published": published,
            "total": total,
            "failed": failed,
            "percentage": round(pct, 1),
            "current_product": current,
        })

    try:
        # Load publish job record
        pj = db.query(PublishJob).filter(PublishJob.id == job_id).first()
        pj.status = "running"
        pj.started_at = datetime.utcnow()
        db.commit()

        # Load all products for the source store
        products = db.query(Product).filter(Product.store_id == store_id).all()
        total = len(products)
        pj.total_products = total
        db.commit()

        update("publishing", f"Starting to publish {total} products to {shop_domain}...",
               total=total, pct=1)

        if total == 0:
            update("complete", "No products to publish.", total=0, pct=100)
            pj.status = "complete"
            pj.finished_at = datetime.utcnow()
            db.commit()
            job["done"] = True
            db.close()
            return

        # Verify credentials first
        update("publishing", "Verifying Shopify credentials...", total=total, pct=2)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            status_code, resp_data = await _shopify_request(
                client, "GET", shop_domain, access_token, "shop.json"
            )
        if status_code == 401:
            update("error", "Invalid credentials — check your access token.", total=total, pct=0)
            pj.status = "error"
            pj.error_log = "401 Unauthorized"
            pj.finished_at = datetime.utcnow()
            db.commit()
            job["done"] = True
            db.close()
            return
        if status_code == 404:
            update("error", f"Store not found: {shop_domain}", total=total, pct=0)
            pj.status = "error"
            pj.finished_at = datetime.utcnow()
            db.commit()
            job["done"] = True
            db.close()
            return
        if status_code not in (200, 201):
            update("error", f"Could not connect to store (HTTP {status_code})", total=total, pct=0)
            pj.status = "error"
            pj.finished_at = datetime.utcnow()
            db.commit()
            job["done"] = True
            db.close()
            return

        shop_name = resp_data.get("shop", {}).get("name", shop_domain)
        update("publishing", f"Connected to '{shop_name}'. Publishing products...",
               total=total, pct=3)

        published = 0
        failed = 0
        errors = []

        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            for idx, product in enumerate(products):

                # Check stop signal
                if job.get("stopped"):
                    update("stopped",
                           f"Stopped by user. Published {published}/{total} products.",
                           published=published, total=total, failed=failed,
                           pct=round(3 + idx / total * 95, 1))
                    break

                pct = 3 + (idx / total) * 95
                update("publishing",
                       f"[{idx+1}/{total}] Publishing: {product.title}",
                       published=published, total=total, failed=failed,
                       pct=pct, current=product.title or "")

                # Load variants and images
                variants = db.query(Variant).filter(Variant.product_id == product.id).all()
                images = db.query(Image).filter(Image.product_id == product.id)\
                            .order_by(Image.position).all()

                # Create product
                payload = _build_product_payload(product, variants)
                await asyncio.sleep(RATE_LIMIT_DELAY)
                sc, resp = await _shopify_request(
                    client, "POST", shop_domain, access_token, "products.json", payload
                )

                if sc not in (200, 201):
                    err = resp.get("errors", str(resp))
                    errors.append(f"{product.title}: {err}")
                    failed += 1
                    update("publishing",
                           f"[{idx+1}/{total}] FAILED: {product.title} — {err}",
                           published=published, total=total, failed=failed,
                           pct=pct, current=product.title or "")
                    continue

                new_product_id = resp.get("product", {}).get("id")

                # Upload images (use original CDN URL — fastest, no re-download)
                for img in images:
                    src = img.original_src
                    if not src:
                        continue
                    img_payload = {
                        "image": {
                            "src": src,
                            "position": img.position,
                            "alt": img.alt_text or "",
                        }
                    }
                    await asyncio.sleep(RATE_LIMIT_DELAY)
                    await _shopify_request(
                        client, "POST", shop_domain, access_token,
                        f"products/{new_product_id}/images.json", img_payload
                    )

                published += 1
                # Update DB record every 10 products
                if published % 10 == 0:
                    pj.published_products = published
                    pj.failed_products = failed
                    db.commit()

        # Finalize
        was_stopped = job.get("stopped", False)
        final_status = "stopped" if was_stopped else "complete"
        pj.status = final_status
        pj.published_products = published
        pj.failed_products = failed
        pj.finished_at = datetime.utcnow()
        if errors:
            pj.error_log = "\n".join(errors[:50])
        db.commit()

        msg = (f"{'Stopped' if was_stopped else 'Done'}! "
               f"Published {published}/{total} products to {shop_domain}."
               + (f" {failed} failed." if failed else ""))
        final_pct = round(3 + published / total * 95, 1) if was_stopped else 100
        update(
            final_status, msg,
            published=published, total=total, failed=failed, pct=final_pct
        )

    except Exception as e:
        update("error", f"Publish failed: {str(e)}")
        try:
            pj = db.query(PublishJob).filter(PublishJob.id == job_id).first()
            if pj:
                pj.status = "error"
                pj.error_log = str(e)
                pj.finished_at = datetime.utcnow()
                db.commit()
        except Exception:
            pass
    finally:
        job["done"] = True
        db.close()
