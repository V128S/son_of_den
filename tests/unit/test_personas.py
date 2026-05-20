import pytest

from claudebots.core.personas import load_personas


def test_load_full_yaml(tmp_path):
    yaml_text = """
agents:
  business_assistant:
    name: "Майя"
    bot_token_env: BUSINESS_BOT_TOKEN
    max_tokens: 400
    system_prompt: "you are Maya"
    fallback: "sorry"

panel:
  agents:
    - id: analyst
      name: "Аналитик"
      bot_token_env: PANEL_BOT_ANALYST_TOKEN
      max_tokens: 500
      system_prompt: "you are analyst"
    - id: moderator
      name: "Модератор"
      bot_token_env: PANEL_BOT_MODERATOR_TOKEN
      is_moderator: true
      max_tokens: 800
      system_prompt: "you are moderator"
      fallback: "tech break"
"""
    f = tmp_path / "p.yaml"
    f.write_text(yaml_text, encoding="utf-8")

    reg = load_personas(f)

    assert reg.business_assistant.name == "Майя"
    assert reg.business_assistant.fallback == "sorry"
    assert reg.business_assistant.max_tokens == 400
    assert len(reg.panel_speakers) == 1
    assert reg.panel_speakers[0].id == "analyst"
    assert reg.moderator.id == "moderator"
    assert reg.moderator.fallback == "tech break"


def test_load_missing_required_field_raises(tmp_path):
    yaml_text = """
agents:
  business_assistant:
    name: "x"
    # no system_prompt
panel:
  agents: []
"""
    f = tmp_path / "p.yaml"
    f.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_personas(f)


def test_load_invalid_yaml_raises(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("key: : :", encoding="utf-8")
    with pytest.raises(ValueError):
        load_personas(f)


def test_registry_requires_exactly_one_moderator(tmp_path):
    yaml_text = """
agents:
  business_assistant:
    name: "x"
    bot_token_env: BUSINESS_BOT_TOKEN
    system_prompt: "x"
    fallback: "x"
panel:
  agents:
    - id: a
      name: "a"
      bot_token_env: T
      system_prompt: "x"
"""
    f = tmp_path / "p.yaml"
    f.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="moderator"):
        load_personas(f)

