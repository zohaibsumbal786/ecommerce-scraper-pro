"""
Marketplace Scraper — Amazon, eBay, AliExpress, Temu
Uses Playwright to open a real browser window so the user can solve CAPTCHAs.
After CAPTCHA is solved, scraping continues automatically.
"""
import asyncio
import json
import re
from datetime import datetime
from typing import Optional
from bs4 import BeautifulSoup

# Global job store for marketplace scrape jobs
marketplace_jobs: dict[str, dict] = {}


def update_job(job: dict, status: str, message: str, products: int = 0,
               total: int = 0, pct: float = 0, current: str = "",
               waiting_captcha: bool = False):
    job["events"].append({
        "status": status,
        "message": message,
        "products_found": products,
        "products_total": total,
        "percentage": round(pct, 1),
        "current_product": current,
        "waiting_captcha": waiting_captcha,
    })


# ── Amazon Scraper ─────────────────────────────────────────────────────────────

async def scrape_amazon(job_id: str, url: str):
    job = marketplace_jobs[job_id]

    try:
        from playwright.async_api import async_playwright

        update_job(job, "starting", "Opening browser for Amazon...", pct=2)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=["--start-maximized", "--no-sandbox"]
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            update_job(job, "scraping", "Loading Amazon page...", pct=5)
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)

            # Check for CAPTCHA
            content = await page.content()
            if "captcha" in content.lower() or "robot" in content.lower() or "verify" in content.lower():
                update_job(job, "captcha", "⚠️ CAPTCHA detected! Please solve it in the browser window.",
                           pct=5, waiting_captcha=True)
                job["captcha_pending"] = True
                # Wait for user to solve CAPTCHA (up to 5 minutes)
                for i in range(300):
                    if job.get("stopped"):
                        break
                    await asyncio.sleep(1)
                    content = await page.content()
                    if "captcha" not in content.lower() and "robot" not in content.lower():
                        job["captcha_pending"] = False
                        update_job(job, "scraping", "CAPTCHA solved! Continuing scrape...", pct=10)
                        break
                else:
                    update_job(job, "error", "CAPTCHA timeout. Please try again.")
                    await browser.close()
                    job["done"] = True
                    return

            products = []
            page_num = 1

            while not job.get("stopped"):
                update_job(job, "scraping", f"Scraping page {page_num}...",
                           products=len(products), pct=min(10 + page_num * 5, 85))

                content = await page.content()
                soup = BeautifulSoup(content, "html.parser")

                # Amazon product cards
                items = soup.select('[data-component-type="s-search-result"]')
                if not items:
                    items = soup.select(".s-result-item[data-asin]")

                for item in items:
                    asin = item.get("data-asin", "")
                    if not asin or asin == "":
                        continue

                    title_el = item.select_one("h2 a span") or item.select_one(".a-text-normal")
                    title = title_el.get_text(strip=True) if title_el else ""

                    price_el = item.select_one(".a-price .a-offscreen") or item.select_one(".a-price")
                    price = price_el.get_text(strip=True) if price_el else ""

                    rating_el = item.select_one(".a-icon-alt")
                    rating = rating_el.get_text(strip=True) if rating_el else ""

                    img_el = item.select_one("img.s-image")
                    image = img_el.get("src", "") if img_el else ""

                    link_el = item.select_one("h2 a")
                    link = "https://www.amazon.com" + link_el.get("href", "") if link_el else ""

                    if title:
                        products.append({
                            "asin": asin,
                            "title": title,
                            "price": price,
                            "rating": rating,
                            "image": image,
                            "url": link,
                            "platform": "amazon",
                        })
                        update_job(job, "scraping", f"Found: {title[:50]}...",
                                   products=len(products), pct=min(10 + page_num * 5, 85),
                                   current=title[:60])

                # Next page
                next_btn = await page.query_selector("a.s-pagination-next:not(.s-pagination-disabled)")
                if not next_btn:
                    break
                await next_btn.click()
                await asyncio.sleep(2)
                page_num += 1

                if page_num > 20:  # Max 20 pages
                    break

            await browser.close()

        job["scraped_products"] = products
        update_job(job, "complete", f"Done! Scraped {len(products)} Amazon products.",
                   products=len(products), total=len(products), pct=100)

    except Exception as e:
        update_job(job, "error", f"Amazon scrape failed: {str(e)}")
    finally:
        job["done"] = True


# ── eBay Scraper ───────────────────────────────────────────────────────────────

async def scrape_ebay(job_id: str, url: str):
    job = marketplace_jobs[job_id]

    try:
        from playwright.async_api import async_playwright

        update_job(job, "starting", "Opening browser for eBay...", pct=2)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()

            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)

            # Check CAPTCHA
            content = await page.content()
            if "captcha" in content.lower() or "robot" in content.lower():
                update_job(job, "captcha", "⚠️ CAPTCHA detected! Solve it in the browser window.",
                           pct=5, waiting_captcha=True)
                for _ in range(300):
                    if job.get("stopped"):
                        break
                    await asyncio.sleep(1)
                    content = await page.content()
                    if "captcha" not in content.lower():
                        update_job(job, "scraping", "CAPTCHA solved! Continuing...", pct=10)
                        break

            products = []
            page_num = 1

            while not job.get("stopped"):
                update_job(job, "scraping", f"Scraping eBay page {page_num}...",
                           products=len(products), pct=min(10 + page_num * 5, 85))

                content = await page.content()
                soup = BeautifulSoup(content, "html.parser")

                items = soup.select(".s-item:not(.s-item--placeholder)")
                for item in items:
                    title_el = item.select_one(".s-item__title")
                    title = title_el.get_text(strip=True) if title_el else ""
                    if not title or title == "Shop on eBay":
                        continue

                    price_el = item.select_one(".s-item__price")
                    price = price_el.get_text(strip=True) if price_el else ""

                    img_el = item.select_one(".s-item__image-img")
                    image = img_el.get("src", "") if img_el else ""

                    link_el = item.select_one(".s-item__link")
                    link = link_el.get("href", "") if link_el else ""

                    condition_el = item.select_one(".SECONDARY_INFO")
                    condition = condition_el.get_text(strip=True) if condition_el else ""

                    products.append({
                        "title": title,
                        "price": price,
                        "image": image,
                        "url": link,
                        "condition": condition,
                        "platform": "ebay",
                    })
                    update_job(job, "scraping", f"Found: {title[:50]}",
                               products=len(products), pct=min(10 + page_num * 5, 85),
                               current=title[:60])

                next_btn = await page.query_selector("a.pagination__next")
                if not next_btn:
                    break
                await next_btn.click()
                await asyncio.sleep(2)
                page_num += 1
                if page_num > 20:
                    break

            await browser.close()

        job["scraped_products"] = products
        update_job(job, "complete", f"Done! Scraped {len(products)} eBay products.",
                   products=len(products), total=len(products), pct=100)

    except Exception as e:
        update_job(job, "error", f"eBay scrape failed: {str(e)}")
    finally:
        job["done"] = True


# ── AliExpress Scraper ─────────────────────────────────────────────────────────

async def scrape_aliexpress(job_id: str, url: str):
    job = marketplace_jobs[job_id]

    try:
        from playwright.async_api import async_playwright

        update_job(job, "starting", "Opening browser for AliExpress...", pct=2)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)

            # Check for CAPTCHA / verification
            content = await page.content()
            if any(w in content.lower() for w in ["captcha", "verify", "robot", "slide"]):
                update_job(job, "captcha", "⚠️ Please complete verification in the browser window.",
                           pct=5, waiting_captcha=True)
                for _ in range(300):
                    if job.get("stopped"):
                        break
                    await asyncio.sleep(1)
                    content = await page.content()
                    if not any(w in content.lower() for w in ["captcha", "verify"]):
                        update_job(job, "scraping", "Verification done! Scraping...", pct=10)
                        break

            products = []
            page_num = 1

            while not job.get("stopped"):
                update_job(job, "scraping", f"Scraping AliExpress page {page_num}...",
                           products=len(products), pct=min(10 + page_num * 5, 85))

                await page.wait_for_timeout(2000)
                content = await page.content()
                soup = BeautifulSoup(content, "html.parser")

                items = (soup.select(".list--gallery--C2f2tvm .card--wrap--2p0MClH") or
                         soup.select("[class*='ProductCard']") or
                         soup.select(".search-item-card-wrapper-gallery"))

                for item in items:
                    title_el = (item.select_one("[class*='title']") or
                                item.select_one("h1") or item.select_one("h3"))
                    title = title_el.get_text(strip=True) if title_el else ""

                    price_el = (item.select_one("[class*='price']") or
                                item.select_one("[class*='Price']"))
                    price = price_el.get_text(strip=True) if price_el else ""

                    img_el = item.select_one("img")
                    image = img_el.get("src", img_el.get("data-src", "")) if img_el else ""

                    link_el = item.select_one("a")
                    link = link_el.get("href", "") if link_el else ""
                    if link and not link.startswith("http"):
                        link = "https:" + link

                    if title:
                        products.append({
                            "title": title,
                            "price": price,
                            "image": image,
                            "url": link,
                            "platform": "aliexpress",
                        })
                        update_job(job, "scraping", f"Found: {title[:50]}",
                                   products=len(products), pct=min(10 + page_num * 5, 85),
                                   current=title[:60])

                # Next page - scroll or click
                next_btn = await page.query_selector("[class*='next-btn']:not([disabled])")
                if not next_btn:
                    break
                await next_btn.click()
                await asyncio.sleep(3)
                page_num += 1
                if page_num > 15:
                    break

            await browser.close()

        job["scraped_products"] = products
        update_job(job, "complete", f"Done! Scraped {len(products)} AliExpress products.",
                   products=len(products), total=len(products), pct=100)

    except Exception as e:
        update_job(job, "error", f"AliExpress scrape failed: {str(e)}")
    finally:
        job["done"] = True


# ── Temu Scraper ───────────────────────────────────────────────────────────────

async def scrape_temu(job_id: str, url: str):
    job = marketplace_jobs[job_id]

    try:
        from playwright.async_api import async_playwright

        update_job(job, "starting", "Opening browser for Temu...", pct=2)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)

            # Check for CAPTCHA
            content = await page.content()
            if any(w in content.lower() for w in ["captcha", "verify", "robot"]):
                update_job(job, "captcha", "⚠️ Please complete verification in the browser window.",
                           pct=5, waiting_captcha=True)
                for _ in range(300):
                    if job.get("stopped"):
                        break
                    await asyncio.sleep(1)
                    content = await page.content()
                    if not any(w in content.lower() for w in ["captcha", "verify"]):
                        update_job(job, "scraping", "Verification done! Scraping...", pct=10)
                        break

            products = []
            scroll_count = 0

            while not job.get("stopped") and scroll_count < 30:
                update_job(job, "scraping", f"Loading Temu products (scroll {scroll_count+1})...",
                           products=len(products), pct=min(10 + scroll_count * 3, 85))

                content = await page.content()
                soup = BeautifulSoup(content, "html.parser")

                items = (soup.select("[class*='ProductCard']") or
                         soup.select("[class*='goods-card']") or
                         soup.select("[data-type='goods']"))

                for item in items:
                    title_el = (item.select_one("[class*='title']") or
                                item.select_one("[class*='name']"))
                    title = title_el.get_text(strip=True) if title_el else ""

                    price_el = (item.select_one("[class*='price']") or
                                item.select_one("[class*='Price']"))
                    price = price_el.get_text(strip=True) if price_el else ""

                    img_el = item.select_one("img")
                    image = img_el.get("src", img_el.get("data-src", "")) if img_el else ""

                    link_el = item.select_one("a")
                    link = link_el.get("href", "") if link_el else ""
                    if link and not link.startswith("http"):
                        link = "https://www.temu.com" + link

                    if title and title not in [p["title"] for p in products]:
                        products.append({
                            "title": title,
                            "price": price,
                            "image": image,
                            "url": link,
                            "platform": "temu",
                        })
                        update_job(job, "scraping", f"Found: {title[:50]}",
                                   products=len(products), pct=min(10 + scroll_count * 3, 85),
                                   current=title[:60])

                # Scroll down to load more products
                await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                await asyncio.sleep(2)
                scroll_count += 1

                # Check if no new products loaded
                new_count = len(products)
                if scroll_count > 5 and new_count == len(products):
                    break

            await browser.close()

        job["scraped_products"] = products
        update_job(job, "complete", f"Done! Scraped {len(products)} Temu products.",
                   products=len(products), total=len(products), pct=100)

    except Exception as e:
        update_job(job, "error", f"Temu scrape failed: {str(e)}")
    finally:
        job["done"] = True


# ── Main dispatcher ────────────────────────────────────────────────────────────

async def run_marketplace_job(job_id: str, platform: str, url: str):
    scrapers = {
        "amazon": scrape_amazon,
        "ebay": scrape_ebay,
        "aliexpress": scrape_aliexpress,
        "temu": scrape_temu,
    }
    fn = scrapers.get(platform)
    if fn:
        await fn(job_id, url)
    else:
        job = marketplace_jobs[job_id]
        update_job(job, "error", f"Unknown platform: {platform}")
        job["done"] = True
