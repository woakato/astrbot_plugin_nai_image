"""Credential migration to the unified native NovelAI protocol configuration."""

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


def test_openai_pair_is_preserved_and_uses_native_protocol(load_plugin):
    plugin, saved = load_plugin(
        {
            "call_mode": "openai",
            "openai_api_base_url": "https://api.almoons.xyz/",
            "openai_api_key": "native-key",
        }
    )
    assert plugin.openai_api_base_url == "https://api.almoons.xyz"
    assert plugin.openai_api_key == "native-key"
    assert saved["openai_api_key"] == "native-key"
    assert saved["_api_credentials_version"] == 2
    assert saved["save_image_history"] is True
    assert saved["save_generation_parameters"] is True

    plugin._session = Session()
    images, reason = asyncio.run(
        plugin._openai_generate("a red apple", "1024x1024", n=1)
    )
    assert images and reason == "ok"
    assert (
        plugin._session.calls[0]["endpoint"]
        == "https://api.almoons.xyz/ai/generate-image"
    )
    assert plugin._session.calls[0]["headers"]["Authorization"] == "Bearer native-key"


def test_legacy_starbridge_pair_is_migrated_into_openai_fields_once(load_plugin):
    plugin, saved = load_plugin(
        {
            "call_mode": "openai",
            "starbridge_api_base_url": "https://starbridge.example",
            "starbridge_api_key": "native-key",
            "enable_translate": True,
            "_api_credentials_version": 1,
        }
    )
    assert plugin.call_mode == "openai"
    assert plugin.openai_api_base_url == "https://starbridge.example"
    assert plugin.openai_api_key == "native-key"
    assert saved["openai_api_base_url"] == "https://starbridge.example"
    assert saved["openai_api_key"] == "native-key"
    assert saved["starbridge_api_base_url"] == ""
    assert saved["starbridge_api_key"] == ""
    assert saved["enable_translate"] == "开启"
    assert saved["_api_credentials_version"] == 2

    reloaded, saved_again = load_plugin()
    assert reloaded.openai_api_key == "native-key"
    assert saved_again == saved


def test_gallery_defaults_enable_once_and_respect_later_user_opt_out(load_plugin):
    plugin, saved = load_plugin(
        {
            "save_image_history": False,
            "save_generation_parameters": False,
        }
    )
    assert plugin.save_image_history is True
    assert plugin.save_generation_parameters is True
    assert saved["save_image_history"] is True
    assert saved["save_generation_parameters"] is True
    assert saved["_gallery_defaults_version"] == 1

    opted_out, saved_again = load_plugin(
        {
            "save_image_history": False,
            "save_generation_parameters": False,
        }
    )
    assert opted_out.save_image_history is False
    assert opted_out.save_generation_parameters is False
    assert saved_again["save_image_history"] is False
    assert saved_again["save_generation_parameters"] is False


def test_unconfigured_service_reports_not_configured(load_plugin):
    plugin, saved = load_plugin(
        {
            "call_mode": "openai",
            "_api_credentials_version": 2,
        }
    )
    plugin._session = Session()
    assert plugin.call_mode == saved["call_mode"] == "openai"
    assert plugin.openai_api_base_url == plugin.openai_api_key == ""
    images, reason = asyncio.run(plugin._openai_generate("apple", "1024x1024"))
    assert images == [] and reason == "openai_not_configured"
    assert plugin._session.calls == []


def test_mode_repair_accepts_dedicated_starbridge_address():
    config = {
        "call_mode": "openai",
        "starbridge_api_base_url": "https://native.example",
    }
    assert migrate_invalid_call_mode(config) is None
    assert config["call_mode"] == "openai"


def test_credential_fields_are_visible_only_in_third_party_mode():
    expected_fields = {
        "openai_active_channel",
        "openai_enable_fallback",
        "openai_api_name",
        "openai_api_base_url",
        "openai_api_key",
        "openai_backup_name",
        "openai_backup_base_url",
        "openai_backup_key",
    }
    for call_mode, expected in (("direct", set()), ("openai", expected_fields)):
        config = {"call_mode": call_mode}
        visible = {
            key
            for key in expected_fields
            if all(config.get(k) == v for k, v in SCHEMA[key]["condition"].items())
        }
        assert visible == expected


def test_legacy_both_openai_and_starbridge_migrates_starbridge_to_backup(load_plugin):
    plugin, saved = load_plugin(
        {
            "call_mode": "openai",
            "openai_api_base_url": "https://api1.syuan.org",
            "openai_api_key": "syuan-key",
            "starbridge_api_base_url": "https://api.almoons.xyz",
            "starbridge_api_key": "almoon-key",
            "_api_credentials_version": 1,
        }
    )
    assert saved["openai_api_base_url"] == "https://api1.syuan.org"
    assert saved["openai_api_key"] == "syuan-key"
    assert saved["openai_backup_base_url"] == "https://api.almoons.xyz"
    assert saved["openai_backup_key"] == "almoon-key"
    assert saved["starbridge_api_base_url"] == ""
    assert saved["starbridge_api_key"] == ""
    channels = plugin._openai_channels()
    assert [c["name"] for c in channels] == ["星渊", "星桥"]


def test_dual_channels_can_be_configured_and_switched_simultaneously(load_plugin):
    plugin, _ = load_plugin(
        {
            "call_mode": "openai",
            "openai_active_channel": "primary",
            "openai_api_base_url": "https://api1.syuan.org/",
            "openai_api_key": "syuan-key",
            "openai_backup_base_url": "https://api.almoons.xyz/",
            "openai_backup_key": "almoon-key",
        }
    )
    plugin._session = Session()

    images, reason = asyncio.run(plugin._openai_generate("apple", "1024x1024", n=1))
    assert images and reason == "ok"
    assert plugin._session.calls[-1]["endpoint"] == "https://api1.syuan.org/ai/generate-image"
    assert plugin._session.calls[-1]["headers"]["Authorization"] == "Bearer syuan-key"

    images, reason = asyncio.run(
        plugin._openai_generate("apple", "1024x1024", n=1, channel="backup")
    )
    assert images and reason == "ok"
    assert plugin._session.calls[-1]["endpoint"] == "https://api.almoons.xyz/ai/generate-image"
    assert plugin._session.calls[-1]["headers"]["Authorization"] == "Bearer almoon-key"

    images, reason = asyncio.run(
        plugin._openai_generate("apple", "1024x1024", n=1, channel="星桥")
    )
    assert images and reason == "ok"
    assert plugin._session.calls[-1]["endpoint"] == "https://api.almoons.xyz/ai/generate-image"


def test_dual_channel_safe_fallback_and_non_fallback(load_plugin):
    from test_starbridge import Response, png
    import base64

    ok_body = {"images": [{"image": base64.b64encode(png()).decode(), "index": 0}]}

    class FallbackSession(Session):
        def __init__(self, primary_handler):
            super().__init__()
            self.primary_handler = primary_handler

        def post(self, endpoint, *, json, **kwargs):
            import copy

            self.calls.append({"endpoint": endpoint, "body": copy.deepcopy(json), **kwargs})
            if "syuan.org" in endpoint:
                return self.primary_handler(endpoint, json)
            return Response(ok_body, status=201)

    plugin, _ = load_plugin(
        {
            "call_mode": "openai",
            "openai_active_channel": "primary",
            "openai_enable_fallback": True,
            "openai_api_base_url": "https://api1.syuan.org",
            "openai_api_key": "syuan-key",
            "openai_backup_base_url": "https://api.almoons.xyz",
            "openai_backup_key": "almoon-key",
        }
    )

    # 429 on primary safely falls back to backup in auto mode
    plugin._session = FallbackSession(
        lambda ep, body: Response({"message": "rate limited"}, status=429)
    )
    images, reason = asyncio.run(plugin._openai_generate("apple", "1024x1024", n=1))
    assert images and reason == "ok"
    assert [c["endpoint"] for c in plugin._session.calls] == [
        "https://api1.syuan.org/ai/generate-image",
        "https://api.almoons.xyz/ai/generate-image",
    ]

    # Explicit channel selection does NOT fall back so testing stays isolated
    plugin._session = FallbackSession(
        lambda ep, body: Response({"message": "rate limited"}, status=429)
    )
    images, reason = asyncio.run(
        plugin._openai_generate("apple", "1024x1024", n=1, channel="primary")
    )
    assert images == [] and reason.startswith("http_429")
    assert len(plugin._session.calls) == 1

    # Timeout does NOT fall back (prevents double billing)
    def raise_timeout(_ep, _body):
        raise asyncio.TimeoutError()

    plugin._session = FallbackSession(raise_timeout)
    images, reason = asyncio.run(plugin._openai_generate("apple", "1024x1024", n=1))
    assert images == [] and reason == "timeout"
    assert len(plugin._session.calls) == 1

    # 400 param error does NOT fall back
    plugin._session = FallbackSession(
        lambda ep, body: Response({"message": "bad size"}, status=400)
    )
    images, reason = asyncio.run(plugin._openai_generate("apple", "1024x1024", n=1))
    assert images == [] and reason.startswith("http_400")
    assert len(plugin._session.calls) == 1

    # Vibe encoding: fail on 1st image (429, uncharged) -> falls back; fail on 2nd image (charged) -> does NOT fall back
    def vibe_handler_factory(fail_on_second: bool):
        state = {"vibe_calls": 0}

        def handler(ep, _body):
            if "syuan.org" in ep and ep.endswith("encode-vibe"):
                state["vibe_calls"] += 1
                if not fail_on_second:
                    return Response({"message": "rate limited"}, status=429)
                if state["vibe_calls"] == 1:
                    return Response(raw=b"encoded-1", status=201)
                return Response({"message": "rate limited"}, status=429)
            if ep.endswith("encode-vibe"):
                return Response(raw=b"encoded-backup", status=201)
            return Response(ok_body, status=201)

        return handler

    class VibeFallbackSession(Session):
        def __init__(self, handler):
            super().__init__(handler)

    plugin._session = VibeFallbackSession(vibe_handler_factory(False))
    images, reason = asyncio.run(
        plugin._openai_generate(
            "apple",
            "1024x1024",
            n=1,
            model="nai-diffusion-4-5-full",
            reference_mode="vibe",
            reference_image_bytes_list=[png(), png()],
        )
    )
    assert images and reason == "ok"
    assert [c["endpoint"] for c in plugin._session.calls] == [
        "https://api1.syuan.org/ai/encode-vibe",
        "https://api.almoons.xyz/ai/encode-vibe",
        "https://api.almoons.xyz/ai/encode-vibe",
        "https://api.almoons.xyz/ai/generate-image",
    ]

    plugin._session = VibeFallbackSession(vibe_handler_factory(True))
    images, reason = asyncio.run(
        plugin._openai_generate(
            "apple",
            "1024x1024",
            n=1,
            model="nai-diffusion-4-5-full",
            reference_mode="vibe",
            reference_image_bytes_list=[png(), png()],
        )
    )
    assert images == [] and "此前 1 张编码已完成并可能计费" in reason
    assert [c["endpoint"] for c in plugin._session.calls] == [
        "https://api1.syuan.org/ai/encode-vibe",
        "https://api1.syuan.org/ai/encode-vibe",
    ]


def test_test_panel_config_exposes_sanitized_channels_and_switches_default(
    load_plugin, monkeypatch
):
    plugin, _ = load_plugin(
        {
            "call_mode": "openai",
            "openai_active_channel": "primary",
            "openai_api_base_url": "https://api1.syuan.org",
            "openai_api_key": "syuan-secret",
            "openai_backup_base_url": "https://api.almoons.xyz",
            "openai_backup_key": "almoon-secret",
        }
    )
    monkeypatch.setattr(plugin, "_sync_channel_labels", lambda: None)
    resp = asyncio.run(plugin._test_panel_get_config())
    raw_body = resp.body.decode("utf-8")
    payload = json.loads(raw_body)
    assert "syuan-secret" not in raw_body
    assert "almoon-secret" not in raw_body
    assert payload["openai_active_channel"] == "primary"
    assert [c["name"] for c in payload["openai_channels"]] == ["星渊", "星桥"]
    assert payload["openai_channels"][0]["is_active"] is True

    class FakeReq:
        async def json(self, default=None):
            return {"channel": "backup"}

    monkeypatch.setattr("main.web_request", FakeReq(), raising=False)
    sw_resp = asyncio.run(plugin._test_panel_switch_channel())
    sw_data = json.loads(sw_resp.body)["data"]
    assert sw_data["openai_active_channel"] == "backup"
    assert sw_data["openai_api_base_url"] == "https://api.almoons.xyz"
    assert plugin.openai_active_channel == "backup"
    assert plugin.openai_api_base_url == "https://api.almoons.xyz"
    assert plugin.openai_api_key == "almoon-secret"

    # Panel generate_openai forwards explicit channel selection
    plugin._session = Session()

    class FakeGenReq:
        async def json(self, default=None):
            return {
                "nai_prompt": "1girl",
                "size": "832x1216",
                "channel": "primary",
            }

    monkeypatch.setattr("main.web_request", FakeGenReq(), raising=False)
    gen_resp = asyncio.run(plugin._test_panel_generate_openai())
    gen_data = json.loads(gen_resp.body)
    assert gen_data["status"] == "ok"
    assert (
        plugin._session.calls[0]["endpoint"]
        == "https://api1.syuan.org/ai/generate-image"
    )
    assert plugin._session.calls[0]["headers"]["Authorization"] == "Bearer syuan-secret"


def test_v211_starbride_novelai_config_migrates_losslessly_from_disk_and_is_idempotent(
    load_plugin, tmp_path
):
    """Simulate a real sanitized v2.11.1 Starbride config loaded via AstrBotConfig."""
    config_path = tmp_path / f"{PLUGIN_NAME}_config.json"
    v211_disk_config = {
        "call_mode": "novelai",
        "image_gen_key": "",
        "base_url": "https://nai.sta1n.cn",
        "request_timeout": 180,
        "novelai_service": "starbride",
        "starbride_api_base_url": "https://api.starbridge.zone",
        "starbride_api_key": "synthetic-starbride-key",
        "novelai_official_api_base_url": "https://image.novelai.net",
        "novelai_official_api_key": "",
        "novelai_api_model": "nai-diffusion-5-curated",
        "novelai_image_size": "832x1216",
        "novelai_reference_model": "nai-diffusion-4-5-full",
        "_novelai_config_version": 1,
        "openai_api_base_url": "",
        "openai_api_key": "",
        "_api_credentials_version": 1,
        "openai_api_model": "nai-diffusion-4-5-full",
        "image_size": "竖图",
        "openai_reference_mode": "vibe",
        "novelai_reference_mode": "director",
        "openai_director_caption": "character",
        "novelai_director_caption": "character&style",
        "openai_vibe_strength": 0.4,
        "novelai_vibe_strength": 0.65,
        "openai_director_strength": 1.0,
        "novelai_director_strength": 0.75,
        "openai_director_secondary_strength": 0.5,
        "novelai_director_secondary_strength": 0.45,
        "openai_director_fallback_images": ["stale-fallback.png"],
        "novelai_director_fallback_images": [],
        "openai_seed": 42,
        "novelai_seed": -1,
        "starbridge_api_base_url": "",
        "starbridge_api_key": "",
    }
    config_path.write_text(
        json.dumps(v211_disk_config, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    plugin, saved = load_plugin()

    assert plugin.call_mode == saved["call_mode"] == "openai"
    assert plugin.openai_api_base_url == "https://api.starbridge.zone"
    assert plugin.openai_api_key == "synthetic-starbride-key"
    assert saved["openai_api_base_url"] == "https://api.starbridge.zone"
    assert saved["openai_api_key"] == "synthetic-starbride-key"
    # Empty-key default official URL must not pollute backup channel.
    assert saved["openai_backup_base_url"] == ""
    assert saved["openai_backup_key"] == ""
    assert plugin.openai_api_model == saved["openai_api_model"] == "nai-diffusion-5-curated"
    assert plugin.image_size == saved["image_size"] == "竖图"
    assert plugin.openai_reference_mode == saved["openai_reference_mode"] == "director"
    assert plugin.openai_director_caption == saved["openai_director_caption"] == "character&style"
    assert plugin.openai_vibe_strength == saved["openai_vibe_strength"] == 0.65
    assert plugin.openai_director_strength == saved["openai_director_strength"] == 0.75
    assert (
        plugin.openai_director_secondary_strength
        == saved["openai_director_secondary_strength"]
        == 0.45
    )
    assert plugin.openai_director_fallback_images == saved["openai_director_fallback_images"] == []
    assert plugin.openai_seed == saved["openai_seed"] == -1
    assert saved["_api_credentials_version"] == 2
    # Legacy secret fields are cleared after migration.
    assert saved["starbride_api_base_url"] == ""
    assert saved["starbride_api_key"] == ""
    assert saved["novelai_official_api_base_url"] == ""
    assert saved["novelai_official_api_key"] == ""
    assert plugin._openai_channels()[0]["name"] == "星桥"

    # End-to-end generation uses the migrated endpoint and key immediately.
    plugin._session = Session()
    images, reason = asyncio.run(plugin._openai_generate("1girl", "832x1216", n=1))
    assert images and reason == "ok"
    assert (
        plugin._session.calls[0]["endpoint"]
        == "https://api.starbridge.zone/ai/generate-image"
    )
    assert (
        plugin._session.calls[0]["headers"]["Authorization"]
        == "Bearer synthetic-starbride-key"
    )

    # Subsequent manual user edits must NOT be overwritten on reload.
    edited, saved_after_edit = load_plugin(
        {
            "openai_reference_mode": "vibe",
            "openai_director_strength": 2.25,
            "openai_seed": 12345,
        }
    )
    assert edited.openai_reference_mode == saved_after_edit["openai_reference_mode"] == "vibe"
    assert edited.openai_director_strength == saved_after_edit["openai_director_strength"] == 2.25
    assert edited.openai_seed == saved_after_edit["openai_seed"] == 12345


def test_v211_novelai_with_existing_openai_moves_starbride_to_active_backup(
    load_plugin, tmp_path
):
    config_path = tmp_path / f"{PLUGIN_NAME}_config.json"
    config_path.write_text(
        json.dumps(
            {
                "call_mode": "novelai",
                "novelai_service": "starbride",
                "openai_api_base_url": "https://api1.syuan.org",
                "openai_api_key": "syuan-key",
                "starbride_api_base_url": "https://api.starbridge.zone",
                "starbride_api_key": "starbride-key",
                "novelai_reference_mode": "director",
                "novelai_director_strength": 0.8,
                "_api_credentials_version": 1,
                "_novelai_config_version": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    plugin, saved = load_plugin()
    assert plugin.call_mode == saved["call_mode"] == "openai"
    assert saved["openai_api_base_url"] == "https://api1.syuan.org"
    assert saved["openai_api_key"] == "syuan-key"
    assert saved["openai_backup_base_url"] == "https://api.starbridge.zone"
    assert saved["openai_backup_key"] == "starbride-key"
    assert plugin.openai_active_channel == saved["openai_active_channel"] == "backup"
    assert plugin.openai_api_base_url == "https://api.starbridge.zone"
    assert plugin.openai_api_key == "starbride-key"
    assert plugin.openai_reference_mode == "director"
    assert plugin.openai_director_strength == 0.8


def test_v211_official_service_and_size_seed_migrate_cleanly(load_plugin, tmp_path):
    config_path = tmp_path / f"{PLUGIN_NAME}_config.json"
    config_path.write_text(
        json.dumps(
            {
                "call_mode": "novelai",
                "novelai_service": "official",
                "starbride_api_base_url": "https://api.almoons.xyz",
                "starbride_api_key": "",
                "novelai_official_api_base_url": "https://image.novelai.net",
                "novelai_official_api_key": "official-token",
                "novelai_image_size": "1216x832",
                "novelai_seed": 0,
                "_api_credentials_version": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    plugin, saved = load_plugin()
    assert plugin.call_mode == "openai"
    assert plugin.openai_api_base_url == "https://image.novelai.net"
    assert plugin.openai_api_key == "official-token"
    assert saved["openai_backup_base_url"] == ""
    assert plugin.image_size == saved["image_size"] == "横图"
    assert plugin.openai_seed == saved["openai_seed"] == 0


def test_v211_direct_mode_ignores_untouched_default_starbride_urls(
    load_plugin, tmp_path
):
    config_path = tmp_path / f"{PLUGIN_NAME}_config.json"
    config_path.write_text(
        json.dumps(
            {
                "call_mode": "direct",
                "image_gen_key": "direct-token",
                "starbride_api_base_url": "https://api.almoons.xyz",
                "starbride_api_key": "",
                "novelai_official_api_base_url": "https://image.novelai.net",
                "novelai_official_api_key": "",
                "_api_credentials_version": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    plugin, saved = load_plugin()
    assert plugin.call_mode == saved["call_mode"] == "direct"
    assert saved["openai_api_base_url"] == ""
    assert saved["openai_backup_base_url"] == ""
    assert saved["_api_credentials_version"] == 2


def test_schema_uses_clean_generic_default_labels():
    assert SCHEMA["openai_active_channel"]["labels"] == [
        "渠道 1（主用）",
        "渠道 2（备选）",
    ]
    slot_labels = SCHEMA["active_artist_slot"]["labels"]
    assert len(slot_labels) == len(SCHEMA["active_artist_slot"]["options"]) == 7
    assert "apiauto.starbridge.zone" not in json.dumps(SCHEMA, ensure_ascii=False)
    assert "群友画风" not in slot_labels
    assert "最精美" not in slot_labels


def test_image_prompt_spec_path_and_silent_absence(tmp_path, monkeypatch):
    import main as nai_main

    assert nai_main._IMAGE_PROMPT_SPEC_PATH == (
        Path(nai_main.__file__).resolve().parent / "image_prompt_spec.txt"
    )
    warnings = []
    monkeypatch.setattr(nai_main.logger, "warning", lambda msg: warnings.append(msg))
    assert nai_main._load_image_prompt_spec(tmp_path / "missing.txt") == ""
    assert warnings == []

    spec_file = tmp_path / "image_prompt_spec.txt"
    spec_file.write_text("  Custom NAI Spec  \n", encoding="utf-8")
    assert nai_main._load_image_prompt_spec(spec_file) == "Custom NAI Spec"
    assert warnings == []


def test_v211_migration_preserves_unmigrated_legacy_keys_when_both_slots_occupied(
    load_plugin, tmp_path
):
    """When primary & backup are already occupied by other channels, do not wipe unmigrated legacy API keys."""
    config_path = tmp_path / f"{PLUGIN_NAME}_config.json"
    config_path.write_text(
        json.dumps(
            {
                "call_mode": "novelai",
                "novelai_service": "starbride",
                "openai_api_base_url": "https://api1.syuan.org",
                "openai_api_key": "syuan-key",
                "openai_backup_base_url": "https://api2.example.com",
                "openai_backup_key": "backup-key",
                "starbride_api_base_url": "https://api.starbridge.zone",
                "starbride_api_key": "unmigrated-starbride-key",
                "novelai_official_api_base_url": "https://image.novelai.net",
                "novelai_official_api_key": "",
                "starbridge_api_base_url": "https://legacy.starbridge.example",
                "starbridge_api_key": "unmigrated-starbridge-key",
                "_api_credentials_version": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    plugin, saved = load_plugin()
    assert plugin.call_mode == saved["call_mode"] == "openai"
    assert saved["openai_api_key"] == "syuan-key"
    assert saved["openai_backup_key"] == "backup-key"
    # Non-empty unmigrated legacy credentials must remain intact instead of being wiped.
    assert saved["starbride_api_base_url"] == "https://api.starbridge.zone"
    assert saved["starbride_api_key"] == "unmigrated-starbride-key"
    assert saved["starbridge_api_base_url"] == "https://legacy.starbridge.example"
    assert saved["starbridge_api_key"] == "unmigrated-starbridge-key"
    # Untouched empty-key default official URL is still cleaned up.
    assert saved["novelai_official_api_base_url"] == ""
    assert saved["novelai_official_api_key"] == ""


def test_v211_migration_resets_active_channel_to_primary_and_clears_fallback_on_v300_recovery(
    load_plugin, tmp_path
):
    """prefer_novelai resets stale backup active channel to primary and honors empty fallback list on v3.0.0 recovery."""
    # Case 1: legacy novelai channel matches primary while stale openai_active_channel was "backup".
    config_path = tmp_path / f"{PLUGIN_NAME}_config.json"
    config_path.write_text(
        json.dumps(
            {
                "call_mode": "novelai",
                "novelai_service": "starbride",
                "openai_active_channel": "backup",
                "openai_api_base_url": "https://api.starbridge.zone",
                "openai_api_key": "starbride-key",
                "openai_backup_base_url": "https://api1.syuan.org",
                "openai_backup_key": "syuan-key",
                "starbride_api_base_url": "https://api.starbridge.zone",
                "starbride_api_key": "starbride-key",
                "_api_credentials_version": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    plugin, saved = load_plugin()
    assert plugin.openai_active_channel == saved["openai_active_channel"] == "primary"
    assert plugin.openai_api_base_url == "https://api.starbridge.zone"
    assert saved["starbride_api_key"] == ""

    # Case 2: v3.0.0 reset recovery (cred_ver==2, call_mode=="direct", empty openai slots, intact starbride key)
    # should also honor empty novelai_director_fallback_images instead of keeping stale openai fallback list.
    config_path.write_text(
        json.dumps(
            {
                "call_mode": "direct",
                "image_gen_key": "",
                "novelai_service": "starbride",
                "starbride_api_base_url": "https://api.starbridge.zone",
                "starbride_api_key": "recovered-starbride-key",
                "openai_active_channel": "backup",
                "openai_api_base_url": "",
                "openai_api_key": "",
                "openai_backup_base_url": "",
                "openai_backup_key": "",
                "openai_director_fallback_images": ["stale-portrait.png"],
                "novelai_director_fallback_images": [],
                "_api_credentials_version": 2,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    recovered, saved_recovered = load_plugin()
    assert recovered.call_mode == saved_recovered["call_mode"] == "openai"
    assert recovered.openai_active_channel == saved_recovered["openai_active_channel"] == "primary"
    assert recovered.openai_api_key == "recovered-starbride-key"
    assert (
        recovered.openai_director_fallback_images
        == saved_recovered["openai_director_fallback_images"]
        == []
    )

