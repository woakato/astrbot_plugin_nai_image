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
