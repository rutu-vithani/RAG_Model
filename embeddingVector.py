import json
import datetime
import re
import traceback
import chromadb
from sentence_transformers import SentenceTransformer
from settings import EMBEDDING_MODEL, CHROMA_DB_PATH, CHROMA_COLLECTION, USER_MEMORY_COLLECTION

# Load all text chunks from JSON file
def load_chunks(file_path="chunks.json"):
    print(" Loading chunks...")
    with open(file_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)
    print(f"    Loaded {len(chunks)} chunks")
    return chunks

# Load sentence transformer embedding model
def load_embedding_model():
    print(f"\n Loading Embedding Model: {EMBEDDING_MODEL}")
    model = SentenceTransformer(EMBEDDING_MODEL)
    print("    Model Loaded")
    return model

# Connect to Chroma database and create collections
def create_chroma_collection():
    print("\n  Connecting ChromaDB...")
    client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
 
    try:
        # Remove old collection before rebuilding
        client.delete_collection(name=CHROMA_COLLECTION)
        print("     Old collection deleted")
    except Exception:
        pass

    # Create a new vector collection using cosine similarity
    collection = client.create_collection(
        name=CHROMA_COLLECTION,
        metadata={"hnsw:space": "cosine"}
    )
    print(f"    Collection Created: {CHROMA_COLLECTION}")
    return collection

# Generate embeddings and store them in ChromaDB
def store_embeddings(chunks, model, collection, batch_size=64):

    print(f"\n Creating & Storing Embeddings (batch={batch_size})...\n")
    total = len(chunks)
    # Process chunks in batches (64 slots) for better performance
    for start in range(0, total, batch_size):
        batch = chunks[start: start + batch_size]
        # Collect chunk text for embedding generation
        texts = [c["chunk_text"] for c in batch]
        # Convert text chunks into vector embeddings
        embeddings = model.encode(
            texts,
            convert_to_numpy=True,
            show_progress_bar=False,
            batch_size=batch_size
        ).tolist()

        ids = [str(c["chunk_id"]) for c in batch]

        # Store useful metadata with each embedding
        metadatas = []
        for c in batch:
            imgs = c.get("images") or []
            if isinstance(imgs, list) and len(imgs) == 0:
                imgs = ""
            metadatas.append({
                "url":            c.get("source_url", ""),
                "title":          c.get("title", ""),
                "meta_description": c.get("meta_description", ""),
                "level":          c.get("level", ""),
                "depth":          str(c.get("depth", 0)),
                "product_name":   c.get("product_name", ""),
                "brand":          c.get("brand", ""),
                "sku":            c.get("sku", ""),
                "price":          str(c.get("price", "")),
                "currency":       c.get("currency", ""),
                "availability":   c.get("availability", ""),
                "rating":         str(c.get("rating", "")),
                "review_count":   str(c.get("review_count", "")),
                "image_url":      c.get("image_url", ""),
                "images":         imgs,
            })

        # Save embeddings, text and metadata into ChromaDB
        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas
        )

        done = min(start + batch_size, total)
        print(f"    {done}/{total} chunks stored")

    print(f"\n Done! {total} embeddings stored in ChromaDB")


# ─────────────────────────────────────────────────────────────
#  USER MEMORY (separate Chroma collection — NOT product data)
# ─────────────────────────────────────────────────────────────
# Per-user memory (name, location, product/category interest,
# preferences) stored as embeddings in its own ChromaDB collection
# (settings.USER_MEMORY_COLLECTION), kept apart from the product
# catalog collection above (settings.CHROMA_COLLECTION).

_memory_collection = None
_memory_embed_model = None

def _get_memory_embed_model():
    """Lazy-load the same sentence-transformer model used for products."""
    global _memory_embed_model
    if _memory_embed_model is None:
        _memory_embed_model = SentenceTransformer(EMBEDDING_MODEL)
    return _memory_embed_model

def get_user_memory_collection():
    """Lazy-connect to the dedicated 'user_memory' Chroma collection."""
    global _memory_collection
    if _memory_collection is not None:
        return _memory_collection
    try:
        client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        try:
            _memory_collection = client.get_collection(name=USER_MEMORY_COLLECTION)
        except Exception:
            _memory_collection = client.create_collection(
                name=USER_MEMORY_COLLECTION,
                metadata={"hnsw:space": "cosine"}
            )
        print(f" Connected to memory collection: {USER_MEMORY_COLLECTION}")
    except Exception as e:
        print(f" ERROR connecting to user_memory collection: {e}")
        _memory_collection = None
    return _memory_collection


MEMORY_EXTRACTION_SYSTEM = """You extract durable memory facts about a single user from one chat turn.

Only output a JSON array of short, self-contained fact strings. Nothing else — no markdown fences, no explanation.

Remember things like:
- The user's name, if they introduce themselves
- The user's location/city, if mentioned
- Products, categories, or brands the user showed interest in or searched for
- Stated preferences (size, color, budget, style)

Do NOT remember:
- Greetings, small talk, or anything with no lasting value
- Anything already obvious/generic ("user said hi")

If there is truly nothing worth remembering from this turn, output exactly: []

Each fact must be a short standalone sentence, e.g.:
["User's name is Raj", "User is located in Surat", "User is interested in men's jackets"]
"""

def _safe_json_array(raw):
    raw = (raw or "").strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except Exception:
        pass
    return []

# Ask the LLM what's worth remembering from this turn, then embed +
# store each fact as its own chunk in the user_memory collection.
def extract_and_store_memory(llm_client, llm_model, user_id, question, answer):
    if not user_id or llm_client is None:
        return

    collection = get_user_memory_collection()
    if collection is None:
        return

    try:
        turn_text = f"User: {question}\nAssistant: {answer}"
        resp = llm_client.chat.completions.create(
            model=llm_model,
            messages=[
                {"role": "system", "content": MEMORY_EXTRACTION_SYSTEM},
                {"role": "user", "content": turn_text},
            ],
            temperature=0,
            max_tokens=300,
        )
        raw   = resp.choices[0].message.content
        facts = _safe_json_array(raw)

        if not facts:
            return

        model = _get_memory_embed_model()
        embeddings = model.encode(facts, convert_to_numpy=True).tolist()

        now = datetime.datetime.utcnow().isoformat()
        ids = [f"{user_id}_{now}_{i}" for i in range(len(facts))]
        metadatas = [{"user_id": user_id, "timestamp": now} for _ in facts]

        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=facts,
            metadatas=metadatas,
        )
        total_in_collection = collection.count()
        print(f" Stored {len(facts)} memory fact(s) for user={user_id}: {facts}")
        print(f" user_memory collection total docs: {total_in_collection}")
    except Exception as e:
        print(f" ERROR in extract_and_store_memory: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    chunks     = load_chunks()
    model      = load_embedding_model()
    collection = create_chroma_collection()
    store_embeddings(chunks, model, collection)
    print("\n Vector Database Ready!")