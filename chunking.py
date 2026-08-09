import json
from settings import CHUNK_SIZE, CHUNK_OVERLAP

# Prefer langchain's splitter if available, otherwise provide a simple fallback
try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except Exception:
    class RecursiveCharacterTextSplitter:
        def __init__(self, chunk_size=500, chunk_overlap=50, separators=None):
            self.chunk_size = chunk_size
            self.chunk_overlap = chunk_overlap
        def split_text(self, text):
            # naive splitter: fixed-size chunks with overlap
            chunks = []
            i = 0
            n = len(text)
            while i < n:
                end = min(i + self.chunk_size, n)
                chunks.append(text[i:end])
                i += self.chunk_size - self.chunk_overlap
            return chunks

# Create text splitter with custom chunk size and overlap
def get_text_splitter():
    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", "? ", "! ", " ", ""]
    )

# Convert cleaned pages into smaller text chunks
def chunk_pages(cleaned_pages):
    splitter = get_text_splitter()
    chunks   = []

    print("\n  Creating chunks...\n")

    for page_num, page in enumerate(cleaned_pages, start=1):

        # Add product metadata before chunking for better context
        metadata_header = ""
        if page.get("product_name"):
            metadata_header += f"Product: {page['product_name']}\n"
        if page.get("brand"):
            metadata_header += f"Brand: {page['brand']}\n"
        if page.get("price"):
            metadata_header += f"Price: {page['price']} {page.get('currency','')}\n"
        if page.get("availability"):
            metadata_header += f"Availability: {page['availability']}\n"
        if page.get("rating"):
            metadata_header += f"Rating: {page['rating']} ({page.get('review_count','')} reviews)\n"
        if page.get("description"):
            metadata_header += f"Description: {page['description']}\n"

        text = (metadata_header + "\n" + page.get("clean_text", "")).strip()

        if not text:
            continue

        page_chunks = splitter.split_text(text)
        #store chunks with metadata 
        for chunk_num, chunk in enumerate(page_chunks, start=1):
            chunks.append({
                "chunk_id":       f"{page_num}_{chunk_num}",
                "source_url":     page.get("url", ""),
                "title":          page.get("title", ""),
                "meta_description": page.get("meta_description", ""),
                "level":          page.get("level", ""),
                "depth":          page.get("depth", 0),
                "chunk_number":   chunk_num,
                "chunk_text":     chunk,
                "product_name":   page.get("product_name", ""),
                "brand":          page.get("brand", ""),
                "price":          str(page.get("price", "")),
                "currency":       page.get("currency", ""),
                "availability":   page.get("availability", ""),
                "rating":         str(page.get("rating", "")),
                "review_count":   str(page.get("review_count", "")),
                "sku":            page.get("sku", ""),
                "image_url":      page.get("image_url", ""),
                "images":         page.get("images", []),
            })

        print(f"   {page.get('url','')[:65]} → {len(page_chunks)} chunks")

    print(f"\n Total Chunks: {len(chunks)}")
    return chunks

# Save generated chunks to a JSON file
def save_chunks(chunks, filename="chunks.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)
    print(f"\n Saved: {filename}")

if __name__ == "__main__":
    print(" Loading cleaned_data.json...\n")

    with open("cleaned_data.json", "r", encoding="utf-8") as f:
        cleaned_data = json.load(f)

    chunks = chunk_pages(cleaned_data)
    save_chunks(chunks)