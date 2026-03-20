import json
import os
import sys
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SEEN_FILE = "seen.json"

# This is the exact URL behind the "Automatic" link in the nav menu
AUTOMATIC_URL = "https://www.hmtwatches.in/shop_type?type=shop_type&id=9"

# ── Seen list helpers ─────────────────────────────────────────────────────────

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


# ── Telegram helpers ──────────────────────────────────────────────────────────

def send_telegram_photo(name: str, image_url: str, product_url: str):
    # Plain text caption — no Markdown so the URL is never corrupted by the parser
    caption = (
        f"🕐 HMT Automatic Watch Available!\n\n"
        f"{name}\n\n"
        f"{product_url}"
    )
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": image_url,
            "caption": caption,
        },
        timeout=15,
    )
    if resp.ok:
        print(f"  ✅  Alert sent: {name}")
    else:
        print(f"  ⚠️  Telegram photo failed for '{name}', trying text: {resp.text}")
        send_telegram_text(
            f"🕐 HMT Automatic Watch Available!\n\n{name}\n\n{product_url}"
        )


def send_telegram_text(message: str):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        },
        timeout=15,
    )


# ── Scraper ───────────────────────────────────────────────────────────────────

def scrape_automatic_watches():
    """
    Uses Playwright (headless Chromium) to load the Automatic filter page,
    wait for JS-rendered product cards, and extract all products.
    Returns a list of dicts: {id, name, url, image, in_stock}
    """
    watches = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        print(f"  🌐  Loading: {AUTOMATIC_URL}")
        page.goto(AUTOMATIC_URL, wait_until="networkidle", timeout=60000)

        # Wait for product cards OR "No Product Found" to appear (up to 30 seconds)
        try:
            page.wait_for_selector(
                "div.product-card, div.col-product, div[class*='product'], a[href*='product_overview'], h3, p",
                timeout=30000
            )
        except PWTimeout:
            print("  ⚠️  Product cards timed out. Saving debug screenshot.")
            page.screenshot(path="debug_screenshot.png")
            browser.close()
            return None

        # Scroll to bottom to trigger lazy loading of all products
        prev_height = 0
        for _ in range(10):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            curr_height = page.evaluate("document.body.scrollHeight")
            if curr_height == prev_height:
                break
            prev_height = curr_height

        # Save rendered HTML for debugging in GitHub Actions artifacts
        html_content = page.content()
        browser.close()

    with open("debug_page.html", "w", encoding="utf-8") as f:
        f.write(html_content)

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, "html.parser")

    # ── Parse product cards ───────────────────────────────────────────────────
    # Try specific selectors first, then fall back to anchor links
    candidates = [
        soup.select("div.product-card"),
        soup.select("div.col-product"),
        soup.select("div[class*='product-item']"),
        soup.select("div[class*='product_item']"),
    ]
    products = next((c for c in candidates if c), [])

    if not products:
        # Fallback: find all anchor tags pointing to product_overview pages
        anchors = soup.select("a[href*='product_overview']")
        if anchors:
            print(f"  ℹ️  Using anchor fallback — {len(anchors)} product links found")
            return parse_from_anchors(anchors)
        else:
            print("  ❌  No product cards or links found.")
            return None

    print(f"  📦  Product cards found: {len(products)}")

    for product in products:
        # Name
        name_tag = (
            product.select_one("p.product-name")
            or product.select_one("div.product-name")
            or product.select_one("h5")
            or product.select_one("h4")
            or product.select_one("p.name")
            or product.select_one("[class*='name']")
        )
        name = name_tag.get_text(strip=True) if name_tag else "Unknown Watch"

        # Product URL
        link_tag = product.select_one("a[href*='product_overview']") or product.select_one("a")
        product_url = link_tag["href"] if link_tag and link_tag.get("href") else AUTOMATIC_URL
        if not product_url.startswith("http"):
            product_url = "https://www.hmtwatches.in" + product_url

        # Unique ID: use the encrypted token from the URL
        product_id = product_url.split("id=")[-1] if "id=" in product_url else product_url

        # Image
        img_tag = product.select_one("img")
        image_url = ""
        if img_tag:
            image_url = (
                img_tag.get("data-src")
                or img_tag.get("data-lazy-src")
                or img_tag.get("src")
                or ""
            )
            if image_url and not image_url.startswith("http"):
                image_url = "https://www.hmtwatches.in" + image_url

        # Stock: look for "Out Of Stock" text anywhere in the card
        card_text = product.get_text().lower()
        in_stock = "out of stock" not in card_text and "outofstock" not in card_text

        watches.append({
            "id": product_id,
            "name": name,
            "url": product_url,
            "image": image_url,
            "in_stock": in_stock,
        })

    return watches


def parse_from_anchors(anchors):
    """Fallback: parse product info directly from anchor tags."""
    watches = []
    seen_ids = set()

    for a in anchors:
        product_url = a.get("href", AUTOMATIC_URL)
        if not product_url.startswith("http"):
            product_url = "https://www.hmtwatches.in" + product_url

        product_id = product_url.split("id=")[-1] if "id=" in product_url else product_url

        # Skip duplicates (same product linked multiple times)
        if product_id in seen_ids:
            continue
        seen_ids.add(product_id)

        img_tag = a.select_one("img")
        image_url = ""
        if img_tag:
            image_url = img_tag.get("src") or img_tag.get("data-src") or ""
            if image_url and not image_url.startswith("http"):
                image_url = "https://www.hmtwatches.in" + image_url

        name_tag = a.select_one("p") or a.select_one("span") or a.select_one("div")
        name = name_tag.get_text(strip=True) if name_tag else "HMT Automatic Watch"
        if not name or len(name) < 3:
            name = "HMT Automatic Watch"

        card_text = a.get_text().lower()
        in_stock = "out of stock" not in card_text

        watches.append({
            "id": product_id,
            "name": name,
            "url": product_url,
            "image": image_url,
            "in_stock": in_stock,
        })

    return watches


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("🚀  HMT Automatic Watch Checker starting...")

    seen = load_seen()
    print(f"  📋  Previously seen: {len(seen)} watches")

    watches = scrape_automatic_watches()
    print(f"  📦  Scraped: {len(watches)} watches")

    if watches is None:
        # Scrape genuinely failed — Playwright timed out or found no page structure
        print("  ❌  Scrape failed. Check debug artifacts in this Actions run.")
        send_telegram_text(
            "❌ HMT Watcher scrape FAILED\n"
            "Playwright could not load the page.\n"
            "Check GitHub Actions for debug files.\n"
            "Manual check: https://www.hmtwatches.in/shop_type?type=shop_type&id=9"
        )
        sys.exit(1)

    if len(watches) == 0:
        # Page loaded fine but no automatic watches found — all out of stock
        print("  😴  No automatic watches found. All likely out of stock.")
        send_telegram_text(
            "🕐 HMT Watcher is running\n"
            "Checked automatic watches — all out of stock right now."
        )
        print("✅  Done (nothing to report).")
        sys.exit(0)

    new_available = [w for w in watches if w["in_stock"] and w["id"] not in seen]
    print(f"  🆕  New in-stock watches: {len(new_available)}")

    for watch in new_available:
        print(f"     → {watch['name']}")
        send_telegram_photo(watch["name"], watch["image"], watch["url"])
        seen.add(watch["id"])

    if not new_available:
        print("  😴  No new watches since last check.")
        send_telegram_text(
            "🕐 HMT Watcher is running\n"
            "Checked automatic watches — none newly available."
        )

    # Clear out-of-stock watches from seen so we re-alert if they restock later
    out_of_stock_ids = {w["id"] for w in watches if not w["in_stock"]}
    before = len(seen)
    seen -= out_of_stock_ids
    removed = before - len(seen)
    if removed:
        print(f"  🔄  Cleared {removed} out-of-stock entries from seen list.")

    save_seen(seen)
    print("✅  Done.")


if __name__ == "__main__":
    main()
