import warnings
from bs4 import XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

import re, json, time, threading, sys
# [CHANGE] Added datetime to stamp every scraped page with scraped_at / last_updated
import datetime
import requests
from bs4 import BeautifulSoup, Comment
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from settings import START_URL, REQUEST_DELAY, TIMEOUT, MAX_WORKERS, RESUME_FILE
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

BLOCKED_PATHS = ["/cart", "/checkout", "/account", "/orders", "/admin", "/services", "/cdn/wpm", "sort_by", "filter", "preview_theme_id"]
_print_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)

def create_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def fix_image_url(url):
    """Remove bad width params and set width=600 for proper display."""
    if not url:
        return url
    url = re.sub(r'[&?]width=\d+', '', url)
    if '?' in url:
        url += '&width=600'
    else:
        url += '?width=600'
    return url

def fetch_all_sitemap_urls(session):
    all_urls = {"products": [], "collections": [], "pages": [], "blogs": []}
    print(" Fetching sitemap.xml...")
    try:
        r = session.get(f"{START_URL.rstrip('/')}/sitemap.xml", timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"   sitemap.xml HTTP {r.status_code}")
            return all_urls
        soup = BeautifulSoup(r.text, "html.parser")
        sub_sitemaps = [loc.text.strip() for loc in soup.find_all("loc")]
        print(f"   Found {len(sub_sitemaps)} sub-sitemaps\n")
        for sitemap_url in sub_sitemaps:
            try:
                r2 = session.get(sitemap_url, timeout=TIMEOUT)
                if r2.status_code != 200:
                    continue
                soup2 = BeautifulSoup(r2.text, "html.parser")
                urls = [loc.text.strip() for loc in soup2.find_all("loc")]
                if "products" in sitemap_url:
                    all_urls["products"].extend(urls)
                    print(f"    {sitemap_url.split('/')[-1].split('?')[0]}: {len(urls)} product URLs")
                elif "collections" in sitemap_url:
                    all_urls["collections"].extend(urls)
                    print(f"    collections sitemap: {len(urls)} URLs")
                elif "pages" in sitemap_url:
                    all_urls["pages"].extend(urls)
                    print(f"    pages sitemap: {len(urls)} URLs")
                elif "blogs" in sitemap_url:
                    all_urls["blogs"].extend(urls)
                    print(f"    blogs sitemap: {len(urls)} URLs")
                time.sleep(0.2)
            except Exception as e:
                print(f"    {sitemap_url}: {e}")
    except Exception as e:
        print(f"    Sitemap error: {e}")
    print(f"\n Total URLs collected:")
    for k, v in all_urls.items():
        print(f"   {k:12}: {len(v):,}")
    return all_urls

def extract_product_urls_from_collection(soup, base_url="https://www.westside.com"):
    found = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/products/" in href:
            if href.startswith("/"):
                href = base_url.rstrip("/") + href
            href = href.split("?")[0].rstrip("/")
            if not any(b in href.lower() for b in BLOCKED_PATHS):
                found.add(href)
    return found

def extract_product_jsonld(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if not script.string:
                continue
            data = json.loads(script.string)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        data = item
                        break
            if not (isinstance(data, dict) and data.get("@type") == "Product"):
                continue

            product_name = data.get("name", "")
            sku = data.get("sku", "")
            brand = data.get("brand", {})
            brand = brand.get("name", "") if isinstance(brand, dict) else str(brand)
            raw_desc = data.get("description", "")
            description = BeautifulSoup(raw_desc, "html.parser").get_text(" ", strip=True)

            images = data.get("image", [])
            if isinstance(images, str):
                images = [images]
            images = [fix_image_url(img) for img in images if img]
            image_url = images[0] if images else ""

            offers = data.get("offers", [])
            prices = []
            availability = ""
            currency = "INR"
            if isinstance(offers, list):
                for o in offers:
                    try:
                        p = float(o.get("price", 0))
                        if p > 0:
                            prices.append(p)
                    except Exception:
                        pass
                    if not availability and o.get("availability"):
                        availability = o["availability"].split("/")[-1]
                    if o.get("priceCurrency"):
                        currency = o["priceCurrency"]
            else:
                try:
                    p = float(offers.get("price", 0))
                    if p > 0:
                        prices.append(p)
                except Exception:
                    pass
                availability = offers.get("availability", "").split("/")[-1]
                currency = offers.get("priceCurrency", "INR")

            price = str(int(min(prices))) if prices else ""
            max_price = str(int(max(prices))) if prices else ""

            rating_data = data.get("aggregateRating", {})
            rating = str(rating_data.get("ratingValue", ""))
            review_count = str(rating_data.get("reviewCount", ""))

            return {
                "product_name": product_name, "brand": brand, "sku": sku,
                "description": description, "image_url": image_url, "images": images,
                "price": price, "max_price": max_price, "currency": currency,
                "availability": availability, "rating": rating, "review_count": review_count,
            }
        except Exception:
            pass
    return {}

def extract_images_from_html(soup, base_url):
    images = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = base_url.rstrip("/") + src
        if "http" not in src:
            continue
        if any(x in src for x in ["icon", "logo", "1x1", "pixel", "placeholder", "spinner"]):
            continue
        src = fix_image_url(src)
        images.append(src)
    seen = set()
    unique = []
    for img in images:
        base = img.split("?")[0]
        if base not in seen:
            seen.add(base)
            unique.append(img)
    return unique[:8]

def extract_page_content(soup):
    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe", "svg", "form"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()
    headings = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"]) if h.get_text(strip=True)]
    main = soup.find("main") or soup.find("div", {"role": "main"}) or soup.body
    body_text = ""
    if main:
        body_text = re.sub(r"\s+", " ", main.get_text(" ", strip=True)).strip()
    full_text = (" | ".join(headings[:8]) + "\n" if headings else "") + body_text
    return full_text[:5000]

def extract_meta(soup):
    def get(name=None, prop=None):
        tag = soup.find("meta", attrs={"name": name}) if name else soup.find("meta", attrs={"property": prop})
        return tag.get("content", "").strip() if tag else ""
    return {
        "meta_description": get(name="description") or get(prop="og:description"),
        "keywords": get(name="keywords"),
    }

def should_skip(url):
    return any(b in url.lower() for b in BLOCKED_PATHS)

def scrape_one_page(session, url, idx, total):
    url = url.strip().rstrip("/")
    if not url or should_skip(url):
        return None
    try:
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            safe_print(f"   [SKIP {r.status_code}] {url[-65:]}")
            return None
        if "text/html" not in r.headers.get("content-type", ""):
            return None

        soup = BeautifulSoup(r.content, "html.parser")
        html_images = extract_images_from_html(soup, "https://www.westside.com")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        meta = extract_meta(soup)
        path_parts = [p for p in urlparse(url).path.split("/") if p]
        depth = len(path_parts)
        level = ("base" if depth == 0 else "category" if depth == 1 else "sub_category" if depth == 2 else "product")

        if "/products/" in url:
            product = extract_product_jsonld(soup)
            raw_text = product.get("description", "")
            # Use HTML images as fallback
            if not product.get("images") and html_images:
                product["images"] = html_images
                product["image_url"] = html_images[0]
            elif product.get("images") and not product.get("image_url"):
                product["image_url"] = product["images"][0]
        else:
            product = {}
            raw_text = extract_page_content(soup)

        page = {
            "url": url, "title": title, "html_images": html_images,
            "meta_description": meta["meta_description"], "keywords": meta["keywords"],
            "raw_text": raw_text, "level": level, "depth": depth,
            "product_name": product.get("product_name", ""), "brand": product.get("brand", ""),
            "description": product.get("description", ""), "sku": product.get("sku", ""),
            "price": product.get("price", ""), "max_price": product.get("max_price", ""),
            "currency": product.get("currency", ""), "availability": product.get("availability", ""),
            "rating": product.get("rating", ""), "review_count": product.get("review_count", ""),
            "image_url": product.get("image_url", ""), "images": product.get("images", []),
            # [CHANGE] Timestamp: when was this URL scraped, and when did its data last change
            "scraped_at":   datetime.datetime.utcnow().isoformat(),
            "last_updated": datetime.datetime.utcnow().isoformat(),
        }
        label = product.get("product_name", "")[:40] if product.get("product_name") else url[-50:]
        tag = "PRODUCT" if product.get("product_name") else level.upper()
        safe_print(f"   [{idx:>5}/{total}] [{tag:<12}] {label}")
        return page
    except Exception as e:
        safe_print(f"    {url[-65:]}: {e}")
        return None

def load_progress(fresh=False):
    if fresh:
        print("   Fresh scrape — ignoring old progress.")
        return set(), []
    try:
        with open(RESUME_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"   Resuming: {len(data.get('scraped_urls',[]))} done, {len(data.get('pages',[]))} pages saved.")
        return set(data.get("scraped_urls", [])), data.get("pages", [])
    except FileNotFoundError:
        return set(), []

def save_progress(scraped_urls, pages):
    with open(RESUME_FILE, "w", encoding="utf-8") as f:
        json.dump({"scraped_urls": list(scraped_urls), "pages": pages}, f, ensure_ascii=False)

def scrape_website(base_url=START_URL, delay=REQUEST_DELAY, timeout=TIMEOUT, fresh=False):
    session = create_session()
    scraped_urls, pages = load_progress(fresh=fresh)
    all_urls = fetch_all_sitemap_urls(session)

    sitemap_product_urls = set(all_urls["products"])
    sitemap_collection_urls = set(all_urls["collections"])
    sitemap_page_urls = set(all_urls["pages"])
    sitemap_blog_urls = set(all_urls["blogs"])

    print(f"\n{'='*60}")
    print(f" STEP 2: Scraping {len(sitemap_collection_urls)} collection pages")
    print(f"{'='*60}\n")

    collection_discovered_products = set()
    collection_pages_data = []
    col_list = sorted(sitemap_collection_urls)

    for i, col_url in enumerate(col_list, 1):
        col_url = col_url.strip().rstrip("/")
        if should_skip(col_url):
            continue
        if col_url not in scraped_urls:
            page_data = scrape_one_page(session, col_url, i, len(col_list))
            if page_data:
                collection_pages_data.append(page_data)
                scraped_urls.add(col_url)
        try:
            r = session.get(col_url, timeout=TIMEOUT)
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                soup = BeautifulSoup(r.content, "html.parser")
                found = extract_product_urls_from_collection(soup)
                if found:
                    all_found = set(found)
                    pg = 2
                    while True:
                        r2 = session.get(f"{col_url}?page={pg}", timeout=TIMEOUT)
                        if r2.status_code != 200:
                            break
                        soup2 = BeautifulSoup(r2.content, "html.parser")
                        new = extract_product_urls_from_collection(soup2)
                        if not new:
                            break
                        new_only = new - all_found
                        all_found |= new
                        safe_print(f"       {col_url.split('/')[-1][:35]} page {pg}: +{len(new_only)} products")
                        pg += 1
                        time.sleep(delay)
                    collection_discovered_products |= all_found
        except Exception as e:
            safe_print(f"    collection error {col_url}: {e}")
        time.sleep(delay)

    all_product_urls = (sitemap_product_urls | collection_discovered_products) - scraped_urls
    print(f"\n{'='*60}")
    print(f" STEP 3: Product URL Summary")
    print(f"   From sitemap     : {len(sitemap_product_urls):,}")
    print(f"   From collections : {len(collection_discovered_products):,}")
    print(f"   Already scraped  : {len(scraped_urls):,}")
    print(f"   To scrape now    : {len(all_product_urls):,}")
    print(f"{'='*60}\n")

    print(f" STEP 4: Scraping {len(all_product_urls):,} products ({MAX_WORKERS} workers)\n")
    product_url_list = list(all_product_urls)
    total = len(product_url_list)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(scrape_one_page, session, url, i+1, total): url for i, url in enumerate(product_url_list)}
        done_count = 0
        for future in as_completed(futures):
            result = future.result()
            if result:
                pages.append(result)
                scraped_urls.add(futures[future])
            done_count += 1
            if done_count % 200 == 0:
                save_progress(scraped_urls, pages)
                safe_print(f"\n    Checkpoint saved ({done_count}/{total})\n")

    pages.extend(collection_pages_data)
    for p in collection_pages_data:
        scraped_urls.add(p["url"])

    other_urls = list((sitemap_page_urls | sitemap_blog_urls) - scraped_urls)
    print(f"\n STEP 5: Scraping {len(other_urls)} pages/blogs...\n")
    for i, url in enumerate(other_urls, 1):
        url = url.strip().rstrip("/")
        if should_skip(url):
            continue
        page_data = scrape_one_page(session, url, i, len(other_urls))
        if page_data:
            pages.append(page_data)
            scraped_urls.add(url)
        time.sleep(delay)

    save_progress(scraped_urls, pages)
    prod_count = sum(1 for p in pages if p.get("product_name"))
    price_count = sum(1 for p in pages if p.get("price"))
    instock = sum(1 for p in pages if p.get("availability") == "InStock")

    print(f"\n{'='*60}")
    print(f"  SCRAPING COMPLETE")
    print(f"   Total pages   : {len(pages):,}")
    print(f"   Products      : {prod_count:,}")
    print(f"   With price    : {price_count:,}")
    print(f"   InStock       : {instock:,}")
    print(f"{'='*60}")
    return pages

# [CHANGE] ─────────────────────────────────────────────────────
# scrape_incremental(): Compare new scrape against old scraped data.
# Returns only pages that are NEW or have CHANGED fields
# (price, availability, product_name added/removed).
# This keeps the vector DB in sync without a full rebuild every time.
# ─────────────────────────────────────────────────────────────────
def scrape_incremental(old_pages: list, base_url=START_URL,
                       delay=REQUEST_DELAY, timeout=TIMEOUT) -> tuple:
    """
    Re-scrape all product URLs and compare against old_pages.

    Returns:
        (all_new_pages, changed_urls, new_urls)
        - all_new_pages : full list of freshly scraped pages (replaces old data)
        - changed_urls  : set of URLs where price / availability / name changed
        - new_urls      : set of brand-new URLs not seen in old data
    """
    # Build a lookup dict: url -> old page data
    old_lookup = {p["url"].rstrip("/"): p for p in old_pages if p.get("url")}

    # Fields we care about for change detection
    TRACK_FIELDS = ["price", "availability", "product_name", "rating", "review_count"]

    # Do a fresh full scrape (re-uses resume progress file so it can be interrupted)
    all_new_pages = scrape_website(base_url=base_url, delay=delay, timeout=timeout, fresh=True)

    changed_urls = set()
    new_urls     = set()

    now_iso = datetime.datetime.utcnow().isoformat()

    for page in all_new_pages:
        url = page.get("url", "").rstrip("/")
        if not url:
            continue

        if url not in old_lookup:
            # Brand-new product/page — mark it
            new_urls.add(url)
            page["last_updated"] = now_iso
            continue

        old = old_lookup[url]
        changed = False
        for field in TRACK_FIELDS:
            if str(page.get(field, "")).strip() != str(old.get(field, "")).strip():
                changed = True
                break

        if changed:
            changed_urls.add(url)
            page["last_updated"] = now_iso          # [CHANGE] mark when data changed
        else:
            # No change — keep original scraped_at, update scraped_at only
            page["last_updated"] = old.get("last_updated", now_iso)
            page["scraped_at"]   = now_iso

    print(f"\n [scrape_incremental] Summary:")
    print(f"    Total pages   : {len(all_new_pages)}")
    print(f"    New URLs      : {len(new_urls)}")
    print(f"    Changed URLs  : {len(changed_urls)}")
    print(f"    Unchanged     : {len(all_new_pages) - len(new_urls) - len(changed_urls)}")

    return all_new_pages, changed_urls, new_urls


if __name__ == "__main__":
    fresh = "--fresh" in sys.argv
    pages = scrape_website(fresh=fresh)
    with open("scraped.json", "w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)
    print(f"\n Saved: scraped.json ({len(pages):,} pages)")