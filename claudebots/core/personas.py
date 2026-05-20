from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError


class _PersonaModel(BaseModel):
    name: str
    bot_token_env: str
    system_prompt: str
    max_tokens: int = 500
    fallback: str = ""
    provider: str = "claude"


class _PanelPersonaModel(_PersonaModel):
    id: str
    is_moderator: bool = False
    provider: str = "claude"


class _AgentsSection(BaseModel):
    business_assistant: _PersonaModel


class _PanelSection(BaseModel):
    common_system_prompt: str = ""
    agents: list[_PanelPersonaModel] = Field(default_factory=list)


class _PersonasFile(BaseModel):
    agents: _AgentsSection
    panel: _PanelSection


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    bot_token_env: str
    system_prompt: str
    max_tokens: int
    fallback: str
    provider: str = "claude"
    is_moderator: bool = False


@dataclass(frozen=True)
class PersonaRegistry:
    business_assistant: Persona
    panel_speakers: list[Persona] = field(default_factory=list)
    moderator: Persona | None = None

    def all_panel(self) -> list[Persona]:
        result = list(self.panel_speakers)
        if self.moderator is not None:
            result.append(self.moderator)
        return result


def _resolve_templates(prompt: str, substitutions: dict[str, str]) -> str:
    """Replace <<MARKER>> placeholders with actual content from the YAML."""
    for marker, content in substitutions.items():
        prompt = prompt.replace(f"<<{marker}>>", content)
    return prompt


def load_personas(path: Path) -> PersonaRegistry:
    text = path.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid personas YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"Personas YAML must be a mapping, got {type(raw).__name__}")

    # Collect template substitutions from YAML sections
    substitutions: dict[str, str] = {}
    for key, value in raw.get("base_prompts", {}).items():
        substitutions[key.upper()] = str(value).strip()
    for key, value in raw.get("shared", {}).items():
        substitutions[key.upper()] = str(value).strip()

    try:
        parsed = _PersonasFile(**raw)
    except ValidationError as e:
        raise ValueError(f"Invalid personas YAML: {e}") from e

    # Add panel common_system_prompt as a substitution
    if parsed.panel.common_system_prompt:
        common_resolved = _resolve_templates(parsed.panel.common_system_prompt, substitutions)
        substitutions["COMMON_PANEL_PROMPT"] = common_resolved.strip()

    biz = parsed.agents.business_assistant
    business = Persona(
        id="business_assistant",
        name=biz.name,
        bot_token_env=biz.bot_token_env,
        system_prompt=_resolve_templates(biz.system_prompt, substitutions),
        max_tokens=biz.max_tokens,
        fallback=biz.fallback,
        provider=biz.provider,
    )

    speakers: list[Persona] = []
    moderator: Persona | None = None
    for p in parsed.panel.agents:
        persona = Persona(
            id=p.id,
            name=p.name,
            bot_token_env=p.bot_token_env,
            system_prompt=_resolve_templates(p.system_prompt, substitutions),
            max_tokens=p.max_tokens,
            fallback=p.fallback,
            provider=p.provider,
            is_moderator=p.is_moderator,
        )
        if p.is_moderator:
            if moderator is not None:
                raise ValueError("More than one moderator declared in personas.panel")
            moderator = persona
        else:
            speakers.append(persona)

    if moderator is None and parsed.panel.agents:
        raise ValueError("personas.panel has speakers but no moderator (is_moderator: true)")

    return PersonaRegistry(
        business_assistant=business, panel_speakers=speakers, moderator=moderator
    )
