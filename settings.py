#  Website
START_URL = "https://www.westside.com/"

# Scraper settings
REQUEST_DELAY = 0.8
TIMEOUT       = 20
RESUME_FILE   = "scraped_progress.json"
MAX_WORKERS   = 3

#  Groq API
import os
from pathlib import Path


def load_env_file() -> bool:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return False

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)

    return True


load_env_file()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()

#  Embedding Model
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

#  ChromaDB
CHROMA_DB_PATH    = "./chroma_db"
CHROMA_COLLECTION = "website_rag"
USER_MEMORY_COLLECTION = "user_memory"

#  Chunking
CHUNK_SIZE    = 800
CHUNK_OVERLAP = 100

#  Retriever
TOP_K_RESULTS = 8  

#  LLM
LLM_MODEL   = "llama-3.3-70b-versatile"
MAX_TOKENS  = 800   
TEMPERATURE = 0.3

# Mongodb
MONGO_URI = "mongodb://127.0.0.1:27017/"
DB_NAME = "SiteGPT"
CHAT_COLLECTION = "chat_history"