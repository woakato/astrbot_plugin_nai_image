"""Separate OpenAI, NovelAI and sta1n configuration and legacy migration."""

STARBRIDE_URL = "https://api.almoons.xyz"
OFFICIAL_URL = "https://image.novelai.net"
NATIVE_MODELS = (
    "nai-diffusion-5-full",
    "nai-diffusion-5-curated",
    "nai-diffusion-4-5-full",
    "nai-diffusion-4-5-curated",
)
NATIVE_SIZE_OPTIONS = (
    ("832x1216", "标准竖图 · 832 × 1216"),
    ("1216x832", "标准横图 · 1216 × 832"),
    ("1024x1024", "标准方图 · 1024 × 1024"),
    ("512x768", "小竖图 · 512 × 768"),
    ("768x512", "小横图 · 768 × 512"),
    ("640x640", "小方图 · 640 × 640"),
    ("1024x1536", "大竖图 · 1024 × 1536"),
    ("1536x1024", "大横图 · 1536 × 1024"),
    ("1472x1472", "大方图 · 1472 × 1472"),
    ("1088x1920", "竖屏壁纸 · 1088 × 1920"),
    ("1920x1088", "横屏壁纸 · 1920 × 1088"),
)
REFERENCE_FIELDS = (
    "reference_mode",
    "director_caption",
    "vibe_strength",
    "director_strength",
    "director_secondary_strength",
    "director_fallback_images",
    "seed",
)


def migrate_novelai_config(config: dict) -> dict:
    """Move a legacy native profile once; subsequent explicit choices win."""
    if config.get("_novelai_config_version"):
        return {}
    updates = {"_novelai_config_version": 1, "_api_credentials_version": 1}
    for suffix in ("api_base_url", "api_key"):
        legacy_value = config.get("starbridge_" + suffix)
        current_value = config.get("starbride_" + suffix)
        if legacy_value and current_value in (
            None,
            "",
            STARBRIDE_URL if suffix == "api_base_url" else "",
        ):
            updates["starbride_" + suffix] = legacy_value
    legacy_native = config.get("openai_api_profile") == "starbridge"
    if legacy_native:
        if config.get("call_mode") == "openai":
            updates["call_mode"] = "novelai"
        updates["novelai_service"] = "starbride"
        shared = not config.get("_api_credentials_version") and not (
            config.get("starbridge_api_base_url") or config.get("starbridge_api_key")
        )
        for suffix in ("api_base_url", "api_key"):
            old_key = ("openai_" if shared else "starbridge_") + suffix
            if config.get(old_key):
                updates["starbride_" + suffix] = config[old_key]
        if shared:
            updates.update(openai_api_base_url="", openai_api_key="")
        if config.get("openai_api_model"):
            updates["novelai_api_model"] = config["openai_api_model"]
        for suffix in REFERENCE_FIELDS:
            if "openai_" + suffix in config:
                updates["novelai_" + suffix] = config["openai_" + suffix]
        if config.get("starbridge_reference_model"):
            updates["novelai_reference_model"] = config["starbridge_reference_model"]
        old_size = config.get("image_size", "竖图")
        legacy_sizes = {
            "竖图": "832x1216",
            "横图": "1216x832",
            "方图": "1024x1024",
            "2K竖图": "1088x1920",
            "2K横图": "1920x1088",
            "2K方图": "1472x1472",
        }
        resolved = legacy_sizes.get(old_size, old_size)
        updates["novelai_image_size"] = resolved if "x" in resolved else "832x1216"
    elif config.get("call_mode") == "openai" and config.get("image_size"):
        updates["openai_image_size"] = config["image_size"]
    config.update(updates)
    return updates


def api_settings(config: dict, mode: str) -> dict:
    """Resolve only the selected service's address, key and model."""
    if mode == "novelai":
        service = str(config.get("novelai_service") or "starbride").lower()
        if service not in {"starbride", "official"}:
            service = "starbride"
        prefix = "starbride" if service == "starbride" else "novelai_official"
        default_url = STARBRIDE_URL if service == "starbride" else OFFICIAL_URL
        return {
            "profile": service,
            "url": str(config.get(prefix + "_api_base_url") or default_url)
            .strip()
            .rstrip("/"),
            "key": str(config.get(prefix + "_api_key") or "").strip(),
            "model": str(config.get("novelai_api_model") or NATIVE_MODELS[0]).strip(),
        }
    return {
        "profile": "compatible",
        "url": str(config.get("openai_api_base_url") or "").strip().rstrip("/"),
        "key": str(config.get("openai_api_key") or "").strip(),
        "model": str(config.get("openai_api_model") or NATIVE_MODELS[0]).strip(),
    }
