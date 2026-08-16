"""
Lightweight loader for the agents/*.md and skills/*.md files.

Design choice (see ARCHITECTURE.md): rather than trying to parse a
Gemini function-calling JSON schema out of free-form prose, each skill
.md file carries a small YAML frontmatter block with the actual schema,
and a prose body below it for human-readable notes. This keeps the
"specs live in an editable .md file" requirement while making parsing
trivial and robust — it's a YAML parse, not an NLP problem.

Agent .md files use the same frontmatter/body split, but only the body
(the system prompt text) is used at runtime; frontmatter there is just
documentation for humans reading the file.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml
from google.genai import types

from config import settings

_FRONTMATTER_DELIM = "---"


def _split_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    """
    Split a Markdown file of the form:

        ---
        key: value
        ---
        # Body starts here...

    into (frontmatter_dict, body_text). Returns ({}, raw_text) unchanged
    if the file has no frontmatter block.
    """
    stripped = raw_text.lstrip()
    if not stripped.startswith(_FRONTMATTER_DELIM):
        return {}, raw_text

    parts = stripped.split(_FRONTMATTER_DELIM, 2)
    if len(parts) < 3:
        # Malformed (opening delimiter with no closing one) — treat the
        # whole thing as body rather than silently dropping content.
        return {}, raw_text

    _, frontmatter_raw, body = parts
    frontmatter = yaml.safe_load(frontmatter_raw) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError(f"Frontmatter did not parse to a dict: {frontmatter_raw!r}")

    return frontmatter, body.strip()


def load_agent_prompt(name: str) -> str:
    """Load the system-instruction body text for agents/{name}.md."""
    path = settings.agents_dir / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent prompt not found: {path}")

    _, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    if not body:
        raise ValueError(f"Agent prompt file has no body content: {path}")
    return body


def _resolve_dotted_callable(dotted_path: str) -> Callable:
    """Import 'tools.search_tools.search_web_news' -> the actual function object."""
    module_path, _, func_name = dotted_path.rpartition(".")
    if not module_path:
        raise ValueError(f"tool_function must be a dotted path, got: {dotted_path!r}")
    module = importlib.import_module(module_path)
    try:
        return getattr(module, func_name)
    except AttributeError as exc:
        raise ValueError(f"'{func_name}' not found in module '{module_path}'") from exc


@dataclass
class SkillBundle:
    """A skill's Gemini-facing schema paired with the real Python callable it maps to."""
    name: str
    declaration: types.FunctionDeclaration
    tool_function_path: str
    description_body: str  # prose notes from below the frontmatter, for humans/debugging

    @property
    def function(self) -> Callable:
        return _resolve_dotted_callable(self.tool_function_path)


def load_skill(name: str) -> SkillBundle:
    """Load skills/{name}.md into a SkillBundle: schema + resolved callable."""
    path = settings.skills_dir / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Skill spec not found: {path}")

    frontmatter, body = _split_frontmatter(path.read_text(encoding="utf-8"))

    for required_key in ("name", "description", "tool_function", "parameters"):
        if required_key not in frontmatter:
            raise ValueError(f"Skill spec {path} is missing required frontmatter key: {required_key!r}")

    declaration = types.FunctionDeclaration(
        name=frontmatter["name"],
        description=frontmatter["description"],
        parameters_json_schema=frontmatter["parameters"],
    )

    return SkillBundle(
        name=frontmatter["name"],
        declaration=declaration,
        tool_function_path=frontmatter["tool_function"],
        description_body=body,
    )


def load_skills(names: list[str]) -> list[SkillBundle]:
    """Convenience: load several skills by name from the skills/ directory."""
    return [load_skill(name) for name in names]
