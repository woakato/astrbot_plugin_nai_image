"""Selected-service credentials and transport-independent companion operations."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from test_api_credentials import load_plugin
from test_starbridge import Session as RequestSession, png
from astrbot_plugin_nai_image.companion_api import NAIImageCompanionExtensionAPI


class Session(RequestSession):
    closed = False


def settings(mode, service="starbride"):
    return {
        "call_mode": mode,
        "novelai_service": service,
        "_novelai_config_version": 1,
        "openai_api_base_url": "https://compatible.example/v1",
        "openai_api_key": "compatible-secret",
        "starbride_api_key": "starbride-secret",
        "novelai_official_api_key": "official-secret",
        "image_gen_key": "unused-legacy-secret",
    }


@pytest.mark.parametrize(
    "mode,service",
    [
        ("direct", "starbride"),
        ("openai", "starbride"),
        ("novelai", "starbride"),
        ("novelai", "official"),
    ],
)
def test_ready_operations_preserve_actual_transport_without_credentials(
    load_plugin, mode, service
):
    plugin, _ = load_plugin(settings(mode, service))
    plugin._session = Session()
    api = NAIImageCompanionExtensionAPI(plugin)
    status = api.status()
    capability = api.capability_status(None)
    expected = ["generate"] if mode == "direct" else ["generate", "edit"]
    assert status["call_mode"] == mode
    assert status["credential_configured"] is True
    assert status["operations"] == capability["operations"] == expected
    assert capability["available"] is True
    encoded = json.dumps([status, capability])
    assert all(
        secret not in encoded
        for secret in (
            "compatible-secret",
            "starbride-secret",
            "official-secret",
            "unused-legacy-secret",
        )
    )


@pytest.mark.parametrize(
    "mode,service,key",
    [
        ("openai", "starbride", "openai_api_key"),
        ("novelai", "starbride", "starbride_api_key"),
        ("novelai", "official", "novelai_official_api_key"),
    ],
)
@pytest.mark.parametrize("missing", ["", "   "])
def test_missing_selected_service_key_cannot_use_another_services_key(
    load_plugin, mode, service, key, missing
):
    config = settings(mode, service)
    config[key] = missing
    plugin, _ = load_plugin(config)
    plugin._session = Session()
    api = NAIImageCompanionExtensionAPI(plugin)
    status, capability = api.status(), api.capability_status(None)
    assert status["credential_configured"] is False
    assert status["credential_reason"] == capability["reason"] == mode + "_key_missing"
    assert capability["available"] is False
    assert capability["backends"]["nai"] is capability["backends"]["external"] is False
    assert status["operations"] == capability["operations"] == []


@pytest.mark.parametrize(
    "state,reason",
    [("none", "no_session"), ("closed", "no_session"), ("disabled", "disabled")],
)
def test_unavailable_session_or_disabled_link_never_advertises_operations(
    load_plugin, state, reason
):
    plugin, _ = load_plugin(settings("novelai"))
    plugin._session = Session()
    if state == "none":
        plugin._session = None
    elif state == "closed":
        plugin._session.closed = True
    else:
        plugin.enable_companion_link = False
    api = NAIImageCompanionExtensionAPI(plugin)
    capability = api.capability_status(None)
    assert capability["available"] is False and capability["reason"] == reason
    assert api.status()["operations"] == capability["operations"] == []


def test_novelai_companion_edit_reaches_native_img2img_with_reference(
    load_plugin, tmp_path
):
    config = settings("novelai")
    config["novelai_reference_mode"] = "img2img"
    plugin, _ = load_plugin(config)
    plugin._session = Session()
    api = NAIImageCompanionExtensionAPI(plugin)
    reference = tmp_path / "reference.png"
    reference.write_bytes(png())
    api._save_companion_image = AsyncMock(return_value=Path("synthetic-output.png"))
    result = asyncio.run(
        api.generate_for_companion(
            None,
            {
                "workflow_kind": "edit",
                "prompt_text": "a girl at the beach",
                "reference_image_paths": [str(reference)],
                "size": "832x1216",
            },
        )
    )
    assert result["handled"] is True and result["image_path"] == "synthetic-output.png"
    request = plugin._session.calls[0]
    assert request["endpoint"].endswith("/ai/generate-image")
    assert request["body"]["action"] == "img2img"
    assert request["body"]["parameters"]["image"]
    assert plugin.call_mode == "novelai"
