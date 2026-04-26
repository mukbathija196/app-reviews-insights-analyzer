"""Configuration loaders for products.yaml and pulse.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

# ── Helpers ───────────────────────────────────────────────────────────────────

def _config_dir() -> Path:
    env = os.environ.get("PULSE_CONFIG_DIR")
    if env:
        return Path(env)
    # Walk up from this file until we find config/
    here = Path(__file__).resolve()
    for parent in [here.parent, here.parent.parent, here.parent.parent.parent]:
        candidate = parent / "config"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("Cannot locate config/ directory. Set PULSE_CONFIG_DIR.")


# ── Products ─────────────────────────────────────────────────────────────────

class AppStoreConfig(BaseModel):
    country: str
    app_id: str


class PlayStoreConfig(BaseModel):
    package: str
    lang: str = "en"
    country: str = "in"


class Stakeholder(BaseModel):
    name: str
    email: str


class ProductConfig(BaseModel):
    display_name: str
    doc_title: str
    doc_url: str | None = None
    app_store: AppStoreConfig
    play_store: PlayStoreConfig
    stakeholders: list[Stakeholder] = Field(default_factory=list)


class ProductRegistry(BaseModel):
    products: dict[str, ProductConfig]

    def get(self, product_id: str) -> ProductConfig:
        if product_id not in self.products:
            known = ", ".join(sorted(self.products.keys()))
            raise KeyError(
                f"Unknown product '{product_id}'. Known products: {known}"
            )
        return self.products[product_id]

    def ids(self) -> list[str]:
        return list(self.products.keys())


def load_products(config_dir: Path | None = None) -> ProductRegistry:
    path = (config_dir or _config_dir()) / "products.yaml"
    with path.open() as f:
        raw: dict[str, object] = yaml.safe_load(f)
    return ProductRegistry(products={k: ProductConfig.model_validate(v) for k, v in raw.items()})


# ── Pulse config ─────────────────────────────────────────────────────────────

class RunConfig(BaseModel):
    window_weeks: int = 12
    min_reviews: int = 40
    top_k_themes: int = 3
    token_budget: int = 250_000
    email_mode: Literal["draft", "send"] = "draft"


class ModelsConfig(BaseModel):
    embedding: str = "sentence-transformers/all-MiniLM-L6-v2"
    llm: str = "llama-3.3-70b-versatile"


class MCPServerConfig(BaseModel):
    command: str
    transport: Literal["stdio", "sse", "http"] = "stdio"


class MCPConfig(BaseModel):
    docs: MCPServerConfig
    gmail: MCPServerConfig


class PulseConfig(BaseModel):
    run: RunConfig = Field(default_factory=RunConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    mcp: MCPConfig | None = None
    stakeholders_default: list[Stakeholder] = Field(default_factory=list)


def load_pulse_config(config_dir: Path | None = None) -> PulseConfig:
    path = (config_dir or _config_dir()) / "pulse.yaml"
    with path.open() as f:
        raw: dict[str, object] = yaml.safe_load(f)
    return PulseConfig.model_validate(raw)
