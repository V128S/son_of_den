"""Tests for core/state.py — bot state persistence."""

import json

from claudebots.core.state import (
    decode_int_keys,
    encode_int_keys,
    load,
    save,
    update,
)

# ---------------------------------------------------------------------------
# load / save / update
# ---------------------------------------------------------------------------

def test_load_returns_empty_dict_when_file_missing(tmp_path):
    assert load(tmp_path / "no_such_file.json") == {}


def test_load_returns_empty_dict_on_corrupt_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("this is not json", encoding="utf-8")
    assert load(f) == {}


def test_load_returns_empty_dict_on_non_dict_json(tmp_path):
    f = tmp_path / "array.json"
    f.write_text("[1, 2, 3]", encoding="utf-8")
    assert load(f) == {}


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    data = {"panel_topics": {"123": "💼 Бизнес"}, "tasks_thread_id": 42}
    save(path, data)
    assert path.exists()
    loaded = load(path)
    assert loaded == data


def test_save_is_atomic(tmp_path):
    """save() writes via .tmp then renames — original survives if write fails."""
    path = tmp_path / "state.json"
    original = {"key": "original"}
    save(path, original)

    # Simulate: we can verify the .tmp file doesn't linger after a successful save
    tmp = path.with_suffix(".tmp")
    assert not tmp.exists(), ".tmp file should be removed after successful save"
    assert load(path) == original


def test_save_preserves_unicode(tmp_path):
    path = tmp_path / "state.json"
    data = {"memories": ["Нужно изучить рынок.", "💼 Бизнес победил 🎉"]}
    save(path, data)
    assert load(path) == data


def test_update_merges_with_existing(tmp_path):
    path = tmp_path / "state.json"
    save(path, {"a": 1, "b": 2})
    update(path, {"b": 99, "c": 3})
    result = load(path)
    assert result == {"a": 1, "b": 99, "c": 3}


def test_update_creates_file_when_missing(tmp_path):
    path = tmp_path / "new_state.json"
    update(path, {"x": 10})
    assert load(path) == {"x": 10}


# ---------------------------------------------------------------------------
# encode_int_keys / decode_int_keys
# ---------------------------------------------------------------------------

def test_encode_int_keys():
    assert encode_int_keys({1: "a", 2: "b"}) == {"1": "a", "2": "b"}


def test_encode_int_keys_empty():
    assert encode_int_keys({}) == {}


def test_decode_int_keys():
    assert decode_int_keys({"1": "a", "2": "b"}) == {1: "a", 2: "b"}


def test_decode_int_keys_skips_non_int():
    result = decode_int_keys({"1": "a", "bad": "b", "3": "c"})
    assert result == {1: "a", 3: "c"}


def test_roundtrip_int_keys():
    original = {100: "💼 Бизнес", 200: "📢 Маркетинг"}
    assert decode_int_keys(encode_int_keys(original)) == original


# ---------------------------------------------------------------------------
# init_panel_state integration
# ---------------------------------------------------------------------------

def test_init_panel_state_restores_topics(tmp_path, monkeypatch):
    import claudebots.routers.panel as panel_mod

    monkeypatch.setattr(panel_mod, "_panel_topics", {})
    monkeypatch.setattr(panel_mod, "_tasks_thread_id", None)
    monkeypatch.setattr(panel_mod, "_panel_memories", [])

    data = {
        "panel_topics": {"111": "💼 Бизнес", "222": "📢 Маркетинг"},
        "tasks_thread_id": 333,
        "panel_memories": ["Вывод 1", "Вывод 2"],
    }
    panel_mod.init_panel_state(tmp_path / "state.json", data)

    assert panel_mod._panel_topics == {111: "💼 Бизнес", 222: "📢 Маркетинг"}
    assert panel_mod._tasks_thread_id == 333
    # Legacy string format is migrated to dict entries on restore
    assert len(panel_mod._panel_memories) == 2
    assert panel_mod._panel_memories[0]["text"] == "Вывод 1"
    assert panel_mod._panel_memories[1]["text"] == "Вывод 2"


def test_init_panel_state_handles_empty_data(tmp_path, monkeypatch):
    import claudebots.routers.panel as panel_mod

    monkeypatch.setattr(panel_mod, "_panel_topics", {})
    monkeypatch.setattr(panel_mod, "_tasks_thread_id", None)
    monkeypatch.setattr(panel_mod, "_panel_memories", [])

    panel_mod.init_panel_state(tmp_path / "state.json", {})

    assert panel_mod._panel_topics == {}
    assert panel_mod._tasks_thread_id is None
    assert panel_mod._panel_memories == []


def test_init_panel_state_caps_memories_at_max(tmp_path, monkeypatch):
    import claudebots.routers.panel as panel_mod

    monkeypatch.setattr(panel_mod, "_panel_topics", {})
    monkeypatch.setattr(panel_mod, "_tasks_thread_id", None)
    monkeypatch.setattr(panel_mod, "_panel_memories", [])

    over_max = [f"memory {i}" for i in range(panel_mod.PANEL_MEMORY_MAX + 5)]
    panel_mod.init_panel_state(tmp_path / "state.json", {"panel_memories": over_max})
    assert len(panel_mod._panel_memories) == panel_mod.PANEL_MEMORY_MAX


# ---------------------------------------------------------------------------
# init_business_state integration
# ---------------------------------------------------------------------------

def test_init_business_state_restores_contacts(tmp_path, monkeypatch):
    import claudebots.routers.business as biz

    monkeypatch.setattr(biz, "_contact_topics", {})
    monkeypatch.setattr(biz, "_topic_contacts", {})
    monkeypatch.setattr(biz, "_admin_topics", {})

    data = {
        "contact_topics": {"101": 501, "102": 502},
        "admin_topics": {"📋 Задачи": 999},
    }
    biz.init_business_state(tmp_path / "state.json", data)

    assert biz._contact_topics == {101: 501, 102: 502}
    assert biz._topic_contacts == {501: 101, 502: 102}  # reverse mapping rebuilt
    assert biz._admin_topics == {"📋 Задачи": 999}


def test_init_business_state_handles_empty(tmp_path, monkeypatch):
    import claudebots.routers.business as biz

    monkeypatch.setattr(biz, "_contact_topics", {})
    monkeypatch.setattr(biz, "_topic_contacts", {})
    monkeypatch.setattr(biz, "_admin_topics", {})

    biz.init_business_state(tmp_path / "state.json", {})

    assert biz._contact_topics == {}
    assert biz._topic_contacts == {}
    assert biz._admin_topics == {}


def test_persist_panel_state_writes_to_file(tmp_path, monkeypatch):
    """_persist_panel_state() actually writes a readable file."""
    import claudebots.routers.panel as panel_mod

    path = tmp_path / "state.json"
    monkeypatch.setattr(panel_mod, "_state_path", path)
    monkeypatch.setattr(panel_mod, "_panel_topics", {10: "💼 Бизнес"})
    monkeypatch.setattr(panel_mod, "_tasks_thread_id", 20)
    monkeypatch.setattr(panel_mod, "_panel_memories", ["Вывод A"])

    panel_mod._persist_panel_state()

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["panel_topics"] == {"10": "💼 Бизнес"}
    assert written["tasks_thread_id"] == 20
    assert written["panel_memories"] == ["Вывод A"]


def test_persist_business_state_writes_to_file(tmp_path, monkeypatch):
    """_persist_business_state() writes contact and admin topics."""
    import claudebots.routers.business as biz

    path = tmp_path / "state.json"
    monkeypatch.setattr(biz, "_biz_state_path", path)
    monkeypatch.setattr(biz, "_contact_topics", {55: 100, 66: 200})
    monkeypatch.setattr(biz, "_admin_topics", {"📋 Задачи": 300})

    biz._persist_business_state()

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["contact_topics"] == {"55": 100, "66": 200}
    assert written["admin_topics"] == {"📋 Задачи": 300}
