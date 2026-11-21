"""Centralized configuration and constants for Dola International (https://www.dola.com/)."""

from __future__ import annotations

import os
from typing import Optional

# ── Dola International Portal & Protocol Constants ───────────────────────────

DOLA_URL = "https://www.dola.com"
DOLA_CHAT_URL = f"{DOLA_URL}/chat/"
DOLA_LOGIN_URL = f"{DOLA_URL}/chat/login"
DOLA_COMPLETION_URL = f"{DOLA_URL}/chat/completion"
DOLA_SAMANTHA_COMPLETION_URL = f"{DOLA_URL}/samantha/chat/completion"
DOLA_ACCOUNT_INFO_URL = f"{DOLA_URL}/passport/account/info/v2/"
DOLA_COOKIE_DOMAIN = ".dola.com"

# Verified from https://www.dola.com/chat/ SSR _ROUTER_DATA & chat bundle:
# oF = {497858: "doubao_web", 495671: "dola_web", 582478: "doubao_pc", 582465: "dola_pc"}
DOLA_WEB_AID = 495671
DOLA_PC_AID = 582465
DOLA_DEFAULT_BOT_ID = "7339470689562525703"
DOLA_PC_VERSION = "3.46.4"
DOLA_VERSION_CODE = "20800"
DOLA_TEA_CACHE_KEY = f"__tea_cache_tokens_{DOLA_WEB_AID}"

DOLA_DEFAULT_LANGUAGE = "en"
DOLA_DEFAULT_LOCALE = "en-US"
DOLA_DEFAULT_REGION = "SG"
DOLA_DEFAULT_TIMEZONE = "America/New_York"

# ByteDance overseas ImageX / TOS defaults
DOLA_IMAGEX_REGION = "ap-singapore-1"
DOLA_IMAGEX_SERVICE = "imagex"
DOLA_TOS_FALLBACK_HOST = "tos-mya2lf.vodupload.com"

# ByteDance overseas SecSDK / VerifyCenter host
DOLA_VERIFY_HOST = "https://verify-sg.byteoversea.com"

# Default persistent profile directory
DOLA_DEFAULT_PROFILE_DIR = os.path.expanduser("~/.dola_browser")

# Verified Dola International AI Creation models (from /alice/slot/action_bar_v3/get_item_conf)
DOLA_IMAGE_MODELS = {
    "seedream-4.5": "Seedream 4.5",
    "seedream-5.0-pro": "Seedream 5.0 Pro",
    "seedream-5.0": "Seedream 5.0 Pro",
    "dola-image": "Seedream 4.5",
    "doubao-image": "Seedream 4.5",
    "dall-e-3": "Seedream 4.5",
    "dall-e-2": "Seedream 4.5",
    "gpt-image-1": "Seedream 5.0 Pro",
}

DOLA_VIDEO_MODELS = {
    "seedance-2.0-fast": "seedance_v2.0",
    "seedance-2.0": "seedance_v2.0",
    "seedance-2.0-pro": "seedance_v2.0",
    "seedance-1.0": "ic_mini",
    "dola-video": "seedance_v2.0",
    "doubao-video": "seedance_v2.0",
}

DOLA_DEFAULT_IMAGE_MODEL = "Seedream 4.5"
DOLA_DEFAULT_VIDEO_MODEL = "seedance_v2.0"


def resolve_dola_image_model(model: Optional[str]) -> str:
    if not model:
        return DOLA_DEFAULT_IMAGE_MODEL
    raw = str(model).strip()
    if raw in ("Seedream 4.5", "Seedream 5.0 Pro"):
        return raw
    return DOLA_IMAGE_MODELS.get(raw.lower(), DOLA_DEFAULT_IMAGE_MODEL)


def resolve_dola_video_model(model: Optional[str]) -> str:
    if not model:
        return DOLA_DEFAULT_VIDEO_MODEL
    raw = str(model).strip()
    if raw in ("seedance_v2.0", "ic_mini"):
        return raw
    return DOLA_VIDEO_MODELS.get(raw.lower(), DOLA_DEFAULT_VIDEO_MODEL)


def get_env(dola_key: str, legacy_key: Optional[str] = None, default: str = "") -> str:
    """Read DOLA_* env var first, falling back to legacy DOUBAO_* env var."""
    val = os.environ.get(dola_key)
    if val is not None and val != "":
        return val
    if legacy_key:
        legacy_val = os.environ.get(legacy_key)
        if legacy_val is not None and legacy_val != "":
            return legacy_val
    return default


def get_default_profile_dir() -> str:
    """Resolve default browser profile data directory from env or home folder."""
    raw = get_env("DOLA_BROWSER_DATA", "DOUBAO_BROWSER_DATA", "").strip()
    if raw:
        return os.path.expanduser(raw)
    return DOLA_DEFAULT_PROFILE_DIR


def get_configured_proxy() -> Optional[str]:
    """Return raw proxy string from DOLA_PROXY / DOUBAO_PROXY env if set."""
    raw = get_env("DOLA_PROXY", "DOUBAO_PROXY", "").strip()
    return raw or None
