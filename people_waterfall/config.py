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
    "getleads": ("getleads", "getleads_api_key", "GETLEADS_API_KEY"),
    "smartlead": ("smartlead", "smartlead_api_key", "SMARTLEAD_API_KEY", "SMARTLEAD_KEY"),
    "ai_ark": ("ai_ark", "aiark", "ai_ark_api_key", "AI_ARK_API_KEY", "AIARK_API_KEY"),
    "leadmagic": ("leadmagic", "leadmagic_api_key", "LEADMAGIC_API_KEY", "LEADMAGIC_KEY"),
    "prospeo": ("prospeo", "prospeo_api_key", "PROSPEO_API_KEY"),
    "apify": ("apify", "apify_token", "APIFY_TOKEN", "APIFY_API_TOKEN"),
}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_service_role_key: str
    supabase_anon_key: str
    getleads_api_key: str
    getleads_base_url: str
    getleads_people_path: str
    getleads_upload_url: str
    ai_ark_api_key: str
    leadmagic_api_key: str
    prospeo_api_key: str
    smartlead_api_key: str
    smartlead_base_url: str
    apify_token: str
    apify_serp_actor: str
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
        getleads_api_key=_env("GETLEADS_API_KEY"),
        getleads_base_url=_env("GETLEADS_BASE_URL", "https://app.getleads.io/api").rstrip("/"),
        getleads_people_path=_env("GETLEADS_PEOPLE_PATH", "/people"),
        getleads_upload_url=_env("GETLEADS_UPLOAD_URL"),
        ai_ark_api_key=_env("AI_ARK_API_KEY") or _env("AIARK_API_KEY"),
        leadmagic_api_key=_env("LEADMAGIC_API_KEY") or _env("LEADMAGIC_KEY"),
        prospeo_api_key=_env("PROSPEO_API_KEY"),
        smartlead_api_key=_env("SMARTLEAD_API_KEY") or _env("SMARTLEAD_KEY"),
        smartlead_base_url=_env(
            "SMARTLEAD_FIND_EMAIL_BASE_URL",
            "https://prospect-api.smartlead.ai/api/v1/search-email-leads",
        ).rstrip("/"),
        apify_token=_env("APIFY_TOKEN") or _env("APIFY_API_TOKEN"),
        apify_serp_actor=_env("APIFY_SERP_ACTOR", "apify/google-search-scraper"),
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
        getleads_api_key=pick(settings.getleads_api_key, *API_KEY_ALIASES["getleads"]),
        smartlead_api_key=pick(settings.smartlead_api_key, *API_KEY_ALIASES["smartlead"]),
        ai_ark_api_key=pick(settings.ai_ark_api_key, *API_KEY_ALIASES["ai_ark"]),
        leadmagic_api_key=pick(settings.leadmagic_api_key, *API_KEY_ALIASES["leadmagic"]),
        prospeo_api_key=pick(settings.prospeo_api_key, *API_KEY_ALIASES["prospeo"]),
        apify_token=pick(settings.apify_token, *API_KEY_ALIASES["apify"]),
    )
    return updated
