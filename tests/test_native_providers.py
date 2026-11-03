"""Native service defaults, account isolation, dimensions and official ZIPs."""

import asyncio
import base64
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_api_credentials import load_plugin
from test_starbridge import Response, Session, png
from provider_config import NATIVE_SIZE_OPTIONS, api_settings


@pytest.mark.parametrize("model", ["nai-diffusion-5-full", "nai-diffusion-5-curated"])
@pytest.mark.parametrize("model_source", ["default", "override", "slot"])
def test_panel_rejects_v5_precise_reference_before_translation_or_generation(
    load_plugin, monkeypatch, model, model_source
):
    plugin, _ = load_plugin(
        {
            "call_mode": "openai",
            "openai_api_base_url": "https://compatible.example/v1",
            "openai_api_key": "test-key",
            "openai_api_model": model,
            "openai_reference_mode": "director",
            "novelai_reference_model": "nai-diffusion-4-5-full",
        }
    )
    body = {
        "nl_prompt": "draw the same girl at the beach",
        "call_format": "openai",
        "reference_image_b64_list": [base64.b64encode(png()).decode()],
    }
    if model_source == "override":
        body["model"] = model
    elif model_source == "slot":
        body["model"] = "nai-diffusion-4-5-full"
    plugin._slot_by_id = lambda _: (
        {"model": model, "artists": ""} if model_source == "slot" else None
    )
    plugin._expand_grimoire = lambda value: value
    plugin._translate_prompt = AsyncMock()
    plugin._openai_generate = AsyncMock()
    monkeypatch.setattr(
        "main.web_request", SimpleNamespace(json=AsyncMock(return_value=body))
    )
    monkeypatch.setattr(
        "main.error_response",
        lambda message, status_code: {
            "message": message,
            "status": status_code,
        },
    )
    result = asyncio.run(plugin._test_panel_generate_openai())
    assert result["status"] == 400
    assert model in result["message"] and "明确选择 V4.5" in result["message"]
    plugin._translate_prompt.assert_not_awaited()
    plugin._openai_generate.assert_not_awaited()


def test_novelai_defaults_to_starbride_and_official_portrait(load_plugin):
    plugin, saved = load_plugin({"call_mode": "novelai"})
    assert plugin.call_mode == "novelai"
    assert plugin.novelai_service == "starbride"
    assert plugin.openai_api_base_url == "https://api.almoons.xyz"
    assert plugin.image_size == "832x1216"
    assert saved["novelai_service"] == "starbride"


def test_legacy_unconfigured_openai_keeps_existing_mode_repair(load_plugin):
    plugin, saved = load_plugin({"call_mode": "openai", "openai_api_base_url": ""})
    assert plugin.call_mode == saved["call_mode"] == "direct"


def test_three_modes_and_both_native_services_keep_separate_keys_and_sizes(load_plugin):
    settings = dict(
        openai_api_base_url="https://compatible.example/v1",
        openai_api_key="openai-key",
        starbride_api_key="native-key",
        novelai_official_api_key="official-key",
        image_size="2K竖图",
        openai_image_size="横图",
        novelai_image_size="640x640",
    )
    for mode, service, expected_url, expected_key, expected_size in [
        ("openai", "starbride", "https://compatible.example/v1", "openai-key", "横图"),
        ("novelai", "starbride", "https://api.almoons.xyz", "native-key", "640x640"),
        ("novelai", "official", "https://image.novelai.net", "official-key", "640x640"),
        ("direct", "official", "https://compatible.example/v1", "openai-key", "2K竖图"),
    ]:
        plugin, saved = load_plugin(
            {
                **settings,
                "call_mode": mode,
                "novelai_service": service,
                "_novelai_config_version": 1,
            }
        )
        assert plugin.call_mode == mode
        assert (
            plugin.openai_api_base_url,
            plugin.openai_api_key,
            plugin.image_size,
        ) == (expected_url, expected_key, expected_size)


@pytest.mark.parametrize(
    "service, expected",
    [
        ("starbride", "https://api.almoons.xyz"),
        ("official", "https://image.novelai.net"),
    ],
)
def test_panel_uses_explicit_mode_credentials_without_mutating_selected_service(
    load_plugin, service, expected
):
    plugin, _ = load_plugin(
        {
            "call_mode": "openai",
            "openai_api_base_url": "https://compatible.example/v1",
            "openai_api_key": "openai-key",
            "novelai_service": service,
            "starbride_api_key": "starbride-key",
            "novelai_official_api_key": "official-key",
        }
    )
    plugin._session = Session()
    images, reason = asyncio.run(
        plugin._openai_generate("apple", "832x1216", transport_mode="novelai")
    )
    assert images and reason == "ok"
    call = plugin._session.calls[0]
    assert call["endpoint"] == expected + "/ai/generate-image"
    assert call["headers"]["Authorization"] == "Bearer " + service + "-key"
    assert plugin.call_mode == "openai"
    assert plugin.openai_api_key == "openai-key"


def test_official_zip_returns_all_images_and_ignores_metadata(load_plugin):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("image_0.png", png())
        archive.writestr("metadata.json", "{}")
        archive.writestr("image_1.png", png((8, 16), "blue"))
    plugin, _ = load_plugin(
        {
            "call_mode": "novelai",
            "novelai_service": "official",
            "novelai_official_api_key": "official-key",
        }
    )
    plugin._session = Session(lambda *_: Response(status=200, raw=output.getvalue()))
    images, reason = asyncio.run(plugin._openai_generate("apple", "832x1216", n=2))
    assert reason == "ok" and images == [png(), png((8, 16), "blue")]
    assert (
        plugin._archive_generated_image.await_args.args[1]["api_profile"] == "official"
    )


def test_native_sizes_match_values_and_labels_without_legacy_tiers():
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    sizes = schema["novelai_image_size"]
    assert list(zip(sizes["options"], sizes["labels"])) == list(NATIVE_SIZE_OPTIONS)
    assert len(NATIVE_SIZE_OPTIONS) == 11
    for value, label in NATIVE_SIZE_OPTIONS:
        width, height = map(int, value.split("x"))
        assert width % 64 == height % 64 == 0
        assert str(width) in label and str(height) in label
        assert not any(tier in label for tier in ("2K", "4K"))


def test_native_credentials_do_not_fall_back_to_other_accounts():
    for service in ("official", "starbride"):
        config = dict(novelai_service=service, openai_api_key="openai-key")
        other = "starbride" if service == "official" else "novelai_official"
        config[other + "_api_key"] = "other-key"
        assert api_settings(config, "novelai")["key"] == ""
