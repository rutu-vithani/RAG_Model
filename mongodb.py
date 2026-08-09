import datetime
import traceback
import hashlib
import secrets

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def now_ist():
    """Current India time as a naive datetime (safe for MongoDB storage)."""
    return datetime.datetime.now(IST).replace(tzinfo=None)

mongo_client              = None
users_collection          = None
chat_history_collection   = None

try:
    from pymongo import MongoClient
    from settings import MONGO_URI, DB_NAME

    print(f"[mongodb.py] Connecting to {MONGO_URI} ...")
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    mongo_client.server_info()

    database                = mongo_client[DB_NAME]
    users_collection        = database["users"]
    chat_history_collection = database["chat_history"]

    # users — unique indexes on user_id AND email
    for idx in ["user_id_1", "email_1"]:
        try: users_collection.drop_index(idx)
        except Exception: pass

    try:
        users_collection.update_many(
            {"$or": [{"email": None}, {"email": ""}]},
            {"$unset": {"email": ""}}
        )
    except Exception as e:
        print(f"[mongodb.py] WARNING: could not clean null/empty emails: {e}")

    users_collection.create_index("user_id", unique=True)
    users_collection.create_index("email", unique=True, sparse=True)

    # chat_history — non-unique index for fast per-user queries
    try:
        chat_history_collection.drop_index("user_id_1")
    except Exception:
        pass
    chat_history_collection.create_index("user_id")
    chat_history_collection.create_index([("user_id", 1), ("timestamp", 1)])

    print(f"[mongodb.py] Connected OK -> db='{DB_NAME}'")
    print(f"[mongodb.py] Unique users:    {users_collection.count_documents({})}")
    print(f"[mongodb.py] Total messages:  {chat_history_collection.count_documents({})}")

except Exception:
    print("[mongodb.py] FAILED TO CONNECT — data will NOT be persisted. Reason:")
    traceback.print_exc()
    mongo_client              = None
    users_collection          = None
    chat_history_collection   = None


def is_connected():
    return users_collection is not None and chat_history_collection is not None


# ─────────────────────────────────────────────────────────────
#  PASSWORD HASHING — plain SHA-256, no salt (per request)
# ─────────────────────────────────────────────────────────────

def _hash_password(password):
    """Plain SHA-256 hash of the password — no salt."""
    return hashlib.sha256(password.encode()).hexdigest()

def _verify_password(password, hashed):
    return hashlib.sha256(password.encode()).hexdigest() == hashed


# ─────────────────────────────────────────────────────────────
#  AUTH — Sign Up / Login
# ─────────────────────────────────────────────────────────────

def signup_user(email, password, name=""):
    """
    Register a new user with email + password.
    Returns: ("ok", user_id) | ("exists", None) | ("error", None)
    """
    if users_collection is None:
        return ("error", None)
    try:
        existing = users_collection.find_one({"email": email})
        if existing:
            return ("exists", None)

        now = now_ist()
        hashed  = _hash_password(password)
        user_id = "u_" + secrets.token_hex(8)

        users_collection.insert_one({
            "user_id":       user_id,
            "email":         email,
            "password_hash": hashed,
            "first_seen":    now,
            "last_seen":     now,
            "visit_count":   1,
        })
        print(f"[mongodb.py] SIGNUP new user: {email} -> {user_id}")
        return ("ok", user_id)
    except Exception as e:
        print(f"[mongodb.py] ERROR in signup_user: {e}")
        return ("error", None)


def login_user(email, password):
    """
    Verify email + password.
    Returns: ("ok", user_doc) | ("wrong_password", None) | ("not_found", None) | ("error", None)
    """
    if users_collection is None:
        return ("error", None)
    try:
        user = users_collection.find_one({"email": email})
        if not user:
            return ("not_found", None)

        # Google-only accounts have no password
        if not user.get("password_hash"):
            return ("wrong_password", None)

        if not _verify_password(password, user["password_hash"]):
            return ("wrong_password", None)

        # Update last_seen + visit_count
        now = now_ist()
        users_collection.update_one(
            {"email": email},
            {"$set": {"last_seen": now}, "$inc": {"visit_count": 1}}
        )
        return ("ok", user)
    except Exception as e:
        print(f"[mongodb.py] ERROR in login_user: {e}")
        return ("error", None)


def google_auth_user(email, name, picture=""):
    """
    Sign in / register via Google OAuth.
    Returns user_id.
    """
    if users_collection is None:
        return None
    try:
        now = now_ist()
        existing = users_collection.find_one({"email": email})
        if existing:
            users_collection.update_one(
                {"email": email},
                {"$set": {"last_seen": now}, "$inc": {"visit_count": 1}}
            )
            return existing["user_id"]
        else:
            user_id = "g_" + secrets.token_hex(8)
            users_collection.insert_one({
                "user_id":    user_id,
                "email":      email,
                "first_seen": now,
                "last_seen":  now,
                "visit_count":1,
            })
            print(f"[mongodb.py] GOOGLE SIGNUP: {email} -> {user_id}")
            return user_id
    except Exception as e:
        print(f"[mongodb.py] ERROR in google_auth_user: {e}")
        return None


def record_unique_user(user_id):
    """
    New user  → insert doc with visit_count = 1
    Returning → update last_seen, increment visit_count
    """
    if users_collection is None or not user_id:
        return
    try:
        now = now_ist()
        existing = users_collection.find_one({"user_id": user_id})
        if existing:
            users_collection.update_one(
                {"user_id": user_id},
                {"$set": {"last_seen": now}, "$inc": {"visit_count": 1}}
            )
        else:
            users_collection.insert_one({
                "user_id":    user_id,
                "first_seen": now,
                "last_seen":  now,
                "visit_count":1,
            })
            print(f"[mongodb.py] NEW user registered: {user_id}")
    except Exception as e:
        print(f"[mongodb.py] ERROR in record_unique_user: {e}")


def get_user_count():
    if users_collection is None:
        return 0
    try:
        return users_collection.count_documents({})
    except Exception:
        return 0


def register_anon_user(user_id):
    """
    Register a visitor with a frontend-generated user_id.
    If user already exists (returning visitor), just update last_seen.
    Returns: 'ok' | 'exists' | 'error'
    """
    if users_collection is None:
        return "error"
    if not user_id:
        return "error"
    try:
        now      = now_ist()
        existing = users_collection.find_one({"user_id": user_id})
        if existing:
            users_collection.update_one(
                {"user_id": user_id},
                {"$set": {"last_seen": now}, "$inc": {"visit_count": 1}}
            )
            print(f"[mongodb.py] RETURNING visitor: {user_id}")
            return "exists"
        else:
            users_collection.insert_one({
                "user_id":    user_id,
                "first_seen": now,
                "last_seen":  now,
                "visit_count":1,
            })
            print(f"[mongodb.py] NEW visitor registered: {user_id}")
            return "ok"
    except Exception as e:
        print(f"[mongodb.py] ERROR in register_anon_user: {e}")
        return "error"


def update_user_email(user_id, email):
    """
    Attach an email to an existing anonymous user.
    If the user document doesn't exist yet (race condition), upsert it.
    Returns: 'ok' | 'already_used' | 'error'

    BUG FIX (email silently not saving after 2-3 messages):
    The old version did update_one({"user_id": user_id}, ..., upsert=True)
    and returned "ok" as long as Mongo didn't raise an exception. But a
    write with matched_count == 0 and upserted_id == None IS possible
    (e.g. the user_id sent with the email-capture request was "", had
    stray whitespace, or otherwise didn't exactly match the user_id
    already stored from earlier chat messages) — Mongo does nothing,
    raises nothing, and the old code still logged "Email updated" and
    returned "ok". The real user document never got the email field, so
    the bug was completely silent. This version checks the actual write
    result and re-reads the document to confirm the email is really
    there before ever returning "ok".
    """
    if users_collection is None:
        return "error"
    try:
        user_id = (user_id or "").strip()
        email   = (email or "").strip().lower()
        if not user_id:
            print("[mongodb.py] update_user_email called with empty user_id — aborting")
            return "error"
        if not email:
            # Never write an empty/null email — a sparse unique index
            # still treats null/"" as a real value, so writing it once
            # can later block every other user's first email save.
            return "error"

        # Check if email already belongs to a DIFFERENT user
        existing_email = users_collection.find_one({"email": email})
        if existing_email and existing_email.get("user_id") != user_id:
            print(f"[mongodb.py] Email {email} already belongs to user="
                  f"{existing_email.get('user_id')}, not {user_id}")
            return "already_used"

        now = now_ist()
        result = users_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {"email": email, "last_seen": now},
                "$setOnInsert": {
                    "user_id":     user_id,
                    "first_seen":  now,
                    "visit_count": 1,
                }
            },
            upsert=True
        )

        # VERIFY the write actually touched/created a document instead of
        # trusting "no exception was raised" — this is the actual fix.
        # A matched_count==0 / upserted_id==None result (no-op) was
        # previously reported as success.
        if result.upserted_id:
            print(f"[mongodb.py] Email upserted (NEW doc — original user_id "
                  f"'{user_id}' had no existing record) -> {email}")
        elif result.matched_count > 0:
            print(f"[mongodb.py] Email updated for existing user={user_id} -> {email}")
        else:
            print(f"[mongodb.py] WARNING: update_user_email wrote NOTHING for "
                  f"user_id={user_id!r}, email={email} "
                  f"(matched={result.matched_count}, upserted={result.upserted_id})")
            return "error"

        # Final safety check: re-read the doc and confirm the email is
        # actually saved before telling the caller it's "ok".
        verify = users_collection.find_one({"user_id": user_id}, {"email": 1})
        if not verify or verify.get("email") != email:
            print(f"[mongodb.py] WARNING: post-write verification FAILED for "
                  f"user_id={user_id} — doc now has email="
                  f"{verify.get('email') if verify else None!r}, expected {email!r}")
            return "error"

        return "ok"
    except Exception as e:
        # Duplicate-key on the email unique index means this email is
        # already attached to some other user doc — surface it as
        # 'already_used' instead of a generic silent 'error'.
        if "E11000" in str(e):
            print(f"[mongodb.py] DUPLICATE KEY saving email for user={user_id} -> {email}: {e}")
            return "already_used"
        print(f"[mongodb.py] ERROR in update_user_email: {e}")
        traceback.print_exc()
        return "error"


# ─────────────────────────────────────────────────────────────
#  CHAT HISTORY — all messages, all users
# ─────────────────────────────────────────────────────────────

def save_message(user_id, session_id, role, content):
    """Insert one message row into chat_history collection."""
    if chat_history_collection is None:
        print(f"[mongodb.py] SKIPPED save_message — not connected (role={role})")
        return
    if not user_id:
        print(f"[mongodb.py] SKIPPED save_message — empty user_id")
        return
    try:
        chat_history_collection.insert_one({
            "user_id":    user_id,
            "session_id": session_id,
            "role":       role,
            "content":    content,
            "timestamp":  now_ist(),
        })
        print(f"[mongodb.py] Saved {role} message for user={user_id}")
    except Exception as e:
        print(f"[mongodb.py] ERROR in save_message: {e}")


def load_history_for_user(user_id, limit=200):
    """
    Return flat chronological list of this user's messages.
    Each item: {role, content, timestamp}
    """
    if chat_history_collection is None or not user_id:
        return []
    try:
        cursor = chat_history_collection.find(
            {"user_id": user_id},
            sort=[("timestamp", 1)]
        )
        result = [
            {
                "role":      m["role"],
                "content":   m["content"],
                "timestamp": m["timestamp"].isoformat(),
            }
            for m in cursor
        ]
        return result[-limit:]
    except Exception as e:
        print(f"[mongodb.py] ERROR in load_history_for_user: {e}")
        return []


def clear_history_for_user(user_id):
    """Delete ALL chat_history rows for this user (🗑 clear button)."""
    if chat_history_collection is None or not user_id:
        return
    try:
        chat_history_collection.delete_many({"user_id": user_id})
        print(f"[mongodb.py] Cleared chat_history for user={user_id}")
    except Exception as e:
        print(f"[mongodb.py] ERROR in clear_history_for_user: {e}")