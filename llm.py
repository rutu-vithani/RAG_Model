from groq import Groq
from settings import GROQ_API_KEY, LLM_MODEL, MAX_TOKENS, TEMPERATURE


def load_llm():
    """Return a raw Groq client. app.py calls it directly with its own system prompt."""
    client = Groq(api_key=GROQ_API_KEY)
    print(" Groq LLM Loaded")
    return client


# ── kept for compatibility if main.py / other scripts call it ─────────────────
def build_context(retrieved_docs):
    parts = []
    for doc in retrieved_docs:
        p = []
        if doc.get("product_name"): p.append(f"Product : {doc['product_name']}")
        if doc.get("price"):        p.append(f"Price   : {doc['price']} {doc.get('currency','INR')}")
        if doc.get("availability"): p.append(f"Stock   : {doc['availability']}")
        if doc.get("brand"):        p.append(f"Brand   : {doc['brand']}")
        if doc.get("url"):          p.append(f"Link    : {doc['url']}")
        if p: parts.append("\n".join(p))
    return "\n\n".join(parts)


def generate_answer(client, question, retrieved_docs, chat_history=None, intent_context=None):
    """
    Kept for backward-compatibility (main.py uses this).
    app.py bypasses this and calls the Groq client directly with MASTER_SYSTEM.
    """
    context = build_context(retrieved_docs)
    history_msgs = []
    if chat_history:
        for m in chat_history[-6:]:
            history_msgs.append({"role": m["role"], "content": m["content"]})

    system = (
        "You are a helpful Westside shopping assistant. "
        "Answer based only on the provided product context. "
        "Be concise and friendly."
    )
    user_msg = f"PRODUCT CONTEXT:\n{context}\n\nQuestion: {question}"
    msgs = [{"role":"system","content":system}] + history_msgs + [{"role":"user","content":user_msg}]

    resp = client.chat.completions.create(
        model=LLM_MODEL, messages=msgs,
        temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
    )
    return resp.choices[0].message.content.strip()