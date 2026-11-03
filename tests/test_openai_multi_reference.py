"""OpenAI 兼容模式多张参考图（vibe / 精准参考 / img2img）单元测试。

不发起真实网络请求：``aiohttp.ClientSession.post`` 由 FakeSession 捕获，
校验 ``reference_image_multiple`` 与 ``director_reference_*`` 五数组按下标
严格等长、逐图强度与描述的回退规则、以及 img2img 仅取第一张主输入图。
"""

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest
import yaml

from main import OPENAI_MAX_REFERENCE_IMAGES, NAIGenerateImagePlugin

# 1x1 PNG（与陪伴 API 冒烟测试同源），足够让 PIL 完成尺寸适配分支。
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)

# 固定的成功响应体（单张 1x1 PNG）
FAKE_RESPONSE_BODY = json.dumps(
    {"data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]}
)


class FakeSession:
    """捕获 post 调用并返回单张图片结果的假 aiohttp 会话。"""

    closed = False

    def __init__(self):
        self.calls = []

    def post(self, endpoint, json=None, headers=None, timeout=None):
        self.calls.append({"endpoint": endpoint, "payload": json})

        class _Response:
            status = 200

            async def text(self):
                return FAKE_RESPONSE_BODY

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        return _Response()


def make_plugin(reference_mode: str = "vibe") -> NAIGenerateImagePlugin:
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.openai_api_base_url = "https://api.example.com"
    plugin.openai_api_key = "test-key"
    plugin.openai_api_model = "nai-diffusion-5-full"
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
    assert call["endpoint"].endswith("/images/generations"), call["endpoint"]
    return call["payload"]


def test_vibe_multi_image_defaults():
    plugin = make_plugin()
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
    # 未提供强度时按文档 §5 默认 0.6，信息提取量固定 0.7，均与张数等长
    assert params["reference_strength_multiple"] == [0.6, 0.6]
    assert params["reference_information_extracted_multiple"] == [0.7, 0.7]


def test_vibe_per_image_strengths_fill_from_scalar():
    plugin = make_plugin()
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
    # 逐图列表优先，缺位由统一 strength 补齐，保证与张数等长
    assert params["reference_strength_multiple"] == [0.9, 0.4]


@pytest.mark.parametrize(
    "model",
    ["nai-diffusion-4-5-full", "nai-diffusion-4-5-curated", "nai45", "nai45-curated"],
)
def test_director_multi_image_parallel_arrays_keep_explicit_45_model(model):
    plugin = make_plugin()
    _, reason = asyncio.run(
        plugin._openai_generate(
            "same girl, sunset beach",
            "1024x1024",
            n=1,
            reference_image_bytes_list=[PNG_1PX, PNG_1PX],
            reference_mode="director",
            model=model,
            director_captions=["character", "not-a-caption"],
        )
    )
    assert reason == "ok"
    payload = last_payload(plugin)
    assert payload["model"] == model
    params = payload["parameters"]
    assert len(params["director_reference_images"]) == 2
    assert params["director_reference_strength_values"] == [1.0, 1.0]
    assert params["director_reference_secondary_strength_values"] == [0.5, 0.5]
    assert params["director_reference_information_extracted"] == [1.0, 1.0]
    descriptions = params["director_reference_descriptions"]
    assert [item["caption"]["base_caption"] for item in descriptions] == [
        "character",
        "character&style",
    ]
    assert all(item["caption"]["char_captions"] == [] for item in descriptions)


@pytest.mark.parametrize(
    "model",
    [
        "nai-diffusion-5-full",
        "nai-diffusion-5-curated",
        "nai5",
        "nai5-curated",
        "nai-diffusion-3",
        "unknown",
    ],
)
@pytest.mark.parametrize("model_source", ["default", "override", "slot"])
def test_director_unsupported_model_is_rejected_without_requests_or_success_records(
    model, model_source
):
    plugin = make_plugin()
    plugin._archive_generated_image = AsyncMock()
    plugin._record_usage_stat = AsyncMock()
    kwargs = {}
    if model_source == "default":
        plugin.openai_api_model = model
    elif model_source == "override":
        plugin.openai_api_model = "nai-diffusion-4-5-full"
        kwargs["model"] = model
    else:
        plugin._slot_model = lambda: model
    images, reason = asyncio.run(
        plugin._openai_generate(
            "same girl",
            "1024x1024",
            n=1,
            reference_image_bytes_list=[PNG_1PX],
            reference_mode="director",
            **kwargs,
        )
    )
    assert images == []
    assert reason.startswith("reference_model_unsupported | ")
    assert model in reason and "明确选择 V4.5" in reason
    assert "传输协议" in reason
    assert plugin._session.calls == []
    plugin._archive_generated_image.assert_not_awaited()
    plugin._record_usage_stat.assert_not_awaited()


def test_director_configured_fallback_image_does_not_bypass_v5_gate(
    tmp_path, monkeypatch
):
    plugin = make_plugin(reference_mode="director")
    image = tmp_path / "reference.png"
    image.write_bytes(PNG_1PX)
    plugin.openai_director_fallback_images = [str(image)]
    monkeypatch.setattr("main.StarTools.get_data_dir", lambda _: tmp_path)
    images, reason = asyncio.run(plugin._openai_generate("same girl", "832x1216"))
    assert images == [] and "明确选择 V4.5" in reason
    assert plugin._session.calls == []


@pytest.mark.parametrize(
    "model", ["nai-diffusion-4-5-full", "nai-diffusion-4-5-curated"]
)
def test_explicit_45_choice_records_actual_model_in_history_and_usage(model):
    plugin = make_plugin(reference_mode="director")
    plugin._archive_generated_image = AsyncMock()
    plugin._record_usage_stat = AsyncMock()
    images, reason = asyncio.run(
        plugin._openai_generate(
            "same girl",
            "832x1216",
            model=model,
            reference_image_bytes=PNG_1PX,
        )
    )
    assert images and reason == "ok"
    assert last_payload(plugin)["model"] == model
    history = plugin._archive_generated_image.await_args.args[1]
    assert history["model"] == history["requested_model"] == model
    assert history["reference_mode"] == "director"
    assert plugin._record_usage_stat.await_args.kwargs["model"] == model


def test_director_without_reference_falls_back_to_text2img():
    plugin = make_plugin(reference_mode="director")
    images, reason = asyncio.run(
        plugin._openai_generate(
            "a girl at the seaside",
            "1024x1024",
            n=1,
            reference_mode="director",
        )
    )
    # 无参考图且未配置兜底参考图：回退纯文生图，不再报 no_reference_image
    assert reason == "ok" and images
    params = last_payload(plugin)["parameters"]
    assert "director_reference_images" not in params
    assert "reference_image_multiple" not in params


def test_configured_director_weights_are_applied():
    plugin = make_plugin(reference_mode="director")
    plugin.openai_api_model = "nai-diffusion-4-5-full"
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
    # 未显式给逐图强度时使用插件设置的精准参考权重与次级特征权重（§8）
    assert params["director_reference_strength_values"] == [0.85, 0.85]
    assert params["director_reference_secondary_strength_values"] == [0.4, 0.4]


def test_img2img_uses_only_first_reference():
    plugin = make_plugin()
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
    call = plugin._session.calls[-1]
    assert call["endpoint"].endswith("/images/edits"), call["endpoint"]
    payload = call["payload"]
    assert payload["action"] == "img2img" and payload["image"].startswith(
        "data:image/png"
    )
    # 多余参考图不会混入 generations 的 vibe 数组字段（§5.3 主输入图）
    assert "reference_image_multiple" not in payload["parameters"]
    assert payload["parameters"]["strength"] == 0.7


def test_reference_list_truncated_to_document_limit():
    plugin = make_plugin()
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
    plugin = make_plugin()
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
    # OpenAI 兼容模式此前漏写归档，导致开启「保存生图历史」后仍然没有落盘
    assert len(saved_images) == 1
    assert len(saved_params) == 1
    assert saved_images[0].read_bytes() == PNG_1PX
    parameters = yaml.safe_load(saved_params[0].read_text(encoding="utf-8"))
    assert parameters["tag"] == "a girl at the seaside"
    assert parameters["transport"] == "openai"
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

    # 模板与 --artist 覆盖都必须进入 OpenAI 模式的最终 prompt
    assert full_prompt == "artist:foo, masterpiece, best quality, 1girl, solo"
    assert outfit_source == "none"
    assert prompt_kind == "off"


def test_openai_size_normalizes_english_tier_names():
    plugin = make_plugin()

    assert plugin._openai_size("portrait") == "832x1216"
    assert plugin._openai_size("2k_landscape") == "1920x1088"
    assert plugin._openai_size("4K竖图") == "1088x1920"


def test_openai_generate_accepts_data_uri_b64_json():
    global FAKE_RESPONSE_BODY
    plugin = make_plugin()
    original = FAKE_RESPONSE_BODY
    data_uri = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
    FAKE_RESPONSE_BODY = json.dumps({"data": [{"b64_json": data_uri}]})
    try:
        images, reason = asyncio.run(
            plugin._openai_generate("a girl at the seaside", "1024x1024", n=1)
        )
    finally:
        FAKE_RESPONSE_BODY = original

    # 部分中转会在 b64_json 里原样返回 data URI 前缀，不能直接解码失败
    assert reason == "ok"
    assert images == [PNG_1PX]
