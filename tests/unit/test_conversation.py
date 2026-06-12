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


def test_trim_noop_when_shorter_than_keep():
    """trim() with keep_last >= current length leaves the store unchanged."""
    store = ConversationStore(max_messages_per_chat=10)
    for i in range(3):
        store.add("k", "user", f"msg{i}")
    store.trim("k", keep_last=10)
    assert [m["content"] for m in store.get("k")] == ["msg0", "msg1", "msg2"]


def test_trim_to_zero_clears_history():
    """trim() with keep_last=0 removes all messages."""
    store = ConversationStore(max_messages_per_chat=10)
    for i in range(5):
        store.add("k", "user", f"msg{i}")
    store.trim("k", keep_last=0)
    assert store.get("k") == []


def test_trim_missing_key_is_noop():
    """trim() on a key that does not exist does not raise."""
    store = ConversationStore(max_messages_per_chat=10)
    store.trim("nonexistent", keep_last=5)  # must not raise
    assert store.get("nonexistent") == []


def test_trim_preserves_maxlen_after_new_adds():
    """After trim(), new adds still respect the ring-buffer maxlen."""
    store = ConversationStore(max_messages_per_chat=4)
    for i in range(4):
        store.add("k", "user", f"msg{i}")
    store.trim("k", keep_last=2)
    assert len(store.get("k")) == 2
    # Add 3 more — the ring buffer (maxlen=4) should evict oldest
    for i in range(4, 7):
        store.add("k", "user", f"msg{i}")
    result = [m["content"] for m in store.get("k")]
    assert len(result) == 4
    assert "msg4" in result
    assert "msg5" in result
    assert "msg6" in result


def test_snapshot_and_restore_round_trip():
    store = ConversationStore(max_messages_per_chat=10)
    store.add("chat:1", "user", "hello")
    store.add("chat:1", "assistant", "hi there")
    store.add("chat:2", "user", "other")

    snap = store.snapshot()
    restored = ConversationStore(max_messages_per_chat=10)
    restored.restore(snap)

    assert restored.get("chat:1") == store.get("chat:1")
    assert restored.get("chat:2") == store.get("chat:2")


def test_snapshot_excludes_empty_keys():
    store = ConversationStore(max_messages_per_chat=10)
    store.add("k1", "user", "x")
    store.reset("k1")
    snap = store.snapshot()
    assert "k1" not in snap


def test_restore_respects_maxlen():
    oversized = [{"role": "user", "content": f"msg{i}"} for i in range(50)]
    store = ConversationStore(max_messages_per_chat=10)
    store.restore({"k": oversized})
    assert len(store.get("k")) == 10
    assert store.get("k")[-1]["content"] == "msg49"


def test_restore_skips_malformed_entries():
    store = ConversationStore(max_messages_per_chat=10)
    store.restore({
        "k": [
            {"role": "user", "content": "valid"},
            {"role": 123, "content": "bad role"},
            "not a dict",
            {"role": "assistant"},
        ]
    })
    assert store.get("k") == [{"role": "user", "content": "valid"}]
