from claudebots.core.conversation import ConversationStore


def test_add_and_get_returns_messages_in_order():
    store = ConversationStore(max_messages_per_chat=10)
    store.add("k1", "user", "hello")
    store.add("k1", "assistant", "hi")
    assert store.get("k1") == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


def test_keys_are_isolated():
    store = ConversationStore(max_messages_per_chat=10)
    store.add("k1", "user", "from k1")
    store.add("k2", "user", "from k2")
    assert store.get("k1") == [{"role": "user", "content": "from k1"}]
    assert store.get("k2") == [{"role": "user", "content": "from k2"}]


def test_ring_buffer_drops_oldest():
    store = ConversationStore(max_messages_per_chat=3)
    for i in range(5):
        store.add("k", "user", f"msg{i}")
    assert store.get("k") == [
        {"role": "user", "content": "msg2"},
        {"role": "user", "content": "msg3"},
        {"role": "user", "content": "msg4"},
    ]


def test_reset_clears_only_target_key():
    store = ConversationStore(max_messages_per_chat=10)
    store.add("k1", "user", "x")
    store.add("k2", "user", "y")
    store.reset("k1")
    assert store.get("k1") == []
    assert store.get("k2") == [{"role": "user", "content": "y"}]


def test_get_unknown_key_returns_empty():
    store = ConversationStore(max_messages_per_chat=10)
    assert store.get("missing") == []


def test_trim_keeps_last_n():
    store = ConversationStore(max_messages_per_chat=10)
    for i in range(8):
        store.add("k", "user", f"msg{i}")
    store.trim("k", keep_last=3)
    assert [m["content"] for m in store.get("k")] == ["msg5", "msg6", "msg7"]
