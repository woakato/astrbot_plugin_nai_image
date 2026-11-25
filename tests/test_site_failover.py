"""站点优先级故障转移（site_failover）与生图链路接入的离线单元测试。

覆盖:
- 站点表规整：优先级排序与同优先级稳定次序、非法站点过滤、enabled 过滤、
  base_url 尾部斜杠清理、站点描述不泄漏 api_key；
- 换站判定：可换站/不可换站两组原因、无剩余候选时的保守结论；
- 插件接入：开关关闭与站点表为空时的单站点退化、调试面板的凭据回退、
  OpenAI 兼容与直连两条链路的换站、超时不换站、全站失败保留最后原因、
  换站不重复归档、任务接口按当前站点地址提交；
- 直连链路的站点级凭据：插件级 token 留空时用站点自己的 token、无凭据的
  站点被跳过并换站、所有候选都无凭据时在入口直接返回 no_token。

全部离线运行，不发起真实网络请求。
"""

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest

from test_api_credentials import load_plugin

from main import NAIGenerateImagePlugin
from site_failover import (
    build_site,
    describe_site,
    enabled_sites,
    is_switchable,
    normalize_base_url,
    normalize_sites,
    switch_decision,
)

# 1x1 PNG（与 test_direct_pricing / test_openai_multi_reference 同源），
# 足够让成功响应解码出非空图片字节。
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)


def _openai_ok_body() -> str:
    """构造 OpenAI 兼容端点的成功响应体（单张图片）。"""

    return json.dumps({"data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]})


def _openai_error_body(message: str) -> str:
    """构造带 error.message 的失败响应体，便于核对透传的错误摘要。"""

    return json.dumps({"error": {"message": message}})


def _openai_site(name, base_url, *, api_key="", priority=1, enabled=True):
    """按 _conf_schema.json 里模板列表的字段构造一条 OpenAI 兼容站点。"""

    return {
        "name": name,
        "mode": "openai",
        "base_url": base_url,
        "api_key": api_key,
        "priority": priority,
        "enabled": enabled,
    }


def _direct_site(name, base_url, *, api_key="", priority=1):
    """构造一条 nai.sta1n.cn（direct）站点。"""

    return {
        "name": name,
        "mode": "direct",
        "base_url": base_url,
        "api_key": api_key,
        "priority": priority,
        "enabled": True,
    }


def _two_openai_sites():
    """两个按优先级排列的 OpenAI 兼容站点。"""

    return [
        _openai_site("第一站", "https://site-a.example", api_key="a-key", priority=1),
        _openai_site("第二站", "https://site-b.example", api_key="b-key", priority=2),
    ]


def _load_openai_plugin(load_plugin, sites, *, enabled=True):
    """用既有 load_plugin fixture 构造启用了站点表的 OpenAI 兼容插件实例。

    重试次数固定为 0：故障转移测试关心的是「换不换站」，
    不需要 2/4/8 秒退避重试真实等待。
    """

    plugin, _ = load_plugin(
        {
            "call_mode": "openai",
            "openai_api_base_url": "https://primary.example/v1",
            "openai_api_key": "primary-key",
            "openai_api_model": "nai-diffusion-5-full",
            "openai_max_retries": 0,
            "enable_site_failover": enabled,
            "site_failover_sites": list(sites or []),
        }
    )
    return plugin


def _direct_plugin(sites, *, token="fallback-token"):
    """构造裸的直连插件实例：只补齐 _generate_one 用到的属性。

    ``token`` 对应插件级配置 ``image_gen_key``；传空串可验证「凭据只配在
    站点表里」的场景。

    ``_submit_web_job`` 由各测试替换成 AsyncMock，因此 _session 只做守卫，
    任何真实 HTTP 调用都会立刻失败。
    """

    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.image_gen_key = token
    plugin.base_url = "https://fallback.example"
    plugin.request_timeout = 30
    plugin.steps = 24
    plugin.scale = 6
    plugin.cfg_value = 1.0
    plugin.sampler = "k_dpmpp_2m_sde"
    plugin.noise_schedule = "karras"
    plugin.negative = ""
    plugin.model = "nai-diffusion-4-5-full"
    plugin.enable_template = False
    plugin.translate_mode = "关闭"
    plugin.character_preset = ""
    plugin.custom_artists = ""
    plugin.default_outfit = ""
    plugin.enable_site_failover = True
    plugin._sites_config = list(sites or [])
    plugin._session = _ForbiddenSession()
    plugin._archive_generated_image = AsyncMock()
    plugin._record_usage_from_parameters = AsyncMock()
    plugin._submit_web_job = AsyncMock()
    return plugin


class _Resp:
    """可编程的假 aiohttp 响应（构造时传入内容，避免嵌套类遮蔽 self）。"""

    def __init__(self, status=200, text="", headers=None, data=b""):
        self.status = status
        self.headers = headers or {}
        self._text = text
        self._data = data

    async def text(self):
        return self._text

    async def read(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeOpenAISession:
    """按站点地址分流响应的假会话，记录每个 endpoint、请求头与请求体。

    ``routes`` 的键是站点地址前缀，值可以是 ``_Resp`` 实例（成功/失败响应）
    或异常实例（模拟超时等连接级失败）。
    """

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def post(self, endpoint, json=None, headers=None, timeout=None, **kwargs):
        self.calls.append(
            {
                "endpoint": endpoint,
                "payload": json,
                "headers": dict(headers or {}),
            }
        )
        for base, handler in self.routes.items():
            if endpoint.startswith(base):
                if isinstance(handler, BaseException):
                    raise handler
                return handler
        raise AssertionError(f"未预期的 endpoint: {endpoint}")

    @property
    def endpoints(self):
        """按调用顺序返回请求过的 endpoint 列表。"""

        return [call["endpoint"] for call in self.calls]


class _ForbiddenSession:
    """直连链路测试用的守卫会话：任何真实 HTTP 调用都视为测试失败。"""

    def post(self, *args, **kwargs):
        raise AssertionError("_submit_web_job 已被替换，不应发起真实 POST 请求")

    def get(self, *args, **kwargs):
        raise AssertionError("不应发起真实 GET 请求")


# ===== 站点表规整（纯函数） =====


def test_normalize_sites_sorts_by_priority_and_keeps_list_order_for_ties():
    # 乱序 priority + 两个相同 priority：同优先级按列表原始顺序，不按名称排序。
    raw = [
        {"name": "第三", "mode": "openai", "base_url": "https://c.example", "priority": 3},
        {"name": "第一", "mode": "direct", "base_url": "https://a.example", "priority": 1},
        {
            "name": "并列甲",
            "mode": "novelai",
            "base_url": "https://b1.example",
            "priority": 2,
        },
        {
            "name": "并列乙",
            "mode": "openai",
            "base_url": "https://b2.example",
            "priority": 2,
        },
    ]

    sites = normalize_sites(raw)

    assert [site["name"] for site in sites] == ["第一", "并列甲", "并列乙", "第三"]
    assert [site["priority"] for site in sites] == [1, 2, 2, 3]
    # order 记录配置里的原始下标，排序不写回它，同优先级据此保持稳定。
    assert [site["order"] for site in sites] == [1, 2, 3, 0]
    # effective_order 是重排后的生效次序，从 1 开始连续编号。
    assert [site["effective_order"] for site in sites] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "item",
    [
        {"name": "缺地址", "mode": "openai"},
        {"name": "空地址", "mode": "openai", "base_url": "   "},
        {"name": "非 http 协议", "mode": "openai", "base_url": "ftp://site.example"},
        {"name": "缺协议", "mode": "openai", "base_url": "site.example"},
        {"name": "非法接入方式", "mode": "socket", "base_url": "https://site.example"},
        {"name": "缺少接入方式", "base_url": "https://site.example"},
    ],
)
def test_normalize_sites_filters_sites_without_valid_url_or_mode(item):
    valid = {"name": "可用", "mode": "direct", "base_url": "https://valid.example"}

    assert normalize_sites([item]) == []

    # 同表里的合法站点不受影响，且生效次序从 1 重新开始。
    sites = normalize_sites([item, valid])
    assert [site["name"] for site in sites] == ["可用"]
    assert sites[0]["base_url"] == "https://valid.example"
    assert sites[0]["effective_order"] == 1


def test_enabled_sites_skips_disabled_site_kept_by_normalize_sites():
    raw = [
        {
            "name": "启用站",
            "mode": "direct",
            "base_url": "https://on.example",
            "priority": 1,
        },
        {
            "name": "停用站",
            "mode": "openai",
            "base_url": "https://off.example",
            "priority": 2,
            "enabled": False,
        },
        {
            "name": "停用字符串",
            "mode": "openai",
            "base_url": "https://off2.example",
            "priority": 3,
            "enabled": "false",
        },
    ]

    all_sites = normalize_sites(raw)
    # 停用站点仍留在站点表里（保留配置便于恢复），只是不参与故障转移。
    assert [site["name"] for site in all_sites] == ["启用站", "停用站", "停用字符串"]
    assert [site["enabled"] for site in all_sites] == [True, False, False]
    assert [site["effective_order"] for site in all_sites] == [1, 2, 3]

    assert [site["name"] for site in enabled_sites(raw)] == ["启用站"]


def test_normalize_base_url_strips_trailing_slashes():
    assert normalize_base_url("  https://site.example/v1///  ") == (
        "https://site.example/v1"
    )
    assert normalize_base_url("http://site.example/") == "http://site.example"
    # 只清理首尾空白与尾部斜杠，不改写大小写与路径。
    assert normalize_base_url("HTTPS://Site.Example/V1/") == "HTTPS://Site.Example/V1"
    assert normalize_base_url("") == ""
    assert normalize_base_url(None) == ""
    assert normalize_base_url("ftp://site.example") == ""

    # 站点表规整走同一条清理路径。
    assert build_site("站", "openai", "https://site.example/v1/")["base_url"] == (
        "https://site.example/v1"
    )


@pytest.mark.parametrize(
    "reason, expected",
    [
        # 可换站：请求确定没有被上游受理，或上游声明失败并退点。
        ("http_4xx", True),
        ("http_5xx", True),
        ("http_400", True),
        ("http_503", True),
        ("http_4xx (HTTP 401)", True),
        ("http_503: upstream busy", True),
        ("web_jobs_unavailable", True),
        ("web_job_failed", True),
        ("openai_not_configured", True),
        ("no_token", True),
        ("reference_model_unsupported", True),
        ("reference_model_unsupported | 请明确选择 V4.5", True),
        ("director_ref_rejected", True),
        ("novelai | 请填写当前 NovelAI 生图服务的 API Key。", True),
        # 不可换站：可能已出图或已计费，或与站点无关。
        ("timeout", False),
        ("empty_response", False),
        ("invalid_response", False),
        ("no_session", False),
        ("no_reference_image", False),
        ("exception", False),
        (
            "novelai | 参考图编码超时，未继续生图；编码可能已计费，请确认后再试。",
            False,
        ),
        ("http_other (HTTP 302)", False),
    ],
)
def test_is_switchable_matches_conservative_billing_rules(reason, expected):
    assert is_switchable(reason) is expected


def test_switch_decision_refuses_to_switch_without_remaining_sites():
    # 没有剩余候选时，即使原因本身可换站也必须停下（避免无谓重发）。
    assert switch_decision("http_503", 0) == (False, "没有更多候选站点")
    assert switch_decision("web_job_failed", -1) == (False, "没有更多候选站点")

    # 有剩余候选时，结论与原因判定一致，并附可直接写日志的说明。
    assert switch_decision("http_503", 1) == (True, "失败原因 http_503 可安全换站")
    assert switch_decision("timeout", 2) == (
        False,
        "失败原因 timeout 可能已出图或已计费，不换站",
    )


def test_describe_site_never_leaks_api_key():
    site = build_site(
        "主力站", "openai", "https://site.example/v1/", api_key="secret-openai-key"
    )
    text = describe_site(site)
    assert text == "主力站(OpenAI 兼容) https://site.example/v1"
    assert "secret-openai-key" not in text

    # NovelAI 站点额外标注服务类型，同样只输出名称/方式/地址。
    native = build_site(
        "原生站",
        "novelai",
        "https://native.example",
        api_key="secret-native-key",
        novelai_service="official",
    )
    native_text = describe_site(native)
    assert native_text == "原生站(NovelAI/official) https://native.example"
    assert "secret-native-key" not in native_text


# ===== 插件接入（main.py） =====


def test_failover_sites_empty_when_switch_disabled(load_plugin):
    plugin = _load_openai_plugin(load_plugin, _two_openai_sites(), enabled=False)

    assert plugin.enable_site_failover is False
    assert plugin._failover_sites() == []
    # 关闭开关时退化为单个「当前配置」站点，站点表里的条目不会被偷偷使用。
    assert [site["name"] for site in plugin._attempt_sites("openai")] == ["当前配置"]


def test_failover_sites_empty_for_empty_site_table(load_plugin):
    plugin = _load_openai_plugin(load_plugin, [])

    assert plugin.enable_site_failover is True
    assert plugin._sites_config == []
    assert plugin._failover_sites() == []
    assert [site["name"] for site in plugin._attempt_sites("openai")] == ["当前配置"]


def test_attempt_sites_without_site_table_returns_single_direct_site(load_plugin):
    plugin, _ = load_plugin(
        {
            "call_mode": "direct",
            "base_url": "https://nai.example.com/",
            "image_gen_key": "sta1n-token",
        }
    )

    assert plugin._failover_sites() == []

    sites = plugin._attempt_sites("direct")

    assert isinstance(sites, list) and len(sites) == 1
    site = sites[0]
    assert site["mode"] == "direct"
    assert site["name"] == "当前配置"
    # 当前配置的地址同样会被规整（尾部斜杠去掉），密钥取自插件配置。
    assert site["base_url"] == "https://nai.example.com"
    assert site["api_key"] == "sta1n-token"
    # 标记来源，供 _site_api_settings 区分「当前配置」包装与站点表条目。
    assert site["_is_current"] is True


def test_site_api_settings_prefers_site_credentials_and_falls_back_to_config():
    config = {
        "openai_api_base_url": "https://fallback.example/v1",
        "openai_api_key": "plugin-openai-key",
        "openai_api_model": "nai-diffusion-5-full",
        "starbride_api_base_url": "https://api.almoons.xyz",
        "starbride_api_key": "plugin-native-key",
        "novelai_api_model": "nai-diffusion-4-5-full",
        "novelai_service": "starbride",
        "model": "nai-diffusion-4-5-full",
    }

    # 站点自己填了密钥时优先用站点的，地址也以站点为准。
    own = build_site(
        "主力", "openai", "https://site.example/v1/", api_key="site-openai-key"
    )
    assert NAIGenerateImagePlugin._site_api_settings(own, config) == {
        "profile": "compatible",
        "url": "https://site.example/v1",
        "key": "site-openai-key",
        "model": "nai-diffusion-5-full",
    }

    # 站点密钥留空时回退插件配置里该协议已保存的凭据，地址仍用站点的。
    shared = build_site("备用", "openai", "https://shared.example/v1")
    shared_settings = NAIGenerateImagePlugin._site_api_settings(shared, config)
    assert shared_settings["url"] == "https://shared.example/v1"
    assert shared_settings["key"] == "plugin-openai-key"

    # NovelAI 原生站点按自己声明的服务取该服务的凭据。
    native = build_site("原生", "novelai", "https://native.example")
    native_settings = NAIGenerateImagePlugin._site_api_settings(native, config)
    assert native_settings["profile"] == "starbride"
    assert native_settings["url"] == "https://native.example"
    assert native_settings["key"] == "plugin-native-key"
    assert native_settings["model"] == "nai-diffusion-4-5-full"

    # 直连链路没有这一层解析，token 回退在单站点实现里完成：
    # 站点留空时使用插件配置的 image_gen_key。
    plugin = _direct_plugin([_direct_site("直连站", "https://direct.example")])
    plugin._submit_web_job = AsyncMock(return_value=(b"direct-image", "ok"))

    images, reason = asyncio.run(plugin._generate_one("1girl", "custom", "方图"))

    assert (images, reason) == (b"direct-image", "ok")
    assert plugin._submit_web_job.await_args.kwargs["token"] == "fallback-token"


def test_openai_generate_switches_to_second_site_after_503(load_plugin):
    plugin = _load_openai_plugin(load_plugin, _two_openai_sites())
    session = FakeOpenAISession(
        {
            "https://site-a.example": _Resp(
                status=503, text=_openai_error_body("upstream busy")
            ),
            "https://site-b.example": _Resp(status=200, text=_openai_ok_body()),
        }
    )
    plugin._session = session

    images, reason = asyncio.run(plugin._openai_generate("a red apple", "832x1216"))

    assert reason == "ok"
    assert images == [PNG_1PX]
    # 两个站点都被按优先级请求过，且各自带自己的凭据。
    assert session.endpoints == [
        "https://site-a.example/v1/images/generations",
        "https://site-b.example/v1/images/generations",
    ]
    assert [call["headers"]["Authorization"] for call in session.calls] == [
        "Bearer a-key",
        "Bearer b-key",
    ]
    # 只在成功那一次归档，失败站点不产生归档。
    archive_calls = plugin._archive_generated_image.await_args_list
    assert len(archive_calls) == 1
    assert archive_calls[0].args[0] == PNG_1PX
    assert archive_calls[0].args[1]["transport"] == "openai"


def test_openai_generate_timeout_does_not_switch_sites(load_plugin):
    plugin = _load_openai_plugin(load_plugin, _two_openai_sites())
    session = FakeOpenAISession(
        {
            "https://site-a.example": TimeoutError(),
            "https://site-b.example": _Resp(status=200, text=_openai_ok_body()),
        }
    )
    plugin._session = session

    images, reason = asyncio.run(plugin._openai_generate("a red apple", "832x1216"))

    assert images == []
    assert reason == "timeout"
    # 超时可能已在上游生成并计费，绝不能换站重发。
    assert session.endpoints == ["https://site-a.example/v1/images/generations"]
    plugin._archive_generated_image.assert_not_awaited()


def test_openai_generate_returns_last_reason_when_all_sites_fail(load_plugin):
    plugin = _load_openai_plugin(load_plugin, _two_openai_sites())
    session = FakeOpenAISession(
        {
            "https://site-a.example": _Resp(
                status=500, text=_openai_error_body("第一站故障")
            ),
            "https://site-b.example": _Resp(
                status=503, text=_openai_error_body("第二站故障")
            ),
        }
    )
    plugin._session = session

    images, reason = asyncio.run(plugin._openai_generate("a red apple", "832x1216"))

    assert images == []
    # 全部失败时返回「最后一个」站点的失败原因，而不是第一个。
    assert reason == "http_503 | 第二站故障"
    assert session.endpoints == [
        "https://site-a.example/v1/images/generations",
        "https://site-b.example/v1/images/generations",
    ]
    plugin._archive_generated_image.assert_not_awaited()


def test_openai_generate_without_sites_keeps_single_site_behavior(load_plugin):
    # 开关打开但站点表为空：仍走插件自己的单站点配置。
    plugin = _load_openai_plugin(load_plugin, [])
    session = FakeOpenAISession(
        {"https://primary.example/v1": _Resp(status=200, text=_openai_ok_body())}
    )
    plugin._session = session

    images, reason = asyncio.run(plugin._openai_generate("a red apple", "832x1216"))

    assert reason == "ok"
    assert images == [PNG_1PX]
    assert session.endpoints == ["https://primary.example/v1/images/generations"]
    assert session.calls[0]["headers"]["Authorization"] == "Bearer primary-key"


def test_generate_one_switches_site_and_archives_only_once():
    plugin = _direct_plugin(
        [
            _direct_site("第一站", "https://site-a.example", api_key="a-token", priority=1),
            _direct_site("第二站", "https://site-b.example", api_key="b-token", priority=2),
        ]
    )
    plugin._submit_web_job = AsyncMock(
        side_effect=[(None, "http_4xx"), (b"second-site-image", "ok")]
    )

    result = asyncio.run(plugin._generate_one("1girl, solo", "custom", "方图", steps=24))

    assert result == (b"second-site-image", "ok")
    assert plugin._submit_web_job.await_count == 2
    # 换站只影响「请求发送」这一段：成功后的归档必须只发生一次。
    plugin._archive_generated_image.assert_awaited_once()
    assert plugin._archive_generated_image.await_args.args[0] == b"second-site-image"
    archived = plugin._archive_generated_image.await_args.args[1]
    assert archived["transport"] == "web_jobs"
    assert archived["steps"] == 24
    assert archived["estimated_cost"] == 1
    plugin._record_usage_from_parameters.assert_awaited_once()


def test_generate_one_timeout_does_not_switch_sites():
    plugin = _direct_plugin(
        [
            _direct_site("第一站", "https://site-a.example", api_key="a-token", priority=1),
            _direct_site("第二站", "https://site-b.example", api_key="b-token", priority=2),
        ]
    )
    plugin._submit_web_job = AsyncMock(return_value=(None, "timeout"))

    result = asyncio.run(plugin._generate_one("1girl, solo", "custom", "方图", steps=24))

    assert result == (None, "timeout")
    # 超时不换站：第二站一次都没被请求。
    assert plugin._submit_web_job.await_count == 1
    assert plugin._submit_web_job.await_args.kwargs["base_url"] == (
        "https://site-a.example"
    )
    plugin._archive_generated_image.assert_not_awaited()


def test_submit_web_job_receives_current_site_base_url_when_switching():
    plugin = _direct_plugin(
        [
            _direct_site("第一站", "https://site-a.example", api_key="a-token", priority=1),
            _direct_site("第二站", "https://site-b.example", api_key="b-token", priority=2),
        ]
    )
    plugin._submit_web_job = AsyncMock(
        side_effect=[(None, "http_5xx"), (b"image-from-site-b", "ok")]
    )

    result = asyncio.run(plugin._generate_one("1girl, solo", "custom", "2K竖图", steps=40))

    assert result == (b"image-from-site-b", "ok")
    calls = plugin._submit_web_job.await_args_list
    assert len(calls) == 2
    # 每次提交都带上当前站点的地址与凭据，不能沿用上一站或插件默认值。
    assert [call.kwargs["base_url"] for call in calls] == [
        "https://site-a.example",
        "https://site-b.example",
    ]
    assert [call.kwargs["token"] for call in calls] == ["a-token", "b-token"]
    assert all(call.kwargs["steps"] == 40 for call in calls)
    assert all(call.kwargs["model"] == "nai-diffusion-4-5-full" for call in calls)


# ===== 直连链路的站点级凭据 =====


def test_generate_one_uses_site_token_when_plugin_token_empty():
    """插件级 token 留空时，凭据只配在站点表里的直连站点仍可用。"""

    plugin = _direct_plugin(
        [_direct_site("唯一站", "https://site-a.example", api_key="site-token")],
        token="",
    )
    plugin._submit_web_job = AsyncMock(return_value=(b"site-token-image", "ok"))

    result = asyncio.run(plugin._generate_one("1girl, solo", "custom", "方图", steps=24))

    assert result == (b"site-token-image", "ok")
    assert plugin._submit_web_job.await_count == 1
    call = plugin._submit_web_job.await_args
    # token 取站点自己的；插件配置为空不应覆盖它，也不应提前拦掉整次生成。
    assert call.kwargs["token"] == "site-token"
    assert call.kwargs["base_url"] == "https://site-a.example"
    plugin._archive_generated_image.assert_awaited_once()
    assert plugin._archive_generated_image.await_args.args[0] == b"site-token-image"


def test_generate_one_skips_site_without_token_and_falls_to_next():
    """首个站点没有可用凭据时按换站规则跳过，只向第二个站点发请求。"""

    plugin = _direct_plugin(
        [
            _direct_site("无凭据站", "https://site-a.example", api_key="", priority=1),
            _direct_site("有凭据站", "https://site-b.example", api_key="t2", priority=2),
        ],
        token="",
    )
    plugin._submit_web_job = AsyncMock(return_value=(b"image-from-site-b", "ok"))

    result = asyncio.run(plugin._generate_one("1girl, solo", "custom", "方图", steps=24))

    assert result == (b"image-from-site-b", "ok")
    # 无凭据站点在单站点实现里直接返回 no_token（可换站），因此只有
    # 第二个站点的地址真正被提交过。
    calls = plugin._submit_web_job.await_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["base_url"] == "https://site-b.example"
    assert calls[0].kwargs["token"] == "t2"
    assert [call.kwargs["base_url"] for call in calls] != ["https://site-a.example"]


def test_generate_one_returns_no_token_when_no_candidate_has_credentials():
    """所有候选站点与插件配置都没有 token 时，入口短路且不发任何请求。"""

    plugin = _direct_plugin(
        [
            _direct_site("空凭据甲", "https://site-a.example", api_key="", priority=1),
            _direct_site("空凭据乙", "https://site-b.example", api_key="   ", priority=2),
        ],
        token="",
    )

    result = asyncio.run(plugin._generate_one("1girl, solo", "custom", "方图", steps=24))

    assert result == (None, "no_token")
    # 入口即返回：任务接口一次都没被提交，归档与记账同样没有发生。
    plugin._submit_web_job.assert_not_awaited()
    plugin._archive_generated_image.assert_not_awaited()
    plugin._record_usage_from_parameters.assert_not_awaited()
