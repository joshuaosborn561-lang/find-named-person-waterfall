"""Environment-backed settings. Vendor keys may also live in private.api_keys."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DEFAULT_SUPABASE_URL = "https://azpapwtnrbzywlnxxecz.supabase.co"
DEFAULT_SUPABASE_PROJECT = "azpapwtnrbzywlnxxecz"

API_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "leadmagic": ("leadmagic", "leadmagic_api_key", "LEADMAGIC_API_KEY", "LEADMAGIC_KEY"),
    "discolike": ("discolike", "discolike_api_key", "DISCOLIKE_API_KEY", "DISCO_API_KEY"),
}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_service_role_key: str
    supabase_anon_key: str
    leadmagic_api_key: str
    discolike_api_key: str
    email_waterfall_url: str

    @property
    def supabase_key(self) -> str:
        return self.supabase_service_role_key or self.supabase_anon_key

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)


def load_settings() -> Settings:
    return Settings(
        supabase_url=_env("SUPABASE_URL", DEFAULT_SUPABASE_URL).rstrip("/"),
        supabase_service_role_key=_env("SUPABASE_SERVICE_ROLE_KEY"),
        supabase_anon_key=_env("SUPABASE_ANON_KEY"),
        leadmagic_api_key=_env("LEADMAGIC_API_KEY") or _env("LEADMAGIC_KEY"),
        discolike_api_key=_env("DISCOLIKE_API_KEY") or _env("DISCO_API_KEY"),
        email_waterfall_url=_env("EMAIL_WATERFALL_URL").rstrip("/"),
    )


settings = load_settings()


def merge_private_keys(rows: list[dict[str, str]]) -> Settings:
    """Overlay blank env keys with private.api_keys rows. Never log values."""
    by_name = {
        str(r.get("name") or "").strip(): str(r.get("value") or "").strip()
        for r in rows
        if r.get("name") and r.get("value")
    }
    if not by_name:
        return settings
    lower = {k.lower(): v for k, v in by_name.items()}

    def pick(current: str, *aliases: str) -> str:
        if current:
            return current
        for alias in aliases:
            if alias in by_name:
                return by_name[alias]
            if alias.lower() in lower:
                return lower[alias.lower()]
        return current

    updated = replace(
        settings,
        leadmagic_api_key=pick(settings.leadmagic_api_key, *API_KEY_ALIASES["leadmagic"]),
        discolike_api_key=pick(settings.discolike_api_key, *API_KEY_ALIASES["discolike"]),
    )
    return updated
