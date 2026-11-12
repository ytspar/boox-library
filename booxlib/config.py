"""Configuration loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(REPO_ROOT, "config", "library.yml")


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    raw: dict[str, Any]

    # ---------------------------------------------------------------- sources
    @property
    def source_root(self) -> str:
        path = (self.raw.get("sources") or {}).get("root")
        if not path:
            raise ConfigError("sources.root is not set")
        return os.path.expanduser(path)

    @property
    def exclude_paths(self) -> set[str]:
        return set((self.raw.get("sources") or {}).get("exclude_paths") or [])

    # ----------------------------------------------------------------- device
    @property
    def device_host(self) -> str:
        return (self.raw.get("device") or {}).get("host", "http://192.168.1.50:8085")

    @property
    def books_root(self) -> str:
        return (self.raw.get("device") or {}).get("books_root", "/storage/emulated/0/Books")

    @property
    def min_free_bytes(self) -> int:
        return int((self.raw.get("device") or {}).get("min_free_bytes", 0))

    # ----------------------------------------------------------------- naming
    @property
    def author_style(self) -> str:
        return (self.raw.get("naming") or {}).get("author_style", "sort")

    @property
    def author_folder_style(self) -> str:
        return (self.raw.get("naming") or {}).get("author_folder_style", "sort")

    @property
    def max_filename_len(self) -> int:
        return int((self.raw.get("naming") or {}).get("max_filename_len", 140))

    # ---------------------------------------------------------------- formats
    @property
    def extensions(self) -> set[str]:
        raw = (self.raw.get("formats") or {}).get("extensions", "")
        if isinstance(raw, str):
            items = [x.strip() for x in raw.replace("\n", ",").split(",")]
        else:
            items = list(raw)
        return {x.lower().lstrip(".") for x in items if x}

    @property
    def preference(self) -> dict[str, int]:
        raw = (self.raw.get("formats") or {}).get("preference") or {}
        return {str(k).lower(): int(v) for k, v in raw.items()}

    @property
    def prefer_pdf_for(self) -> set[str]:
        raw = (self.raw.get("formats") or {}).get("prefer_pdf_for_categories") or {}
        return {k for k, v in raw.items() if v}

    @property
    def convert(self) -> dict[str, str]:
        """Source format -> format to upload instead (e.g. djvu -> pdf)."""
        raw = (self.raw.get("formats") or {}).get("convert") or {}
        return {str(k).lower().lstrip("."): str(v).lower().lstrip(".") for k, v in raw.items()}

    @property
    def tiny_epub_bytes(self) -> int:
        return int((self.raw.get("formats") or {}).get("tiny_epub_bytes", 0))

    # ------------------------------------------------------------- categories
    @property
    def categories(self) -> list[dict[str, Any]]:
        return self.raw.get("categories") or []

    @property
    def fallback_category(self) -> str:
        return self.raw.get("fallback_category") or "Unsorted"

    @property
    def overrides(self) -> dict[str, Any]:
        return self.raw.get("overrides") or {}

    @property
    def directory_category(self) -> dict[str, str]:
        return self.raw.get("directory_category") or {}


def load_config(path: str | None = None) -> Config:
    path = os.path.expanduser(path or os.environ.get("BOOXLIB_CONFIG") or DEFAULT_CONFIG)
    if not os.path.exists(path):
        raise ConfigError(f"config not found: {path}")
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config must be a mapping: {path}")
    return Config(raw=raw)
