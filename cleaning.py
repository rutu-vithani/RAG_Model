import re      # for text cleaning
import json

# ftfy(fix text for you) Fix broken or incorrectly encoded text
try:
    import ftfy
    def _fix_text(text):
        return ftfy.fix_text(text)
except ImportError:
    def _fix_text(text):
        return text


def clean_text(raw_text):
    if not raw_text:
        return None

    text = _fix_text(raw_text)
    boilerplate = [
        r"Skip to content",
        r"CHOOSE YOUR SHIPPING LOCATION",
        r"Search\s*0\s*Cart",
        r"DOWNLOAD THE APP",
        r"Remember Selection",
        r"View All",
        r"Sites & Stores",
        r"Sign Up",
        r"0 items",
        r"Shopping Cart",
        r"Free shipping",
        r"Easy returns",
        r"Continue Shopping",
        r"Add to Cart",
        r"Add to Wishlist",
        r"Share this",
        r"©.*Tata",
        r"Cookie Policy",
        r"Privacy Policy",
        r"Terms.*Conditions",
    ]
    for pattern in boilerplate:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)

    # Remove URLs, emails, long phone numbers
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    text = re.sub(r"\S+@\S+\.\S+", " ", text)
    text = re.sub(r"[\+]?\d[\d\s\-\(\)]{8,}", " ", text)
    text = re.sub(r"\b\d{8,}\b", " ", text)

    # Remove non-useful special chars
    text = re.sub(r"[^\w\s\.,!?\-:;()&%₹/]", " ", text)

    # Remove consecutive duplicate words
    text = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", text, flags=re.IGNORECASE)

    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) < 30:
        return None

    return text

def clean_all_pages(scraped_pages):
    cleaned = []
    skipped = 0

    print("\n Cleaning pages...\n")

    for page in scraped_pages:
        title            = page.get("title", "").strip()
        meta_description = page.get("meta_description", "").strip()
        raw_text         = page.get("raw_text", "").strip()

        # Combine all text sources
        combined = " ".join(part for part in [title, meta_description, raw_text] if part)
        clean    = clean_text(combined)

        if clean:
            cleaned.append({
                "url":          page["url"],
                "title":        page.get("title", ""),
                "meta_description": meta_description,
                "image_url":    page.get("image_url", ""),
                "images":       page.get("images", []),
                "level":        page.get("level", ""),
                "depth":        page.get("depth", 0),
                "product_name": page.get("product_name", ""),
                "brand":        page.get("brand", ""),
                "price":        page.get("price", ""),
                "currency":     page.get("currency", ""),
                "availability": page.get("availability", ""),
                "rating":       page.get("rating", ""),
                "review_count": page.get("review_count", ""),
                "sku":          page.get("sku", ""),
                "description":  page.get("description", ""),

                "clean_text":   clean,
            })
            print(f"   {page.get('url','')[:70]}")
            print(f"     {len(combined)} → {len(clean)} chars")
        else:
            skipped += 1
            print(f"    Skipped (too short): {page.get('url','')[:70]}")

    total        = len(scraped_pages)
    noise_pct    = int((1 - len(cleaned) / total) * 100) if total else 0

    print(f"\n Cleaning Summary")
    print(f"    Cleaned : {len(cleaned)}")
    print(f"     Skipped : {skipped}")
    print(f"    Noise   : ~{noise_pct}%")

    return cleaned


if __name__ == "__main__":
    print(" Loading scraped.json...")

    with open("scraped.json", "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    if isinstance(raw_data, dict) and "pages" in raw_data:
        scraped_pages = raw_data["pages"]
    else:
        scraped_pages = raw_data

    print(f"   Loaded {len(scraped_pages)} pages")

    cleaned = clean_all_pages(scraped_pages)

    with open("cleaned_data.json", "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)

    print(f"\n Saved: cleaned_data.json ({len(cleaned)} pages)")