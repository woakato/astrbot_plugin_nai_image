"""Native NovelAI protocol multi-reference (vibe / director / img2img) unit tests."""

import asyncio
import base64
import io
import json

import yaml
from PIL import Image

from main import OPENAI_MAX_REFERENCE_IMAGES, NAIGenerateImagePlugin


def make_png_bytes(size=(16, 16), color="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


PNG_1PX = make_png_bytes()


class FakeSession:
    """Capture native /ai/ post calls without touching the network."""

    closed = False

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def post(self, endpoint, json=None, headers=None, timeout=None, **kwargs):
        self.calls.append({"endpoint": endpoint, "payload": json, **kwargs})
        if self.handler is not None:
            return self.handler(endpoint, json)

        if endpoint.endswith("/ai/encode-vibe"):
            raw = b"encoded-vibe-data"
            status = 201
        else:
            raw = json_response_bytes(PNG_1PX)
            status = 201

        class _Response:
            def __init__(self, resp_status, resp_raw):
                self.status = resp_status
                self._raw = resp_raw

            async def text(self):
                return self._raw.decode("utf-8")

            async def read(self):
                return self._raw

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        return _Response(status, raw)


def json_response_bytes(img_bytes: bytes) -> bytes:
    return json.dumps(
        {"images": [{"image": base64.b64encode(img_bytes).decode()}]}
    ).encode("utf-8")


def make_plugin(
    reference_mode: str = "vibe", model: str = "nai-diffusion-4-5-full"
) -> NAIGenerateImagePlugin:
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.openai_api_base_url = "https://api.example.com"
    plugin.openai_api_key = "test-key"
    plugin.openai_api_model = model
    plugin.openai_reference_mode = reference_mode
    plugin.openai_director_caption = "character&style"
    plugin.openai_director_fallback_images = []
    plugin.openai_vibe_strength = 0.6
    plugin.openai_director_strength = 1.0
    plugin.openai_director_secondary_strength = 0.5
    plugin.openai_seed = -1
    plugin.request_timeout = 60
    plugin.openai_max_retries = 0
    plugin.steps = 24
    plugin.scale = 6
    plugin.sampler = "k_euler_ancestral"
    plugin.noise_schedule = "karras"
    plugin.negative = ""
    plugin.save_image_history = False
    plugin.save_generation_parameters = False
    plugin.enable_usage_stats = False
    plugin._session = FakeSession()
    return plugin


def last_payload(plugin: NAIGenerateImagePlugin) -> dict:
    call = plugin._session.calls[-1]
    assert call["endpoint"].endswith("/ai/generate-image"), call["endpoint"]
    return call["payload"]


def test_vibe_multi_image_defaults():
    plugin = make_plugin(model="nai-diffusion-4-5-full")
    images, reason = asyncio.run(
        plugin._openai_generate(
            "a girl at the seaside",
            "1024x1024",
            n=1,
            reference_image_bytes_list=[PNG_1PX, PNG_1PX],
            reference_mode="vibe",
        )
    )
    assert reason == "ok" and images
    params = last_payload(plugin)["parameters"]
    assert len(params["reference_image_multiple"]) == 2
    assert params["reference_strength_multiple"] == [0.6, 0.6]
    assert params["reference_information_extracted_multiple"] == [0.7, 0.7]


def test_vibe_per_image_strengths_fill_from_scalar():
    plugin = make_plugin(model="nai-diffusion-4-5-full")
    _, reason = asyncio.run(
        plugin._openai_generate(
            "a girl at the seaside",
            "1024x1024",
            n=1,
            reference_image_bytes_list=[PNG_1PX, PNG_1PX],
            reference_mode="vibe",
            strength=0.4,
            reference_strengths=[0.9],
        )
    )
    assert reason == "ok"
    params = last_payload(plugin)["parameters"]
    assert params["reference_strength_multiple"] == [0.9, 0.4]


def test_director_multi_image_parallel_arrays_keeps_v5_model():
    plugin = make_plugin(model="nai-diffusion-5-full")
    _, reason = asyncio.run(
        plugin._openai_generate(
            "same girl, sunset beach",
            "1024x1024",
            n=1,
            reference_image_bytes_list=[PNG_1PX, PNG_1PX],
            reference_mode="director",
            reference_strengths=[1.0, 2.5],
            director_captions=["character", "not-a-caption"],
        )
    )
    assert reason == "ok"
    payload = last_payload(plugin)
    assert payload["model"] == "nai-diffusion-5-full"
    params = payload["parameters"]
    assert len(params["director_reference_images"]) == 2
    assert params["director_reference_strength_values"] == [1.0, 2.5]
    assert params["director_reference_secondary_strength_values"] == [0, 0]
    assert params["director_reference_information_extracted"] == [1, 1]
    descriptions = params["director_reference_descriptions"]
    assert [item["caption"]["base_caption"] for item in descriptions] == [
        "character",
        "character&style",
    ]
    assert all(item["caption"]["char_captions"] == [] for item in descriptions)


def test_director_unsupported_model_is_rejected_without_silent_fallback():
    plugin = make_plugin(model="nai-diffusion-3")
    images, reason = asyncio.run(
        plugin._openai_generate(
            "same girl",
            "1024x1024",
            n=1,
            reference_image_bytes_list=[PNG_1PX],
            reference_mode="director",
        )
    )
    assert images == [] and reason.startswith("starbridge | ")
    assert plugin._session.calls == []


def test_director_without_reference_falls_back_to_text2img():
    plugin = make_plugin(reference_mode="director", model="nai-diffusion-5-full")
    images, reason = asyncio.run(
        plugin._openai_generate(
            "a girl at the seaside",
            "1024x1024",
            n=1,
            reference_mode="director",
        )
    )
    assert reason == "ok" and images
    params = last_payload(plugin)["parameters"]
    assert "director_reference_images" not in params
    assert "reference_image_multiple" not in params


def test_configured_director_weights_are_applied():
    plugin = make_plugin(reference_mode="director", model="nai-diffusion-4-5-full")
    plugin.openai_vibe_strength = 0.45
    plugin.openai_director_strength = 0.85
    plugin.openai_director_secondary_strength = 0.4
    _, reason = asyncio.run(
        plugin._openai_generate(
            "same girl",
            "1024x1024",
            n=1,
            reference_image_bytes_list=[PNG_1PX, PNG_1PX],
            reference_mode="director",
        )
    )
    assert reason == "ok"
    params = last_payload(plugin)["parameters"]
    assert params["director_reference_strength_values"] == [0.85, 0.85]
    assert params["director_reference_secondary_strength_values"] == [0.4, 0.4]


def test_v5_director_enforces_fixed_secondary_and_allows_strength_up_to_5():
    plugin = make_plugin(reference_mode="director", model="nai-diffusion-5-full")
    plugin.openai_director_strength = 3.5
    plugin.openai_director_secondary_strength = 0.4
    _, reason = asyncio.run(
        plugin._openai_generate(
            "same girl",
            "1024x1024",
            n=1,
            reference_image_bytes_list=[PNG_1PX, PNG_1PX],
            reference_mode="director",
            reference_strengths=[2.2, 5.0],
        )
    )
    assert reason == "ok"
    params = last_payload(plugin)["parameters"]
    assert params["director_reference_strength_values"] == [2.2, 5.0]
    assert params["director_reference_secondary_strength_values"] == [0, 0]
    assert params["director_reference_information_extracted"] == [1, 1]


def test_v45_director_clamps_configured_strength_above_1():
    plugin = make_plugin(reference_mode="director", model="nai-diffusion-4-5-full")
    plugin.openai_director_strength = 3.5
    plugin.openai_director_secondary_strength = 0.4
    _, reason = asyncio.run(
        plugin._openai_generate(
            "same girl",
            "1024x1024",
            n=1,
            reference_image_bytes_list=[PNG_1PX],
            reference_mode="director",
        )
    )
    assert reason == "ok"
    params = last_payload(plugin)["parameters"]
    assert params["director_reference_strength_values"] == [1.0]
    assert params["director_reference_secondary_strength_values"] == [0.4]


def test_img2img_uses_only_first_reference():
    plugin = make_plugin(model="nai-diffusion-5-full")
    _, reason = asyncio.run(
        plugin._openai_generate(
            "same character, walking",
            "1024x1024",
            n=1,
            reference_image_bytes_list=[PNG_1PX, PNG_1PX],
            reference_mode="img2img",
            strength=0.7,
        )
    )
    assert reason == "ok"
    payload = last_payload(plugin)
    assert payload["action"] == "img2img"
    assert not payload["parameters"]["image"].startswith("data:")
    assert "reference_image_multiple" not in payload["parameters"]
    assert payload["parameters"]["strength"] == 0.7


def test_reference_list_truncated_to_document_limit():
    plugin = make_plugin(model="nai-diffusion-4-5-full")
    _, reason = asyncio.run(
        plugin._openai_generate(
            "style transfer",
            "1024x1024",
            n=1,
            reference_image_bytes_list=[PNG_1PX] * (OPENAI_MAX_REFERENCE_IMAGES + 2),
            reference_mode="vibe",
        )
    )
    assert reason == "ok"
    params = last_payload(plugin)["parameters"]
    assert len(params["reference_image_multiple"]) == OPENAI_MAX_REFERENCE_IMAGES


def test_openai_generate_archives_history_with_parameters(tmp_path):
    plugin = make_plugin(model="nai-diffusion-5-full")
    plugin.save_image_history = True
    plugin.save_generation_parameters = True
    plugin.image_history_limit = 0
    plugin._image_history_dir = tmp_path / "image_history"
    plugin._image_history_lock = asyncio.Lock()

    images, reason = asyncio.run(
        plugin._openai_generate("a girl at the seaside", "1024x1024", n=1)
    )

    assert reason == "ok" and len(images) == 1
    saved_images = list(plugin._image_history_dir.glob("nai_*.png"))
    saved_params = list(plugin._image_history_dir.glob("nai_*.yaml"))
    assert len(saved_images) == 1
    assert len(saved_params) == 1
    assert saved_images[0].read_bytes() == PNG_1PX
    parameters = yaml.safe_load(saved_params[0].read_text(encoding="utf-8"))
    assert parameters["tag"] == "a girl at the seaside"
    assert parameters["transport"] == "openai"
    assert parameters["api_profile"] == "starbridge"
    assert parameters["size"] == "1024x1024"
    assert parameters["model"] == "nai-diffusion-5-full"
    assert parameters["steps"] == 24


def test_prepare_openai_prompt_applies_template_and_custom_artist():
    plugin = make_plugin()
    plugin.translate_mode = "关闭"
    plugin.enable_translate = False
    plugin.enable_template = True
    plugin.character_preset = "masterpiece, best quality"
    plugin.default_outfit = ""

    full_prompt, outfit_source, prompt_kind = asyncio.run(
        plugin._prepare_openai_prompt(
            "1girl, solo",
            "custom",
            custom_artists="artist:foo",
            enable_template=True,
        )
    )

    assert full_prompt == "artist:foo, masterpiece, best quality, 1girl, solo"
    assert outfit_source == "none"
    assert prompt_kind == "off"


def test_openai_size_normalizes_english_tier_names():
    plugin = make_plugin()

    assert plugin._openai_size("portrait") == "832x1216"
    assert plugin._openai_size("2k_landscape") == "1920x1088"
    assert plugin._openai_size("4K竖图") == "1088x1920"


def test_openai_generate_accepts_native_images_payload():
    plugin = make_plugin()
    images, reason = asyncio.run(
        plugin._openai_generate("a girl at the seaside", "1024x1024", n=1)
    )
    assert reason == "ok"
    assert images == [PNG_1PX]
