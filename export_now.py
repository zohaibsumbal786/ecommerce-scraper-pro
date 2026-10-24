#!/usr/bin/env python3
"""
Direct export script - exports luxurydealsdirect.com products to CSV files
Split at 15MB, all modifications applied
"""

import sys
import csv
import io
import re
from pathlib import Path
from models import Store, Product, Variant, Image
from database import SessionLocal

# Store ID for luxurydealsdirect.com
STORE_ID = 18
OUTPUT_DIR = Path("./exported_csvs")

# Shopify CSV Headers (57 columns)
HEADERS = [
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

NUM_COLS = len(HEADERS)
EMPTY = [""] * NUM_COLS
MAX_FILE_SIZE = 15 * 1024 * 1024  # 15MB in bytes

def fmt_price(val):
    """Format price value"""
    if val is None: return ""
    try:
        f = float(val)
        return f"{f:.2f}" if f > 0 else ""
    except: 
        return ""

def clean_img_url(url: str) -> str:
    """Remove query parameters from image URL"""
    return url.split("?")[0] if url else ""

def map_to_shopify_category(product_type: str, title: str, tags: str) -> str:
    """Map product type to Shopify Standard Product Taxonomy"""
    if not product_type: 
        return ""
    
    product_type_lower = product_type.lower()
    title_lower = (title or "").lower()
    tags_lower = (tags or "").lower()
    
    category_map = {
        "bag": "Apparel & Accessories > Handbags, Wallets & Cases > Handbags",
        "tote": "Apparel & Accessories > Handbags, Wallets & Cases > Tote Bags",
        "backpack": "Apparel & Accessories > Handbags, Wallets & Cases > Backpacks",
        "wallet": "Apparel & Accessories > Handbags, Wallets & Cases > Wallets & Money Clips",
        "purse": "Apparel & Accessories > Handbags, Wallets & Cases > Handbags",
        "clutch": "Apparel & Accessories > Handbags, Wallets & Cases > Clutches",
        "shoe": "Apparel & Accessories > Shoes",
        "sneaker": "Apparel & Accessories > Shoes > Athletic Shoes",
        "boot": "Apparel & Accessories > Shoes > Boots",
        "sandal": "Apparel & Accessories > Shoes > Sandals",
        "heel": "Apparel & Accessories > Shoes > Heels",
        "jewelry": "Apparel & Accessories > Jewelry",
        "necklace": "Apparel & Accessories > Jewelry > Necklaces",
        "bracelet": "Apparel & Accessories > Jewelry > Bracelets",
        "ring": "Apparel & Accessories > Jewelry > Rings",
        "earring": "Apparel & Accessories > Jewelry > Earrings",
        "watch": "Apparel & Accessories > Jewelry > Watches",
        "dress": "Apparel & Accessories > Clothing > Dresses",
        "shirt": "Apparel & Accessories > Clothing > Shirts & Tops",
        "pant": "Apparel & Accessories > Clothing > Pants",
        "jean": "Apparel & Accessories > Clothing > Pants > Jeans",
        "jacket": "Apparel & Accessories > Clothing > Outerwear > Coats & Jackets",
        "coat": "Apparel & Accessories > Clothing > Outerwear > Coats & Jackets",
        "sweater": "Apparel & Accessories > Clothing > Shirts & Tops > Sweaters",
        "hoodie": "Apparel & Accessories > Clothing > Shirts & Tops > Hoodies",
        "skirt": "Apparel & Accessories > Clothing > Skirts",
        "shorts": "Apparel & Accessories > Clothing > Shorts",
    }
    
    for keyword, category in category_map.items():
        if keyword in product_type_lower or keyword in title_lower or keyword in tags_lower:
            return category
    
    return ""

def resolve_vendor(vendor: str, tags: str) -> str:
    """Always return 'Luxury Wardrobe' as vendor"""
    return "Luxury Wardrobe"

def clean_description(html: str) -> str:
    """Clean and modify product description"""
    if not html: 
        return ""
    
    # Replace WhatsApp links
    html = re.sub(
        r'<a[^>]*href=["\'][^"\']*(?:wa\.me|whatsapp|contact)[^"\']*["\'][^>]*>.*?</a>',
        '<a href="https://wa.me/12363148438">Contact us on WhatsApp</a>',
        html, flags=re.IGNORECASE
    )
    
    # Replace WhatsApp text
    html = re.sub(
        r'(?:chat with|contact)(?:\s+our)?\s+(?:support team|team|us)(?:\s+on)?\s+whatsapp',
        '<a href="https://wa.me/12363148438">Contact us on WhatsApp</a>',
        html, flags=re.IGNORECASE
    )
    
    # Remove shipping time
    html = re.sub(r'shipping\s+time\s*\(?\s*click\s+here\s*\)?', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<a[^>]*>[^<]*shipping\s+time[^<]*</a>', '', html, flags=re.IGNORECASE)
    
    # Replace return policy
    html = re.sub(r'within\s+1\s+month', 'within 7 days', html, flags=re.IGNORECASE)
    html = re.sub(r'(?:1|one)\s+month', '7 days', html, flags=re.IGNORECASE)
    
    # Clean empty tags
    html = re.sub(r'<p[^>]*>\s*</p>', '', html)
    html = re.sub(r'\s+', ' ', html)
    
    return html.strip()

def export_store():
    """Export all products from store to CSV files"""
    print("=" * 80)
    print("EXPORTING LUXURYDEALSDIRECT.COM PRODUCTS")
    print("=" * 80)
    
    # Create output directory
    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"\n✓ Output directory: {OUTPUT_DIR.absolute()}")
    
    # Connect to database
    db = SessionLocal()
    try:
        # Get store
        store = db.query(Store).filter(Store.id == STORE_ID).first()
        if not store:
            print(f"\n❌ Store ID {STORE_ID} not found in database!")
            return
        
        print(f"✓ Found store: {store.domain}")
        print(f"✓ Total products: {store.total_products}")
        
        # Get all products
        print("\n📦 Loading products from database...")
        products = db.query(Product).filter(Product.store_id == STORE_ID).order_by(Product.id).all()
        print(f"✓ Loaded {len(products)} products")
        
        # Start first file
        file_index = 1
        current_file_data = io.BytesIO()
        current_file_data.write('\ufeff'.encode("utf-8"))  # BOM for Excel
        
        # Write headers
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_ALL)
        w.writerow(HEADERS)
        current_file_data.write(buf.getvalue().encode("utf-8"))
        buf.close()
        
        product_count_in_file = 0
        total_products_exported = 0
        
        print("\n📝 Processing products...")
        
        # Process each product
        for idx, p in enumerate(products, 1):
            if idx % 1000 == 0:
                print(f"   Processing product {idx}/{len(products)}...")
            
            # Get variants and images
            variants = db.query(Variant).filter(Variant.product_id == p.id).all()
            images = db.query(Image).filter(Image.product_id == p.id).order_by(Image.position).all()
            
            if not variants:
                continue
            
            # Prepare product data
            opt1_name = p.option1_name or "Title"
            opt2_name = p.option2_name or ""
            opt3_name = p.option3_name or ""
            raw_body = p.description_html or p.description or ""
            body = clean_description(raw_body)
            vendor = resolve_vendor(p.vendor or "", p.tags or "")
            product_type = p.product_type or ""
            shopify_category = map_to_shopify_category(product_type, p.title or "", p.tags or "")
            first_img = images[0] if images else None
            first_img_url = clean_img_url(first_img.original_src) if first_img else ""
            first_img_alt = (first_img.alt_text or p.title or "") if first_img else ""
            
            # Build CSV rows for this product
            product_csv = io.StringIO()
            pw = csv.writer(product_csv, quoting=csv.QUOTE_ALL)
            
            # Write variant rows
            for v_idx, v in enumerate(variants):
                row = list(EMPTY)
                if v_idx == 0:
                    row[0] = p.title or ""
                    row[1] = p.handle or ""
                    row[2] = body
                    row[3] = vendor  # Luxury Wardrobe
                    row[4] = shopify_category
                    row[5] = product_type
                    row[6] = p.tags or ""
                    row[7] = "TRUE"
                    row[8] = "Active"
                else:
                    row[1] = p.handle or ""
                
                row[9] = v.sku or ""
                row[11] = opt1_name if v_idx == 0 else ""
                row[12] = v.option1 if v.option1 else "Default Title"
                row[14] = opt2_name if v_idx == 0 else ""
                row[15] = v.option2 or ""
                row[17] = opt3_name if v_idx == 0 else ""
                row[18] = v.option3 or ""
                row[20] = fmt_price(v.price * 0.85 if v.price else 0)  # 15% discount
                row[21] = fmt_price(v.compare_at_price * 0.85 if v.compare_at_price else None)  # 15% discount
                row[23] = "TRUE"
                row[29] = "shopify"
                row[30] = "300"  # Inventory
                row[31] = "DENY"
                row[32] = "0"
                row[33] = "g"
                row[34] = "TRUE"
                row[35] = "manual"
                if v_idx == 0:
                    row[36] = first_img_url
                    row[37] = "1" if first_img else ""
                    row[38] = first_img_alt
                row[40] = "FALSE"
                pw.writerow(row)
            
            # Write additional image rows
            for img in images[1:]:
                row = list(EMPTY)
                row[1] = p.handle or ""
                row[36] = clean_img_url(img.original_src)
                row[37] = str(img.position)
                row[38] = img.alt_text or p.title or ""
                pw.writerow(row)
            
            product_csv_bytes = product_csv.getvalue().encode("utf-8")
            product_csv.close()
            
            # Check if adding this product exceeds 15MB
            if current_file_data.tell() + len(product_csv_bytes) > MAX_FILE_SIZE and product_count_in_file > 0:
                # Save current file
                filename = OUTPUT_DIR / f"{store.domain}_part{file_index}.csv"
                with open(filename, 'wb') as f:
                    f.write(current_file_data.getvalue())
                
                file_size_mb = current_file_data.tell() / (1024 * 1024)
                print(f"\n✓ Saved: {filename.name}")
                print(f"  Size: {file_size_mb:.2f} MB")
                print(f"  Products: {product_count_in_file}")
                
                # Start new file
                file_index += 1
                current_file_data = io.BytesIO()
                current_file_data.write('\ufeff'.encode("utf-8"))
                buf = io.StringIO()
                w = csv.writer(buf, quoting=csv.QUOTE_ALL)
                w.writerow(HEADERS)
                current_file_data.write(buf.getvalue().encode("utf-8"))
                buf.close()
                product_count_in_file = 0
            
            # Add product to current file
            current_file_data.write(product_csv_bytes)
            product_count_in_file += 1
            total_products_exported += 1
        
        # Save last file
        if product_count_in_file > 0:
            if file_index == 1:
                filename = OUTPUT_DIR / f"{store.domain}_products.csv"
            else:
                filename = OUTPUT_DIR / f"{store.domain}_part{file_index}.csv"
            
            with open(filename, 'wb') as f:
                f.write(current_file_data.getvalue())
            
            file_size_mb = current_file_data.tell() / (1024 * 1024)
            print(f"\n✓ Saved: {filename.name}")
            print(f"  Size: {file_size_mb:.2f} MB")
            print(f"  Products: {product_count_in_file}")
        
        # Summary
        print("\n" + "=" * 80)
        print("✅ EXPORT COMPLETE!")
        print("=" * 80)
        print(f"Total products exported: {total_products_exported}")
        print(f"Total files created: {file_index}")
        print(f"Output directory: {OUTPUT_DIR.absolute()}")
        print("\n✓ All modifications applied:")
        print("  - Vendor: Luxury Wardrobe")
        print("  - Prices: 15% reduced (×0.85)")
        print("  - Inventory: 300 units")
        print("  - WhatsApp: https://wa.me/12363148438")
        print("  - Return policy: within 7 days")
        print("  - Categories: Mapped to Shopify taxonomy")
        print("\n📁 Files are ready for Shopify import!")
        
    finally:
        db.close()

if __name__ == "__main__":
    try:
        export_store()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
