import asyncio
import csv
import functools
import httpx
import io
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, or_

from database import init_db, SessionLocal
from models import Store, Product, Variant, Image, ShopifyCredential, PublishJob
from scraper import jobs, run_scrape_job
from publisher import publish_jobs, run_publish_job
from woo_scraper import woo_jobs, run_woo_scrape_job

app = FastAPI(title="Shopify Store Scraper")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    init_db()
    images_dir = os.environ.get("IMAGES_PATH", "images")
    Path(images_dir).mkdir(parents=True, exist_ok=True)
    Path("static").mkdir(exist_ok=True)


app.mount("/static", StaticFiles(directory="static"), name="static")

_images_dir = os.environ.get("IMAGES_PATH", "images")
Path(_images_dir).mkdir(parents=True, exist_ok=True)
try:
    app.mount("/images", StaticFiles(directory=_images_dir), name="images")
except Exception:
    pass


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/app")
async def serve_app():
    return FileResponse("static/index.html")


# ─── Scrape ───────────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str


@app.post("/api/scrape")
async def start_scrape(req: ScrapeRequest):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"events": [], "done": False, "stopped": False, "store_id": None, "url": req.url}
    asyncio.create_task(run_scrape_job(job_id, req.url))
    await asyncio.sleep(0.05)
    return {"job_id": job_id}


@app.post("/api/scrape/{job_id}/stop")
async def stop_scrape(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    jobs[job_id]["stopped"] = True
    return {"detail": "Stop signal sent"}


@app.get("/api/progress/{job_id}")
async def sse_progress(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream():
        job = jobs[job_id]
        sent_index = 0
        yield ": keepalive\n\n"
        while sent_index < len(job["events"]):
            yield f"data: {json.dumps(job['events'][sent_index])}\n\n"
            sent_index += 1
            await asyncio.sleep(0)
        while True:
            current_len = len(job["events"])
            while sent_index < current_len:
                yield f"data: {json.dumps(job['events'][sent_index])}\n\n"
                sent_index += 1
                await asyncio.sleep(0)
            if job["done"] and sent_index >= len(job["events"]):
                yield f"data: {json.dumps({'status': '__done__'})}\n\n"
                break
            await asyncio.sleep(0.2)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ─── Stores ───────────────────────────────────────────────────────────────────

@app.get("/api/stores")
def get_stores():
    db = SessionLocal()
    try:
        stores = db.query(Store).order_by(Store.id.desc()).all()
        return [
            {
                "id": s.id,
                "name": s.name,
                "domain": s.domain,
                "original_url": s.original_url,
                "total_products": s.total_products,
                "total_variants": s.total_variants,
                "total_images": s.total_images,
                "scrape_status": s.scrape_status,
                "scraped_at": s.scraped_at.isoformat() if s.scraped_at else None,
            }
            for s in stores
        ]
    finally:
        db.close()


@app.delete("/api/stores/{store_id}")
def delete_store(store_id: int):
    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            raise HTTPException(status_code=404, detail="Store not found")
        domain = store.domain
        db.delete(store)
        db.commit()
        img_folder = Path(os.environ.get("IMAGES_PATH", "images")) / domain
        if img_folder.exists():
            shutil.rmtree(img_folder)
        return {"detail": "Store deleted"}
    finally:
        db.close()


# ─── Products ─────────────────────────────────────────────────────────────────

@app.get("/api/stores/{store_id}/products")
def get_products(
    store_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    search: Optional[str] = None,
    product_type: Optional[str] = None,
    vendor: Optional[str] = None,
    availability: Optional[str] = None,
    sort_by: Optional[str] = "newest",
):
    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            raise HTTPException(status_code=404, detail="Store not found")
        q = db.query(Product).filter(Product.store_id == store_id)
        if search:
            term = f"%{search}%"
            q = q.filter(or_(Product.title.ilike(term), Product.vendor.ilike(term),
                              Product.tags.ilike(term), Product.description.ilike(term)))
        if product_type:
            q = q.filter(Product.product_type == product_type)
        if vendor:
            q = q.filter(Product.vendor == vendor)
        if availability == "in_stock":
            sub = db.query(Variant.product_id).filter(Variant.available == True).distinct().subquery()
            q = q.filter(Product.id.in_(sub))
        elif availability == "out_of_stock":
            sub = db.query(Variant.product_id).filter(Variant.available == True).distinct().subquery()
            q = q.filter(~Product.id.in_(sub))
        if sort_by == "price_asc":
            q = q.order_by(Product.price_min.asc())
        elif sort_by == "price_desc":
            q = q.order_by(Product.price_max.desc())
        elif sort_by == "name_asc":
            q = q.order_by(Product.title.asc())
        else:
            q = q.order_by(Product.id.desc())
        total = q.count()
        products = q.offset((page - 1) * page_size).limit(page_size).all()
        result = []
        for p in products:
            first_image = db.query(Image).filter(Image.product_id == p.id).order_by(Image.position).first()
            in_stock = db.query(Variant).filter(Variant.product_id == p.id, Variant.available == True).first() is not None
            variant_count = db.query(func.count(Variant.id)).filter(Variant.product_id == p.id).scalar()
            result.append({
                "id": p.id, "title": p.title, "handle": p.handle, "vendor": p.vendor,
                "product_type": p.product_type, "tags": p.tags,
                "price_min": p.price_min, "price_max": p.price_max,
                "variant_count": variant_count, "in_stock": in_stock,
                "thumbnail": (first_image.original_src or "").split("?")[0] if first_image else None,
            })
        return {"total": total, "page": page, "page_size": page_size,
                "pages": (total + page_size - 1) // page_size, "products": result}
    finally:
        db.close()


@app.get("/api/stores/{store_id}/products/{product_id}")
def get_product_detail(store_id: int, product_id: int):
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id, Product.store_id == store_id).first()
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        variants = db.query(Variant).filter(Variant.product_id == product_id).all()
        images = db.query(Image).filter(Image.product_id == product_id).order_by(Image.position).all()
        return {
            "id": product.id, "title": product.title, "handle": product.handle,
            "vendor": product.vendor, "product_type": product.product_type,
            "tags": product.tags, "description": product.description,
            "price_min": product.price_min, "price_max": product.price_max,
            "created_at_shopify": product.created_at_shopify,
            "updated_at_shopify": product.updated_at_shopify,
            "scraped_at": product.scraped_at.isoformat() if product.scraped_at else None,
            "variants": [{"id": v.id, "title": v.title, "sku": v.sku, "price": v.price,
                          "compare_at_price": v.compare_at_price, "available": v.available,
                          "option1": v.option1, "option2": v.option2, "option3": v.option3}
                         for v in variants],
            "images": [{"id": i.id, "position": i.position, "alt_text": i.alt_text,
                        "original_src": i.original_src, "local_path": i.local_path}
                       for i in images],
        }
    finally:
        db.close()


# ─── Stats ────────────────────────────────────────────────────────────────────

@app.get("/api/stores/{store_id}/stats")
def get_store_stats(store_id: int):
    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            raise HTTPException(status_code=404, detail="Store not found")
        total_products = db.query(func.count(Product.id)).filter(Product.store_id == store_id).scalar() or 0
        total_variants = db.query(func.count(Variant.id)).join(Product).filter(Product.store_id == store_id).scalar() or 0
        total_images = db.query(func.count(Image.id)).join(Product).filter(Product.store_id == store_id).scalar() or 0
        avg_price = db.query(func.avg(Product.price_min)).filter(Product.store_id == store_id).scalar() or 0
        min_price = db.query(func.min(Product.price_min)).filter(Product.store_id == store_id).scalar() or 0
        max_price = db.query(func.max(Product.price_max)).filter(Product.store_id == store_id).scalar() or 0
        vendors = db.query(Product.vendor).filter(Product.store_id == store_id, Product.vendor != None, Product.vendor != "").distinct().all()
        product_types = db.query(Product.product_type).filter(Product.store_id == store_id, Product.product_type != None, Product.product_type != "").distinct().all()
        return {
            "total_products": total_products, "total_variants": total_variants,
            "total_images": total_images, "avg_price": round(float(avg_price), 2),
            "min_price": round(float(min_price), 2), "max_price": round(float(max_price), 2),
            "vendors": [v[0] for v in vendors if v[0]],
            "product_types": [pt[0] for pt in product_types if pt[0]],
        }
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# ─── EXPORT HELPERS (shared, module-level, called via run_in_executor) ────────
# ═══════════════════════════════════════════════════════════════════════════════

EXPORT_HEADERS = [
    "Title", "URL handle", "Description", "Vendor", "Product category",
    "Type", "Tags", "Published on online store", "Status",
    "SKU", "Barcode", "Option1 name", "Option1 value", "Option1 Linked To",
    "Option2 name", "Option2 value", "Option2 Linked To",
    "Option3 name", "Option3 value", "Option3 Linked To",
    "Price", "Compare-at price", "Cost per item", "Charge tax", "Tax code",
    "Unit price total measure", "Unit price total measure unit",
    "Unit price base measure", "Unit price base measure unit",
    "Inventory tracker", "Inventory quantity", "Continue selling when out of stock",
    "Weight value (grams)", "Weight unit for display", "Requires shipping",
    "Fulfillment service", "Product image URL", "Image position", "Image alt text",
    "Variant image URL", "Gift card", "SEO title", "SEO description",
    "Color (product.metafields.shopify.color-pattern)",
    "Google Shopping / Google product category", "Google Shopping / Gender",
    "Google Shopping / Age group", "Google Shopping / Manufacturer part number (MPN)",
    "Google Shopping / Ad group name", "Google Shopping / Ads labels",
    "Google Shopping / Condition", "Google Shopping / Custom product",
    "Google Shopping / Custom label 0", "Google Shopping / Custom label 1",
    "Google Shopping / Custom label 2", "Google Shopping / Custom label 3",
    "Google Shopping / Custom label 4",
]
_EMPTY_ROW = [""] * len(EXPORT_HEADERS)
_MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB

BRAND_FIX = {
    "Herm*s": "Hermès", "Hermes": "Hermès",
    "Balenc*aga": "Balenciaga", "Balenci*ga": "Balenciaga",
    "Goy*rd": "Goyard", "Guc*i": "Gucci", "F*ndi": "Fendi",
    "Christi*n Di*r": "Christian Dior", "Christ*an Dior": "Christian Dior",
    "Christ*an Loubout*n": "Christian Louboutin", "Christian Loubout*n": "Christian Louboutin",
    "Chan*l": "Chanel", "Cel*ne": "Céline", "Carti*r": "Cartier",
    "Bvlg*ri": "Bvlgari", "Bott*ga Ven*ta": "Bottega Veneta",
    "Burb*rry": "Burberry", "Burber*y": "Burberry",
    "Audem*rs Pigu*t": "Audemars Piguet", "Am*ri": "Amiri",
    "Alexand*r McQue*n": "Alexander McQueen", "Lou*s Vuitt*n": "Louis Vuitton",
    "Sai*t Laure*t / YLS": "Saint Laurent", "Saint Laure*t": "Saint Laurent",
    "Van Cle*f": "Van Cleef", "Rol*x": "Rolex",
    "Ri*k Ow*ns": "Rick Owens", "Rich*rd Mil*e": "Richard Mille",
    "Pr*da": "Prada", "Pat*k Philip*e": "Patek Philippe",
    "Off Wh*te": "Off-White", "Lo*we": "Loewe", "Hubl*t": "Hublot",
    "Mess*ka": "Messika", "MiuM*u": "Miu Miu", "Chaum*t": "Chaumet",
    "Chl*e": "Chloé", "Fr*d": "Fred", "Valent*no": "Valentino",
    "Boucher*n": "Boucheron", "Loro Pi*na": "Loro Piana",
    "AL*IA": "ALAIA", "Chop*rd": "Chopard", "Piag*t": "Piaget",
    "Quel*n": "Quelân",
}


def _fix_vendor(raw: str) -> str:
    if not raw:
        return ""
    if raw in BRAND_FIX:
        return BRAND_FIX[raw]
    for censored, real in BRAND_FIX.items():
        pattern = re.escape(censored).replace(r'\*', r'[^a-zA-Z]*')
        if re.fullmatch(pattern, raw, flags=re.IGNORECASE):
            return real
    return raw


def _fmt_price(val) -> str:
    if val is None:
        return ""
    try:
        f = float(val)
        return f"{f:.2f}" if f > 0 else ""
    except Exception:
        return ""


def _clean_url(url: str) -> str:
    return url.split("?")[0] if url else ""


def _shopify_category(product_type: str, title: str, tags: str) -> str:
    if not product_type:
        return ""
    pt, ti, ta = product_type.lower(), (title or "").lower(), (tags or "").lower()
    MAP = {
        "bag": "Apparel & Accessories > Handbags, Wallets & Cases > Handbags",
        "tote": "Apparel & Accessories > Handbags, Wallets & Cases > Tote Bags",
        "backpack": "Apparel & Accessories > Handbags, Wallets & Cases > Backpacks",
        "wallet": "Apparel & Accessories > Handbags, Wallets & Cases > Wallets & Money Clips",
        "shoe": "Apparel & Accessories > Shoes",
        "sneaker": "Apparel & Accessories > Shoes",
        "boot": "Apparel & Accessories > Shoes > Boots",
        "heel": "Apparel & Accessories > Shoes > Heels",
        "sandal": "Apparel & Accessories > Shoes > Sandals",
        "jewelry": "Apparel & Accessories > Jewelry",
        "watch": "Apparel & Accessories > Jewelry > Watches",
        "belt": "Apparel & Accessories > Clothing Accessories > Belts",
        "scarf": "Apparel & Accessories > Clothing Accessories > Scarves & Wraps",
        "sunglasses": "Apparel & Accessories > Clothing Accessories > Sunglasses",
        "hat": "Apparel & Accessories > Clothing Accessories > Hats",
        "coat": "Apparel & Accessories > Clothing > Outerwear",
        "jacket": "Apparel & Accessories > Clothing > Outerwear",
        "dress": "Apparel & Accessories > Clothing > Dresses",
        "skirt": "Apparel & Accessories > Clothing > Skirts",
        "pants": "Apparel & Accessories > Clothing > Pants",
        "shirt": "Apparel & Accessories > Clothing > Shirts & Tops",
        "top": "Apparel & Accessories > Clothing > Shirts & Tops",
        "sweater": "Apparel & Accessories > Clothing > Sweaters",
    }
    for kw, cat in MAP.items():
        if kw in pt or kw in ti or kw in ta:
            return cat
    return ""


def _clean_desc(html: str, whatsapp: str, return_policy: str) -> str:
    if not html:
        return ""
    
    # Replace ALL phone numbers with your WhatsApp number
    # Matches formats: +1(236)314-8438, +1 236 314 8438, 236-314-8438, etc.
    html = re.sub(
        r'[\+]?[\d\s\(\)\-\.]{10,20}',
        f'+{whatsapp}',
        html, flags=re.IGNORECASE)
    
    # Replace WhatsApp links and contact info
    if whatsapp:
        html = re.sub(
            r'<a[^>]*href=["\'][^"\']*(?:wa\.me|whatsapp|contact)[^"\']*["\'][^>]*>.*?</a>',
            f'<a href="https://wa.me/{whatsapp}">Contact us on WhatsApp</a>',
            html, flags=re.IGNORECASE)
        html = re.sub(
            r'(?:chat with|contact)(?:\s+our)?\s+(?:support team|team|us)(?:\s+on)?\s+whatsapp',
            f'<a href="https://wa.me/{whatsapp}">Contact us on WhatsApp</a>',
            html, flags=re.IGNORECASE)
        
        # Replace email addresses
        html = re.sub(
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
            f'<a href="https://wa.me/{whatsapp}">Contact us on WhatsApp</a>',
            html)
        
        # Replace "call us" or "phone" mentions with WhatsApp link
        html = re.sub(
            r'(?:call|phone|contact|reach)\s+(?:us|me)(?:\s+at)?',
            f'<a href="https://wa.me/{whatsapp}">Contact us on WhatsApp</a>',
            html, flags=re.IGNORECASE)
    
    # Remove shipping time links
    html = re.sub(r'shipping\s+time\s*\(?\s*click\s+here\s*\)?', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<a[^>]*>[^<]*shipping\s+time[^<]*</a>', '', html, flags=re.IGNORECASE)
    
    # Replace return policy
    if return_policy:
        html = re.sub(r'within\s+1\s+month', return_policy, html, flags=re.IGNORECASE)
        html = re.sub(r'(?:1|one)\s+month', return_policy, html, flags=re.IGNORECASE)
    
    # Clean up empty paragraphs and extra whitespace
    html = re.sub(r'<p[^>]*>\s*</p>', '', html)
    html = re.sub(r'\s+', ' ', html)
    
    return html.strip()


def _product_rows_bytes(p, variants, images, vendor_override, price_adjust, inventory,
                         whatsapp, return_policy, status, price_adjust_type="percentage") -> bytes:
    """Build all CSV rows for one product. Pure Python – safe to run in thread."""
    body = _clean_desc(p.description_html or p.description or "", whatsapp, return_policy)
    pv = vendor_override if vendor_override else _fix_vendor(p.vendor or "")
    pt = p.product_type or ""
    sc = _shopify_category(pt, p.title or "", p.tags or "")
    fi = images[0] if images else None
    fi_url = _clean_url(fi.original_src) if fi else ""
    fi_alt = (fi.alt_text or p.title or "") if fi else ""
    o1 = p.option1_name or "Title"
    o2 = p.option2_name or ""
    o3 = p.option3_name or ""

    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    for vi, v in enumerate(variants):
        row = list(_EMPTY_ROW)
        if vi == 0:
            row[0] = p.title or ""
            row[1] = p.handle or ""
            row[2] = body
            row[3] = pv
            row[4] = sc
            row[5] = pt
            row[6] = p.tags or ""
            row[7] = "TRUE"
            row[8] = status
        else:
            row[1] = p.handle or ""
        row[9] = v.sku or ""
        row[11] = o1 if vi == 0 else ""
        row[12] = v.option1 or "Default Title"
        row[14] = o2 if vi == 0 else ""
        row[15] = v.option2 or ""
        row[17] = o3 if vi == 0 else ""
        row[18] = v.option3 or ""
        
        # Apply price adjustment based on type
        if price_adjust_type == "fixed":
            rp = (v.price or 0) + price_adjust if price_adjust != 0 else (v.price or 0)
            rc = (v.compare_at_price + price_adjust) if (v.compare_at_price and price_adjust != 0) else v.compare_at_price
        else:  # percentage
            rp = (v.price or 0) * (1 + price_adjust / 100) if price_adjust != 0 else (v.price or 0)
            rc = (v.compare_at_price * (1 + price_adjust / 100)) if (v.compare_at_price and price_adjust != 0) else v.compare_at_price
        
        row[20] = _fmt_price(rp)
        row[21] = _fmt_price(rc)
        row[23] = "TRUE"
        row[29] = "shopify"
        row[30] = str(inventory) if inventory > 0 else str(getattr(v, "inventory_quantity", None) or 0)
        row[31] = "DENY"
        row[32] = "0"
        row[33] = "g"
        row[34] = "TRUE"
        row[35] = "manual"
        if vi == 0:
            row[36] = fi_url
            row[37] = "1" if fi else ""
            row[38] = fi_alt
        row[40] = "FALSE"
        w.writerow(row)
    for img in images[1:]:
        row = list(_EMPTY_ROW)
        row[1] = p.handle or ""
        row[36] = _clean_url(img.original_src)
        row[37] = str(img.position)
        row[38] = img.alt_text or p.title or ""
        w.writerow(row)
    data = buf.getvalue().encode("utf-8")
    buf.close()
    return data


def _new_csv_buf() -> io.BytesIO:
    """New BytesIO with UTF-8 BOM + header row."""
    f = io.BytesIO()
    f.write('\ufeff'.encode("utf-8"))
    hb = io.StringIO()
    csv.writer(hb, quoting=csv.QUOTE_ALL).writerow(EXPORT_HEADERS)
    f.write(hb.getvalue().encode("utf-8"))
    hb.close()
    return f


def _build_zip(store_domain, products, variants_map, images_map,
               vendor_override, price_adjust, inventory, whatsapp, return_policy, status,
               filename_prefix=None, price_adjust_type="percentage") -> bytes:
    """
    Build a ZIP of 15-MB-split CSV files. Runs synchronously – call via run_in_executor.
    filename_prefix: if set, used instead of store_domain for file naming.
    price_adjust_type: "percentage" or "fixed" - determines how price_adjust is applied.
    """
    import zipfile as _zf
    prefix = filename_prefix or store_domain
    zip_buf = io.BytesIO()
    file_index = 1
    cur = _new_csv_buf()
    count = 0

    with _zf.ZipFile(zip_buf, 'w', _zf.ZIP_DEFLATED) as zf:
        for p in products:
            variants = variants_map.get(p.id, [])
            images = images_map.get(p.id, [])
            if not variants:
                continue
            chunk = _product_rows_bytes(p, variants, images, vendor_override, price_adjust,
                                        inventory, whatsapp, return_policy, status, price_adjust_type)
            if cur.tell() + len(chunk) > _MAX_FILE_SIZE and count > 0:
                zf.writestr(f"{prefix}_part{file_index}.csv", cur.getvalue())
                file_index += 1
                cur = _new_csv_buf()
                count = 0
            cur.write(chunk)
            count += 1
        if count > 0:
            name = f"{prefix}_products.csv" if file_index == 1 else f"{prefix}_part{file_index}.csv"
            zf.writestr(name, cur.getvalue())

    return zip_buf.getvalue()


def _build_zip_by_category(store_domain, categories, products_by_cat, variants_map, images_map,
                            vendor_override, price_adjust, inventory, whatsapp, return_policy, status, price_adjust_type="percentage") -> bytes:
    """Build ZIP grouped by category. Runs synchronously – call via run_in_executor."""
    import zipfile as _zf
    zip_buf = io.BytesIO()

    with _zf.ZipFile(zip_buf, 'w', _zf.ZIP_DEFLATED) as zf:
        for cat in categories:
            products = products_by_cat.get(cat, [])
            if not products:
                continue
            safe = cat.replace("/", "-").replace("\\", "-").replace(" ", "_")
            file_index = 1
            cur = _new_csv_buf()
            count = 0
            for p in products:
                variants = variants_map.get(p.id, [])
                images = images_map.get(p.id, [])
                if not variants:
                    continue
                chunk = _product_rows_bytes(p, variants, images, vendor_override, price_adjust,
                                            inventory, whatsapp, return_policy, status, price_adjust_type)
                if cur.tell() + len(chunk) > _MAX_FILE_SIZE and count > 0:
                    zf.writestr(f"{safe}_part{file_index}.csv", cur.getvalue())
                    file_index += 1
                    cur = _new_csv_buf()
                    count = 0
                cur.write(chunk)
                count += 1
            if count > 0:
                name = f"{safe}.csv" if file_index == 1 else f"{safe}_part{file_index}.csv"
                zf.writestr(name, cur.getvalue())

    return zip_buf.getvalue()


# ─── Export Endpoints ─────────────────────────────────────────────────────────

@app.get("/api/stores/{store_id}/export/csv")
async def export_csv(
    store_id: int,
    vendor: Optional[str] = "Luxury Wardrobe",
    price_adjust: Optional[float] = -15,
    price_adjust_type: Optional[str] = "percentage",
    inventory: Optional[int] = 300,
    whatsapp: Optional[str] = "12363148438",
    return_policy: Optional[str] = "within 7 days",
    status: Optional[str] = "Active",
):
    """
    Export ALL products as split 15-MB CSVs inside a ZIP. Non-blocking.
    price_adjust_type: "percentage" or "fixed" - determines how price_adjust is applied.
    """
    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            raise HTTPException(status_code=404, detail="Store not found")

        products = db.query(Product).filter(Product.store_id == store_id).order_by(Product.id).all()
        pids = [p.id for p in products]

        all_variants = db.query(Variant).filter(Variant.product_id.in_(pids)).order_by(Variant.product_id, Variant.id).all()
        vm: dict = {}
        for v in all_variants:
            vm.setdefault(v.product_id, []).append(v)

        all_images = db.query(Image).filter(Image.product_id.in_(pids)).order_by(Image.product_id, Image.position).all()
        im: dict = {}
        for img in all_images:
            im.setdefault(img.product_id, []).append(img)

        domain = store.domain
        db.close()

        loop = asyncio.get_event_loop()
        zip_bytes = await loop.run_in_executor(
            None,
            functools.partial(_build_zip, domain, products, vm, im,
                              vendor or "", price_adjust or 0, inventory or 0,
                              whatsapp or "", return_policy or "", status or "Active",
                              None, price_adjust_type or "percentage")
        )
        return StreamingResponse(
            io.BytesIO(zip_bytes), media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{domain}_products.zip"'}
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try: db.close()
        except Exception: pass


@app.get("/api/stores/{store_id}/export/by-category")
async def export_by_category_split(
    store_id: int,
    vendor: Optional[str] = "Luxury Wardrobe",
    price_adjust: Optional[float] = -15,
    price_adjust_type: Optional[str] = "percentage",
    inventory: Optional[int] = 300,
    whatsapp: Optional[str] = "12363148438",
    return_policy: Optional[str] = "within 7 days",
    status: Optional[str] = "Active",
):
    """
    Export products grouped by category, each split at 15 MB. Non-blocking.
    price_adjust_type: "percentage" or "fixed" - determines how price_adjust is applied.
    """
    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            raise HTTPException(status_code=404, detail="Store not found")

        cats_raw = db.query(Product.product_type).filter(
            Product.store_id == store_id, Product.product_type != None, Product.product_type != ""
        ).distinct().all()
        categories = [c[0] for c in cats_raw if c[0]]

        products = db.query(Product).filter(Product.store_id == store_id).order_by(Product.id).all()
        pids = [p.id for p in products]

        all_variants = db.query(Variant).filter(Variant.product_id.in_(pids)).order_by(Variant.product_id, Variant.id).all()
        vm: dict = {}
        for v in all_variants:
            vm.setdefault(v.product_id, []).append(v)

        all_images = db.query(Image).filter(Image.product_id.in_(pids)).order_by(Image.product_id, Image.position).all()
        im: dict = {}
        for img in all_images:
            im.setdefault(img.product_id, []).append(img)

        by_cat: dict = {}
        for p in products:
            if p.product_type:
                by_cat.setdefault(p.product_type, []).append(p)

        domain = store.domain
        db.close()

        loop = asyncio.get_event_loop()
        zip_bytes = await loop.run_in_executor(
            None,
            functools.partial(_build_zip_by_category, domain, categories, by_cat, vm, im,
                              vendor or "", price_adjust or 0, inventory or 0,
                              whatsapp or "", return_policy or "", status or "Active",
                              price_adjust_type or "percentage")
        )
        return StreamingResponse(
            io.BytesIO(zip_bytes), media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{domain}_by_category.zip"'}
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try: db.close()
        except Exception: pass


@app.get("/api/stores/{store_id}/export/filtered-csv")
async def export_filtered_csv(
    store_id: int,
    search: Optional[str] = None,
    product_type: Optional[str] = None,
    vendor: Optional[str] = None,
    availability: Optional[str] = None,
    sort_by: Optional[str] = "newest",
    exp_vendor: Optional[str] = "Luxury Wardrobe",
    price_adjust: Optional[float] = -15,
    price_adjust_type: Optional[str] = "percentage",
    inventory: Optional[int] = 300,
    whatsapp: Optional[str] = "12363148438",
    return_policy: Optional[str] = "within 7 days",
    status: Optional[str] = "Active",
):
    """
    Export filtered products as split 15-MB CSVs inside a ZIP. Non-blocking.
    price_adjust_type: "percentage" or "fixed" - determines how price_adjust is applied.
    """
    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            raise HTTPException(status_code=404, detail="Store not found")

        q = db.query(Product).filter(Product.store_id == store_id)
        if search:
            term = f"%{search}%"
            q = q.filter(or_(Product.title.ilike(term), Product.vendor.ilike(term),
                             Product.tags.ilike(term), Product.description.ilike(term)))
        if product_type:
            q = q.filter(Product.product_type == product_type)
        if vendor:
            q = q.filter(Product.vendor == vendor)
        if availability == "in_stock":
            sub = db.query(Variant.product_id).filter(Variant.available == True).distinct().subquery()
            q = q.filter(Product.id.in_(sub))
        elif availability == "out_of_stock":
            sub = db.query(Variant.product_id).filter(Variant.available == True).distinct().subquery()
            q = q.filter(~Product.id.in_(sub))
        if sort_by == "price_asc":
            q = q.order_by(Product.price_min.asc())
        elif sort_by == "price_desc":
            q = q.order_by(Product.price_max.desc())
        elif sort_by == "name_asc":
            q = q.order_by(Product.title.asc())
        else:
            q = q.order_by(Product.id.desc())

        products = q.all()
        pids = [p.id for p in products]

        all_variants = db.query(Variant).filter(Variant.product_id.in_(pids)).order_by(Variant.product_id, Variant.id).all()
        vm: dict = {}
        for v in all_variants:
            vm.setdefault(v.product_id, []).append(v)

        all_images = db.query(Image).filter(Image.product_id.in_(pids)).order_by(Image.product_id, Image.position).all()
        im: dict = {}
        for img in all_images:
            im.setdefault(img.product_id, []).append(img)

        domain = store.domain
        db.close()

        # Build suffix for filename
        suffix = ""
        if search:
            suffix += f"_{search[:20]}"
        if product_type:
            suffix += f"_{product_type[:20]}"
        if vendor:
            suffix += f"_{vendor[:20]}"
        suffix = suffix.replace(" ", "-").replace("/", "-")
        prefix = f"{domain}{suffix}_filtered"

        loop = asyncio.get_event_loop()
        zip_bytes = await loop.run_in_executor(
            None,
            functools.partial(_build_zip, domain, products, vm, im,
                              exp_vendor or "", price_adjust or 0, inventory or 0,
                              whatsapp or "", return_policy or "", status or "Active",
                              prefix, price_adjust_type or "percentage")
        )
        return StreamingResponse(
            io.BytesIO(zip_bytes), media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{prefix}_products.zip"'}
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try: db.close()
        except Exception: pass


@app.get("/api/stores/{store_id}/export/json")
def export_json(store_id: int):
    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            raise HTTPException(status_code=404, detail="Store not found")
        products = db.query(Product).filter(Product.store_id == store_id).all()
        result = []
        for p in products:
            variants = db.query(Variant).filter(Variant.product_id == p.id).all()
            images = db.query(Image).filter(Image.product_id == p.id).order_by(Image.position).all()
            result.append({
                "id": p.id, "shopify_id": p.shopify_id, "title": p.title,
                "handle": p.handle, "vendor": p.vendor, "product_type": p.product_type,
                "tags": p.tags, "description": p.description,
                "price_min": p.price_min, "price_max": p.price_max,
                "variants": [{"title": v.title, "sku": v.sku, "price": v.price,
                               "compare_at_price": v.compare_at_price, "available": v.available,
                               "option1": v.option1, "option2": v.option2, "option3": v.option3}
                              for v in variants],
                "images": [{"position": i.position, "src": i.original_src, "alt": i.alt_text}
                           for i in images],
            })
        content = json.dumps(result, indent=2, ensure_ascii=False)
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")), media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{store.domain}_products.json"'},
        )
    finally:
        db.close()


# ─── Store Filters ────────────────────────────────────────────────────────────

@app.get("/api/stores/{store_id}/filters")
def get_store_filters(store_id: int):
    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            raise HTTPException(status_code=404, detail="Store not found")
        types = db.query(Product.product_type).filter(
            Product.store_id == store_id, Product.product_type != None, Product.product_type != ""
        ).distinct().all()
        vendors = db.query(Product.vendor).filter(
            Product.store_id == store_id, Product.vendor != None, Product.vendor != ""
        ).distinct().all()
        return {
            "product_types": [t[0] for t in types if t[0]],
            "vendors": [v[0] for v in vendors if v[0]],
        }
    finally:
        db.close()


# ─── Shopify Credentials ──────────────────────────────────────────────────────

class CredentialRequest(BaseModel):
    label: str
    shop_domain: str
    access_token: str


@app.post("/api/credentials")
def save_credential(req: CredentialRequest):
    db = SessionLocal()
    try:
        cred = ShopifyCredential(
            label=req.label,
            shop_domain=req.shop_domain.strip().lower(),
            access_token=req.access_token.strip(),
        )
        db.add(cred)
        db.commit()
        db.refresh(cred)
        return {"id": cred.id, "label": cred.label, "shop_domain": cred.shop_domain}
    finally:
        db.close()


@app.get("/api/credentials")
def list_credentials():
    db = SessionLocal()
    try:
        creds = db.query(ShopifyCredential).order_by(ShopifyCredential.id.desc()).all()
        return [{"id": c.id, "label": c.label, "shop_domain": c.shop_domain} for c in creds]
    finally:
        db.close()


@app.delete("/api/credentials/{cred_id}")
def delete_credential(cred_id: int):
    db = SessionLocal()
    try:
        cred = db.query(ShopifyCredential).filter(ShopifyCredential.id == cred_id).first()
        if not cred:
            raise HTTPException(status_code=404, detail="Credential not found")
        db.delete(cred)
        db.commit()
        return {"detail": "Deleted"}
    finally:
        db.close()


@app.post("/api/credentials/{cred_id}/test")
async def test_credential(cred_id: int):
    db = SessionLocal()
    try:
        cred = db.query(ShopifyCredential).filter(ShopifyCredential.id == cred_id).first()
        if not cred:
            raise HTTPException(status_code=404, detail="Credential not found")
        url = f"https://{cred.shop_domain}/admin/api/2024-01/shop.json"
        headers = {"X-Shopify-Access-Token": cred.access_token}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            shop_name = resp.json().get("shop", {}).get("name", cred.shop_domain)
            return {"success": True, "shop_name": shop_name}
        return {"success": False, "detail": f"HTTP {resp.status_code}"}
    finally:
        db.close()


# ─── Publisher ────────────────────────────────────────────────────────────────

class PublishRequest(BaseModel):
    source_store_id: int
    credential_id: int


@app.post("/api/publish")
async def start_publish(req: PublishRequest):
    db = SessionLocal()
    try:
        cred = db.query(ShopifyCredential).filter(ShopifyCredential.id == req.credential_id).first()
        if not cred:
            raise HTTPException(status_code=404, detail="Credential not found")
        pj = PublishJob(credential_id=req.credential_id, source_store_id=req.source_store_id, status="pending")
        db.add(pj)
        db.commit()
        db.refresh(pj)
        job_key = str(uuid.uuid4())
        publish_jobs[job_key] = {"events": [], "done": False, "stopped": False}
        asyncio.create_task(run_publish_job(job_key, pj.id, req.source_store_id, cred.shop_domain, cred.access_token))
        return {"job_key": job_key}
    finally:
        db.close()


@app.post("/api/publish/{job_key}/stop")
async def stop_publish(job_key: str):
    if job_key not in publish_jobs:
        raise HTTPException(status_code=404, detail="Publish job not found")
    publish_jobs[job_key]["stopped"] = True
    return {"detail": "Stop signal sent"}


@app.get("/api/publish/{job_key}/progress")
async def publish_progress(job_key: str):
    if job_key not in publish_jobs:
        raise HTTPException(status_code=404, detail="Publish job not found")

    async def event_stream():
        job = publish_jobs[job_key]
        sent_index = 0
        while True:
            while sent_index < len(job["events"]):
                yield f"data: {json.dumps(job['events'][sent_index])}\n\n"
                sent_index += 1
            if job["done"] and sent_index >= len(job["events"]):
                yield f"data: {json.dumps({'status': '__done__'})}\n\n"
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/publish/jobs")
def list_publish_jobs():
    db = SessionLocal()
    try:
        pjobs = db.query(PublishJob).order_by(PublishJob.id.desc()).limit(20).all()
        result = []
        for j in pjobs:
            cred = db.query(ShopifyCredential).filter(ShopifyCredential.id == j.credential_id).first()
            store = db.query(Store).filter(Store.id == j.source_store_id).first()
            result.append({
                "id": j.id, "status": j.status,
                "source_store": store.domain if store else "",
                "target_store": cred.shop_domain if cred else "",
                "total_products": j.total_products,
                "published_products": j.published_products,
                "failed_products": j.failed_products,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            })
        return result
    finally:
        db.close()


# ─── WooCommerce Scraper ──────────────────────────────────────────────────────

class WooScrapeRequest(BaseModel):
    url: str
    consumer_key: Optional[str] = ""
    consumer_secret: Optional[str] = ""


@app.post("/api/woo/scrape")
async def start_woo_scrape(req: WooScrapeRequest):
    job_id = str(uuid.uuid4())
    woo_jobs[job_id] = {"events": [], "done": False, "stopped": False, "store_id": None, "url": req.url}
    asyncio.create_task(run_woo_scrape_job(job_id, req.url, req.consumer_key or "", req.consumer_secret or ""))
    return {"job_id": job_id}


@app.post("/api/woo/scrape/{job_id}/stop")
async def stop_woo_scrape(job_id: str):
    if job_id not in woo_jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    woo_jobs[job_id]["stopped"] = True
    return {"detail": "Stop signal sent"}


@app.get("/api/woo/progress/{job_id}")
async def woo_sse_progress(job_id: str):
    if job_id not in woo_jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream():
        job = woo_jobs[job_id]
        sent_index = 0
        while True:
            while sent_index < len(job["events"]):
                yield f"data: {json.dumps(job['events'][sent_index])}\n\n"
                sent_index += 1
            if job["done"] and sent_index >= len(job["events"]):
                yield f"data: {json.dumps({'status': '__done__'})}\n\n"
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
