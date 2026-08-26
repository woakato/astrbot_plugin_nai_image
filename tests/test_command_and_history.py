import asyncio
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import yaml

from astrbot.api.message_components import Image, Plain
from main import NAIGenerateImagePlugin, _format_generate_error


async def collect_results(generator):
    return [result async for result in generator]


class FakeCommandEvent:
    def __init__(self, message_str: str):
        self.message_str = message_str

    def get_sender_id(self):
        return "test-user"

    def plain_result(self, text: str):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


def make_command_plugin(reply_mode: str, generate_result):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.call_mode = "direct"
    plugin.bot_reply_mode = reply_mode
    plugin.image_gen_key = "test-token"
    plugin.image_count = 1
    plugin.image_style = "custom"
    plugin.image_size = "竖图"
    plugin._generate_one = AsyncMock(return_value=generate_result)
    return plugin


def test_image_only_mode_sends_only_the_generated_image():
    plugin = make_command_plugin("仅图片", (b"test-image-bytes", "ok"))
    event = FakeCommandEvent("a very long prompt")

    results = asyncio.run(collect_results(plugin.image(event)))

    assert len(results) == 1
    result_type, chain = results[0]
    assert result_type == "chain"
    assert len(chain) == 1
    assert isinstance(chain[0], Image)


def test_concise_mode_omits_prompt_and_keeps_brief_parameters():
    prompt = "a very long private prompt"
    plugin = make_command_plugin("简洁", (b"test-image-bytes", "ok"))
    event = FakeCommandEvent(prompt)

    results = asyncio.run(collect_results(plugin.image(event)))

    assert len(results) == 2
    result_type, text = results[0]
    assert result_type == "plain"
    assert "正在画图" in text
    assert "风格:" in text
    assert prompt not in text
    result_type, chain = results[1]
    assert result_type == "chain"
    assert isinstance(chain[0], Plain)
    assert isinstance(chain[1], Image)


def test_image_only_mode_keeps_generation_errors():
    plugin = make_command_plugin("仅图片", (None, "timeout"))
    event = FakeCommandEvent("1girl")

    results = asyncio.run(collect_results(plugin.image(event)))

    assert len(results) == 2
    assert all(result[0] == "plain" for result in results)
    assert "超时" in results[0][1]
    assert "全部 1 张图片生成失败" in results[1][1]


def test_image_command_passes_all_generation_overrides():
    plugin = make_command_plugin("仅图片", (b"test-image-bytes", "ok"))
    event = FakeCommandEvent(
        "image 1girl, solo --style=自定义 --size=landscape --steps=28 "
        "--scale=6.5 --cfg=0.3 --sampler=k_euler_ancestral "
        "--noise=exponential --translate=auto --template=off "
        "--model=nai-diffusion-4-5-full "
        '--artist="best quality, artist:foo" '
        '--negative="bad anatomy, blurry"'
    )

    results = asyncio.run(collect_results(plugin.image(event)))

    assert len(results) == 1
    plugin._generate_one.assert_awaited_once_with(
        "1girl, solo",
        "custom",
        "横图",
        steps=28,
        scale=6.5,
        cfg=0.3,
        sampler="k_euler_ancestral",
        noise_schedule="exponential",
        negative="bad anatomy, blurry",
        model="nai-diffusion-4-5-full",
        custom_artists="best quality, artist:foo",
        enable_template=False,
        enable_translate="自动",
    )


def test_image_command_rejects_unknown_argument_without_generating():
    plugin = make_command_plugin("完整", (b"test-image-bytes", "ok"))
    event = FakeCommandEvent("1girl --unknown=value")

    results = asyncio.run(collect_results(plugin.image(event)))

    assert results == [("plain", "参数错误：未知参数: --unknown。")]
    plugin._generate_one.assert_not_awaited()


def test_image_command_rejects_char_argument_outside_openai_mode():
    plugin = make_command_plugin("完整", (b"test-image-bytes", "ok"))
    event = FakeCommandEvent('2girls --char="1girl|0.3|0.5"')

    results = asyncio.run(collect_results(plugin.image(event)))

    assert len(results) == 1
    assert results[0][0] == "plain"
    assert "--char" in results[0][1]
    plugin._generate_one.assert_not_awaited()


def test_image_command_passes_characters_to_openai_generate():
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.call_mode = "openai"
    plugin.bot_reply_mode = "仅图片"
    plugin.openai_api_base_url = "https://example.invalid/v1"
    plugin.openai_api_key = "test-key"
    plugin.image_count = 1
    plugin.image_style = "custom"
    plugin.image_size = "竖图"
    plugin.steps = 24
    plugin.scale = 6
    plugin.cfg_value = 7.0
    plugin.sampler = "k_dpmpp_2m_sde"
    plugin.noise_schedule = "karras"
    plugin.negative = ""
    plugin.model = "nai-diffusion-4-5-full"
    plugin.translate_mode = "关闭"
    plugin.enable_template = False
    plugin.custom_artists = ""
    plugin._openai_generate = AsyncMock(return_value=([b"test-image-bytes"], "ok"))
    event = FakeCommandEvent(
        '2girls --char="1girl, red dress|0.3|0.5" --char="1boy|0.7|0.5"'
    )

    results = asyncio.run(collect_results(plugin.image(event)))

    assert len(results) == 1
    result_type, chain = results[0]
    assert result_type == "chain"
    assert isinstance(chain[0], Image)
    plugin._openai_generate.assert_awaited_once()
    assert plugin._openai_generate.await_args.kwargs["characters"] == (
        ("1girl, red dress", 0.3, 0.5),
        ("1boy", 0.7, 0.5),
    )


def test_generate_one_uses_overrides_in_request_and_history():
    class FakeResponse:
        status = 200

        async def read(self):
            return b"generated-image"

    class FakeRequestContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeSession:
        def __init__(self):
            self.url = ""

        def get(self, url, **kwargs):
            self.url = url
            return FakeRequestContext()

    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.image_gen_key = "test-token"
    plugin._session = FakeSession()
    plugin.steps = 24
    plugin.scale = 6
    plugin.cfg_value = 0.0
    plugin.sampler = "k_dpmpp_2m_sde"
    plugin.noise_schedule = "karras"
    plugin.negative = "default negative"
    plugin.model = "default-model"
    plugin.enable_template = True
    plugin.translate_mode = "关闭"
    plugin.character_preset = "character preset"
    plugin.custom_artists = "default artist"
    plugin.default_outfit = ""
    plugin.base_url = "https://example.invalid"
    plugin._prepare_translated_prompt = AsyncMock(
        return_value=("1girl, solo", "none", False, "nai")
    )
    plugin._archive_generated_image = AsyncMock()

    result = asyncio.run(
        plugin._generate_one(
            "raw prompt",
            "custom",
            "横图",
            steps=28,
            scale=6.5,
            cfg=0.3,
            sampler="k_euler_ancestral",
            noise_schedule="exponential",
            negative="bad anatomy, blurry",
            model="nai-diffusion-4-5-full",
            custom_artists="best quality, artist:foo",
            enable_template=False,
            enable_translate="自动",
        )
    )

    assert result == (b"generated-image", "ok")
    query = parse_qs(urlparse(plugin._session.url).query, keep_blank_values=True)
    assert query == {
        "tag": ["1girl, solo"],
        "token": ["test-token"],
        "model": ["nai-diffusion-4-5-full"],
        "artist": ["best quality, artist:foo"],
        "size": ["横图"],
        "steps": ["28"],
        "scale": ["6.5"],
        "cfg": ["0.3"],
        "sampler": ["k_euler_ancestral"],
        "negative": ["bad anatomy, blurry"],
        "nocache": ["1"],
        "noise_schedule": ["exponential"],
    }
    plugin._prepare_translated_prompt.assert_awaited_once_with(
        "raw prompt",
        translate_mode="自动",
        apply_outfit=True,
    )
    saved_parameters = plugin._archive_generated_image.await_args.args[1]
    assert saved_parameters == {
        "tag": "1girl, solo",
        "model": "nai-diffusion-4-5-full",
        "artist": "best quality, artist:foo",
        "size": "横图",
        "steps": 28,
        "scale": 6.5,
        "cfg": 0.3,
        "sampler": "k_euler_ancestral",
        "negative": "bad anatomy, blurry",
        "nocache": 1,
        "noise_schedule": "exponential",
    }


def test_generate_one_allows_empty_custom_artist_without_fallback():
    class FakeResponse:
        status = 200

        async def read(self):
            return b"generated-image"

    class FakeRequestContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeSession:
        def __init__(self):
            self.url = ""

        def get(self, url, **kwargs):
            self.url = url
            return FakeRequestContext()

    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.image_gen_key = "test-token"
    plugin._session = FakeSession()
    plugin.steps = 24
    plugin.scale = 6
    plugin.cfg_value = 0.0
    plugin.sampler = "k_dpmpp_2m_sde"
    plugin.noise_schedule = "karras"
    plugin.negative = "default negative"
    plugin.model = "default-model"
    plugin.enable_template = False
    plugin.translate_mode = "关闭"
    plugin.character_preset = ""
    plugin.custom_artists = "default artist"
    plugin.default_outfit = ""
    plugin.base_url = "https://example.invalid"
    plugin._prepare_translated_prompt = AsyncMock(
        return_value=("1girl, solo", "none", False, "nai")
    )
    plugin._archive_generated_image = AsyncMock()

    result = asyncio.run(
        plugin._generate_one(
            "raw prompt",
            "custom",
            "竖图",
            custom_artists="",
        )
    )

    assert result == (b"generated-image", "ok")
    query = parse_qs(urlparse(plugin._session.url).query, keep_blank_values=True)
    assert query["artist"] == [""]


def test_image_history_keeps_newest_managed_files(tmp_path):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.save_image_history = True
    plugin.save_generation_parameters = True
    plugin.image_history_limit = 2
    plugin._image_history_dir = tmp_path / "image_history"
    plugin._image_history_lock = asyncio.Lock()
    unmanaged_file = plugin._image_history_dir / "notes.txt"
    plugin._image_history_dir.mkdir()
    unmanaged_file.write_text("keep", encoding="utf-8")

    async def save_images():
        await plugin._archive_generated_image(
            b"first",
            {"tag": "first prompt line 1\nfirst prompt line 2"},
        )
        first_image = next(plugin._image_history_dir.glob("nai_*.img"))
        first_image.with_suffix(".json").write_text(
            '{"tag": "legacy parameters"}',
            encoding="utf-8",
        )
        await plugin._archive_generated_image(b"second", {"tag": "second prompt"})
        await plugin._archive_generated_image(b"third", {"tag": "third prompt"})

    asyncio.run(save_images())

    saved_files = sorted(plugin._image_history_dir.glob("nai_*.img"))
    parameter_files = sorted(plugin._image_history_dir.glob("nai_*.yaml"))
    assert len(saved_files) == 2
    assert len(parameter_files) == 2
    assert {path.read_bytes() for path in saved_files} == {b"second", b"third"}
    saved_prompts = {
        yaml.safe_load(path.read_text(encoding="utf-8"))["tag"]
        for path in parameter_files
    }
    assert saved_prompts == {
        "second prompt",
        "third prompt",
    }
    saved_stems = {path.stem for path in saved_files}
    parameter_stems = {path.stem for path in parameter_files}
    assert saved_stems == parameter_stems
    assert unmanaged_file.read_text(encoding="utf-8") == "keep"
    assert not list(plugin._image_history_dir.glob("nai_*.json"))


def test_zero_history_limit_disables_cleanup(tmp_path):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.save_image_history = True
    plugin.save_generation_parameters = True
    plugin.image_history_limit = 0
    plugin._image_history_dir = tmp_path / "image_history"
    plugin._image_history_lock = asyncio.Lock()

    async def save_images():
        await plugin._archive_generated_image(b"first", {"tag": "first prompt"})
        await plugin._archive_generated_image(b"second", {"tag": "second prompt"})
        await plugin._archive_generated_image(b"third", {"tag": "third prompt"})

    asyncio.run(save_images())

    assert len(list(plugin._image_history_dir.glob("nai_*.img"))) == 3
    assert len(list(plugin._image_history_dir.glob("nai_*.yaml"))) == 3


def test_parameter_history_can_be_disabled(tmp_path):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.save_image_history = True
    plugin.save_generation_parameters = False
    plugin.image_history_limit = 0
    plugin._image_history_dir = tmp_path / "image_history"
    plugin._image_history_lock = asyncio.Lock()

    asyncio.run(plugin._archive_generated_image(b"image", {"tag": "prompt"}))

    assert len(list(plugin._image_history_dir.glob("nai_*.img"))) == 1
    assert not list(plugin._image_history_dir.glob("nai_*.yaml"))


def test_parameter_history_uses_yaml_literal_block_for_multiline_prompt(tmp_path):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.save_image_history = True
    plugin.save_generation_parameters = True
    plugin.image_history_limit = 0
    plugin._image_history_dir = tmp_path / "image_history"
    plugin._image_history_lock = asyncio.Lock()
    parameters = {
        "tag": "1girl, solo\ncute\nglare nude",
        "steps": 28,
        "cfg": 0.3,
    }

    asyncio.run(plugin._archive_generated_image(b"image", parameters))

    parameters_path = next(plugin._image_history_dir.glob("nai_*.yaml"))
    content = parameters_path.read_text(encoding="utf-8")
    assert "tag: |-\n  1girl, solo\n  cute\n  glare nude\n" in content
    assert "\\n" not in content
    assert yaml.safe_load(content) == parameters


def test_auto_translate_skips_pure_nai_prompt():
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.translate_mode = "自动"
    plugin.enable_translate = True
    plugin._translate_prompt = AsyncMock(return_value="should not be used")
    plugin._resolve_outfit = lambda prompt: ("unexpected", "prompt", False)
    prompt = "1girl, solo, 1.2::blue hair::, {{looking at viewer}}"

    prepared, outfit_source, use_default, prompt_kind = asyncio.run(
        plugin._prepare_translated_prompt(
            prompt,
            translate_mode="自动",
            apply_outfit=True,
        )
    )

    assert prepared == prompt
    assert outfit_source == "none"
    assert not use_default
    assert prompt_kind == "nai"
    plugin._translate_prompt.assert_not_awaited()


def test_prompt_is_normalized_before_disabled_translation_pipeline():
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.translate_mode = "关闭"
    plugin.enable_translate = False

    prepared, outfit_source, use_default, prompt_kind = asyncio.run(
        plugin._prepare_translated_prompt(
            "1girl,\n\n  solo\t  best quality",
            translate_mode="关闭",
            apply_outfit=True,
        )
    )

    assert prepared == "1girl, solo best quality"
    assert outfit_source == "none"
    assert not use_default
    assert prompt_kind == "off"


def test_auto_translate_only_sends_natural_segments_to_llm():
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.translate_mode = "自动"
    plugin.enable_translate = True
    plugin._translate_prompt = AsyncMock(
        return_value="1girl, black dress, standing, rain"
    )
    plugin._resolve_outfit = lambda prompt: ("", "none", False)

    prepared, _, _, prompt_kind = asyncio.run(
        plugin._prepare_translated_prompt(
            "1girl, solo, 她穿着黑色连衣裙站在雨里, best quality",
            translate_mode="自动",
            apply_outfit=True,
        )
    )

    assert prompt_kind == "mixed"
    assert prepared == "1girl, solo, black dress, standing, rain, best quality"
    plugin._translate_prompt.assert_awaited_once_with(
        "她穿着黑色连衣裙站在雨里",
        force=True,
    )


def test_outfit_cache_switch_off_still_uses_excerpt_but_skips_cache():
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.enable_outfit_cache = False
    plugin.outfit_cache_ttl_seconds = 3600
    plugin.default_outfit = "modern fashion"
    plugin.outfit_cache_text = None
    plugin.outfit_cache_expires_at = None

    # 命中具体服装词：片段仍作为本次上下文返回，但不写入缓存
    ctx, source, use_default = plugin._resolve_outfit("她穿上了红色连衣裙")
    assert ctx and "红色连衣裙" in ctx, ctx
    assert source == "prompt"
    assert use_default is False
    assert plugin.outfit_cache_text is None

    # 模糊描述：开关关闭不读缓存，回退默认服装标记
    ctx, source, use_default = plugin._resolve_outfit("a girl standing in the rain")
    assert ctx == ""
    assert source == "none"
    assert use_default is True


def test_outfit_cache_switch_on_writes_and_reuses_cache():
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.enable_outfit_cache = True
    plugin.outfit_cache_ttl_seconds = 3600
    plugin.default_outfit = ""
    plugin.outfit_cache_text = None
    plugin.outfit_cache_expires_at = None

    _, source, _ = plugin._resolve_outfit("她穿上了红色连衣裙")
    assert source == "prompt"
    assert plugin.outfit_cache_text and "红色连衣裙" in plugin.outfit_cache_text

    cached_ctx, source, use_default = plugin._resolve_outfit(
        "a girl standing in the rain"
    )
    assert cached_ctx == plugin.outfit_cache_text
    assert source == "cache"
    assert use_default is False


class FakeQuotaResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class FakeQuotaRequestContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeQuotaSession:
    def __init__(self, response):
        self._response = response
        self.posted_url = ""
        self.posted_json = None

    def post(self, url, **kwargs):
        self.posted_url = url
        self.posted_json = kwargs.get("json")
        return FakeQuotaRequestContext(self._response)


def make_quota_plugin(response):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.image_gen_key = "test-token"
    plugin.base_url = "https://example.invalid"
    plugin._session = FakeQuotaSession(response)
    return plugin


def test_fetch_quota_parses_upstream_success_payload():
    plugin = make_quota_plugin(
        FakeQuotaResponse(
            200,
            {
                "status": "ok",
                "type": "sta1n",
                "data": {"value": 943, "balance": 943, "enabled": True},
            },
        )
    )

    result = asyncio.run(plugin._fetch_quota())

    assert result == {"ok": True, "value": 943, "balance": 943, "enabled": True}
    assert plugin._session.posted_url == "https://example.invalid/api/api/getUser"
    assert plugin._session.posted_json == {"toUserId": "test-token"}


def test_fetch_quota_surfaces_upstream_business_error():
    plugin = make_quota_plugin(
        FakeQuotaResponse(
            200,
            {
                "status": "error",
                "type": "std",
                "message": "user not found",
                "data": {"value": 0},
            },
        )
    )

    result = asyncio.run(plugin._fetch_quota())

    assert result == {"ok": False, "message": "user not found"}


def test_fetch_quota_reports_non_200_status():
    plugin = make_quota_plugin(FakeQuotaResponse(403, {}))

    result = asyncio.run(plugin._fetch_quota())

    assert result == {"ok": False, "message": "上游返回 HTTP 403"}


def test_quota_command_reports_remaining_and_disabled_token():
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.image_gen_key = "test-token"
    plugin._fetch_quota = AsyncMock(
        return_value={"ok": True, "value": 12, "balance": 12, "enabled": False}
    )
    event = FakeCommandEvent("quota")

    results = asyncio.run(collect_results(plugin.quota(event)))

    assert [text for _, text in results] == [
        "正在查询额度...",
        "剩余额度: 12\n⚠️ 该 token 已被站点停用（enabled=false），生图可能失败",
    ]


def test_quota_command_reports_upstream_error_message():
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.image_gen_key = "test-token"
    plugin._fetch_quota = AsyncMock(
        return_value={"ok": False, "message": "user not found"}
    )
    event = FakeCommandEvent("quota")

    results = asyncio.run(collect_results(plugin.quota(event)))

    assert [text for _, text in results] == [
        "正在查询额度...",
        "额度查询失败：user not found",
    ]


def test_quota_command_requires_token():
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.image_gen_key = ""
    event = FakeCommandEvent("quota")

    results = asyncio.run(collect_results(plugin.quota(event)))

    assert results == [("plain", "未配置 image_gen_key。")]


def test_format_error_director_ref_rejected_gives_targeted_guidance():
    msg = _format_generate_error("director_ref_rejected")

    assert "精准参考" in msg
    assert "未开通" in msg or "不兼容" in msg
    assert "vibe" in msg

    msg_with_detail = _format_generate_error(
        "director_ref_rejected | 模型 nai-diffusion-4-5-full：参数校验失败"
    )
    assert "详情: 模型 nai-diffusion-4-5-full：参数校验失败" in msg_with_detail


def test_format_error_generic_http_400_keeps_parameter_hint():
    msg = _format_generate_error("http_400 | invalid image size")

    assert "HTTP 400" in msg
    assert "参数错误" in msg
    assert "详情: invalid image size" in msg
