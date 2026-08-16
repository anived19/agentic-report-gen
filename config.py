"""
Centralized configuration for the Financial Report Generator.

Every other module reads settings from here rather than calling
os.environ directly — one place to see the full config surface, and
pydantic-settings validates types/required fields at startup instead of
failing deep inside a pipeline run.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- API Keys (required — will raise at startup if missing, which is
    # the point: fail fast before burning any API calls) ---
    gemini_api_key: str
    tavily_api_key: str

    # --- Model ---
    gemini_model: str = "gemini-3.6-flash"

    # --- PDF ---
    pdf_engine: str = "weasyprint"  # "weasyprint" | "xhtml2pdf"

    # --- Paths ---
    output_dir: Path = Path("outputs")
    cache_dir: Path = Path("cache")
    templates_dir: Path = Path("templates")
    static_dir: Path = Path("static")
    agents_dir: Path = Path("agents")
    skills_dir: Path = Path("skills")

    # --- Cache ---
    cache_ttl_hours: int = 6

    # --- Agent loop bounds ---
    # Hard cap so the Research agent can't spiral into an unbounded number
    # of tool calls: search, optionally refine once, summarize.
    research_agent_max_turns: int = 4

    # --- Outlook window ---
    # Number of months used for the price trend window and outlook section.
    # Changing this here is the only code change needed — it flows through
    # finance_tools, schemas, and the Chief Editor prompt automatically.
    outlook_months: int = 6

    def ensure_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
