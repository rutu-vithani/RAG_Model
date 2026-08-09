import re
import json
import os
import shutil
import threading
# APScheduler for Windows-compatible cron job that auto-re-scrapes the website
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify, render_template
import mongodb  
from settings import LLM_MODEL

app = Flask(__name__, template_folder="template")

retriever  = None
llm_client = None

# Dual-file scraping state:
#   scraped.json       → ACTIVE data (served to users at all times)
#   scraped_new.json   → STAGING file written during background scrape
#   scraped_old.json   → BACKUP of previous active data (for rollback)
# While a scrape is running users still read from scraped.json (old data).
# Only after the full pipeline finishes do we atomically swap the files.
_scrape_lock   = threading.Lock()   # prevent two scrapes running at the same time
_scrape_status = {                  # expose via /scrape-status endpoint
    "running":    False,
    "last_run":   None,
    "last_result": None,           
    "new_count":   0,
    "changed_count": 0,
}

try:

    from retriever import load_vector_db, create_retriever, search_query, retrieve_user_memory, get_recommendations_from_memory, gender_allows
    from llm import load_llm
    from embeddingVector import extract_and_store_memory
    print("Loading Vector DB...")
    vectordb   = load_vector_db()
    retriever  = create_retriever(vectordb)
    llm_client = load_llm()
    print("Ready!")
except Exception as e:
    print("Warning: ML/vector modules not available:", e)

# Fallbacks if retriever.py/embeddingVector.py (or their deps) failed to
# import above — keeps /chat working even without the memory feature.
if "retrieve_user_memory" not in dir():
    def retrieve_user_memory(user_id, question, k=5):
        return []
if "extract_and_store_memory" not in dir():
    def extract_and_store_memory(llm_client, llm_model, user_id, question, answer):
        return
if "get_recommendations_from_memory" not in dir():
    def get_recommendations_from_memory(user_id, retriever_obj, current_category=None, k=4):
        return []
if "gender_allows" not in dir():
    def gender_allows(combined_text_low, gender_low, brand_low=None):
        return True

chat_histories  = {}
session_context = {}

#  BRAND → GENDER MAPPING (kept for session memory only)
MEN_BRANDS = {"wes casuals","wes formals","wes lounge","nuoflexx","ascot","eta","studiofit"}
WOMEN_BRANDS = {"nuon","lov","wardrobe","utsa","gia","wunderlove","bombay paisley","superstar","vark","diza","zuba","studiowest","white door x samoh","studiofit"}
KIDS_BRANDS = {"hop","hop baby","hop kids","y&f teen","utsa kids"}

#  PRICE RANGE PARSING — the LLM JSON schema has no price field, so
#  "under ₹1000" / "between ₹500 and ₹1200" etc. were never actually
#  used to filter products; they only showed up in the LLM's free text.
#  We parse the user's own message with regex and enforce it ourselves.
_PRICE_BETWEEN_RE = re.compile(
    r'between\s*(?:rs\.?|inr|₹)?\s*(\d{2,6})\s*(?:and|to|-)\s*(?:rs\.?|inr|₹)?\s*(\d{2,6})', re.I)
_PRICE_DASH_RE    = re.compile(r'(?:rs\.?|inr|₹)?\s*(\d{2,6})\s*-\s*(?:rs\.?|inr|₹)?\s*(\d{2,6})')
_PRICE_UNDER_RE   = re.compile(r'(?:under|below|less than|upto|up to|within)\s*(?:rs\.?|inr|₹)?\s*(\d{2,6})', re.I)
_PRICE_OVER_RE    = re.compile(r'(?:above|over|more than)\s*(?:rs\.?|inr|₹)?\s*(\d{2,6})', re.I)

def extract_price_range(text):
    """Returns (price_min, price_max) parsed from the user's message, or (None, None)."""
    if not text:
        return (None, None)
    t = text.lower()
    m = _PRICE_BETWEEN_RE.search(t) or _PRICE_DASH_RE.search(t)
    if m:
        lo, hi = sorted([int(m.group(1)), int(m.group(2))])
        return (lo, hi)
    m = _PRICE_UNDER_RE.search(t)
    if m:
        return (None, int(m.group(1)))
    m = _PRICE_OVER_RE.search(t)
    if m:
        return (int(m.group(1)), None)
    return (None, None)

def _parse_price(val):
    """Coerce a price field (may be '₹1,299', '1299', '' etc.) to a float, or None."""
    try:
        s = re.sub(r"[^\d.]", "", str(val or ""))
        return float(s) if s else None
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────
#  ATTRIBUTE (color / material) STRICT MATCHING
# ─────────────────────────────────────────────────────────────
# If a user names a specific attribute ("silver earrings", "black
# shirt", "cotton kurta"), we must only show products that actually
# have that attribute — never substitute a different color/material
# and present it as a match. These are matched against product_name
# and url (same fields cat_filters already checks).
ATTRIBUTE_WORDS = [
    # metals / jewellery materials
    "silver","gold","rose gold","platinum","oxidised","oxidized",
    "antique","kundan","pearl","diamond","cz","stone","brass",
    # colors
    "black","white","red","blue","green","yellow","pink","purple",
    "orange","beige","brown","maroon","navy","grey","gray","cream",
    "olive","mustard","peach","lavender","turquoise","gold tone",
    "silver tone","multicolor","multicoloured","multicolored",
    # fabrics / materials (clothing)
    "cotton","silk","linen","denim","leather","velvet","satin",
    "wool","polyester","chiffon","georgette","rayon","khadi",
]
# Sort longest-first so multi-word phrases ("rose gold") are checked
# before their single-word substrings ("gold") get a chance to match.
_ATTRIBUTE_WORDS_SORTED = sorted(ATTRIBUTE_WORDS, key=len, reverse=True)

def extract_attributes(text):
    """
    Return the list of attribute words (color/material) the user
    explicitly typed, e.g. "silver earrings under 500" -> ["silver"].
    Empty list if none mentioned — in that case we don't filter by
    attribute at all (user didn't ask for one).
    """
    if not text:
        return []
    t = " " + text.lower() + " "
    found = []
    for word in _ATTRIBUTE_WORDS_SORTED:
        if f" {word} " in t or t.startswith(word + " ") or t.endswith(" " + word):
            # avoid adding "gold" if "rose gold" already matched at this spot
            if not any(word in f and word != f for f in found):
                found.append(word)
    return found

# Category → substrings that must NOT match even though the category's
# own keyword is a substring of them (e.g. "shirt" is a substring of
# "t-shirt", so a plain "Shirts" search was wrongly pulling in T-Shirts).
CATEGORY_EXCLUDES = {
    "shirts": ["t-shirt", "tshirt", " tee", "tank"],
    "shirt":  ["t-shirt", "tshirt", " tee", "tank"],
}

def cat_excludes_for(cat):
    if not cat:
        return []
    return CATEGORY_EXCLUDES.get(cat.lower().strip(), [])

def fix_img(img):
    if not img: return ""
    img = re.sub(r'[&?]width=\d+', '', img)
    img = img + ("&width=600" if "?" in img else "?width=600")
    return img

# Detects a plain email address typed directly into a chat message
# (separate from the dedicated /auth/capture-email popup flow), so that
# typing an email mid-conversation also gets saved to the users collection.
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')

def extract_email(text):
    if not text:
        return None
    m = EMAIL_RE.search(text)
    return m.group(0).strip().lower() if m else None

def retrieve(query, k=None):
    if retriever is None: return []
    return search_query(retriever, query, k=k)

def get_image(doc):
    """Extract image from doc — supports image_url field and images array."""
    # Direct image_url field (your scraped data format)
    img = doc.get("image_url") or doc.get("image") or ""
    if img:
        return fix_img(img)
    # Fallback: images array
    images = doc.get("images") or []
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except Exception:
            images = []
    if images and isinstance(images, list):
        return fix_img(images[0])
    return ""

def docs_to_cards(docs, cat_filters=None, limit=6, gender=None, price_min=None, price_max=None, exclude_filters=None, attribute_filters=None):
    """
    Convert retrieved docs to product cards.
    cat_filters: list of lowercase strings to match in product_name or url.
    gender: "men"|"women"|"kids"|None — hard-excludes cross-gender products
            (was previously NOT applied here at all, only in the
            "Based on your interests" recommendation list, which is why
            a men's-context query could surface women's products).
    price_min/price_max: optional budget bounds parsed from the user's
            message (previously never enforced anywhere).
    exclude_filters: substrings that must NOT appear (e.g. excludes
            "t-shirt" when the category is plain "shirts").
    attribute_filters: color/material words the user explicitly typed
            (e.g. "silver" from "silver earrings"). STRICT — a product
            is only included if its name/url actually contains the
            attribute. This prevents substituting a different
            color/material than what was asked for (e.g. showing gold
            earrings when the user asked for silver).
    """
    products   = []
    seen_urls  = set()
    seen_names = set()
    gender_low = (gender or "").lower().strip()

    for doc in docs:
        # Support both dict and object with .metadata
        if hasattr(doc, "metadata"):
            d = doc.metadata
        elif isinstance(doc, dict):
            d = doc
        else:
            continue

        pname = (d.get("product_name") or d.get("title") or "").strip()
        url   = (d.get("url") or d.get("link") or "").strip().rstrip("/")

        if not pname or not url:
            continue

        # Only product pages
        if "/products/" not in url:
            continue

        plow    = pname.lower()
        url_low = url.lower()

        # Category filter — strip leading/trailing spaces from filter words
        # so " tee" (with leading space) still matches "tee" in URLs/names.
        if cat_filters:
            cf_stripped = [f.strip() for f in cat_filters]
            if not any(f in plow for f in cf_stripped) and \
               not any(f in url_low for f in cf_stripped):
                continue

        # Category exclude filter (e.g. "shirts" must not match "t-shirt")
        if exclude_filters and (any(f in plow for f in exclude_filters) or
                                 any(f in url_low for f in exclude_filters)):
            continue

        # Attribute filter (color/material the user explicitly named) —
        # STRICT, every requested attribute must be present. Checks both
        # product name and URL slug (hyphens replaced with spaces so
        # "white" matches "/products/men-white-graphic-tee" correctly).
        if attribute_filters:
            url_slug_words = url_low.replace("-", " ").replace("/", " ")
            combined = plow + " " + url_low + " " + url_slug_words
            if not all(attr in combined for attr in attribute_filters):
                continue

        # Gender filter — prevents cross-gender products (e.g. women's
        # shirts showing up for a men's-context query)
        if gender_low and not gender_allows(
            plow + " " + url_low, gender_low,
            brand_low=(d.get("brand") or "").lower().strip()
        ):
            continue

        # Budget filter
        if price_min is not None or price_max is not None:
            p_val = _parse_price(d.get("price"))
            if p_val is None:
                continue
            if price_min is not None and p_val < price_min:
                continue
            if price_max is not None and p_val > price_max:
                continue

        # Dedup
        url_key = url.split("?")[0]
        if url_key in seen_urls or plow in seen_names:
            continue
        seen_urls.add(url_key)
        seen_names.add(plow)

        products.append({
            "name":         pname,
            "brand":        (d.get("brand") or "Westside").strip(),
            "price":        d.get("price") or "",
            "url":          url,
            "image":        get_image(d),
            "availability": d.get("availability") or "InStock",
        })
        if len(products) >= limit:
            break

    return products

def call_llm(system_prompt, messages):
    if llm_client is None:
        return json.dumps({"type":"error","answer":"LLM not loaded","chips":[],"show_products":False,"more_options":[]})
    from settings import LLM_MODEL, MAX_TOKENS, TEMPERATURE
    all_msgs = [{"role":"system","content":system_prompt}] + messages
    resp = llm_client.chat.completions.create(
        model=LLM_MODEL, messages=all_msgs,
        temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
    )
    return resp.choices[0].message.content.strip()

MASTER_SYSTEM = """You are a shopping assistant ONLY for Westside (westside.com) — an Indian fashion retail brand owned by Tata.

YOU MUST ALWAYS output a single valid JSON object. No text outside JSON. No markdown fences.

═══════════════════════════════════════
SCOPE RULE — HIGHEST PRIORITY
═══════════════════════════════════════
ONLY answer questions about Westside: products, collections, categories, prices, availability, store info, offers, brands.

If user asks ANYTHING outside Westside shopping scope → output EXACTLY:
{"type":"out_of_scope","gender":null,"category":null,"answer":"I'm here only to help you shop at Westside 😊 Ask me about our collections, products, prices or availability!","chips":[],"show_products":false,"more_options":[]}

═══════════════════════════════════════
STEP-BY-STEP REASONING
═══════════════════════════════════════
Step 1 — Scope check. Out of scope? → out_of_scope JSON.

Step 2 — Message type:
  GREETING   : hi/hello/namaste/kem cho/hey/good morning/whats up/sup
  BROWSE     : user picks a top-level section without specific product type
  PRODUCT    : specific product requested — category is clear
  GENERAL    : in-scope but not product

Step 3 — Gender (carry forward from SESSION_MEMORY if not stated now):
  men/mens/gents/boys → "men"
  women/ladies/girls/kurta/saree → "women"
  kids/children/baby → "kids"

Step 4 — Category: extract exact product type word.

═══════════════════════════════════════
OUTPUT JSON SCHEMA
═══════════════════════════════════════
{
  "type":          "greeting"|"browse"|"product"|"general"|"out_of_scope",
  "gender":        "men"|"women"|"kids"|null,
  "category":      "<product type>"|null,
  "answer":        "<reply>",
  "chips":         ["label",...],
  "show_products": true|false,
  "more_options":  ["label",...]
}

═══════════════════════════════════════
CHIP RULES
═══════════════════════════════════════
greeting     → ["Men's Collection","Women's Collection","Kids Collection","Jewellery","Perfumes & Beauty","Gifts","New Arrivals","Sale"]
browse men   → ["Shirts","T-Shirts","Jeans","Trousers","Kurtas","Jackets","Shoes","Accessories","Perfumes"]
browse women → ["Kurtas","Dresses","Sarees","Tops","Jeans","Lehengas","Shoes","Jewellery","Bags","Perfumes"]
browse kids  → ["Boys Clothes","Girls Clothes","Shoes","Accessories","School Bags"]
browse jewellery → ["Necklaces","Earrings","Bracelets","Rings","Bangles"]
browse "Perfumes & Beauty" → ["Perfumes","Skincare","Makeup","Hair Care","Body Care","Deodorants"]
browse gifts → ["Gift Sets","Jewellery Gifts","Accessories","Perfume Gift Sets","Home Gifts"]
browse new arrivals → ["Men's New Arrivals","Women's New Arrivals","Kids New Arrivals","New Jewellery"]
browse sale  → ["Men's Sale","Women's Sale","Kids Sale","Jewellery Sale"]
product shown → chips: []
general      → chips: []

═══════════════════════════════════════
ANSWER TEXT RULES
═══════════════════════════════════════
GREETING: "Hey! 👋 Welcome to Westside!\\n\\nI'm your personal shopping assistant. Pick a category below to get started! 😊"

BROWSE: "Great! We have a wonderful [gender] collection! Here are the categories:\\n\\n1. ...\\n\\nPlease select a category to see products!"

PRODUCT: "Here are some [gender] [category] from Westside! 👕\\n\\nWant to explore other options from [gender]'s collection? 👇"

GENERAL: helpful answer.

═══════════════════════════════════════
FEW-SHOT EXAMPLES
═══════════════════════════════════════
Input: "hi"
{"type":"greeting","gender":null,"category":null,"answer":"Hey! 👋 Welcome to Westside!\\n\\nI'm your personal shopping assistant. I can help you find the perfect outfit, check prices and explore our latest collections.\\n\\nWhat are you shopping for today?","chips":["Men's Collection","Women's Collection","Kids Collection","Jewellery","Perfumes & Beauty","Gifts","New Arrivals","Sale"],"show_products":false,"more_options":[]}

Input: "Men's Collection"
{"type":"browse","gender":"men","category":null,"answer":"Great! We have a wonderful Men collection! Here are the categories:\\n\\n1. Shirts\\n2. T-Shirts\\n3. Jeans\\n4. Trousers\\n5. Kurtas\\n6. Jackets\\n7. Shoes\\n8. Accessories\\n9. Perfumes\\n\\nPlease select a category to see products!","chips":["Shirts","T-Shirts","Jeans","Trousers","Kurtas","Jackets","Shoes","Accessories","Perfumes"],"show_products":false,"more_options":[]}

Input: "Shirts" (SESSION: gender=men)
{"type":"product","gender":"men","category":"shirts","answer":"Here are some great Men's Shirts from Westside! 👔\\n\\nWant to explore other options from Men's collection? 👇","chips":[],"show_products":true,"more_options":["T-Shirts","Jeans","Trousers","Kurtas","Jackets","Shoes","Accessories","Perfumes"]}

Input: "Perfumes & Beauty"
{"type":"browse","gender":null,"category":null,"answer":"Explore our Beauty & Fragrance collection! ✨\\n\\n1. Perfumes\\n2. Skincare\\n3. Makeup\\n4. Hair Care\\n5. Body Care\\n6. Deodorants\\n\\nSelect a category to see products!","chips":["Perfumes","Skincare","Makeup","Hair Care","Body Care","Deodorants"],"show_products":false,"more_options":[]}

Input: "Gifts"
{"type":"browse","gender":null,"category":null,"answer":"Looking for the perfect gift? 🎁 Here are our gift categories:\\n\\n1. Gift Sets\\n2. Jewellery Gifts\\n3. Accessories\\n4. Perfume Gift Sets\\n5. Home Gifts\\n\\nSelect a category!","chips":["Gift Sets","Jewellery Gifts","Accessories","Perfume Gift Sets","Home Gifts"],"show_products":false,"more_options":[]}

Input: "Perfumes" (SESSION: from beauty browse)
{"type":"product","gender":null,"category":"perfumes","answer":"Here are some wonderful Perfumes from Westside! 🌸\\n\\nWant to explore more beauty products? 👇","chips":[],"show_products":true,"more_options":["Skincare","Makeup","Hair Care","Body Care","Deodorants"]}

Input: "in black" (SESSION: gender=men, current_topic=shirts — user previously asked for "Men's Collection" then "Shirts")
{"type":"product","gender":"men","category":"shirts","answer":"Here are some great Men's Black Shirts from Westside! 👔\\n\\nWant to explore other options from Men's collection? 👇","chips":[],"show_products":true,"more_options":["T-Shirts","Jeans","Trousers","Kurtas","Jackets","Shoes","Accessories","Perfumes"]}

Input: "what is the capital of France"
{"type":"out_of_scope","gender":null,"category":null,"answer":"I'm here only to help you shop at Westside 😊 Ask me about our collections, products, prices or availability!","chips":[],"show_products":false,"more_options":[]}

═══════════════════════════════════════
STRICT RULES
═══════════════════════════════════════
1. Output ONLY valid JSON — nothing before or after
2. show_products=true means backend will show product cards
3. Gender carries forward from SESSION_MEMORY
4. Reply language follows user (English/Gujarati/Hindi) — JSON keys stay English
5. more_options = subcats NOT yet chosen from the current browse list

═══════════════════════════════════════
USER_LONG_TERM_MEMORY
═══════════════════════════════════════
You may receive a USER_LONG_TERM_MEMORY block with facts remembered about
THIS specific user from earlier visits/sessions (name, location, past
product/category interest, preferences). Use it naturally:
- If the user's name is known, address them by name where it feels natural.
- If a location is known and relevant (e.g. user mentions visiting somewhere,
  or asks about stores), you can mention nearby Westside stores or that
  context, but don't force it into unrelated replies.
- If past product/category interest is known, you may proactively suggest
  related or similar products, or remind the user what they searched for
  before, when it's relevant to the current message (e.g. greetings,
  "I'm back", vague browsing).
- Never invent facts that aren't in USER_LONG_TERM_MEMORY or the current
  conversation. If the block is empty, just behave normally.

═══════════════════════════════════════
PAST_CHAT_HISTORY_ACROSS_ALL_SESSIONS
═══════════════════════════════════════
You will also receive a PAST_CHAT_HISTORY_ACROSS_ALL_SESSIONS block. This
is the user's actual saved chat history from EVERY previous session/day
they've talked to you (NOT just today's conversation), grouped by date,
oldest first. Use this whenever the user asks something like:
- "what did I search for earlier/yesterday/this morning?"
- "what collection did I explore today?"
- "have we talked before?" / "do you remember me?"
If this block contains relevant dated entries, answer directly from them
(e.g. "Earlier today you were looking at Men's Shirts and Jeans") instead
of saying you have no memory or that the conversation just started — that
response is ONLY correct if this block is empty/"(no past chat history
found for this user)". If the block has entries, you DO have memory of
this user; act like it. Still don't invent details not present in this
block or USER_LONG_TERM_MEMORY.

═══════════════════════════════════════
NO HALLUCINATION RULE — HIGH PRIORITY
═══════════════════════════════════════
NEVER state a specific fact (budget/price, size, color, fit, name, location,
or anything else) about the user unless it was EXPLICITLY said by the user
in this conversation OR appears verbatim in USER_LONG_TERM_MEMORY. If you
are not sure of a detail, omit it entirely rather than guessing a number —
e.g. do NOT say "your budget is around Rs399" unless the user actually
typed that amount.

═══════════════════════════════════════
RECENCY RULE FOR VAGUE REQUESTS ("recommend something", "any suggestions")
═══════════════════════════════════════
When the user is vague, ALWAYS prefer SESSION_MEMORY's current_topic /
last_browse / gender (what they were JUST looking at, this conversation)
over USER_LONG_TERM_MEMORY (older, cross-session facts). Only fall back to
USER_LONG_TERM_MEMORY if current_topic/last_browse is empty/null. Do not
jump to an unrelated older interest (e.g. Jewellery) while the user is
mid-conversation about something else (e.g. men's shirts).

═══════════════════════════════════════
FOLLOW-UP / ANAPHORA RULE
═══════════════════════════════════════
If SESSION_MEMORY includes a LAST_SHOWN_PRODUCT and the user says
"this/it/that (product)" or asks to know more about "this", answer about
that specific product (use its name/brand/price/url), not a generic
category list.

GENERAL FOLLOW-UP RULE (applies to ANY attribute, not just one example):
When the user's new message only adds ONE missing detail on top of what
they already said earlier in this conversation (color, size, fit, price
range, material, sleeve length, occasion, brand, etc.), treat it as a
refinement of the SAME request — combine it with the most recent
category/gender/product from SESSION_MEMORY or the last few turns of
history, don't ask the user to repeat the whole thing.
Example pattern: user says "men's collection" → then "shirts" → then
"in black" → the 3rd message means "men's black shirts", not a fresh
unrelated query. The same logic applies if instead of "in black" the
user had said "size L", "under 1000", "cotton ones", "full sleeve", or
any other single attribute — always merge it with the prior context
rather than asking what they meant.

═══════════════════════════════════════
STRICT EXACT-MATCH RULE — HIGH PRIORITY
═══════════════════════════════════════
When the user names a SPECIFIC attribute (color, material, metal type,
fabric, etc.) along with a category — e.g. "silver earrings", "black
shirt", "cotton kurta", "rose gold necklace" — the backend will ONLY
show products that genuinely have that exact attribute. It will NEVER
substitute a different color/material (e.g. gold earrings when silver
was asked) and call it a match.

Because of this:
- Do NOT write your "answer" text as if the exact match is guaranteed.
  Phrase it neutrally, e.g. "Here are some Silver Earrings from
  Westside!" is fine as a lead-in IF products are actually returned —
  but never claim a specific attribute is in stock, never say "we have
  silver earrings" as a flat factual claim, since you don't know
  in advance whether any exist.
- If the backend finds NO products matching the requested attribute,
  it will replace your answer text entirely with an honest
  "we don't currently have that" message — you do not need to handle
  that case yourself, just don't write text that would contradict it
  or oversell availability.
- NEVER invent, assume, or imply a color/material/attribute that the
  user didn't ask for. If the user just says "earrings" with no
  attribute, don't add "silver" or any other specific attribute
  yourself in the answer text — only mention attributes the user
  explicitly stated.
- Still set show_products=true normally when the request is a clear
  product request (with or without an attribute) — the backend handles
  the strict filtering and will override your answer text if nothing
  matches, so you don't need to second-guess catalog availability.
"""

def build_llm_messages(question, history, context_docs, sess_ctx, user_memory=None, past_sessions_summary=None):
    ctx_lines = []
    for d in context_docs[:12]:
        if hasattr(d, "metadata"):
            d = d.metadata
        pname = d.get("product_name","") or d.get("title","")
        price = d.get("price","")
        avail = d.get("availability","")
        url   = d.get("url","")
        brand = d.get("brand","")
        if pname:
            ctx_lines.append(f"- {pname} | Rs{price} | {avail} | Brand:{brand} | {url}")
    ctx_block = "\n".join(ctx_lines) if ctx_lines else "(no product context)"
    mem = (f"gender_memory={sess_ctx.get('gender')} | "
           f"last_browse={sess_ctx.get('last_browse')} | "
           f"last_subcats={sess_ctx.get('last_subcats')} | "
           f"current_topic={sess_ctx.get('last_category')}")
    last_product = sess_ctx.get("last_product")
    if last_product:
        mem += (f" | LAST_SHOWN_PRODUCT={last_product.get('name')} "
                f"(Rs{last_product.get('price')}, {last_product.get('url')})")

    # Long-term, cross-session memory for THIS user (semantic facts from ChromaDB)
    if user_memory:
        memory_block = "\n".join(f"- {fact}" for fact in user_memory)
    else:
        memory_block = "(no memory yet for this user)"

    # Cross-session RAW CHAT HISTORY for this user (from MongoDB) — this is
    # what tells the bot "you've talked to this person across previous
    # days/sessions, here's what was actually said". Previously the LLM was
    # NEVER given this at all — only the in-RAM history of the CURRENT
    # session (which resets on server restart / new tab / new session_id)
    # and a narrow semantic-search memory that often missed broad questions
    # like "what did I look at earlier?". That's why the bot would say
    # "we just started talking" to a user who had chatted for 3 days.
    if past_sessions_summary:
        past_sessions_block = past_sessions_summary
    else:
        past_sessions_block = "(no past chat history found for this user)"

    msgs = []
    for m in history[-6:]:
        msgs.append({"role": m["role"], "content": m["content"]})
    user_content = (f"SESSION_MEMORY: {mem}\n\n"
                    f"USER_LONG_TERM_MEMORY:\n{memory_block}\n\n"
                    f"PAST_CHAT_HISTORY_ACROSS_ALL_SESSIONS (most recent last):\n{past_sessions_block}\n\n"
                    f"PRODUCT CONTEXT FROM DATABASE:\n{ctx_block}\n\n"
                    f"USER MESSAGE: {question}")
    msgs.append({"role":"user","content": user_content})
    return msgs

def build_past_sessions_summary(user_id, current_session_id, max_messages=40):
    """
    Pull this user's FULL persisted chat history from MongoDB (across every
    past session/day, not just the current in-RAM session) and turn it into
    a compact, dated summary block for the LLM.

    This is what was MISSING before: the bot only ever saw (a) the current
    session's in-RAM history, which resets per session_id/server restart,
    and (b) a narrow semantic-search memory that doesn't reliably answer
    broad recall questions like "what did I look at this morning?". Actual
    saved Mongo chat history was only ever used for the /history endpoint
    (frontend replay on page load) and never reached the LLM prompt at all.

    Keeps only USER messages (what they searched/asked) grouped by local
    calendar date, most recent last, capped at max_messages to keep the
    prompt small. Excludes the current session so we don't duplicate what's
    already sent via the in-session `history` list.
    """
    if not user_id:
        return ""
    try:
        all_msgs = mongodb.load_history_for_user(user_id, limit=500)
    except Exception as e:
        print(f"[app.py] build_past_sessions_summary error: {e}")
        return ""
    if not all_msgs:
        return ""

    # Only user-authored messages are useful for "what did I search for"
    # recall — assistant replies are mostly product cards / boilerplate.
    user_msgs = [m for m in all_msgs if m.get("role") == "user"]
    if not user_msgs:
        return ""

    # Group by calendar date (from the stored ISO timestamp) so the LLM can
    # answer date-relative questions ("this morning", "yesterday", "3 days
    # ago") instead of just dumping an undated list.
    from collections import OrderedDict
    grouped = OrderedDict()
    for m in user_msgs[-max_messages:]:
        ts = m.get("timestamp", "")
        date_label = ts.split("T")[0] if "T" in ts else (ts[:10] if ts else "unknown date")
        grouped.setdefault(date_label, []).append(m.get("content", ""))

    lines = []
    for date_label, msgs in grouped.items():
        joined = "; ".join(msgs)
        lines.append(f"[{date_label}] {joined}")
    return "\n".join(lines)


CATEGORY_KEYWORDS = {
    "shirts":     ["shirt"],
    "shirt":      ["shirt"],
    "t-shirts":   ["t-shirt","tshirt"," tee","tank"],
    "t-shirt":    ["t-shirt","tshirt"," tee","tank"],
    "tshirts":    ["t-shirt","tshirt"," tee"],
    "tops":       ["top","blouse","tank","tee","t-shirt","tshirt"],
    "top":        ["top","blouse","tank","tee"],
    "jeans":      ["jean","denim"],
    "denim":      ["jean","denim"],
    "trousers":   ["trouser","pant","chino","jogger","cargo"],
    "trouser":    ["trouser","pant","chino"],
    "kurtas":     ["kurta","kurti"],
    "kurta":      ["kurta","kurti"],
    "kurtis":     ["kurta","kurti"],
    "kurti":      ["kurta","kurti"],
    "jackets":    ["jacket","blazer","coat","bomber","windcheater"],
    "jacket":     ["jacket","blazer","coat","bomber"],
    "shoes":      ["shoe","sneaker","sandal","boot","slide","oxford","loafer","heel","footwear","slipper","mule","flat","wedge","pump","espadrille"],
    "shoe":       ["shoe","sneaker","sandal","boot","slide","oxford","loafer","heel"],
    "accessories":["belt","wallet","cap","hat","sunglass","watch","tie","scarf","muffler","purse","clutch","bag","sling","backpack","tote"],
    "perfumes":   ["perfume","fragrance","deo","cologne","deodorant","eau de"],
    "perfume":    ["perfume","fragrance","deo","cologne","deodorant"],
    "bags":       ["bag","tote","sling","clutch","purse","backpack","handbag"],
    "bag":        ["bag","tote","sling","clutch","purse","backpack"],
    "sarees":     ["saree","sari"],
    "saree":      ["saree","sari"],
    "lehengas":   ["lehenga","lehnga","lehenga choli"],
    "lehenga":    ["lehenga","lehnga"],
    "dresses":    ["dress","frock","gown","jumpsuit","romper"],
    "dress":      ["dress","frock","gown"],
    "skirts":     ["skirt"],
    "skirt":      ["skirt"],
    "jewellery":  ["jewellery","jewelry","necklace","earring","bracelet","ring","bangle","anklet","pendant","chain","choker"],
    "jewelry":    ["jewellery","jewelry","necklace","earring","bracelet","ring","bangle","anklet"],
    "necklaces":  ["necklace","pendant","chain","choker"],
    "earrings":   ["earring","stud","hoop","drop"],
    "bracelets":  ["bracelet","bangle","cuff"],
    "rings":      ["ring"],
    "bangles":    ["bangle","bracelet"],
    "watches":    ["watch","timepiece"],
    "palazzo":    ["palazzo"],
    "palazzos":   ["palazzo"],
    "dupatta":    ["dupatta","scarf","stole"],
    "skincare":   ["skin","serum","moisturiser","moisturizer","face wash","sunscreen","toner","cleanser"],
    "makeup":     ["makeup","cosmetic","lip","foundation","mascara","kajal","blush","eyeshadow"],
    "boys clothes":["shirt","t-shirt","pant","trouser","kurta","short","jacket"],
    "girls clothes":["dress","top","skirt","kurti","frock","legging"],
    "school bags": ["bag","backpack"],
    "gift sets":   ["gift"],
    "gifts":       ["gift"],
    "swimwear":    ["swim","swimwear","bikini","swimsuit"],
    "co-ord":      ["co-ord","coord","co ord","set"],
    "sets":        ["co-ord","coord","set"],
    "indo western":["indo western","indo-western","fusion"],
    "loungewear":  ["lounge","pyjama","pyjamas","nightwear","sleepwear","shorts"],
    "activewear":  ["active","sport","gym","yoga","workout","athleisure"],
}

def cat_filters_for(cat):
    if not cat: return []
    cl = cat.lower().strip()
    # Normalize hyphens/spaces so "t-shirt", "t shirt", "tshirt" all hit same key
    cl_norm = re.sub(r"[-\s]+", "", cl)
    if cl in CATEGORY_KEYWORDS:
        return CATEGORY_KEYWORDS[cl]
    if cl_norm in CATEGORY_KEYWORDS:
        return CATEGORY_KEYWORDS[cl_norm]
    for key, vals in CATEGORY_KEYWORDS.items():
        key_norm = re.sub(r"[-\s]+", "", key)
        if key in cl or cl in key or key_norm in cl_norm or cl_norm in key_norm:
            return vals
    return [cl_norm, cl]  # fallback: both normalized and raw forms

def multi_stage_product_fetch(context_docs, llm_category, llm_gender, sess_ctx, question):
    """
    APPROACH 2 — Smarter Query Construction, top_k=8 throughout.

    No attribute (e.g. plain "jeans"):
        Reuse already-fetched context_docs (k=8). No extra DB call.

    With attribute (e.g. "white jeans", "silver earrings"):
        Split the query into chunks — each attribute word, the category,
        the gender, AND the combined "attribute category" phrase (e.g.
        "white shirt") — and run a separate, wide vector search per
        chunk (k=60 each) instead of one combined-phrase search alone.
        This catches titles like "White Other Test Jeans" where "white"
        and "jeans" are both present but not adjacent, AND rare
        attribute-specific items (e.g. "white shirt" among thousands of
        "white" products of every other type) that a narrow top-k
        window on an isolated single-word search would otherwise miss.
        All chunk results are merged into one de-duplicated candidate
        pool, the existing strict exact-match filter (attribute_filters/
        cat_filters) is applied over that whole pool, and only the
        final exact-matched products are capped at top_k = 8.

    Budget fallback: price range given but excluded everything → retry
        without budget. Category/attribute/gender stay strict always.
    """
    effective_gender = llm_gender or sess_ctx.get("gender")
    cf      = cat_filters_for(llm_category or "")
    cf_excl = cat_excludes_for(llm_category or "")
    attrs   = extract_attributes(question)
    if not attrs:
        attrs = sess_ctx.get("last_attributes", [])
    price_min, price_max = extract_price_range(question)

    TOP_K = 8  # final exact-match result cap, used throughout this function

    def _fetch(docs, with_budget=True, limit=6):
        return docs_to_cards(
            docs, cat_filters=cf, limit=limit,
            gender=effective_gender,
            price_min=(price_min if with_budget else None),
            price_max=(price_max if with_budget else None),
            exclude_filters=cf_excl,
            attribute_filters=attrs,
        )

    if attrs and retriever is not None:
        # ── Chunked exact-match search ──────────────────────────────
        # PROBLEM: a single combined query like "white jeans men" was
        # sent to the vector DB as one phrase, and vector search ranks
        # candidates by *semantic* closeness to the whole phrase — not
        # by whether each individual word is actually present. A title
        # like "White Other Test Jeans" (a genuine exact-word match —
        # both "white" and "jeans" appear in it) can still rank outside
        # the top k=8 window for that combined-phrase search, so it
        # never even reaches the strict exact-match filter below and
        # gets dropped — even though it should have matched.
        #
        # FIX: split the query into its individual chunks (each
        # attribute word, the category, the gender) and run a separate,
        # wider vector search per chunk. Merge all the candidates from
        # every chunk into one de-duplicated pool, THEN apply the
        # existing strict exact-match filter (attribute_filters /
        # cat_filters inside docs_to_cards) over that whole pool.
        # Only the final, already-exact-matched results are capped at
        # top_k = 8 — not the raw retrieval, so a real match buried
        # deep in one chunk's results still survives.
        chunks = list(attrs)  # e.g. ["white"]
        if llm_category:
            chunks.append(llm_category)
        elif question:
            chunks.append(question)
        if effective_gender:
            chunks.append(effective_gender)
        # ALSO add a combined "attribute + category" chunk (e.g. "white shirt"),
        # not just the isolated single words. A lone "white" chunk's top-K is
        # dominated by every white product across the whole catalog (bags,
        # shoes, jewellery...), drowning out white shirts specifically. The
        # combined phrase steers the embedding search directly toward the
        # actual thing the user wants, giving white shirts a real chance to
        # land in this chunk's own top-K — on top of (not instead of) the
        # individual-word chunks above.
        if llm_category:
            for attr in attrs:
                combo = f"{attr} {llm_category}".strip()
                if combo:
                    chunks.append(combo)
        # de-dupe chunks (case-insensitive) while preserving order
        seen_chunks = set()
        clean_chunks = []
        for c in chunks:
            c = (c or "").strip()
            if c and c.lower() not in seen_chunks:
                seen_chunks.add(c.lower())
                clean_chunks.append(c)

        CHUNK_SEARCH_K = 60  # wider net per chunk so exact matches aren't cut off early —
        # bumped from 20: a single-word chunk like "white" matches thousands of
        # unrelated products (bags, shoes, jewellery) across the whole catalog,
        # so a narrow top-20 window often misses attribute-specific items like
        # "white shirt" entirely. A wider pool gives the later strict
        # attribute_filters/cat_filters match in docs_to_cards a real chance
        # to find genuine matches buried deeper in the semantic ranking.

        candidate_docs = []
        seen_keys = set()
        for chunk in clean_chunks:
            # DEFENSIVE: one bad/failing chunk search must never take down
            # the whole /chat request — multi_stage_product_fetch is called
            # with no try/except around it in the /chat route, so an
            # uncaught exception here would 500 the entire request and skip
            # both the answer AND the mongodb.save_message() history save
            # that happens later in that route. Catch, log, and move on.
            try:
                chunk_docs = search_query(retriever, chunk, k=CHUNK_SEARCH_K) or []
            except Exception as _chunk_err:
                print(f"[multi_stage_product_fetch] search_query failed for chunk={chunk!r}: {_chunk_err}")
                continue
            for doc in chunk_docs:
                d = doc.metadata if hasattr(doc, "metadata") else (doc if isinstance(doc, dict) else {})
                key = (d.get("url") or d.get("link") or "").strip().rstrip("/")
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                candidate_docs.append(doc)

        # Fallback: if every chunk search failed/returned nothing, fall back
        # to the original single combined-phrase query so we still try
        # something rather than silently giving up.
        if not candidate_docs:
            try:
                exact_q = f"{' '.join(attrs)} {llm_category or question} {effective_gender or ''}".strip()
                candidate_docs = search_query(retriever, exact_q, k=TOP_K) or []
            except Exception as _fallback_err:
                print(f"[multi_stage_product_fetch] fallback combined-query search failed: {_fallback_err}")
                candidate_docs = []

        products = _fetch(candidate_docs, limit=TOP_K)
        if products:
            return products
        all_docs_seen = candidate_docs
    else:
        # No attribute — reuse LLM context_docs, no extra DB call
        products = _fetch(context_docs)
        if products:
            return products
        all_docs_seen = list(context_docs)

        # FALLBACK: the user may have typed/clicked an exact, specific
        # product name (e.g. "Studiowest Litchi Eau De Parfum" from a
        # suggested chip) that contains no recognized color/material
        # attribute word — so the wide chunked search above never ran,
        # and we only checked the LLM's generic top-8 context_docs,
        # which can easily miss one specific product among thousands of
        # similar ones (e.g. dozens of other perfumes). Run one more
        # wide, direct search using the user's exact question text
        # before giving up — this is the same widening idea as the
        # attribute branch, just keyed off the literal question instead
        # of a detected attribute word.
        if retriever is not None and question and question.strip():
            try:
                wide_docs = search_query(retriever, question, k=60) or []
            except Exception as _wide_err:
                print(f"[multi_stage_product_fetch] wide direct-question search failed: {_wide_err}")
                wide_docs = []
            if wide_docs:
                products = _fetch(wide_docs, limit=6)
                if products:
                    return products
                all_docs_seen = wide_docs

    # Budget fallback: retry without price filter if it excluded everything.
    # Category, attribute and gender are NOT relaxed.
    if price_min is not None or price_max is not None:
        products = _fetch(all_docs_seen, with_budget=False, limit=(TOP_K if attrs else 6))
        if products:
            return products

    # Nothing matched — return empty, never substitute unrelated products.
    return []

# ── Routes ─────────────────────────────────────────────────────
# ── Auth Routes ────────────────────────────────────────────────
@app.route("/auth/anon", methods=["POST"])
def auth_anon():
    """Register anonymous user. Frontend generates user_id locally and sends it."""
    try:
        data    = request.get_json(force=True) or {}
        user_id = (data.get("user_id") or "").strip()
        result  = mongodb.register_anon_user(user_id)
        return jsonify({"status": result})
    except Exception as ex:
        print(f"[app.py] /auth/anon error: {ex}")
        return jsonify({"status": "error", "message": str(ex)}), 500


@app.route("/auth/capture-email", methods=["POST"])
def capture_email():
    """Attach email to an existing anonymous user."""
    try:
        data    = request.get_json(force=True) or {}
        user_id = (data.get("user_id") or "").strip()
        email   = (data.get("email") or "").strip().lower()

        if not user_id or not email:
            return jsonify({"status": "error", "message": "user_id and email required"}), 400

        print(f"[app.py] /auth/capture-email called: user_id={user_id!r} email={email!r}")
        result = mongodb.update_user_email(user_id, email)
        print(f"[app.py] /auth/capture-email result for user_id={user_id!r}: {result}")
        if result == "ok":
            return jsonify({"status": "ok"})
        elif result == "already_used":
            return jsonify({"status": "already_used", "message": "This email is already registered."})
        else:
            return jsonify({"status": "error", "message": "DB not connected"}), 500
    except Exception as ex:
        print(f"[app.py] /auth/capture-email error: {ex}")
        return jsonify({"status": "error", "message": str(ex)}), 500


@app.route("/auth/signup", methods=["POST"])
def auth_signup():
    try:
        data     = request.get_json(force=True) or {}
        email    = (data.get("email") or "").strip().lower()
        password = (data.get("password") or "").strip()
        name     = (data.get("name") or "").strip()

        if not email or not password:
            return jsonify({"status": "error", "message": "Email and password required"}), 400
        if not email.endswith("@gmail.com"):
            return jsonify({"status": "error", "message": "Please use a Gmail address (@gmail.com)"}), 400
        if len(password) < 6:
            return jsonify({"status": "error", "message": "Password must be at least 6 characters"}), 400

        result, user_id = mongodb.signup_user(email, password, name)
        if result == "ok":
            return jsonify({"status": "ok", "user_id": user_id, "email": email,
                            "name": name or email.split("@")[0]})
        elif result == "exists":
            return jsonify({"status": "exists", "message": "Email already registered. Please log in."}), 409
        else:
            return jsonify({"status": "error", "message": "Database not connected. Try again."}), 500
    except Exception as ex:
        print(f"[app.py] /auth/signup error: {ex}")
        return jsonify({"status": "error", "message": str(ex)}), 500


@app.route("/auth/login", methods=["POST"])
def auth_login():
    try:
        data     = request.get_json(force=True) or {}
        email    = (data.get("email") or "").strip().lower()
        password = (data.get("password") or "").strip()

        if not email or not password:
            return jsonify({"status": "error", "message": "Email and password required"}), 400
        if not email.endswith("@gmail.com"):
            return jsonify({"status": "error", "message": "Please use your Gmail address (@gmail.com)"}), 400

        result, user = mongodb.login_user(email, password)
        if result == "ok":
            return jsonify({"status": "ok", "user_id": user["user_id"],
                            "email": user["email"], "name": user.get("name", "")})
        elif result == "not_found":
            return jsonify({"status": "error", "message": "No account found with this email."}), 404
        elif result == "wrong_password":
            return jsonify({"status": "error", "message": "Incorrect password."}), 401
        else:
            return jsonify({"status": "error", "message": "Database not connected. Try again."}), 500
    except Exception as ex:
        print(f"[app.py] /auth/login error: {ex}")
        return jsonify({"status": "error", "message": str(ex)}), 500


@app.route("/auth/google", methods=["POST"])
def auth_google():
    try:
        data    = request.get_json(force=True) or {}
        email   = (data.get("email") or "").strip().lower()
        name    = (data.get("name") or "").strip()
        picture = (data.get("picture") or "").strip()

        if not email:
            return jsonify({"status": "error", "message": "Email required"}), 400

        user_id = mongodb.google_auth_user(email, name, picture)
        if user_id:
            return jsonify({"status": "ok", "user_id": user_id, "email": email, "name": name})
        else:
            return jsonify({"status": "error", "message": "Database not connected."}), 500
    except Exception as ex:
        print(f"[app.py] /auth/google error: {ex}")
        return jsonify({"status": "error", "message": str(ex)}), 500


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/chat", methods=["POST"])
def chat():
    data     = request.get_json()
    question = data.get("question","").strip()
    session  = data.get("session_id","default")
    user_id  = data.get("user_id","").strip()
    if not question:
        return jsonify({"error":"Empty question"}), 400

    if session not in chat_histories:
        chat_histories[session]  = []
        session_context[session] = {"gender":None,"last_browse":None,"last_subcats":[],
                                      "last_category":None,"last_product":None,
                                      "last_attributes":[],
                                      "_last_q":None,"_last_q_time":0,"_last_response":None}
    history  = chat_histories[session]
    sess_ctx = session_context[session]

    # Duplicate-send guard: the same exact message arriving twice within a
    # few seconds is almost always an accidental double-click/double-submit
    # from the client, not two genuine questions. Re-serve the cached
    # answer instead of re-querying the LLM and double-saving history.
    import time as _time
    _now = _time.time()
    if (question == sess_ctx.get("_last_q")
            and sess_ctx.get("_last_response")
            and (_now - sess_ctx.get("_last_q_time", 0)) < 5):
        return jsonify(sess_ctx["_last_response"])
    sess_ctx["_last_q"]      = question
    sess_ctx["_last_q_time"] = _now

    # If the user typed an email address directly in the chat (not via the
    # capture-email popup), still update it on their user document so it's
    # never lost. Doesn't block/alter the normal chat flow below.
    if user_id:
        typed_email = extract_email(question)
        if typed_email:
            result = mongodb.update_user_email(user_id, typed_email)
            print(f"[app.py] Email captured from chat message for user={user_id}: {typed_email} -> {result}")

    # Retrieve this user's long-term memory (facts from past visits/sessions)
    user_memory = retrieve_user_memory(user_id, question) if user_id else []

    # Retrieve this user's actual past chat history across ALL previous
    # sessions/days from MongoDB — this is the piece that was missing and
    # caused the bot to say "we just started talking" to returning users.
    past_sessions_summary = build_past_sessions_summary(user_id, session) if user_id else ""

    # Retrieve
    enriched_q = question
    if sess_ctx.get("gender"):
        enriched_q = f"{sess_ctx['gender']} {question}"
    context_docs = retrieve(enriched_q)

    # Call LLM
    msgs = build_llm_messages(question, history, context_docs, sess_ctx,
                               user_memory=user_memory,
                               past_sessions_summary=past_sessions_summary)
    raw  = call_llm(MASTER_SYSTEM, msgs)
    raw  = re.sub(r'^```[a-z]*\n?','', raw.strip())
    raw  = re.sub(r'\n?```$','', raw.strip())

    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {"type":"general","gender":None,"category":None,
                  "answer":raw,"chips":[],"show_products":False,"more_options":[]}

    llm_type     = parsed.get("type","general")
    llm_gender   = parsed.get("gender")
    llm_category = parsed.get("category")
    answer       = parsed.get("answer","")
    chips        = parsed.get("chips") or []
    show_products= bool(parsed.get("show_products", False))
    more_options = parsed.get("more_options") or []

    # Update session memory
    if llm_gender:
        sess_ctx["gender"] = llm_gender
    if llm_type == "browse":
        sess_ctx["last_browse"]  = llm_gender
        sess_ctx["last_subcats"] = chips[:]
        sess_ctx["last_attributes"] = []   # new browse = clear old attribute filter
    if llm_type == "product":
        remaining = [c for c in sess_ctx.get("last_subcats",[])
                     if c.lower().rstrip("s") != (llm_category or "").lower().rstrip("s")
                     and c.lower() != (llm_category or "").lower()]
        sess_ctx["last_subcats"] = remaining
        if not more_options:
            more_options = remaining[:8]
        if llm_category:
            sess_ctx["last_category"] = llm_category
        # Save attributes from this question for followup turns
        # e.g. "white t-shirt" → next "show more" still keeps white filter
        current_attrs = extract_attributes(question)
        if current_attrs:
            sess_ctx["last_attributes"] = current_attrs
        # If no new attribute in this message, keep whatever was saved before

    # Fetch product cards
    products = []
    if show_products:
        # SAFETY NET: this call used to be unguarded — any exception inside
        # it (vector DB error, bad search_query call, etc.) would 500 the
        # whole /chat request and skip everything below, INCLUDING the
        # mongodb.save_message() calls further down that persist chat
        # history. Wrapping it means a product-search failure degrades to
        # "no products found" instead of silently breaking history saving
        # and returning no answer at all.
        try:
            products = multi_stage_product_fetch(
                context_docs, llm_category, llm_gender, sess_ctx, question
            )
        except Exception as _fetch_err:
            print(f"[app.py] multi_stage_product_fetch failed: {_fetch_err}")
            import traceback as _tb
            _tb.print_exc()
            products = []
        if products:
            sess_ctx["last_product"] = products[0]
        else:
            # The LLM's `answer` text was written assuming products
            # would be shown (e.g. "Here are some Silver Earrings...").
            # Since strict matching found none, replace it with an
            # honest message instead of leaving that promise dangling
            # with no products attached to it.
            attrs_asked = extract_attributes(question)
            descriptor = " ".join(attrs_asked + ([llm_category] if llm_category else [])).strip()
            descriptor = descriptor or (llm_category or "that")
            answer = (
                f"Sorry, we don't currently have {descriptor} available 😔\n\n"
                "Would you like to see other options instead?"
            )
            show_products = False

    # ── Memory-based recommendations ────────────────────────────
    # STRICT RULE: only show recommendations if the user has past
    # history/memory for THIS EXACT category/product they searched
    # right now (e.g. searched t-shirts before -> recommend t-shirts,
    # NOT jewellery). If there's no matching history for this category,
    # the recommendations section is fully suppressed (empty list).
    #
    # NOTE: recommendations are fetched for ALL message types (not just
    # show_products=true) so that history-based browsing and general
    # questions also surface past-interest suggestions.
    # Gender is passed to prevent cross-gender product suggestions.
    recommendations = []
    if user_id and llm_category:
        try:
            # Use LLM gender first, fall back to session gender memory
            effective_gender = llm_gender or sess_ctx.get("gender")
            rec_docs = get_recommendations_from_memory(
                user_id, retriever, current_category=llm_category, k=4,
                current_gender=effective_gender
            )
            if rec_docs:
                # Convert to card format (same as docs_to_cards but from search_query output)
                seen_rec_names = set(p["name"].lower() for p in products)
                effective_gender_low = (effective_gender or "").lower().strip()
                for doc in rec_docs:
                    pname = (doc.get("product_name") or "").strip()
                    url   = (doc.get("url") or "").strip()
                    if not pname or not url or pname.lower() in seen_rec_names:
                        continue
                    # Gender guard — rec_docs come from get_recommendations_from_memory
                    # which already filters, but double-check here as a safety net.
                    if effective_gender_low:
                        brand_low_rec = (doc.get("brand") or "").lower().strip()
                        if not gender_allows(
                            pname.lower() + " " + url.lower(),
                            effective_gender_low,
                            brand_low=brand_low_rec
                        ):
                            continue
                    seen_rec_names.add(pname.lower())
                    avail = doc.get("availability", "InStock")
                    recommendations.append({
                        "name":           pname,
                        "brand":          (doc.get("brand") or "Westside").strip(),
                        "price":          doc.get("price") or "",
                        "url":            url,
                        "image":          get_image(doc),
                        "availability":   avail,
                        "matched_reason": doc.get("matched_reason", ""),
                    })
                    if len(recommendations) >= 4:
                        break
        except Exception as _rec_err:
            print(f"[app.py] Recommendation fetch error: {_rec_err}")

    # Append product text list to answer (name, brand, price, stock, link)
    if products:
        lines = []
        for p in products:
            avail = (p.get("availability") or "").lower()
            stock = "✅ In Stock" if "out" not in avail else "❌ Out of Stock"
            price = f"\u20b9{p['price']}" if p.get("price") else ""
            brand = p.get("brand", "Westside")
            name  = p.get("name", "")
            url   = p.get("url", "")
            lines.append(f"**{name}**\n{brand} | {price} | {stock}\n{url}")
        answer = answer.rstrip() + "\n\n" + "\n\n".join(lines)

    history.append({"role":"user","content":question})
    history.append({"role":"assistant","content":answer})
    if len(history) > 20:
        chat_histories[session] = history[-20:]

    # Persist to MongoDB — only if user_id is present
    if user_id:
        mongodb.save_message(user_id, session, "user", question)
        mongodb.save_message(user_id, session, "assistant", answer)
        # record_unique_user only for legacy/anon ids (not auth users u_/g_ prefix)
        if not (user_id.startswith("u_") or user_id.startswith("g_")):
            mongodb.record_unique_user(user_id)

        # Real-time memory extraction: ask the LLM what's worth remembering
        # from this turn (name, location, product interest, preferences) and
        # store it as embeddings in the user_memory ChromaDB collection.
        extract_and_store_memory(llm_client, LLM_MODEL, user_id, question, answer)
    else:
        print(f"[app.py] WARNING: chat message not saved — no user_id in request")

    response_payload = {
        "answer":          answer,
        "products":        products,
        "chips":           chips,
        "more_options":    more_options,
        "recommendations": recommendations,
    }
    sess_ctx["_last_response"] = response_payload
    return jsonify(response_payload)

@app.route("/history", methods=["GET"])
def get_history():
    """Return this user's full past chat history from MongoDB (for page reload / flat replay)."""
    user_id = request.args.get("user_id","").strip()
    if not user_id:
        return jsonify({"history":[]})
    history = mongodb.load_history_for_user(user_id)
    return jsonify({"history": history})


@app.route("/clear", methods=["POST"])
def clear():
    data    = request.get_json(force=True) or {}
    session = data.get("session_id","default")
    # Only clear in-memory session — history in MongoDB stays untouched
    chat_histories[session]  = []
    session_context[session] = {"gender":None,"last_browse":None,"last_subcats":[],
                                  "last_category":None,"last_product":None,
                                  "_last_q":None,"_last_q_time":0,"_last_response":None}
    return jsonify({"status":"cleared"})

@app.route("/inquiries", methods=["GET"])
def get_inquiries():
    """
    Return grouped inquiry cards for the Messages panel.
    Groups chat_history by session_id, uses first user message as title.
    """
    user_id = request.args.get("user_id", "").strip()
    if not user_id:
        return jsonify({"inquiries": []})
    try:
        from mongodb import chat_history_collection
        if chat_history_collection is None:
            return jsonify({"inquiries": []})

        # Get all messages for this user, sorted by time
        cursor = chat_history_collection.find(
            {"user_id": user_id},
            sort=[("timestamp", 1)]
        )

        # Group by session_id
        sessions = {}
        session_order = []
        for m in cursor:
            sid = m.get("session_id", "default")
            if sid not in sessions:
                sessions[sid] = {"messages": [], "created_at": m["timestamp"]}
                session_order.append(sid)
            sessions[sid]["messages"].append({
                "role":      m["role"],
                "content":   m["content"],
                "timestamp": m["timestamp"].isoformat(),
            })

        # Build inquiry list (newest first)
        inquiries = []
        for sid in reversed(session_order):
            sess = sessions[sid]
            msgs = sess["messages"]
            # Title = first user message, fallback to "Shopping session"
            title = next(
                (msg["content"][:60] for msg in msgs if msg["role"] == "user"),
                "Shopping session"
            )
            inquiries.append({
                "id":         sid,
                "title":      title,
                "created_at": sess["created_at"].isoformat(),
                "messages":   msgs,
            })

        return jsonify({"inquiries": inquiries})
    except Exception as e:
        print(f"[app.py] ERROR in get_inquiries: {e}")
        return jsonify({"inquiries": []})


@app.route("/inquiries/<session_id>", methods=["DELETE"])
def delete_inquiry(session_id):
    """Delete all messages for a specific session_id for this user."""
    user_id = request.args.get("user_id", "").strip()
    if not user_id:
        return jsonify({"status": "error", "message": "No user_id"}), 400
    try:
        from mongodb import chat_history_collection
        if chat_history_collection is not None:
            chat_history_collection.delete_many({
                "user_id":    user_id,
                "session_id": session_id,
            })
        # Also clear in-memory if same session
        if session_id in chat_histories:
            del chat_histories[session_id]
        if session_id in session_context:
            del session_context[session_id]
        return jsonify({"status": "deleted"})
    except Exception as e:
        print(f"[app.py] ERROR in delete_inquiry: {e}")
        return jsonify({"status": "error"}), 500


@app.route("/health")
def health():
    return jsonify({"status":"ok"}), 200

@app.route("/debug", methods=["POST"])
def debug():
    """Debug endpoint — shows raw retriever output and card results."""
    data     = request.get_json()
    query    = data.get("query", "men shirts")
    cat      = data.get("category", "")
    raw_docs = retrieve(query)

    # Show first 5 raw docs
    raw_sample = []
    for i, doc in enumerate(raw_docs[:5]):
        d = doc.metadata if hasattr(doc, "metadata") else (doc if isinstance(doc, dict) else {})
        raw_sample.append({
            "index": i,
            "keys": list(d.keys()),
            "product_name": d.get("product_name",""),
            "url": d.get("url",""),
            "brand": d.get("brand",""),
            "image_url": d.get("image_url",""),
            "price": d.get("price",""),
        })

    # Show what cards come out
    cf = cat_filters_for(cat) if cat else []
    cards_with_filter = docs_to_cards(raw_docs, cat_filters=cf, limit=6)
    cards_no_filter   = docs_to_cards(raw_docs, cat_filters=[], limit=6)

    return jsonify({
        "query": query,
        "category": cat,
        "cat_filters": cf,
        "total_docs": len(raw_docs),
        "raw_sample": raw_sample,
        "cards_with_filter": cards_with_filter,
        "cards_no_filter": cards_no_filter,
    })

def _rebuild_vector_db(pages):
    """Re-run cleaning → chunking → embedding for a list of pages."""
    from cleaning      import clean_all_pages
    from chunking      import chunk_pages
    from embeddingVector import load_embedding_model, create_chroma_collection, store_embeddings

    cleaned = clean_all_pages(pages)
    if not cleaned:
        print("[scheduler] No cleaned pages — skipping embedding rebuild")
        return False

    chunks = chunk_pages(cleaned)
    if not chunks:
        print("[scheduler] No chunks — skipping embedding rebuild")
        return False

    model      = load_embedding_model()
    collection = create_chroma_collection()   # drops & recreates collection
    store_embeddings(chunks, model, collection)
    return True


def _reload_retriever():
    """Hot-swap the global retriever to point at freshly rebuilt ChromaDB."""
    global retriever
    try:
        from retriever import load_vector_db, create_retriever
        vdb      = load_vector_db()
        retriever = create_retriever(vdb)
        print("[scheduler] Retriever reloaded with new data")
    except Exception as e:
        print(f"[scheduler] ERROR reloading retriever: {e}")


def run_scheduled_scrape():
    """
    [CHANGE] Full incremental scrape + rebuild pipeline.
    Called by APScheduler on the configured interval.
    """
    # Prevent overlapping runs
    if not _scrape_lock.acquire(blocking=False):
        print("[scheduler] Scrape already running — skipping this cycle")
        return

    from mongodb import now_ist
    _scrape_status["running"]  = True
    _scrape_status["last_run"] = now_ist().isoformat()

    try:
        print("\n[scheduler] ══════ Starting scheduled scrape ══════")
        from scrapping import scrape_incremental

        # Load existing active data for comparison
        old_pages = []
        if os.path.exists("scraped.json"):
            with open("scraped.json", "r", encoding="utf-8") as f:
                old_pages = json.load(f)
        print(f"[scheduler] Loaded {len(old_pages)} old pages for comparison")

        # Run incremental scrape — returns full new page list + change sets
        all_new_pages, changed_urls, new_urls = scrape_incremental(old_pages)

        # Write staging file — users still see scraped.json during this
        # scraped_new.json is the staging file; scraped.json stays live
        with open("scraped_new.json", "w", encoding="utf-8") as f:
            json.dump(all_new_pages, f, ensure_ascii=False, indent=2)
        print(f"[scheduler] Wrote scraped_new.json ({len(all_new_pages)} pages)")

        if not changed_urls and not new_urls:
            print("[scheduler] No changes detected — skipping vector rebuild")
            _scrape_status["last_result"]  = "no_changes"
            _scrape_status["new_count"]    = 0
            _scrape_status["changed_count"] = 0
            # Still do the file swap so timestamps are current
        else:
            # Only rebuild vector DB for pages that actually changed
            pages_to_index = [
                p for p in all_new_pages
                if p.get("url","").rstrip("/") in (changed_urls | new_urls)
            ]
            print(f"[scheduler] Rebuilding vectors for {len(pages_to_index)} changed/new pages")
            ok = _rebuild_vector_db(all_new_pages)   # full rebuild for consistency
            if ok:
                _reload_retriever()
            _scrape_status["last_result"]   = "ok"
            _scrape_status["new_count"]     = len(new_urls)
            _scrape_status["changed_count"] = len(changed_urls)

        # [CHANGE] Atomic file swap: old active → backup, new staging → active
        if os.path.exists("scraped.json"):
            shutil.copy2("scraped.json", "scraped_old.json")   # backup
        shutil.move("scraped_new.json", "scraped.json")         # promote staging
        print("[scheduler] File swap complete: scraped_new.json → scraped.json")
        print("[scheduler] ══════ Scrape cycle finished ══════\n")

    except Exception as e:
        import traceback
        print(f"[scheduler] ERROR during scrape: {e}")
        traceback.print_exc()
        _scrape_status["last_result"] = "error"
    finally:
        _scrape_status["running"] = False
        _scrape_lock.release()

_scheduler = BackgroundScheduler()
_scheduler.add_job(
    run_scheduled_scrape,
    trigger="cron",            
    hour=18,                 
    minute=28,                  
    id="westside_scrape",
    replace_existing=True,
    max_instances=1,            
)
_scheduler.start()
print("[scheduler] APScheduler started — scrape daily ")


@app.route("/scrape-status", methods=["GET"])
def scrape_status():
    """[CHANGE] Endpoint to check current scrape job state from a browser or admin panel."""
    return jsonify(_scrape_status)


@app.route("/scrape-now", methods=["POST"])
def scrape_now():
    """[CHANGE] Manually trigger a scrape immediately (admin use)."""
    t = threading.Thread(target=run_scheduled_scrape, daemon=True)
    t.start()
    return jsonify({"status": "started"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)