"""Credential migration, service switching, and request isolation."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from test_starbridge import Session

from astrbot.core.config.astrbot_config import AstrBotConfig
from main import PLUGIN_NAME, NAIGenerateImagePlugin, migrate_invalid_call_mode

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
        encoding="utf-8"
    )
)


@pytest.fixture
def load_plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "astrbot.core.utils.astrbot_path.get_astrbot_config_path", lambda: str(tmp_path)
    )
    monkeypatch.setattr("main.NAIImageCompanionExtensionAPI", Mock(), raising=False)
    monkeypatch.setattr("main._active_plugin", None)
    monkeypatch.setattr(
        NAIGenerateImagePlugin, "_refresh_llm_tool_schema", lambda _: None
    )
    monkeypatch.setattr(NAIGenerateImagePlugin, "_slot_model", lambda _: "")
    monkeypatch.setattr(NAIGenerateImagePlugin, "_archive_generated_image", AsyncMock())
    monkeypatch.setattr(NAIGenerateImagePlugin, "_record_usage_stat", AsyncMock())
    config_path = tmp_path / f"{PLUGIN_NAME}_config.json"

    def load(updates=None):
        config = AstrBotConfig(config_path=str(config_path), schema=SCHEMA)
        if updates:
            config.update(updates)
            config.save_config()
        plugin = NAIGenerateImagePlugin(Mock(), config)
        saved = json.loads(config_path.read_text(encoding="utf-8-sig"))
        return plugin, saved

    return load


def test_legacy_openai_pair_is_preserved_and_migration_is_persisted(load_plugin):
    plugin, saved = load_plugin(
        {
            "call_mode": "openai",
            "openai_api_base_url": "https://compatible.example/v1/",
            "openai_api_key": "compatible-key",
        }
    )
    assert plugin.openai_api_base_url == "https://compatible.example/v1"
    assert plugin.openai_api_key == "compatible-key"
    assert saved["openai_api_key"] == "compatible-key"
    assert saved["starbridge_api_key"] == ""
    assert saved["_api_credentials_version"] == 1


def test_legacy_starbridge_pair_is_moved_once(load_plugin):
    plugin, saved = load_plugin(
        {
            "call_mode": "openai",
            "openai_api_profile": "starbridge",
            "openai_api_base_url": "https://starbridge.example",
            "openai_api_key": "native-key",
            "enable_translate": True,
        }
    )
    assert plugin.call_mode == "openai"
    assert plugin.openai_api_base_url == "https://starbridge.example"
    assert plugin.openai_api_key == "native-key"
    assert saved["starbridge_api_base_url"] == "https://starbridge.example"
    assert saved["starbridge_api_key"] == "native-key"
    assert saved["openai_api_base_url"] == saved["openai_api_key"] == ""
    assert saved["enable_translate"] == "开启"
    assert saved["_api_credentials_version"] == 1

    reloaded, saved_again = load_plugin()
    assert reloaded.openai_api_key == "native-key"
    assert saved_again == saved


def test_existing_separate_pair_is_not_overwritten(load_plugin):
    plugin, saved = load_plugin(
        {
            "call_mode": "openai",
            "openai_api_profile": "starbridge",
            "openai_api_base_url": "https://compatible.example",
            "openai_api_key": "compatible-key",
            "starbridge_api_base_url": "https://native.example",
            "starbridge_api_key": "native-key",
        }
    )
    assert plugin.openai_api_key == "native-key"
    assert saved["openai_api_key"] == "compatible-key"
    assert saved["starbridge_api_key"] == "native-key"


def test_switching_services_uses_matching_url_and_authorization(load_plugin):
    updates = {
        "call_mode": "openai",
        "openai_api_base_url": "https://compatible.example/v1",
        "openai_api_key": "compatible-key",
        "starbridge_api_base_url": "https://native.example",
        "starbridge_api_key": "native-key",
    }
    for profile, endpoint, key in (
        (
            "compatible",
            "https://compatible.example/v1/images/generations",
            "compatible-key",
        ),
        ("starbridge", "https://native.example/ai/generate-image", "native-key"),
        (
            "compatible",
            "https://compatible.example/v1/images/generations",
            "compatible-key",
        ),
    ):
        plugin, saved = load_plugin({**updates, "openai_api_profile": profile})
        plugin._session = Session()
        images, reason = asyncio.run(
            plugin._openai_generate("a red apple", "1024x1024", n=1)
        )
        assert images and reason == "ok"
        assert plugin._session.calls[0]["endpoint"] == endpoint
        assert plugin._session.calls[0]["headers"]["Authorization"] == f"Bearer {key}"
        assert saved["openai_api_key"] == "compatible-key"
        assert saved["starbridge_api_key"] == "native-key"


@pytest.mark.parametrize("profile", ["compatible", "starbridge"])
def test_unconfigured_service_does_not_borrow_credentials_or_change_mode(
    load_plugin, profile
):
    other = "starbridge" if profile == "compatible" else "openai"
    plugin, saved = load_plugin(
        {
            "call_mode": "openai",
            "openai_api_profile": profile,
            "_api_credentials_version": 1,
            f"{other}_api_base_url": "https://other.example",
            f"{other}_api_key": "other-key",
        }
    )
    plugin._session = Session()
    assert plugin.call_mode == saved["call_mode"] == "openai"
    assert plugin.openai_api_base_url == plugin.openai_api_key == ""
    images, reason = asyncio.run(plugin._openai_generate("apple", "1024x1024"))
    assert images == [] and reason == "openai_not_configured"
    assert plugin._session.calls == []


def test_switch_to_unconfigured_starbridge_after_upgrade_preserves_openai(load_plugin):
    load_plugin(
        {
            "call_mode": "openai",
            "openai_api_base_url": "https://compatible.example",
            "openai_api_key": "compatible-key",
        }
    )
    plugin, saved = load_plugin({"openai_api_profile": "starbridge"})
    assert plugin.call_mode == "openai"
    assert plugin.openai_api_base_url == plugin.openai_api_key == ""
    assert saved["openai_api_key"] == "compatible-key"
    assert saved["starbridge_api_key"] == ""


def test_mode_repair_accepts_dedicated_starbridge_address():
    config = {
        "call_mode": "openai",
        "openai_api_profile": "starbridge",
        "starbridge_api_base_url": "https://native.example",
    }
    assert migrate_invalid_call_mode(config) is None
    assert config["call_mode"] == "openai"


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("compatible", {"openai_api_base_url", "openai_api_key"}),
        ("starbridge", {"starbridge_api_base_url", "starbridge_api_key"}),
    ],
)
def test_only_selected_credential_fields_are_visible(profile, expected):
    config = {"call_mode": "openai", "openai_api_profile": profile}
    fields = {
        "openai_api_base_url",
        "openai_api_key",
        "starbridge_api_base_url",
        "starbridge_api_key",
    }
    visible = {
        key
        for key in fields
        if all(config.get(k) == v for k, v in SCHEMA[key]["condition"].items())
    }
    assert visible == expected
