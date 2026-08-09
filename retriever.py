import re
import chromadb
from sentence_transformers import SentenceTransformer
from settings import (
    EMBEDDING_MODEL,
    CHROMA_DB_PATH,
    CHROMA_COLLECTION,
    TOP_K_RESULTS,
    USER_MEMORY_COLLECTION
)

def load_vector_db():
    print(" Loading Vector Database (chromadb)...")
    client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
    try:
        collection = client.get_collection(name=CHROMA_COLLECTION)
    except Exception:
        collection = client.create_collection(name=CHROMA_COLLECTION)
    print(" Vector DB Loaded")
    return {"client": client, "collection": collection}

def create_retriever(vectordb):
    model = SentenceTransformer(EMBEDDING_MODEL)
    collection = vectordb["collection"]
    client = vectordb.get("client")

    class SimpleRetriever:
        def __init__(self, model, collection, client=None, k=TOP_K_RESULTS):
            self.model = model
            self.collection = collection
            self.client = client
            self.k = k

        def invoke(self, query, k=None):
            # k can be passed by caller to override self.k —
            # used by multi_stage_product_fetch when the user named a
            # specific attribute and we need a much wider candidate pool.
            effective_k = k if k is not None else self.k
            emb = self.model.encode(query).tolist()

            # ── Try 1: products only via where filter ─────────────
            try:
                res = self.collection.query(
                    query_embeddings=[emb],
                    n_results=effective_k * 3,   # fetch more, filter blogs out
                    include=['metadatas', 'documents'],
                    where={"product_name": {"$ne": ""}}  # only docs with product_name
                )
            except Exception:
                # where filter not supported — fallback to plain query
                res = None

            # ── Try 2: plain query, filter blogs in Python ────────
            if res is None or not res.get('documents', [[]])[0]:
                try:
                    res = self.collection.query(
                        query_embeddings=[emb],
                        n_results=effective_k * 5,
                        include=['metadatas', 'documents'],
                    )
                except Exception as e:
                    print(" Retriever query failed:", e)
                    return []

            docs = []
            for i, doc_text in enumerate(res.get('documents', [[]])[0]):
                meta = res.get('metadatas', [[]])[0][i] if res.get('metadatas') else {}

                # Skip blog/category pages — only keep product pages
                url = meta.get("url", "")
                pname = meta.get("product_name", "")
                if not pname or "/products/" not in url:
                    continue

                docs.append(type('D', (), {
                    'page_content': doc_text,
                    'metadata': meta
                }))

                if len(docs) >= effective_k:
                    break

            return docs

    retriever = SimpleRetriever(model, collection, client=client)
    print(f" Retriever Ready (Top {TOP_K_RESULTS})")
    return retriever


def search_query(retriever, query, k=None):
    raw_docs = retriever.invoke(query, k=k)
    if not raw_docs:
        return []
    docs = []
    for doc in raw_docs:
        meta = doc.metadata if hasattr(doc, "metadata") else {}
        docs.append({
            "text":             doc.page_content,
            "title":            meta.get("title", ""),
            "level":            meta.get("level", ""),
            "depth":            meta.get("depth", ""),
            "url":              meta.get("url", ""),
            "product_name":     meta.get("product_name", ""),
            "brand":            meta.get("brand", ""),
            "price":            meta.get("price", ""),
            "currency":         meta.get("currency", "INR"),
            "availability":     meta.get("availability", ""),
            "rating":           meta.get("rating", ""),
            "review_count":     meta.get("review_count", ""),
            "sku":              meta.get("sku", ""),
            "image_url":        meta.get("image_url", ""),
            "images":           meta.get("images", ""),
            "meta_description": meta.get("meta_description", ""),
        })
    return docs


# ─────────────────────────────────────────────────────────────
#  USER MEMORY RETRIEVAL (separate Chroma collection — NOT products)
# ─────────────────────────────────────────────────────────────
# Semantic-searches the user_memory collection (filled by
# embeddingVector.extract_and_store_memory), scoped to ONE user_id,
# so each user's memory stays isolated from every other user's.

_memory_embed_model_r = None
_memory_collection_r  = None

def _get_memory_model_for_retrieval():
    global _memory_embed_model_r
    if _memory_embed_model_r is None:
        _memory_embed_model_r = SentenceTransformer(EMBEDDING_MODEL)
    return _memory_embed_model_r

def _get_memory_collection_for_retrieval():
    global _memory_collection_r
    if _memory_collection_r is not None:
        return _memory_collection_r
    try:
        client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        try:
            _memory_collection_r = client.get_collection(name=USER_MEMORY_COLLECTION)
        except Exception:
            _memory_collection_r = client.create_collection(
                name=USER_MEMORY_COLLECTION,
                metadata={"hnsw:space": "cosine"}
            )
    except Exception as e:
        print(f" ERROR connecting to user_memory collection: {e}")
        _memory_collection_r = None
    return _memory_collection_r

def retrieve_user_memory(user_id, question, k=5):
    """
    Semantic-search the user_memory collection, scoped to this user_id only.
    Returns a list of memory fact strings (most relevant first).
    """
    if not user_id or not question:
        return []

    collection = _get_memory_collection_for_retrieval()
    if collection is None:
        return []

    try:
        model = _get_memory_model_for_retrieval()
        emb   = model.encode(question).tolist()

        # ChromaDB throws if n_results > total docs in collection,
        # or if the where-filtered subset is smaller than n_results.
        # Safe: get total count first and clamp n_results accordingly.
        try:
            total_count = collection.count()
        except Exception:
            total_count = k

        safe_k = max(1, min(k, total_count))

        # Try with user_id filter first
        try:
            res = collection.query(
                query_embeddings=[emb],
                n_results=safe_k,
                include=["documents", "metadatas"],
                where={"user_id": user_id},
            )
            docs = res.get("documents", [[]])[0] if res else []
            # Filter to only this user's facts (extra safety)
            metas = res.get("metadatas", [[]])[0] if res else []
            filtered = [
                d for d, m in zip(docs, metas)
                if d and m.get("user_id") == user_id
            ]
            if filtered:
                return filtered
        except Exception as e:
            print(f" retrieve_user_memory where-filter failed: {e}")

        # Fallback: fetch all and filter in Python
        try:
            res = collection.query(
                query_embeddings=[emb],
                n_results=safe_k,
                include=["documents", "metadatas"],
            )
            docs  = res.get("documents", [[]])[0] if res else []
            metas = res.get("metadatas", [[]])[0] if res else []
            filtered = [
                d for d, m in zip(docs, metas)
                if d and m.get("user_id") == user_id
            ]
            return filtered
        except Exception as e2:
            print(f" retrieve_user_memory fallback failed: {e2}")
            return []

    except Exception as e:
        print(f" ERROR in retrieve_user_memory: {e}")
        return []

GENDER_INCLUDE = {
    "men":   {"men", "mens", "man", "gents", "male"},
    "women": {"women", "womens", "woman", "ladies", "female", "girls", "girl"},
    "kids":  {"kids", "kid", "children", "child", "baby", "toddler", "boys", "girls", "hop"},
}
GENDER_EXCLUDE = {
    "men":   {"women", "womens", "woman", "ladies", "female", "girls", "girl"},
    "women": {"men", "mens", "man", "gents", "male"},
    "kids":  set(),
}

# Brand → gender mapping — used as a hard signal when URL/name has no
# explicit gender words. Women-only brands must NEVER appear in a men's
# context, and vice-versa.  Kids brands are excluded from adult searches.
BRAND_GENDER = {
    # MEN-only brands
    "wes casuals":  "men",
    "wes formals":  "men",
    "wes lounge":   "men",
    "nuoflexx":     "men",
    "ascot":        "men",
    "eta":          "men",
    # WOMEN-only brands
    "nuon":            "women",
    "lov":             "women",
    "wardrobe":        "women",
    "utsa":            "women",
    "gia":             "women",
    "wunderlove":      "women",
    "bombay paisley":  "women",
    "superstar":       "women",
    "vark":            "women",
    "diza":            "women",
    "zuba":            "women",
    "studiowest":      "women",
    "white door x samoh": "women",
    # KIDS-only brands
    "hop":       "kids",
    "hop baby":  "kids",
    "hop kids":  "kids",
    "y&f teen":  "kids",
    "utsa kids": "kids",
}
# Gender-neutral brands (shared) — explicitly allow everywhere
BRAND_NEUTRAL = {"studiofit"}


def gender_allows(combined_text_low, gender_low, brand_low=None):
    """
    True if a product is OK to show for the given gender context.

    Priority order:
    1. Brand mapping (hard signal) — if brand is known gender-specific, enforce it.
    2. Explicit gender keywords in product name / URL.
    3. Gender-neutral (no gender words, no gender brand) → always allow.

    `combined_text_low` should be  pname.lower() + " " + url.lower()
    `brand_low`        should be   brand.lower().strip()  (optional but recommended)
    """
    if not gender_low or gender_low not in GENDER_EXCLUDE:
        return True

    # ── 1. Brand hard-check ────────────────────────────────────────
    if brand_low:
        # Try longest-matching brand key first (e.g. "hop kids" before "hop")
        matched_brand_gender = None
        for b_key in sorted(BRAND_GENDER, key=len, reverse=True):
            if b_key in brand_low or brand_low in b_key:
                matched_brand_gender = BRAND_GENDER[b_key]
                break
        if matched_brand_gender is not None:
            # Neutral brands are always OK
            if brand_low in BRAND_NEUTRAL:
                return True
            # Kids brand → only allowed in kids context
            if matched_brand_gender == "kids":
                return gender_low == "kids"
            # Gender-specific brand → must match requested gender exactly
            return matched_brand_gender == gender_low

    # ── 2. Keyword-based check ─────────────────────────────────────
    text_words = set(re.findall(r"[a-zA-Z]+", combined_text_low))

    # Hard exclude — opposite-gender words present → reject
    if GENDER_EXCLUDE[gender_low] & text_words:
        return False

    # If any gender-include word is present but does NOT match ours → reject
    all_gender_words = GENDER_INCLUDE.get("men", set()) | GENDER_INCLUDE.get("women", set())
    # (kids words overlap with adults so skip them in this check)
    has_any_adult_gender = bool(all_gender_words & text_words)
    has_our_gender       = bool(GENDER_INCLUDE.get(gender_low, set()) & text_words)
    if has_any_adult_gender and not has_our_gender:
        return False

    # ── 3. Gender-neutral → allow ──────────────────────────────────
    return True


if __name__ == "__main__":
    vectordb  = load_vector_db()
    retriever = create_retriever(vectordb)
    while True:
        query = input("\n Ask Question: ")
        if query.lower() in ["exit", "quit"]:
            break
        results = search_query(retriever, query)
        print(f"\n Found {len(results)} results")
        for i, r in enumerate(results, 1):
            print(f"  [{i}] {r.get('product_name') or r.get('title', 'N/A')}")
            print(f"      Price: {r.get('price')} | Brand: {r.get('brand')}")

#  MEMORY-BASED RECOMMENDATIONS

def get_recommendations_from_memory(user_id, retriever_obj, current_category=None, k=4, current_gender=None):
    """
   Use the user's past memory to recommend — BUT STRICTLY only if that memory is related to the current category/product type.
   Rule (as per user's requirement):The history must match only the category/product that the user has searched for in the CURRENT message (current_category).
   If no matching memory is found for that category → do not recommend anything, return an empty list → so the section is not shown in the frontend at all.
   The matching memory fact is also returned as a "matched_reason", so that text like "You searched for this previously..." can be displayed on the frontend.
   If current_category is not provided at all (in case some call site calls it using an old signature), still DO NOT recommend — because the rule is "only if there is a particular category history", so making a blanket recommendation when the "category is not even known" is not correct.
   current_gender: "men"|"women"|"kids"|None — if provided, ONLY recommend products whose name/url matches that gender, preventing cross-gender suggestions (e.g. men's search → no women's products).
   Returns: list of product dicts (search_query format) each with an extra "matched_reason" key.
    """
    if not user_id or retriever_obj is None or not current_category:
        return []

    cat_low = current_category.lower().strip()
    cat_norm = re.sub(r"[-\s]+", "", cat_low)  # "t-shirts" -> "tshirts", "kids wear" -> "kidswear"
    cat_words = set(re.findall(r"[a-zA-Z]+", cat_low))
    if not cat_words:
        return []

    gender_low = (current_gender or "").lower().strip()

    # Search user_memory SPECIFICALLY for this category — not a generic
    # "product category interest" query that pulls in everything.
    # Also include gender in the search query to scope memory retrieval.
    mem_query = f"{gender_low} {current_category}".strip() if gender_low else current_category
    memory_facts = retrieve_user_memory(user_id, mem_query, k=10)
    if not memory_facts:
        # Fallback: try without gender prefix
        memory_facts = retrieve_user_memory(user_id, current_category, k=10)
    if not memory_facts:
        return []

    # Hard literal check: the remembered fact must actually mention this
    # category (or a clear word-overlap with it). Semantic similarity
    # alone is what caused jewellery to show up for a t-shirt search, so
    # we don't trust embeddings here — we require an explicit keyword
    # match against the user's own remembered text.
    relevant_facts = []
    for fact in memory_facts:
        fact_low = fact.lower()
        fact_norm = re.sub(r"[-\s]+", "", fact_low)
        fact_words = set(re.findall(r"[a-zA-Z]+", fact_low))
        if cat_low in fact_low or cat_norm in fact_norm or (cat_words & fact_words):
            relevant_facts.append(fact)

    if not relevant_facts:
        # No real history for THIS category → show nothing.
        return []

    matched_reason = relevant_facts[0]
    memory_query = f"{gender_low} {current_category} ".strip() + " ".join(relevant_facts)

    # Now search products, but ALSO require the returned product itself
    # to belong to this category (name/url match) — otherwise a generic
    # embedding search could still drift to an unrelated product.
    docs = search_query(retriever_obj, memory_query)

    seen_names = set()
    recs = []
    for doc in docs:
        pname = (doc.get("product_name") or "").strip()
        url   = doc.get("url", "")
        if not pname or "/products/" not in url:
            continue

        pname_low  = pname.lower()
        url_low    = url.lower()
        pname_norm = re.sub(r"[-\s]+", "", pname_low)

        # ── Category match (same as before) ──────────────────────
        if (cat_low not in pname_low and cat_norm not in pname_norm
                and cat_low not in url_low
                and not (cat_words & set(re.findall(r"[a-zA-Z]+", pname_low)))):
            continue

        # ── Gender filter — prevent cross-gender suggestions ─────
        if gender_low:
            brand_low_val = (doc.get("brand") or "").lower().strip()
            combined_text = pname_low + " " + url_low
            if not gender_allows(combined_text, gender_low, brand_low=brand_low_val):
                continue

        if pname_low in seen_names:
            continue
        seen_names.add(pname_low)
        doc["matched_reason"] = matched_reason
        recs.append(doc)
        if len(recs) >= k:
            break

    return recs