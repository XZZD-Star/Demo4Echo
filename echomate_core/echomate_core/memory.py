"""Memory operations shared by voice and text entry points."""

from .store import (
    add_chat_memory,
    clear_chat_memories,
    clear_memories,
    decide_memory_candidate,
    delete_chat_memory,
    delete_memory,
    detect_category,
    enqueue_memory_candidate,
    list_chat_memories,
    list_memories,
    list_memory_candidates,
    memory_context,
    search_chat_memories,
    upsert_preference_memory,
)

# Compatibility aliases for the former Server/tools/memory_store.py API.
enqueue_candidate = enqueue_memory_candidate
list_candidates = list_memory_candidates
decide_candidate = decide_memory_candidate


def init_memory_tables():
    from .schema import init_core_schema

    return init_core_schema()


__all__ = [
    "add_chat_memory",
    "clear_chat_memories",
    "clear_memories",
    "decide_candidate",
    "decide_memory_candidate",
    "delete_chat_memory",
    "delete_memory",
    "detect_category",
    "enqueue_candidate",
    "enqueue_memory_candidate",
    "init_memory_tables",
    "list_candidates",
    "list_chat_memories",
    "list_memories",
    "list_memory_candidates",
    "memory_context",
    "search_chat_memories",
    "upsert_preference_memory",
]
