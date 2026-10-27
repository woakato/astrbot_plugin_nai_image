"""Native Starbridge requests and isolation from the existing provider profile."""

import asyncio
import base64
import copy
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from main import NAIGenerateImagePlugin, _format_generate_error
from starbridge import StarbridgeError, native_endpoint, prepare_starbridge_request


def png(size=(16, 8), color="red"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


class Response:
    def __init__(self, body=None, *, status=201, raw=None):
        self.status = status
        self.raw = raw if raw is not None else json.dumps(body).encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self.raw.decode()

    async def read(self):
        return self.raw


class Session:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def post(self, endpoint, *, json, **kwargs):
        self.calls.append({"endpoint": endpoint, "body": copy.deepcopy(json), **kwargs})
        if self.handler:
            return self.handler(endpoint, json)
        image = base64.b64encode(png()).decode()
        if "/ai/" in endpoint:
            return Response({"images": [{"image": image, "index": 0}]})
        return Response({"data": [{"b64_json": image}]}, status=200)


def plugin(profile="starbridge", handler=None):
    value = object.__new__(NAIGenerateImagePlugin)
    value.openai_api_profile = profile
    value.openai_api_base_url = "https://api.almoons.xyz/v1"
    value.openai_api_key = "test-key"
    value.openai_api_model = "nai-diffusion-5-full"
    value.openai_reference_mode = "vibe"
    value.openai_director_caption = "character&style"
    value.openai_director_fallback_images = []
    value.openai_vibe_strength = 0.6
    value.openai_director_strength = 1
    value.openai_director_secondary_strength = 0.5
    value.openai_seed = -1
    value.request_timeout = 60
    value.openai_max_retries = 3
    value.steps = 24
    value.scale = 6
    value.sampler = "k_euler_ancestral"
    value.noise_schedule = "karras"
    value.negative = ""
    value._session = Session(handler)
    value._archive_generated_image = AsyncMock()
    value._record_usage_stat = AsyncMock()
    return value


def generate(value, **kwargs):
    return asyncio.run(value._openai_generate("a red apple", "1024x1024", **kwargs))


@pytest.mark.parametrize(
    "suffix", ["", "/", "/v1", "/v1/", "/v1/images/generations", "/v1/images/edits"]
)
def test_native_base_url_accepts_existing_configuration_styles(suffix):
    assert (
        native_endpoint("https://aiapi.almoons.xyz" + suffix, "generate-image")
        == "https://aiapi.almoons.xyz/ai/generate-image"
    )


@pytest.mark.parametrize(
    "base",
    [
        "https://user:password@example.com",
        "https://example.com?token=secret",
        "https://example.com#key",
        "file:///tmp/image",
    ],
)
def test_native_base_url_rejects_embedded_credentials_and_non_http_urls(base):
    with pytest.raises(StarbridgeError):
        native_endpoint(base, "generate-image")


def test_existing_configuration_defaults_to_compatible_profile():
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
    )
    assert schema["openai_api_profile"]["default"] == "compatible"
    value = plugin("compatible")
    del value.openai_api_profile
    images, reason = generate(value, n=6)
    assert images and reason == "ok"
    call = value._session.calls[0]
    assert call["endpoint"] == "https://api.almoons.xyz/v1/images/generations"
    assert call["body"]["action"] == "generate"
    assert call["body"]["n"] == 6
    assert "Accept" not in call["headers"]
    assert "allow_redirects" not in call


def test_native_generation_preserves_zero_values_roles_and_history():
    value = plugin()
    images, reason = generate(
        value,
        seed=0,
        scale=0,
        negative="blurry",
        characters=[("red dress", 0.2, 0.5), ("blue suit", 0.8, 0.5)],
    )
    assert images and reason == "ok"
    call = value._session.calls[0]
    assert call["endpoint"] == "https://api.almoons.xyz/ai/generate-image"
    assert call["headers"]["Accept"] == "application/json"
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["allow_redirects"] is False
    body = call["body"]
    assert set(body) == {"model", "input", "action", "parameters"}
    params = body["parameters"]
    assert params["seed"] == 0 and params["scale"] == 0
    assert params["n_samples"] == 1
    assert params["v4_prompt"]["use_coords"] is True
    assert params["v4_prompt"]["caption"]["char_captions"][1]["centers"] == [
        {"x": 0.8, "y": 0.5}
    ]
    assert params["v4_negative_prompt"]["caption"]["base_caption"] == "blurry"
    assert "characterPrompts" not in params
    value._archive_generated_image.assert_awaited_once()
    history = value._archive_generated_image.await_args.args[1]
    assert history["api_profile"] == "starbridge" and history["seed"] == 0
    assert "test-key" not in json.dumps(history)
    value._record_usage_stat.assert_awaited_once()


def test_native_image_edit_has_matching_pixels_and_no_data_uri():
    value = plugin()
    images, reason = generate(
        value,
        reference_image_bytes=png(),
        reference_mode="img2img",
        strength=0,
        noise=0,
    )
    assert images and reason == "ok"
    body = value._session.calls[0]["body"]
    assert body["action"] == "img2img"
    params = body["parameters"]
    assert params["strength"] == 0 and params["noise"] == 0
    assert not params["image"].startswith("data:")
    with Image.open(io.BytesIO(base64.b64decode(params["image"]))) as image:
        assert image.size == (1024, 1024)


def test_native_multiple_images_are_all_returned_and_archived():
    red, blue = png(), png(color="blue")
    value = plugin(
        handler=lambda endpoint, body: Response(
            {
                "images": [
                    {"image": base64.b64encode(red).decode()},
                    {"image": base64.b64encode(blue).decode()},
                ]
            }
        )
    )
    images, reason = generate(value, n=2)
    assert reason == "ok" and images == [red, blue]
    assert value._archive_generated_image.await_count == 2
    assert value._record_usage_stat.await_args.kwargs["count"] == 2


def test_precise_references_are_padded_and_ordered_for_v45():
    value = plugin()
    images, reason = generate(
        value,
        model="nai-diffusion-4-5-full",
        reference_mode="director",
        reference_image_bytes_list=[png(), png((8, 16), "blue")],
        reference_strengths=[0, 1],
        director_captions=["character", "style"],
    )
    assert images and reason == "ok"
    params = value._session.calls[0]["body"]["parameters"]
    assert params["director_reference_strength_values"] == [0, 1]
    assert params["director_reference_information_extracted"] == [1, 1]
    assert [
        item["caption"]["base_caption"]
        for item in params["director_reference_descriptions"]
    ] == ["character", "style"]
    for raw, size, color in zip(
        params["director_reference_images"],
        [(1536, 1024), (1024, 1536)],
        [(255, 0, 0), (0, 0, 255)],
    ):
        with Image.open(io.BytesIO(base64.b64decode(raw))) as image:
            assert image.size == size
            assert image.getpixel((size[0] // 2, size[1] // 2)) == color
            assert image.getpixel((0, 0)) == (0, 0, 0)


def test_vibe_encodes_each_reference_before_generation():
    def handler(endpoint, body):
        if endpoint.endswith("encode-vibe"):
            return Response(raw=b"encoded-feature", status=201)
        return Response({"images": [{"image": base64.b64encode(png()).decode()}]})

    value = plugin(handler=handler)
    images, reason = generate(
        value,
        model="nai-diffusion-4-5-curated",
        reference_image_bytes_list=[png(), png()],
        reference_strengths=[0, 0.9],
    )
    assert images and reason == "ok"
    assert [c["endpoint"].rsplit("/", 1)[1] for c in value._session.calls] == [
        "encode-vibe",
        "encode-vibe",
        "generate-image",
    ]
    first, _, last = value._session.calls
    assert first["headers"]["Accept"] == "application/binary"
    assert first["body"]["information_extracted"] == 0.7
    params = last["body"]["parameters"]
    assert params["reference_strength_multiple"] == [0, 0.9]
    assert (
        params["reference_image_multiple"]
        == [base64.b64encode(b"encoded-feature").decode()] * 2
    )


def test_malformed_later_reference_is_rejected_before_paid_encoding():
    value = plugin()
    images, reason = generate(
        value,
        model="nai-diffusion-4-5-full",
        reference_image_bytes_list=[png(), b"invalid image"],
    )
    assert images == [] and reason.startswith("starbridge | ")
    assert value._session.calls == []


@pytest.mark.parametrize("model", ["nai-diffusion-5-full", "nai-diffusion-5-curated"])
@pytest.mark.parametrize("mode", ["vibe", "director"])
def test_v5_reference_modes_fail_before_encoding_or_model_fallback(model, mode):
    value = plugin()
    images, reason = generate(
        value, model=model, reference_mode=mode, reference_image_bytes=png()
    )
    assert images == [] and "V5" in reason
    assert "星桥" in _format_generate_error(reason)
    assert value._session.calls == []


@pytest.mark.parametrize(
    "action", ["bg-removal", "lineart", "sketch", "colorize", "emotion", "declutter"]
)
def test_director_tools_use_native_augment_and_real_image_dimensions(action):
    value = plugin()
    images, reason = generate(
        value, director_action=action, reference_image_bytes=png()
    )
    assert images and reason == "ok"
    call = value._session.calls[0]
    assert call["endpoint"].endswith("/ai/augment-image")
    body = call["body"]
    assert body["req_type"] == action
    assert body["model"] == "nai-diffusion-5-full"
    assert (body["width"], body["height"]) == (16, 8)
    assert "parameters" not in body and not body["image"].startswith("data:")
    if action == "emotion":
        assert body["prompt"].startswith("neutral;;")


def test_native_count_limits_do_not_silently_drop_images():
    value = plugin()
    images, reason = generate(value, n=5)
    assert images == [] and "1–4" in reason
    assert value._session.calls == []
    images, reason = asyncio.run(value._openai_generate("apple", "512x512", n=6))
    assert images and reason == "ok"
    assert value._session.calls[0]["body"]["parameters"]["n_samples"] == 6


def test_native_http_error_keeps_the_root_message_and_does_not_retry():
    value = plugin(
        handler=lambda endpoint, body: Response(
            {"statusCode": 503, "message": "请稍后重试"}, status=503
        )
    )
    images, reason = generate(value)
    assert images == [] and reason == "http_503 | 请稍后重试"
    assert len(value._session.calls) == 1


def test_native_timeout_does_not_resubmit():
    def timeout(endpoint, body):
        raise asyncio.TimeoutError()

    value = plugin(handler=timeout)
    images, reason = generate(value)
    assert images == [] and reason == "timeout"
    assert len(value._session.calls) == 1


def test_failed_vibe_encoding_stops_before_generation():
    value = plugin(
        handler=lambda endpoint, body: Response(
            {"statusCode": 403, "message": "没有编码权限"}, status=403
        )
    )
    images, reason = generate(
        value, model="nai-diffusion-4-5-full", reference_image_bytes=png()
    )
    assert images == [] and "没有编码权限" in reason
    assert len(value._session.calls) == 1
    assert value._session.calls[0]["endpoint"].endswith("encode-vibe")


def test_later_encoding_failure_reports_completed_encodings_and_never_generates():
    count = 0

    def handler(endpoint, body):
        nonlocal count
        count += 1
        return (
            Response(raw=b"encoded")
            if count == 1
            else Response({"message": "unavailable"}, status=503)
        )

    value = plugin(handler=handler)
    images, reason = generate(
        value, model="nai-diffusion-4-5-full", reference_image_bytes_list=[png(), png()]
    )
    assert images == [] and "1 张编码已完成" in reason
    assert len(value._session.calls) == 2
    assert all(c["endpoint"].endswith("encode-vibe") for c in value._session.calls)


def test_native_unsupported_model_is_not_silently_replaced():
    value = plugin()
    images, reason = generate(
        value,
        model="nai-diffusion-3",
        reference_mode="director",
        reference_image_bytes=png(),
    )
    assert images == [] and "仅支持 V4.5/V5" in reason
    assert value._session.calls == []


def test_native_redirect_is_not_accepted_as_a_successful_image():
    value = plugin(
        handler=lambda endpoint, body: Response(
            {"images": [{"image": base64.b64encode(png()).decode()}]}, status=302
        )
    )
    images, reason = generate(value)
    assert images == [] and "重定向" in reason
    assert len(value._session.calls) == 1


@pytest.mark.parametrize(
    "prompt", ["happy", "happy;;smile", "artist:example, happy;;smile"]
)
def test_emotion_prefix_survives_artist_prompt_merging(prompt):
    value = plugin()
    images, reason = asyncio.run(
        value._openai_generate(
            prompt, "1024x1024", director_action="emotion", reference_image_bytes=png()
        )
    )
    assert images and reason == "ok"
    assert value._session.calls[0]["body"]["prompt"].startswith("happy;;")


def test_existing_profile_keeps_v5_director_payload_and_transient_retries(monkeypatch):
    calls = 0

    def handler(endpoint, body):
        nonlocal calls
        calls += 1
        if calls == 1:
            return Response({"error": {"message": "busy"}}, status=503)
        return Response(
            {"data": [{"b64_json": base64.b64encode(png()).decode()}]}, status=200
        )

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    value = plugin("compatible", handler)
    images, reason = generate(
        value, reference_mode="director", reference_image_bytes=png()
    )
    assert images and reason == "ok"
    assert len(value._session.calls) == 2
    body = value._session.calls[1]["body"]
    assert body["model"] == "nai-diffusion-5-full"
    assert body["action"] == "generate"
    assert body["parameters"]["director_reference_images"][0].startswith("data:")


def test_request_conversion_does_not_mutate_caller_payload():
    body = {
        "model": "nai-diffusion-4-5-full",
        "prompt": "apple",
        "action": "generate",
        "size": "512x512",
        "n": 1,
        "parameters": {"seed": 0},
    }
    before = copy.deepcopy(body)
    prepared = asyncio.run(
        prepare_starbridge_request(
            Session(), "https://api.almoons.xyz", body, headers={}, timeout=None
        )
    )
    assert body == before
    assert prepared.payload["parameters"]["seed"] == 0
