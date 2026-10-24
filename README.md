# Shopify Store Scraper

A full-stack web app that scrapes **every** product from any public Shopify store — no pagination limits, live progress via SSE, images downloaded to disk, and a full dashboard to browse, filter, and export results.

---

## Features

- **Complete scraping** — iterates through every paginated page until the products array is empty. No product left behind.
- **All product data** — title, handle, vendor, type, tags, description, all variants (SKU, price, options, availability), all image URLs.
- **Image downloads** — every product image saved to disk, served locally, with async concurrency (10 at a time).
- **Live progress** — Server-Sent Events stream real-time updates: page number, product count, images downloaded, percentage.
- **Dashboard** — search, filter by type/vendor/availability, sort, paginated grid of product cards.
- **Product modal** — image gallery, variant table, tags, full description.
- **CSV & JSON exports** — one-click downloads.
- **Re-scrape** — deletes old data and scrapes fresh.
- **SQLite storage** — zero setup, stores everything locally.

---

## Quick Start

### 1. Prerequisites

- Python 3.10 or higher

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run the app

```bash
python main.py
```

### 4. Open the dashboard

Open your browser and go to:

```
http://localhost:8000
```

That's it. Enter any Shopify store URL and click **Scrape Store**.

---

## Example Stores to Try

These are well-known public Shopify stores:

- `https://palomawool.com`
- `https://kith.com`
- `https://allbirds.com`
- `https://gymshark.com`
- `https://ohpolly.com`

---

## File Structure

```
shopify-scraper/
├── main.py          # FastAPI app, all API endpoints
├── scraper.py       # Async scraping logic, SSE job system
├── models.py        # SQLAlchemy ORM models
├── database.py      # DB engine, session, init
├── requirements.txt
├── README.md
└── static/
    └── index.html   # Full frontend (no build step)
```

Files created at runtime:
```
shopify_scraper.db   # SQLite database
images/              # Downloaded product images, organized by store/product
```

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/scrape` | Start a scrape job, returns `job_id` |
| GET | `/api/progress/{job_id}` | SSE stream of live progress |
| GET | `/api/stores` | List all scraped stores |
| DELETE | `/api/stores/{id}` | Delete store + all data + images |
| GET | `/api/stores/{id}/products` | Paginated product list with search/filters |
| GET | `/api/stores/{id}/products/{pid}` | Full product detail |
| GET | `/api/stores/{id}/stats` | Aggregate stats, vendor/type lists |
| GET | `/api/stores/{id}/export/csv` | Download CSV |
| GET | `/api/stores/{id}/export/json` | Download JSON |

---

## How the Scraper Works

1. **Resolve domain** — follows redirects to find the real Shopify domain.
2. **Check access** — tests `/products.json` and returns a clear error if the store is password-protected.
3. **Paginate** — fetches `/products.json?limit=250&page=1`, `page=2`, etc. until the response returns an empty `products` array.
4. **Parse products** — extracts every field including all variants and images.
5. **Save to DB** — commits every 50 products so progress survives failures.
6. **Download images** — async with `asyncio.gather` + `Semaphore(10)`. Saves to `images/{domain}/{handle}/{filename}`.
7. **Update store record** — final counts and `status: complete`.

---

## Notes

- Respects Shopify by waiting 0.5s between page fetches.
- Retries failed requests up to 3 times with exponential backoff.
- Failed image downloads are skipped silently — the scrape continues.
- Timeout per request: 30 seconds.
- Re-scraping a store deletes all previous data first.
