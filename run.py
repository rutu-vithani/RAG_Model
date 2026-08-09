import json, sys

print(" WESTSIDE RAG PIPELINE")

# Pass --fresh to delete old progress and re-scrape everything
FRESH = "--fresh" in sys.argv
if FRESH:
    import os
    for f in ["scraped_progress.json"]:
        if os.path.exists(f):
            os.remove(f)
            print(f"   Deleted {f} for fresh scrape")

# SCRAPING
print("\n  Step 1: Scraping Website...")
print("-" * 40)
try:
    from scrapping import scrape_website
    pages = scrape_website(fresh=FRESH)
    with open("scraped.json", "w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)
    print(f"\n Saved scraped.json ({len(pages)} pages)")
    # [CHANGE] Also seed scraped_old.json so the first cron-job run has a baseline to compare against
    import shutil as _sh
    _sh.copy2("scraped.json", "scraped_old.json")
    print(f" Saved scraped_old.json (baseline for incremental scraper)")
    if len(pages) == 0:
        print(" No pages scraped! Check internet connection or START_URL.")
        sys.exit(1)
except Exception as e:
    print(f" Scraping failed: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)

# CLEANING
print("\n  Step 2: Cleaning Text...")
print("-" * 40)
try:
    from cleaning import clean_all_pages
    cleaned_pages = clean_all_pages(pages)
    with open("cleaned_data.json", "w", encoding="utf-8") as f:
        json.dump(cleaned_pages, f, ensure_ascii=False, indent=2)
    print(f"\n Saved cleaned_data.json ({len(cleaned_pages)} pages)")
    if len(cleaned_pages) == 0:
        print(" All pages filtered out in cleaning!")
        sys.exit(1)
except Exception as e:
    print(f" Cleaning failed: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)

# CHUNKING
print("\n  Step 3: Chunking Documents...")
print("-" * 40)
try:
    from chunking import chunk_pages, save_chunks
    chunks = chunk_pages(cleaned_pages)
    save_chunks(chunks)
    print(f"\n Saved chunks.json ({len(chunks)} chunks)")
    if len(chunks) == 0:
        print(" No chunks created!")
        sys.exit(1)
except Exception as e:
    print(f" Chunking failed: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)

# EMBEDDING
print("\n  Step 4: Creating Embeddings & Storing in ChromaDB...")
print("-" * 40)
try:
    from embeddingVector import load_embedding_model, create_chroma_collection, store_embeddings
    model      = load_embedding_model()
    collection = create_chroma_collection()
    store_embeddings(chunks, model, collection)
except Exception as e:
    print(f" Embedding failed: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 60)
print(" RUN COMPLETE!")
print(f"    Pages scraped  : {len(pages)}")
print(f"    Pages cleaned  : {len(cleaned_pages)}")
print(f"    Chunks created : {len(chunks)}")
print("=" * 60)
print("\n Now run:  python app.py")