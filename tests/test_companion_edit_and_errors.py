"""Unit tests for companion edit img2img routing, subject/style contracts, and structured errors."""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_PARENT = _REPO_ROOT.parent
for _entry in (str(_REPO_ROOT), str(_REPO_PARENT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
if (
    "astrbot_plugin_nai_image" not in sys.modules
    and _REPO_ROOT.name != "astrbot_plugin_nai_image"
):
    _pkg = types.ModuleType("astrbot_plugin_nai_image")
    _pkg.__path__ = [str(_REPO_ROOT)]
    _pkg.__file__ = str(_REPO_ROOT / "__init__.py")
    sys.modules["astrbot_plugin_nai_image"] = _pkg

from astrbot_plugin_nai_image.companion_api import NAIImageCompanionExtensionAPI
from main import NAIGenerateImagePlugin


def make_png(size: tuple[int, int] = (16, 16), color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class FakeResponse:
    def __init__(self, body=None, *, status: int = 201, raw: bytes | None = None):
        self.status = status
        self._raw = raw if raw is not None else json.dumps(body).encode("utf-8")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self) -> str:
        return self._raw.decode("utf-8")

    async def read(self) -> bytes:
        return self._raw


class FakeSession:
    closed = False

    def __init__(self, handler=None):
        self.calls: list[dict] = []
        self.handler = handler

    def post(self, endpoint, *, json, **kwargs):
        self.calls.append(
            {"endpoint": endpoint, "body": copy.deepcopy(json), **kwargs}
        )
        if self.handler is not None:
            return self.handler(endpoint, json)
        return FakeResponse(
            {"images": [{"image": base64.b64encode(make_png()).decode("ascii")}]},
            status=201,
        )


def make_openai_plugin(
    *,
    model: str = "nai-diffusion-5-curated",
    reference_mode: str = "director",
    custom_artists: str = "1.3::artist:nakamura takeshi, 1.1::white album 2::, visual novel cg",
    fallback_images: list[str] | None = None,
    enable_artist_slots: bool = False,
    api_key: str = "synthetic-secret-key",
    handler=None,
) -> NAIGenerateImagePlugin:
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.call_mode = "openai"
    plugin.enable_companion_link = True
    plugin.companion_prompt_format = "nai tag模式"
    plugin.companion_image_retention_days = 30
    plugin.enable_proxy = False
    plugin.bypass_system_proxy = True
    plugin.proxy_runner = None
    plugin.proxy_port = 8765
    plugin.base_url = "https://nai.sta1n.cn"
    plugin.image_gen_key = ""
    plugin.model = "nai-diffusion-4-5-full"
    plugin.image_style = "custom"
    plugin.image_size = "竖图"
    plugin.custom_artists = custom_artists
    plugin.enable_artist_slots = enable_artist_slots
    plugin.active_artist_slot = "1"
    plugin._slots_cache = None
    plugin._slots_cache_mtime = 0.0
    plugin.enable_template = True
    plugin.character_preset = "1girl, solo, preset_character"
    plugin.translate_mode = "关闭"
    plugin.enable_translate = False
    plugin.default_outfit = "default_uniform"
    plugin.enable_outfit_cache = False
    plugin.outfit_cache_ttl_seconds = 0
    plugin.outfit_cache_text = None
    plugin.outfit_cache_expires_at = None
    plugin.enable_grimoire = False
    plugin.openai_active_channel = "primary"
    plugin.openai_enable_fallback = True
    plugin.openai_api_name = "星桥"
    plugin.openai_primary_base_url = "https://api.starbridge.zone"
    plugin.openai_primary_key = api_key
    plugin.openai_backup_name = ""
    plugin.openai_backup_base_url = ""
    plugin.openai_backup_key = ""
    plugin._refresh_active_openai_attributes()
    plugin.openai_api_model = model
    plugin.openai_reference_mode = reference_mode
    plugin.openai_director_caption = "character&style"
    plugin.openai_vibe_strength = 0.6
    plugin.openai_director_strength = 0.75
    plugin.openai_director_secondary_strength = 0.45
    plugin.openai_director_fallback_images = list(fallback_images or [])
    plugin.openai_seed = -1
    plugin.request_timeout = 60
    plugin.openai_max_retries = 0
    plugin.steps = 28
    plugin.scale = 5.5
    plugin.sampler = "k_euler_ancestral"
    plugin.noise_schedule = "karras"
    plugin.negative = "lowres"
    plugin.save_image_history = False
    plugin.save_generation_parameters = False
    plugin.enable_usage_stats = False
    plugin._session = FakeSession(handler)
    plugin._archive_generated_image = AsyncMock()
    plugin._record_usage_stat = AsyncMock()
    return plugin


# ---------------------------------------------------------------------------
# 1. Pure edit_source img2img routing + V5 retention + aspect-ratio sizing
# ---------------------------------------------------------------------------
def test_pure_edit_source_routes_to_v5_img2img_matches_ratio_and_skips_style(tmp_path):
    source_path = tmp_path / "house_drawing.png"
    source_path.write_bytes(make_png((1200, 900), "skyblue"))
    fallback_path = tmp_path / "character_portrait.png"
    fallback_path.write_bytes(make_png((832, 1216), "pink"))

    plugin = make_openai_plugin(
        model="nai-diffusion-5-curated",
        reference_mode="director",
        fallback_images=[str(fallback_path)],
    )
    api = NAIImageCompanionExtensionAPI(plugin)
    api._save_companion_image = AsyncMock(return_value=tmp_path / "out.png")

    result = asyncio.run(
        api.generate_for_companion(
            None,
            {
                "workflow_kind": "edit",
                "prompt_text": "simple flat 2d illustration, warm sunset sky, no humans",
                "negative": "1girl, humans, people",
                "reference_roles": ["edit_source"],
                "reference_image_paths": [str(source_path)],
                "character_source": "companion",
                "subject_mode": "edit_preserve",
                "preserve_source_style": True,
                "allow_fallback_references": False,
                "strength": 0.65,
            },
        )
    )

    assert result["handled"] is True
    assert result["image_path"] == str(tmp_path / "out.png")
    assert len(plugin._session.calls) == 1
    body = plugin._session.calls[0]["body"]
    assert body["action"] == "img2img"
    assert body["model"] == "nai-diffusion-5-curated"
    # Landscape 4:3 (1200x900) matches 64-multiple landscape (not 832x1216 portrait!)
    params = body["parameters"]
    assert params["width"] > params["height"]
    assert params["width"] % 64 == 0 and params["height"] % 64 == 0
    assert params["width"] * params["height"] <= 1_048_576
    assert params["strength"] == 0.65
    assert "image" in params
    assert "director_reference_images" not in params
    # Work/character-biased artist prefix and standalone character preset must be skipped.
    assert "white album 2" not in body["input"]
    assert "nakamura takeshi" not in body["input"]
    assert "preset_character" not in body["input"]
    assert "simple flat 2d illustration" in body["input"]


def test_legacy_companion_edit_source_auto_routes_to_img2img_without_new_fields(tmp_path):
    """Older companion releases only pass workflow_kind='edit' + reference_roles=['edit_source']."""
    source_path = tmp_path / "landscape.png"
    source_path.write_bytes(make_png((1920, 1080), "green"))

    plugin = make_openai_plugin(
        model="nai-diffusion-5-curated",
        reference_mode="director",
    )
    api = NAIImageCompanionExtensionAPI(plugin)
    api._save_companion_image = AsyncMock(return_value=tmp_path / "out.png")

    result = asyncio.run(
        api.generate_for_companion(
            None,
            {
                "workflow_kind": "edit",
                "prompt_text": "sunset sky, no humans",
                "reference_roles": ["edit_source"],
                "reference_image_paths": [str(source_path)],
                "character_source": "companion",
            },
        )
    )

    assert result["handled"] is True and result["image_path"]
    body = plugin._session.calls[0]["body"]
    assert body["action"] == "img2img"
    assert body["model"] == "nai-diffusion-5-curated"
    params = body["parameters"]
    assert (params["width"], params["height"]) == (1344, 768)


def test_identity_reference_keeps_configured_director_mode(tmp_path):
    ref_path = tmp_path / "char_ref.png"
    ref_path.write_bytes(make_png((832, 1216), "blue"))

    plugin = make_openai_plugin(
        model="nai-diffusion-5-curated",
        reference_mode="director",
    )
    api = NAIImageCompanionExtensionAPI(plugin)
    api._save_companion_image = AsyncMock(return_value=tmp_path / "out.png")

    result = asyncio.run(
        api.generate_for_companion(
            None,
            {
                "workflow_kind": "edit",
                "prompt_text": "1girl, smiling at cafe",
                "reference_roles": ["identity"],
                "reference_image_paths": [str(ref_path)],
                "character_source": "companion",
                "subject_mode": "character",
            },
        )
    )

    assert result["handled"] is True and result["image_path"]
    body = plugin._session.calls[0]["body"]
    assert body["action"] == "generate"
    assert body["model"] == "nai-diffusion-5-curated"
    assert len(body["parameters"]["director_reference_images"]) == 1
    assert "white album 2" in body["input"]


# ---------------------------------------------------------------------------
# 2. subject_mode / allow_fallback_references / preserve_source_style contracts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "extra_req",
    [
        {"allow_fallback_references": False},
        {"subject_mode": "scene_only"},
        {"subject_mode": "edit_preserve"},
    ],
)
def test_non_character_or_disallowed_fallback_blocks_director_fallback_images(
    tmp_path, extra_req
):
    fallback_path = tmp_path / "character_portrait.png"
    fallback_path.write_bytes(make_png((832, 1216), "pink"))

    plugin = make_openai_plugin(
        model="nai-diffusion-5-curated",
        reference_mode="director",
        fallback_images=[str(fallback_path)],
    )
    api = NAIImageCompanionExtensionAPI(plugin)
    api._save_companion_image = AsyncMock(return_value=tmp_path / "out.png")

    result = asyncio.run(
        api.generate_for_companion(
            None,
            {
                "workflow_kind": "generate",
                "prompt_text": "scenery, mountain lake, sunrise, no humans",
                "character_source": "companion",
                **extra_req,
            },
        )
    )

    assert result["handled"] is True and result["image_path"]
    params = plugin._session.calls[0]["body"]["parameters"]
    assert "director_reference_images" not in params


def test_character_mode_still_uses_configured_director_fallback_images(tmp_path):
    fallback_path = tmp_path / "character_portrait.png"
    fallback_path.write_bytes(make_png((832, 1216), "pink"))

    plugin = make_openai_plugin(
        model="nai-diffusion-5-curated",
        reference_mode="director",
        fallback_images=[str(fallback_path)],
    )
    api = NAIImageCompanionExtensionAPI(plugin)
    api._save_companion_image = AsyncMock(return_value=tmp_path / "out.png")

    result = asyncio.run(
        api.generate_for_companion(
            None,
            {
                "workflow_kind": "generate",
                "prompt_text": "1girl, coffee shop window",
                "character_source": "companion",
                "subject_mode": "character",
            },
        )
    )

    assert result["handled"] is True and result["image_path"]
    params = plugin._session.calls[0]["body"]["parameters"]
    assert len(params["director_reference_images"]) == 1


def test_preserve_source_style_also_skips_active_artist_slot(tmp_path, monkeypatch):
    plugin = make_openai_plugin(
        model="nai-diffusion-5-curated",
        enable_artist_slots=True,
    )
    monkeypatch.setattr(
        plugin,
        "_active_artist_slot",
        lambda: {
            "id": 1,
            "name": "槽位1",
            "model": "nai-diffusion-5-curated",
            "artists": "artist:slot_biased, white album 2",
        },
    )
    monkeypatch.setattr(plugin, "_slot_model", lambda: "nai-diffusion-5-curated")
    api = NAIImageCompanionExtensionAPI(plugin)
    api._save_companion_image = AsyncMock(return_value=tmp_path / "out.png")

    result = asyncio.run(
        api.generate_for_companion(
            None,
            {
                "workflow_kind": "generate",
                "prompt_text": "flat vector icon, red apple",
                "character_source": "companion",
                "subject_mode": "scene_only",
                "preserve_source_style": True,
            },
        )
    )

    assert result["handled"] is True and result["image_path"]
    sent_prompt = plugin._session.calls[0]["body"]["input"]
    assert "slot_biased" not in sent_prompt
    assert "white album 2" not in sent_prompt
    assert sent_prompt == "flat vector icon, red apple"


# ---------------------------------------------------------------------------
# 3. Aspect-ratio 64-multiple matcher
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("src_w", "src_h", "expected"),
    [
        (832, 1216, "832x1216"),
        (1216, 832, "1216x832"),
        (1024, 1024, "1024x1024"),
        (768, 512, "768x512"),
        (1920, 1080, "1344x768"),
        (1080, 1920, "768x1344"),
        (500, 500, "1024x1024"),
        (4000, 3000, "1088x832"),
    ],
)
def test_match_edit_source_dimensions_preserves_ratio_and_limits(src_w, src_h, expected):
    matched = NAIImageCompanionExtensionAPI._match_edit_source_dimensions(src_w, src_h)
    assert matched == expected
    w, h = (int(x) for x in matched.split("x"))
    assert w >= 64 and h >= 64 and w % 64 == 0 and h % 64 == 0
    assert max(w, h) <= 1920 and w * h <= 1_048_576


def test_resolve_edit_source_size_respects_exif_orientation(tmp_path):
    img = Image.new("RGB", (1920, 1080), "red")
    exif = img.getexif()
    exif[274] = 6  # Rotate 90 CW -> displayed as 1080x1920 portrait
    path = tmp_path / "rotated.jpg"
    img.save(path, format="JPEG", exif=exif.tobytes())

    assert NAIImageCompanionExtensionAPI._resolve_edit_source_size(str(path)) == "768x1344"


# ---------------------------------------------------------------------------
# 4. Status & capability_status (operations, credential_reason, key check)
# ---------------------------------------------------------------------------
def test_status_and_capability_expose_operations_and_validate_openai_key():
    plugin = make_openai_plugin(api_key="super-secret-key")
    api = NAIImageCompanionExtensionAPI(plugin)

    status = api.status()
    cap = api.capability_status(None)
    assert status["credential_configured"] is True
    assert status["credential_reason"] == "ready"
    assert status["operations"] == cap["operations"] == ["generate", "edit"]
    assert cap["available"] is True and cap["reason"] == "ready"
    assert "super-secret-key" not in json.dumps([status, cap])

    # Missing API key must report openai_key_missing instead of ready.
    plugin_no_key = make_openai_plugin(api_key="")
    api_no_key = NAIImageCompanionExtensionAPI(plugin_no_key)
    st_nk = api_no_key.status()
    cap_nk = api_no_key.capability_status(None)
    assert st_nk["credential_configured"] is False
    assert st_nk["credential_reason"] == cap_nk["reason"] == "openai_key_missing"
    assert cap_nk["available"] is False
    assert st_nk["operations"] == cap_nk["operations"] == []

    # Dual channel: primary missing key, backup has URL+key -> still ready via fallback.
    plugin_no_key.openai_backup_base_url = "https://api.almoons.xyz"
    plugin_no_key.openai_backup_key = "backup-secret"
    plugin_no_key._refresh_active_openai_attributes()
    cap_fb = api_no_key.capability_status(None)
    assert cap_fb["available"] is True
    assert cap_fb["operations"] == ["generate", "edit"]
    assert "backup-secret" not in json.dumps([api_no_key.status(), cap_fb])

    # Direct mode advertises only generate when token is present.
    plugin_direct = make_openai_plugin()
    plugin_direct.call_mode = "direct"
    plugin_direct.image_gen_key = "direct-token"
    api_direct = NAIImageCompanionExtensionAPI(plugin_direct)
    assert api_direct.status()["operations"] == ["generate"]
    assert api_direct.capability_status(None)["operations"] == ["generate"]

    # Disabled or closed session never advertises operations.
    plugin_direct.enable_companion_link = False
    assert api_direct.capability_status(None)["operations"] == []
    assert api_direct.status()["operations"] == []


# ---------------------------------------------------------------------------
# 5. Structured failure responses (reason, http_status, retryable)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status_code", "body", "expected_status", "expected_retryable"),
    [
        (502, b"<html>502 Bad Gateway</html>", 502, True),
        (503, {"message": "service busy"}, 503, True),
        (504, {"message": "gateway timeout"}, 504, True),
        (429, {"message": "rate limited"}, 429, True),
        (400, {"message": "invalid image size"}, 400, False),
        (401, {"message": "invalid api key"}, 401, False),
        (422, {"message": "bad payload"}, 422, False),
    ],
)
def test_companion_failure_exposes_structured_reason_status_and_retryable(
    status_code, body, expected_status, expected_retryable
):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    plugin = make_openai_plugin(
        handler=lambda _ep, _payload: FakeResponse(raw=raw, status=status_code)
    )
    api = NAIImageCompanionExtensionAPI(plugin)

    result = asyncio.run(
        api.generate_for_companion(None, {"prompt_text": "1girl, sunset"})
    )

    assert result["handled"] is True
    assert result["image_path"] == ""
    assert result["reason"].startswith(f"http_{expected_status}")
    assert result["http_status"] == expected_status
    assert result["retryable"] is expected_retryable
    assert result["note"]


def test_companion_timeout_and_partial_billing_errors_are_not_retryable():
    def raise_timeout(_ep, _payload):
        raise asyncio.TimeoutError()

    plugin = make_openai_plugin(handler=raise_timeout)
    api = NAIImageCompanionExtensionAPI(plugin)
    timeout_res = asyncio.run(api.generate_for_companion(None, {"prompt_text": "1girl"}))
    assert timeout_res["reason"] == "timeout"
    assert timeout_res["http_status"] == 0
    assert timeout_res["retryable"] is False

    # Partial vibe billing failure must never be marked retryable even with HTTP 503.
    reason, status, retryable = NAIImageCompanionExtensionAPI._classify_generation_error(
        "starbridge | Vibe 编码失败（此前 1 张编码已完成并可能计费），未提交生图：HTTP 503"
    )
    assert status == 503
    assert retryable is False
    assert "可能计费" in reason


def test_companion_empty_prompt_and_save_error_structured_fields():
    plugin = make_openai_plugin()
    api = NAIImageCompanionExtensionAPI(plugin)

    empty_res = asyncio.run(api.generate_for_companion(None, {"prompt_text": "   "}))
    assert empty_res["reason"] == "empty_prompt"
    assert empty_res["http_status"] == 400
    assert empty_res["retryable"] is False

    api._save_companion_image = AsyncMock(side_effect=OSError("disk full"))
    save_res = asyncio.run(api.generate_for_companion(None, {"prompt_text": "1girl"}))
    assert save_res["reason"] == "save_failed"
    assert save_res["http_status"] == 0
    assert save_res["retryable"] is False


@pytest.mark.parametrize(
    ("explicit_size", "expected_w", "expected_h"),
    [
        ("1920x1080", 1344, 768),
        ("1200x900", 1088, 832),
        ("4096x4096", 1024, 1024),
        ("1536x2048", 832, 1088),
        ("1024x1536", 1024, 1536),
        ("1024x1024", 1024, 1024),
        ("0x0", 832, 1216),
    ],
)
def test_pure_edit_explicit_size_coerces_non_64_multiples_and_invalid_dimensions(
    tmp_path, explicit_size, expected_w, expected_h
):
    src = tmp_path / "source.png"
    src.write_bytes(make_png((1200, 800)))
    plugin = make_openai_plugin()
    api = NAIImageCompanionExtensionAPI(plugin)
    api._save_companion_image = AsyncMock(return_value=tmp_path / "out.png")

    res = asyncio.run(
        api.generate_for_companion(
            None,
            {
                "workflow_kind": "edit",
                "prompt_text": "add soft sunset lighting",
                "reference_image_paths": [str(src)],
                "reference_roles": ["edit_source"],
                "size": explicit_size,
            },
        )
    )

    assert res["handled"] is True
    assert res["image_path"] == str(tmp_path / "out.png")
    params = plugin._session.calls[0]["body"]["parameters"]
    assert params["width"] == expected_w
    assert params["height"] == expected_h
    assert params["width"] % 64 == 0 and params["height"] % 64 == 0
    assert params["width"] * params["height"] <= 3_145_728


@pytest.mark.parametrize(
    ("ratio", "expected_w", "expected_h"),
    [
        ("1:1", 1024, 1024),
        ("2:3", 832, 1216),
        ("3:2", 1216, 832),
        ("3:4", 896, 1152),
        ("4:3", 1152, 896),
        ("9:16", 768, 1344),
        ("16:9", 1344, 768),
    ],
)
def test_companion_openai_ratio_maps_to_exact_1mp_64_multiple_resolution(
    tmp_path, ratio, expected_w, expected_h
):
    plugin = make_openai_plugin()
    api = NAIImageCompanionExtensionAPI(plugin)
    api._save_companion_image = AsyncMock(return_value=tmp_path / "out.png")

    res = asyncio.run(
        api.generate_for_companion(
            None,
            {
                "workflow_kind": "generate",
                "prompt_text": "1girl, landscape cafe",
                "ratio": ratio,
                "character_source": "companion",
            },
        )
    )

    assert res["handled"] is True
    params = plugin._session.calls[0]["body"]["parameters"]
    assert (params["width"], params["height"]) == (expected_w, expected_h)
    assert params["width"] % 64 == 0 and params["height"] % 64 == 0
    assert params["width"] * params["height"] <= 1_048_576
    assert res["metadata"]["size"] == f"{expected_w}x{expected_h}"


def test_companion_ratio_preserves_direct_tiers_and_edit_override(tmp_path):
    plugin_direct = make_openai_plugin()
    plugin_direct.call_mode = "direct"
    api_direct = NAIImageCompanionExtensionAPI(plugin_direct)
    assert api_direct._coerce_size("16:9") == "横图"
    assert api_direct._coerce_size("9:16") == "竖图"
    assert api_direct._coerce_size("1:1") == "方图"
    assert api_direct._coerce_size("16:9", openai_mode=True) == "1344x768"

    # Explicit ratio on pure edit_source overrides source aspect ratio cleanly.
    src = tmp_path / "portrait.png"
    src.write_bytes(make_png((832, 1216)))
    plugin_openai = make_openai_plugin()
    api_openai = NAIImageCompanionExtensionAPI(plugin_openai)
    api_openai._save_companion_image = AsyncMock(return_value=tmp_path / "edit_out.png")
    res = asyncio.run(
        api_openai.generate_for_companion(
            None,
            {
                "workflow_kind": "edit",
                "prompt_text": "extend into wide scene",
                "reference_image_paths": [str(src)],
                "reference_roles": ["edit_source"],
                "ratio": "3:2",
            },
        )
    )
    assert res["handled"] is True
    params = plugin_openai._session.calls[0]["body"]["parameters"]
    assert (params["width"], params["height"]) == (1216, 832)


def test_nai_generate_image_tool_attaches_zero_coupling_outbound_metadata():
    class FakeEvent:
        def __init__(self):
            self.extras = {}
            self.sent = []

        def get_extra(self, key, default=None):
            return self.extras.get(key, default)

        def set_extra(self, key, value):
            self.extras[key] = value

        async def send(self, chain):
            self.sent.append(chain)

    plugin = make_openai_plugin(model="nai-diffusion-5-curated")
    plugin.enable_llm_tool = True
    plugin.enable_tool_style = True
    plugin.enable_character_blocks = False
    event = FakeEvent()

    async def _run():
        outputs = []
        async for chunk in plugin.NAI_Generate_Image(
            event, prompt="1girl, solo, landscape park", style="custom", size_cn="1216x832"
        ):
            outputs.append(chunk)
        return outputs

    outputs = asyncio.run(_run())
    assert len(event.sent) == 1
    assert any("图片已生成并发送给用户" in item for item in outputs)
    meta = getattr(event.sent[0], "_companion_generated_image_meta", None)
    assert isinstance(meta, dict)
    assert meta["source_plugin"] == "astrbot_plugin_nai_image"
    assert meta["tool_name"] == "NAI_Generate_Image"
    assert meta["action"] == "image.generate"
    assert meta["model"] == "nai-diffusion-5-curated"
    assert meta["size"] == "1216x832"
    assert "landscape park" in meta["prompt"]

