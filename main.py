from retriever import (
    load_vector_db,
    create_retriever,
    search_query
)

from llm import (
    load_llm,
    generate_answer
)

# Detect intent
def detect_intent(query):
    q = query.lower()

    if any(k in q for k in ["product", "price", "buy", "shirt", "shoe"]):
        return "product"

    if any(k in q for k in ["collection", "range", "category"]):
        return "collection"

    return "normal"


# Main chatbot application
def main():
    print("=" * 60)
    print("   WESTSIDE RAG CHATBOT")
    print("   Type 'exit' to quit | Type 'clear' to reset memory")
    print("=" * 60)

    vectordb = load_vector_db()
    if not vectordb:
        print("ERROR: Vector DB load failed")
        return

    retriever = create_retriever(vectordb)
    if not retriever:
        print("ERROR: Retriever creation failed")
        return

    llm = load_llm()
    if not llm:
        print("ERROR: LLM load failed")
        return

    chat_history = []

    print("\nChatbot Ready!\n")

    while True:
        try:
            question = input("You: ").strip()
        except:
            break

        if not question:
            continue

        if question.lower() in ["exit", "quit", "bye"]:
            print("Goodbye!")
            break

        if question.lower() in ["clear", "reset"]:
            chat_history = []
            print("Memory cleared!")
            continue

        try:
            # STEP 1: intent
            intent = detect_intent(question)

            # STEP 2: retrieve
            retrieved_docs = search_query(retriever, question)

            # STEP 3: response
            if not retrieved_docs:
                answer = "No relevant info found."
            else:
                answer = generate_answer(
                    client=llm,
                    question=question,
                    retrieved_docs=retrieved_docs,
                    chat_history=chat_history
                )

            # OUTPUT
            print(f"\nBot: {answer}\n")

            # memory
            chat_history.append({"role": "user", "content": question})
            chat_history.append({"role": "assistant", "content": answer})

            if len(chat_history) > 20:
                chat_history = chat_history[-20:]

        except Exception as e:
            print("Error:", e)


if __name__ == "__main__":
    main()