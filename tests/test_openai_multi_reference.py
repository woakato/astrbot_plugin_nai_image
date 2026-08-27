"""OpenAI 兼容模式多张参考图（vibe / 精准参考 / img2img）单元测试。

不发起真实网络请求：``aiohttp.ClientSession.post`` 由 FakeSession 捕获，
校验 ``reference_image_multiple`` 与 ``director_reference_*`` 五数组按下标
严格等长、逐图强度与描述的回退规则、以及 img2img 仅取第一张主输入图。
"""

import asyncio
import base64
import json

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
    plugin.openai_timeout = 60
    plugin.openai_max_retries = 0
    plugin.steps = 24
    plugin.scale = 6
    plugin.sampler = "k_euler_ancestral"
    plugin.noise_schedule = "karras"
    plugin.negative = ""
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


def test_director_multi_image_parallel_arrays_keeps_v5_model():
    plugin = make_plugin()
    _, reason = asyncio.run(
        plugin._openai_generate(
            "same girl, sunset beach",
            "1024x1024",
            n=1,
            reference_image_bytes_list=[PNG_1PX, PNG_1PX],
            reference_mode="director",
            director_captions=["character", "not-a-caption"],
        )
    )
    assert reason == "ok"
    payload = last_payload(plugin)
    # NAI 5 全系列同样支持精准参考（实测），不应误切换为 4.5
    assert payload["model"] == "nai-diffusion-5-full"
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


def test_director_unsupported_model_falls_back_to_45():
    plugin = make_plugin()
    plugin.openai_api_model = "nai-diffusion-3"
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
    # 非 4.5 / 5 系列模型不支持精准参考，自动回退 nai-diffusion-4-5-full
    assert last_payload(plugin)["model"] == "nai-diffusion-4-5-full"


def test_configured_director_weights_are_applied():
    plugin = make_plugin(reference_mode="director")
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
