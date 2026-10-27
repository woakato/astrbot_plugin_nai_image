import asyncio
import base64
import json
import mimetypes
import re
import shutil
import time
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import aiohttp
import yaml
from aiohttp import web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image as Img
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import error_response, json_response
from astrbot.api.web import request as web_request

if __package__:
    from .command_args import (
        ImageCommandArgumentError,
        normalize_image_size,
        normalize_image_style,
        parse_image_command,
        strip_image_command_prefix,
    )
    from .companion_api import NAIImageCompanionExtensionAPI
    from .grimoire_service import (
        build_grimoire_translation_hint,
        expand_grimoire_macros,
        normalize_model_ver,
        search_grimoire,
    )
    from .pricing import (
        DIRECT_URL_STEPS_MAX,
        TRANSPORT_GET,
        TRANSPORT_WEB_JOBS,
        WEB_JOB_STEPS_MAX,
        direct_url_generation_cost,
        estimate_openai_cost,
        price_table_lines,
        web_job_generation_cost,
    )
    from .pricing import (
        effective_steps as effective_transport_steps,
    )
    from .prompt_processing import (
        TRANSLATE_MODE_AUTO,
        TRANSLATE_MODE_OFF,
        TRANSLATE_MODE_ON,
        extract_mixed_prompt,
        merge_translated_prompt,
        normalize_prompt,
        normalize_translate_mode,
    )
    from .starbridge import (
        StarbridgeError,
        native_error,
        normalize_starbridge_response,
        prepare_starbridge_request,
    )
    from .usage_stats import (
        STATS_FILE_NAME,
        UsageStatsStore,
        format_usage_report,
    )
else:
    from command_args import (
        ImageCommandArgumentError,
        normalize_image_size,
        normalize_image_style,
        parse_image_command,
        strip_image_command_prefix,
    )
    from grimoire_service import (
        build_grimoire_translation_hint,
        expand_grimoire_macros,
        normalize_model_ver,
        search_grimoire,
    )
    from pricing import (
        DIRECT_URL_STEPS_MAX,
        TRANSPORT_GET,
        TRANSPORT_WEB_JOBS,
        WEB_JOB_STEPS_MAX,
        direct_url_generation_cost,
        estimate_openai_cost,
        price_table_lines,
        web_job_generation_cost,
    )
    from pricing import (
        effective_steps as effective_transport_steps,
    )
    from prompt_processing import (
        TRANSLATE_MODE_AUTO,
        TRANSLATE_MODE_OFF,
        TRANSLATE_MODE_ON,
        extract_mixed_prompt,
        merge_translated_prompt,
        normalize_prompt,
        normalize_translate_mode,
    )
    from starbridge import (
        StarbridgeError,
        native_error,
        normalize_starbridge_response,
        prepare_starbridge_request,
    )
    from usage_stats import (
        STATS_FILE_NAME,
        UsageStatsStore,
        format_usage_report,
    )

LOG_TAG = "[NAI-Image]"

# 画风槽位表结构版本。v2 起改为「内置画风预设」形式，旧默认会被自动升级。
_SLOTS_VERSION = 2

# 只在提示词转译开启时有意义的配置项：转译关闭时整块从设置页隐藏，
# 运行时也一律不参与任何请求。AstrBot 的 schema condition 只支持「单值
# 相等」，无法表达"开启 或 自动"，所以由插件按当前转译模式动态改写这些
# 条目的 condition（见 _sync_translate_visibility）。
TRANSLATE_GATED_KEYS = (
    "translate_provider",
    "default_outfit",
    "enable_outfit_cache",
    "outfit_cache_ttl_seconds",
)

_IMAGE_PROMPT_SPEC_PATH = Path(__file__).resolve().parents[1] / "image_prompt_spec.txt"


def _load_image_prompt_spec() -> str:
    """Load the verbatim image prompt specification shared by image workflows.

    Returns:
        The specification text, or an empty string when the bundled resource is unavailable.
    """
    try:
        return _IMAGE_PROMPT_SPEC_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning(f"{LOG_TAG} image prompt spec unavailable: {exc}")
        return ""


IMAGE_PROMPT_SPEC = _load_image_prompt_spec()

IMAGE_GEN_BASE_URL_DEFAULT = "https://nai.sta1n.cn"
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8765
PLUGIN_NAME = "astrbot_plugin_nai_image"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/test_panel"
BOT_REPLY_MODES = {"仅图片", "简洁", "完整"}

_active_plugin: Optional["NAIGenerateImagePlugin"] = None


def get_nai_image_api() -> Any:
    """返回当前插件实例的陪伴直连扩展 API；插件未加载时返回 None。"""
    plugin = _active_plugin
    return getattr(plugin, "extension_api", None) if plugin is not None else None


class _LiteralYamlString(str):
    """标记需要由 YAML 使用 ``|`` 块样式写出的多行字符串。"""

    pass


class _GenerationParametersDumper(yaml.SafeDumper):
    """生图参数专用 Dumper，不修改 PyYAML 全局字符串表示规则。"""

    pass


def _represent_literal_yaml_string(
    dumper: yaml.SafeDumper,
    value: _LiteralYamlString,
):
    """将标记后的字符串表示为 YAML 字面量块，保留真实换行。"""

    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")


_GenerationParametersDumper.add_representer(
    _LiteralYamlString,
    _represent_literal_yaml_string,
)


def _prepare_generation_parameters_for_yaml(value: Any) -> Any:
    """递归标记参数中的多行字符串，仅改变 YAML 的展示格式。

    这里不会修改实际发送给生图接口的数据；同时把不同平台的换行统一为
    ``\n``，保证历史文件跨平台读取时内容稳定。
    """
    if isinstance(value, str) and ("\n" in value or "\r" in value):
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        return _LiteralYamlString(normalized)
    if isinstance(value, dict):
        return {
            key: _prepare_generation_parameters_for_yaml(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_prepare_generation_parameters_for_yaml(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_prepare_generation_parameters_for_yaml(item) for item in value)
    return value


# ==== 试用生成（代码内 XOR 混淆方案）====
# XOR 解密密钥（不是试用密钥本身）
_TRIAL_OBF_KEY = b"nai_plugin_trial_2024_obf_key"
# XOR 加密后的试用密钥（base64 编码）——用 scripts/generate_trial_key.py 生成
# 非明文存储；需轮换时重新生成并发布新版插件
_TRIAL_KEY_ENC = "PTUobj5BRjBcWz0aX1kSDhxofAp6AApbUjIuPCw2"
TRIAL_MAX_USES = 3

# 自然语言 → SD/NAI 标签风格提示词 的转译系统提示
TRANSLATE_SYSTEM_PROMPT = (
    f"{IMAGE_PROMPT_SPEC}\n\n"
    "You translate natural-language descriptions into compact, comma-separated "
    "Stable Diffusion / NovelAI prompt tags. Output ONLY the tags — no "
    "explanations, no markdown, no thinking block, no labels, no preamble.\n"
    "\n"
    "Output format (strict):\n"
    "  - Single line, English, lowercase, tags separated by ', '.\n"
    "  - Each tag = 1-3 words. Noun/adjective form. No articles, no verbs, "
    "no full sentences.\n"
    "  - Tag cap: 25-40 total. Stop once the input is covered; do not pad.\n"
    "\n"
    "Output order (mandatory):\n"
    "  1) Subject tags (1-8): who/what is in the image, anatomy, identity.\n"
    "  2) Action/scene tags (1-8): pose, location, props, lighting, atmosphere.\n"
    "  3) Style tags (0-4): medium, art style, mood.\n"
    "\n"
    "NAI weighting (apply to 3-5 key tags, prefer subject identity & key props):\n"
    "  - 1.2::keyword::  emphasizes; 1.5::keyword::  strong emphasis.\n"
    "  - -1::keyword::  or 0.5::keyword::  suppresses.\n"
    "  - {{keyword}}   ≈ 1.05× boost.\n"
    "\n"
    "STRICT RULES — never violate:\n"
    "  - Do NOT add quality tags (masterpiece, best quality, absurdres, "
    "highly detailed, etc.). Quality and artist tags are injected separately "
    "via an artist/preset parameter; re-adding them here causes duplication "
    "and weight conflicts.\n"
    "  - Do NOT invent visual details not in the input — no inferring "
    "makeup, lighting direction, weather, time-of-day, indoor/outdoor, or "
    "accessories that are not explicitly mentioned. If a concept is "
    "implied by a named object (e.g. 'umbrella' implies 'rainy'), that "
    "counts as in the input; do not extend further.\n"
    "  - Do NOT add aspect-ratio, framing-shape, or size tags — even if "
    "the input mentions 1:1, 方形, square, 横图, portrait, landscape, etc. "
    "These are handled by a separate size parameter; mention them only as "
    "a passive composition tag (e.g. 'half body', 'upper body') never as "
    "an aspect.\n"
    "  - Do NOT add negative-prompt tags like 'no text, no watermark, "
    "no logo' (handled via negative prompt).\n"
    "  - Do NOT output multiple synonymous tags — pick ONE concise "
    "descriptor per concept (e.g. 'urban style' alone, not 'urban style, "
    "contemporary style, stylish ensemble'). Same applies to atmosphere "
    "tags and quality concepts.\n"
    "  - Do NOT translate character names or transliterate; keep canonical "
    "form (e.g. 'muelsyse(Arknights)' if it appears in input, stays as-is).\n"
    "\n"
    "Examples (note: NO quality tags, weighted syntax on key descriptors):\n"
    "\n"
    "Input: 孤独的少女站在月光下的废墟里，穿着黑色连衣裙\n"
    "Output: 1girl, solo, 1.2::black dress::, standing, ruins, 1.3::moonlight::, night, dramatic lighting, full body\n"
    "\n"
    "Input: 一个开朗的动漫男孩拿武士刀，日落海滩，动态姿势\n"
    "Output: 1boy, 1.1::katana::, happy expression, beach, 1.3::sunset::, 1.2::dynamic pose::, ocean waves, wind, full body\n"
    "\n"
    "Input: modern living room, a cat sleeping on sofa, oil painting style\n"
    "Output: indoor, modern living room, cat, sleeping, sofa, oil painting, soft lighting, cozy atmosphere, 0.5::cluttered::, 1.2::oil painting style::\n"
    "\n"
    "Input: 镜前自拍穿搭，银色长发，戴墨镜\n"
    "Output: 1girl, mirror selfie, half body, looking at viewer, modern fashion, 1.1::sunglasses::, 0.8::casual outfit::, indoor, soft lighting"
)

IMAGE_STYLES = {
    "vertical": "韩漫小清新风",
    "comicDoujin": "漫画同人风",
    "r18": "2.5D唯美风",
    "lolita25d": "2.5D唯美风（萝）",
    "anime": "本子里番风",
    "galgame": "GalGame风",
    "custom": "自定义",
}

IMAGE_SIZES = {
    "竖图": "portrait",
    "横图": "landscape",
    "方图": "square",
    "2K竖图": "2k_portrait",
    "2K横图": "2k_landscape",
    "2K方图": "2k_square",
    "4K竖图": "4k_portrait",
    "4K横图": "4k_landscape",
    "4K方图": "4k_square",
}

# OpenAI 兼容接口下 LLM 工具可选的像素分辨率（对齐接口文档 §12.1 常用尺寸）：
# 宽高为 64 的倍数、最大边 1920、面积不超过 3686400。OpenAI 兼容模式的尺寸
# 直接用分辨率表示，不再经由「竖图/2K/4K」等 GET 分档名换算（分档名与真实
# 分辨率在 4K 档会静默降级，容易对不上）。
OPENAI_TOOL_SIZES = (
    "640x640",
    "832x1216",
    "1216x832",
    "1024x1024",
    "1024x1536",
    "1536x1024",
    "1472x1472",
    "1088x1920",
    "1920x1088",
)

# 像素分辨率写法 "1024x1536"（含 ×/* 变体）。
_PIXEL_SIZE_RE = re.compile(r"^\s*(\d{1,5})\s*[x×*]\s*(\d{1,5})\s*$", re.IGNORECASE)

# OpenAI 兼容接口的模型列表（对齐接口文档 §9）。
OPENAI_COMPATIBLE_MODELS = [
    "nai-diffusion-5-full",
    "nai-diffusion-5-curated",
    "nai-diffusion-4-5-full",
    "nai-diffusion-4-5-curated",
    "nai-diffusion-4-full",
    "nai-diffusion-4-curated-preview",
    "nai-diffusion-3",
    "nai-diffusion-furry-3",
]

# director-tools 图片处理动作（对齐接口文档 §8）。
OPENAI_DIRECTOR_ACTIONS = {
    "bg-removal": "去除背景",
    "lineart": "提取线稿",
    "sketch": "生成草图",
    "colorize": "线稿上色",
    "emotion": "调整人物表情",
    "declutter": "清理画面元素",
}

# 参考图单次请求张数上限（§5.2）：vibe 的 reference_image_multiple 与
# 精准参考的 director_reference_* 数组均按此收敛。
OPENAI_MAX_REFERENCE_IMAGES = 8

# 支持精准参考（director_reference_*）的模型集合：NAI 4.5 与 NAI 5 全系
# 实测均支持精准参考。其他模型携带精准参考字段会被服务端拒绝，仍自动切换
# 为 nai-diffusion-4-5-full。
OPENAI_DIRECTOR_MODELS = {
    "nai-diffusion-4-5-full",
    "nai-diffusion-4-5-curated",
    "nai45",
    "nai45-curated",
    "nai-diffusion-5-full",
    "nai-diffusion-5-curated",
    "nai5",
    "nai5-curated",
}

# 精准参考 base_caption 仅允许的枚举值（新文档 §8.4）。
OPENAI_DIRECTOR_CAPTIONS = ("character", "style", "character&style")

# OpenAI 兼容生图允许自动重试的状态码（对齐接口文档 §14）；
# 400/401/403 一律不重试，重试间隔为 2/4/8 秒指数退避。
OPENAI_RETRYABLE_STATUS = {408, 429, 502, 503, 504}
OPENAI_RETRY_DELAYS = (2.0, 4.0, 8.0)

# 任务接口（POST /api/web/jobs）轮询参数：提交/单次轮询/总等待的秒数上限。
# 总等待上限自 v2.7.3 起跟随「请求超时」配置（request_timeout，默认 180s），
# 与 GET 直链生图共用；上游 /generate 60s 必回，任务接口排队 + 4K 生成可能
# 更久，超过配置上限仍按超时处理（上游失败会退点）。
WEB_JOB_SUBMIT_TIMEOUT = 30
WEB_JOB_POLL_INTERVAL = 2.0
WEB_JOB_POLL_REQUEST_TIMEOUT = 15
WEB_JOB_TOTAL_TIMEOUT = 180

# 中转网关会把 NovelAI 上游的瞬时故障（如 vibe 编码"服务繁忙"）包装成
# HTTP 400（type=bad_response_status_code），错误文案本身提示"请稍后重试"；
# 错误信息命中下列任一关键词时，即便状态码不可重试也按可重试处理。
OPENAI_TRANSIENT_MARKERS = (
    "服务繁忙",
    "请稍后重试",
    "稍后再试",
    "try again later",
    "service busy",
    "temporarily unavailable",
    "too many requests",
    "overloaded",
)

DEFAULT_ARTISTS = {
    "vertical": "[[[artist:dishwasher1910]]], {{yd_(orange_maru)}}, [artist:ciloranko], [artist:sho_(sho_lwlw)], [ningen mame], year 2024,",
    "comicDoujin": (
        "(masterpiece:1.3), (best quality:1.2), (highres), (absurdres),\n"
        "(extremely detailed illustration:1.2), (anime style:1.1),\n\n"
        "(artist:feipin zhanshi:1.0), (artist:nlebo-hentai:0.9), (artist:sos adult:0.85),\n"
        "(artist:hews:0.4),\n\n"
        "(detailed skin texture:1.15), (glossy skin:1.1),\n"
        "(thick lineart:1.1), (high contrast:1.15),\n"
        "(vivid colors:1.1), (detailed shading:1.15),\n"
        "(warm color palette:1.05),\n"
        "(cute face:1.1), (detailed eyes:1.15), (detailed face:1.1),"
    ),
    "r18": (
        "0.9::misaka_12003-gou ::, dino_(dinoartforame), wanke, liduke, year 2025, realistic, 4k, -2::green ::, "
        "textless version, The image is highly intricate finished drawn. "
        "Only the character's face is in anime style, but their body is in realistic style. "
        "1.35::A highly finished photo-style artwork that has lively color, graphic texture, realistic skin surface, "
        "and lifelike flesh with little obliques::. 1.63::photorealistic::, 1.63::photo(medium)::, \n"
        "20::best quality, absurdres, very aesthetic, detailed, masterpiece::,, very aesthetic, masterpiece, no text,"
    ),
    "lolita25d": (
        "0.9::misaka_12003-gou & dino, rurudo,  mignon,wanke & liduk::, year 2025, realistic, 4k, -2::green ::, "
        "textless version, The image is highly intricate finished drawn. "
        "Only the character's face is in anime style, but their body is in realistic style. "
        "1.35::A highly finished photo-style artwork that has lively color, graphic texture, realistic skin surface, "
        "and lifelike flesh with little obliques::. 1.63::photorealistic::, 1.63::photo(medium)::, \n"
        "20::best quality, absurdres, very aesthetic, detailed, masterpiece::,, very aesthetic, masterpiece, no text,"
    ),
    "anime": (
        "1.4::asanagi::,{{{{{artist:asanagi}}}}},1.2::xiaoluo_xl::,1.3::Artist: misaka_12003-gou::,"
        "1.2::Artist:shexyo::,0.7::Artist:b.sa_(bbbs)::,1::Artist:qiandaiyiyu::,"
        "1.05::artist:natedecock::,1.05::artist:kunaboto::,0.75::artist:kandata_nijou::,"
        "1.05::artist:zer0.zer0 ::,1.05::artist:jasony::,0.75::misaka_12003-gou ::, "
        "dino_(dinoartforame), wanke, liduke, year 2025, realistic, 4k, -2::green ::, "
        "{textless version, The image is highly intricate finished drawn,write realistically,true to life}, "
        "1.35::A highly finished photo-style artwork that has lively color, graphic texture, "
        "realistic skin surface, and lifelike flesh with little obliques::, "
        "1.63::photorealistic::,3::age slider::,1.63::photo(medium)::, "
        "2::best quality, absurdres, very aesthetic, detailed, masterpiece::,-4::Muscle definition, abs::"
    ),
    "galgame": (
        "artist:ningen_mame,, noyu_(noyu23386566),, toosaka asagi,, location, "
        "20::best quality, absurdres, very aesthetic, detailed, masterpiece::,:,, "
        "very aesthetic, masterpiece, no text,"
    ),
}

DEFAULT_NEGATIVE = (
    "{{bad anatomy}},{bad feet},bad hands,{{{bad proportions}}},{blurry},cloned face,cropped,"
    "{{{deformed}}},{{{disfigured}}},error,{{{extra arms}}},{extra digit},{{{extra legs}}},extra limbs,"
    "{{extra limbs}},{fewer digits},{{{fused fingers}}},gross proportions,ink eyes,ink hair,"
    "jpeg artifacts,{{{{long neck}}}},low quality,{malformed limbs},{{missing arms}},{missing fingers},"
    "{{missing legs}},{{{more than 2 nipples}}},mutated hands,{{{mutation}}},normal quality,lowres,"
    "{{poorly drawn face}},{{poorly drawn hands}},reen eyes,signature,text,{{too many fingers}},"
    "{{{ugly}}},username,uta,watermark,worst quality,{{{more than 2 legs}}},"
    "awkward hand sign,weird hand gesture,contorted hand,unnatural finger pose,deformed hand gesture,"
    "{shaka},{hang loose},{{rock on}},{shaka sign}"
)


def _summarize_error_body(body: str) -> str:
    """从上游错误正文提取一句话摘要（优先 JSON 的 error/message 字段）。"""

    body = (body or "").strip()
    if not body:
        return ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            for key in ("error", "message"):
                value = data.get(key)
                if value:
                    return str(value)[:200].replace("\n", " ").strip()
    except (ValueError, TypeError):
        pass
    return body[:200].replace("\n", " ").strip()


def _empty_usage_payload(enabled: bool, days: int) -> dict[str, Any]:
    """统计不可用（数据目录不可达 / 尚未初始化）时的空快照。"""

    empty_row = {
        "date": "",
        "cost": 0,
        "count": 0,
        "failed": 0,
        "upstream_cost": 0,
        "upstream_events": 0,
    }
    empty_totals = {
        "days_in_window": 0,
        "active_days": 0,
        "cost": 0,
        "count": 0,
        "failed": 0,
        "upstream_cost": 0,
        "upstream_events": 0,
        "avg_cost": 0.0,
        "peak_cost": 0,
        "peak_date": "",
    }
    return {
        "enabled": enabled,
        "days": days,
        "today": empty_row,
        "window": dict(empty_totals),
        "overall": dict(empty_totals),
        "series": [],
        "upstream": {},
        "upstream_enabled": False,
        "breakdown": {"model": [], "size": [], "transport": []},
        "first_day": "",
        "is_empty": True,
    }


def _format_generate_error(reason: str) -> str:
    """把生图方法返回的 reason 翻译成给用户的中文报错。"""
    _map = {
        "no_token": "❌ 插件未配置 image_gen_key，请先在插件管理面板填入 token。",
        "no_session": "❌ 插件 session 未初始化，请重载插件。",
        "openai_not_configured": (
            "❌ 未配置 OpenAI 兼容生图接口地址（openai_api_base_url），"
            "请先在插件设置中填写。"
        ),
        "web_job_failed": (
            "🚫 上游生成任务失败（失败任务的点数会自动退回），请查看日志或稍后重试。"
        ),
        "timeout": "⏱ 生图超时。上游可能仍在生成（可能已扣费），请稍后手动重试；若频繁超时可在设置中调大「请求超时」。",
        "empty_response": "📭 上游返回 200 但内容为空，可能是接口限流或临时异常。",
        "invalid_response": "📭 上游响应不是有效 JSON，请检查接口地址是否正确或查看日志。",
        "no_reference_image": "🖼 该操作需要输入一张源图片（参考图/待处理图），请先提供图片。",
        "exception": "💥 生图过程发生未捕获异常，请查看 AstrBot 日志获取详情。",
    }
    # 精确匹配
    if reason in _map:
        return _map[reason]
    if reason.startswith("starbridge | "):
        return "🚫 星桥：" + reason.split(" | ", 1)[1]
    # 精准参考（§6）被上游拒绝：参数已按文档格式提交，多为能力未开通
    if reason.startswith("director_ref_rejected"):
        detail = reason.split(" | ", 1)[1] if " | " in reason else ""
        msg = (
            "🚫 精准参考（director）被上游拒绝：参数已按接口文档 §6 格式提交，"
            "中转/上游仍判定参数校验失败。这通常表示当前 Key 未开通精准参考能力，"
            "或该中转站的 director 转换暂不兼容（文档 §6：若模型不支持该能力，"
            "服务会返回参数错误或模型能力错误）。建议联系服务方确认是否已开通"
            "精准参考；在此之前可改用 vibe 参考或 img2img 模式。"
        )
        if detail:
            msg += f"\n详情: {detail}"
        return msg
    # OpenAI 兼容模式的具体状态码（形如 "http_400 | invalid image size"）
    _status_match = re.match(r"http_(\d{3})", reason)
    if _status_match:
        status = int(_status_match.group(1))
        detail = reason.split(" | ", 1)[1] if " | " in reason else ""
        _status_hints = {
            400: "参数错误（JSON / 模型 / 尺寸 / 步数 / 图片参数），请按提示修正字段",
            401: "API Key 无效、缺失或过期，请检查 openai_api_key",
            403: "当前 Key 没有使用权限，请联系服务管理员",
            408: "请求超时",
            429: "请求过于频繁或余额不足，请稍后重试或充值",
            500: "服务内部处理失败",
            502: "暂时无法完成生成，请稍后重试",
            503: "暂时无法完成生成，请稍后重试",
            504: "请求超时",
        }
        hint = _status_hints.get(
            status,
            "4xx 参数或鉴权错误" if status < 500 else "5xx 服务端异常",
        )
        suffix = f"\n详情: {detail}" if detail else ""
        return f"🚫 上游返回 HTTP {status}：{hint}。{suffix}"
    # 前缀匹配（传统直连模式 reason，如 "http_4xx (HTTP 400): ..."）
    if reason.startswith("http_4xx"):
        return (
            "🚫 上游返回 4xx。常见原因：密钥无效、提示词含敏感词、或参数不合法。\n"
            + reason
        )
    if reason.startswith("http_5xx"):
        return "🔥 上游返回 5xx（服务端异常/繁忙）。\n" + reason
    if reason.startswith("http_other"):
        return "⚠️ 上游返回非预期状态码。\n" + reason
    return f"❓ 生图失败（原因: {reason}）"


# ==== Outfit 缓存池：具体服装词 / 换装动词 / 抽出片段 ====

# 命中即视为"具体服装"的关键词（中文为主，覆盖常见服饰品类）
_OUTFIT_CONCRETE_TOKENS = (
    "裙",
    "裤",
    "衣",
    "上衣",
    "下装",
    "外套",
    "衬衫",
    "T恤",
    "罩衫",
    "卫衣",
    "汉服",
    "校服",
    "旗袍",
    "和服",
    "西装",
    "风衣",
    "夹克",
    "毛衣",
    "针织衫",
    "连衣裙",
    "半裙",
    "短裙",
    "长裙",
    "牛仔裤",
    "阔腿裤",
    "喇叭裤",
    "运动裤",
    "皮衣",
    "羽绒服",
    "棉衣",
    "大衣",
    "靴",
    "鞋",
    "袜",
    "丝袜",
    "帽",
    "围巾",
    "手套",
    "披风",
    "斗篷",
    "JK",
    "jk",
    "洛丽塔",
    "lolita",
)

# 命中即视为"换装动作"的关键词（组合型，避免裸"穿"/"换"误判）
_OUTFIT_CHANGE_KEYWORDS = (
    "换上新",
    "换了新",
    "换上",
    "今天穿",
    "今晚穿",
    "早上穿",
    "刚换上",
    "新换了",
    "换了件",
    "换了条",
    "穿上了",
)


def _has_specific_outfit(prompt: str) -> bool:
    """源 prompt 中是否包含具体服装词。"""
    return any(tok in prompt for tok in _OUTFIT_CONCRETE_TOKENS)


def _detect_outfit_change(prompt: str) -> bool:
    """源 prompt 中是否出现换装动作关键词。"""
    return any(kw in prompt for kw in _OUTFIT_CHANGE_KEYWORDS)


def _extract_outfit_excerpt(prompt: str, max_chars: int = 200) -> str:
    """从源 prompt 中抽出服装相关片段（截第一个具体词或换装词附近的小段文字）。"""
    # 优先匹配具体服装词，否则退到换装动词
    candidates = []
    for tok in _OUTFIT_CONCRETE_TOKENS:
        i = prompt.find(tok)
        if i >= 0:
            candidates.append((i, tok))
    for kw in _OUTFIT_CHANGE_KEYWORDS:
        i = prompt.find(kw)
        if i >= 0:
            candidates.append((i, kw))
    if not candidates:
        return ""
    candidates.sort()
    idx, marker = candidates[0]
    start = max(0, idx - 30)
    end = min(len(prompt), idx + len(marker) + max_chars)
    excerpt = prompt[start:end].strip()
    # 找离片段中段最近的句子边界来截断
    cut_at = -1
    for sep in ("。", "！", "？", "；", "\n", "，", ",", ";", ":"):
        pos = excerpt.find(sep, len(marker) + 20)
        if pos > 0 and (cut_at < 0 or pos < cut_at):
            cut_at = pos
    if cut_at > 0:
        excerpt = excerpt[: cut_at + 1]
    return excerpt.strip() or prompt[idx:end].strip()


def migrate_legacy_translate_config(config: dict) -> str | None:
    """检测旧版布尔 enable_translate，归一化为字符串三态并写回 dict。

    返回迁移后的字符串值；未发生迁移（已是字符串或缺失）返回 None。
    """
    legacy = config.get("enable_translate")
    if isinstance(legacy, bool):
        normalized = normalize_translate_mode(legacy)
        config["enable_translate"] = normalized
        return normalized
    return None


def migrate_invalid_call_mode(config: dict) -> str | None:
    """归一化非法或被污染的 call_mode 并写回 dict。

    call_mode 为 "openai" 但未配置 OpenAI 兼容接口地址时（新版本默认值
    被污染的典型表现），回退为 "direct"，避免生图请求打到空地址；其余
    非法值同样回退 "direct"，与运行时归一化行为一致。已完整配置 OpenAI
    站点的配置不受影响。

    返回迁移后的字符串值；无需迁移返回 None。
    """
    mode = str(config.get("call_mode") or "").strip()
    if mode == "openai" and not str(config.get("openai_api_base_url") or "").strip():
        config["call_mode"] = "direct"
        return "direct"
    if mode not in ("direct", "openai"):
        config["call_mode"] = "direct"
        return "direct"
    return None


@register(
    "astrbot_plugin_nai_image",
    "缪缪的小水泡",
    "基于 nai.sta1n.cn 的 NovelAI 生图插件",
    "2.9.0",
)
class NAIGenerateImagePlugin(Star):
    def __init__(self, context: Context, config: dict):
        global _active_plugin
        super().__init__(context, config)
        logger.info(f"{LOG_TAG} [init] 插件实例化开始")
        logger.debug(f"{LOG_TAG} [init] config keys: {list(config.keys())}")

        self.base_url: str = (
            config.get("base_url") or IMAGE_GEN_BASE_URL_DEFAULT
        ).strip() or IMAGE_GEN_BASE_URL_DEFAULT
        self.image_gen_key: str = (config.get("image_gen_key") or "").strip()
        self.image_style: str = (
            normalize_image_style(config.get("image_style")) or "vertical"
        )
        self.image_size: str = normalize_image_size(config.get("image_size")) or "竖图"
        try:
            self.image_count: int = max(1, min(6, int(config.get("image_count") or 2)))
        except (TypeError, ValueError):
            self.image_count = 2
        self.bot_reply_mode: str = config.get("bot_reply_mode") or "完整"
        if self.bot_reply_mode not in BOT_REPLY_MODES:
            self.bot_reply_mode = "完整"
        self.save_image_history: bool = bool(config.get("save_image_history", False))
        self.save_generation_parameters: bool = bool(
            config.get("save_generation_parameters", False)
        )
        try:
            history_limit = config.get("image_history_limit")
            self.image_history_limit: int = max(
                0,
                int(history_limit)
                if history_limit is not None and history_limit != ""
                else 0,
            )
        except (TypeError, ValueError):
            self.image_history_limit = 0
        self._image_history_dir: Path | None = None
        self._image_history_lock = asyncio.Lock()
        self.custom_artists: str = config.get("custom_artists") or ""
        self.model: str = config.get("model") or "nai-diffusion-4-5-full"
        # 正式生图启用的槽位（槽位表本身存在插件数据目录的 artist_slots.json）。
        self.active_artist_slot: str = str(
            config.get("active_artist_slot") or "1"
        ).strip() or "1"
        self._slots_cache: list[dict[str, Any]] | None = None
        self._slots_cache_mtime: float = 0.0
        try:
            self.steps: int = max(1, min(50, int(config.get("steps") or 24)))
        except (TypeError, ValueError):
            self.steps = 24
        try:
            # scale 0 是有效值，不能用 `config.get("scale") or 默认值` 读取；
            # v2.7.3 起支持小数（float）。
            raw_scale = config.get("scale")
            self.scale: float = max(
                0.0,
                min(
                    20.0,
                    float(raw_scale)
                    if raw_scale is not None and raw_scale != ""
                    else 6.0,
                ),
            )
        except (TypeError, ValueError):
            self.scale = 6.0
        try:
            cfg = config.get("cfg")
            # CFG 的 0 是有效值，不能用 `config.get("cfg") or 默认值` 读取。
            # 上游 providers.js 将 cfg 钳制在 0-1（旧配置里的 7.0 会被钳到 1.0），
            # 这里同步钳制，使面板显示值与实际生效值一致。
            raw_cfg = float(cfg) if cfg is not None and cfg != "" else 1.0
            self.cfg_value: float = max(0.0, min(raw_cfg, 1.0))
        except (TypeError, ValueError):
            self.cfg_value = 1.0
        # ddim 仅适用于 V3 模型，本插件仅支持 V4.5/V5（v2.7.3 起选项里已移除，
        # 旧配置残留的 ddim 归一化回默认采样器）。
        _sampler_cfg = (config.get("sampler") or "").strip()
        if _sampler_cfg == "ddim":
            _sampler_cfg = ""
        self.sampler: str = _sampler_cfg or "k_dpmpp_2m_sde"
        # 噪声调度固定 karras（v2.7.2 起移除该配置项）：直连仅支持
        # V4.5/V5，karras 为通用推荐值；--noise 指令参数仍可临时覆盖。
        self.noise_schedule: str = "karras"
        neg = config.get("negative")
        self.negative: str = neg if neg else DEFAULT_NEGATIVE
        self.enable_template: bool = bool(config.get("enable_template", True))
        self.character_preset: str = (config.get("character_preset") or "").strip()
        # 调用模式：direct（传统 GET nai.sta1n.cn）/ openai（OpenAI 兼容站点）。
        # 先归一化被污染/非法的 call_mode（如 openai 但未配置接口地址），再读取；
        # 迁移结果在 init 末尾统一持久化。
        healed_call_mode = migrate_invalid_call_mode(config)
        self.call_mode: str = (config.get("call_mode") or "direct").strip()
        if self.call_mode not in ("direct", "openai"):
            self.call_mode = "direct"
        # 聊天回复与测试面板是否展示「预计消耗点数」。
        self.show_cost_estimate: bool = bool(config.get("show_cost_estimate", True))
        # 点数用量统计：按日期累计每次成功出图的点数，供 /usage 与面板图表读取。
        self.enable_usage_stats: bool = bool(config.get("enable_usage_stats", True))
        self._usage_stats_lock = asyncio.Lock()
        self._usage_stats: UsageStatsStore | None = None
        # 上游余额扣费同步：仅传统 GET 直连模式可用，定时查询密钥余额，
        # 把两次查询之间的余额减少量记为「上游实际扣费」。
        self.enable_upstream_billing_sync: bool = bool(
            config.get("enable_upstream_billing_sync", False)
        )
        try:
            self.upstream_billing_sync_interval: int = max(
                1,
                min(
                    1440,
                    int(config.get("upstream_billing_sync_interval") or 10),
                ),
            )
        except (TypeError, ValueError):
            self.upstream_billing_sync_interval = 10
        self._upstream_billing_task: asyncio.Task | None = None
        self._upstream_billing_last_error = ""
        # OpenAI 兼容格式（可配置的第三方 OpenAI 兼容生图站点）
        self.openai_api_base_url: str = (
            (config.get("openai_api_base_url") or "").strip().rstrip("/")
        )
        self.openai_api_key: str = (config.get("openai_api_key") or "").strip()
        # Explicit opt-in: existing configurations retain their original wire format.
        self.openai_api_profile: str = (
            config.get("openai_api_profile") or "compatible"
        ).strip().casefold()
        # model 为接口必填字段，留空时回退文档推荐的 nai-diffusion-5-full。
        self.openai_api_model: str = (
            config.get("openai_api_model") or "nai-diffusion-5-full"
        ).strip() or "nai-diffusion-5-full"
        # 参考图模式：vibe（风格参考，走 generations 的 reference_* 参数）、
        # img2img（图生图，走 /v1/images/edits）或 director（精准参考，走
        # generations 的 director_reference_* 参数，§6）。
        self.openai_reference_mode: str = (
            (config.get("openai_reference_mode") or "vibe").strip().casefold()
        )
        if self.openai_reference_mode not in ("img2img", "vibe", "director"):
            self.openai_reference_mode = "vibe"
        # 精准参考描述（新文档 §8.4 的 base_caption），仅三个枚举值合法
        self.openai_director_caption: str = str(
            config.get("openai_director_caption") or "character&style"
        ).strip()
        if self.openai_director_caption not in OPENAI_DIRECTOR_CAPTIONS:
            self.openai_director_caption = "character&style"

        # 参考图默认权重（§5/§8）：聊天指令、LLM 工具与陪伴联动等未显式
        # 给逐图强度时使用；面板逐图设置优先于这些默认值。
        def _load_ref_strength(key: str, default: float) -> float:
            try:
                value = config.get(key)
                return (
                    round(max(0.0, min(1.0, float(value))), 2)
                    if value not in (None, "")
                    else default
                )
            except (TypeError, ValueError):
                return default

        self.openai_vibe_strength: float = _load_ref_strength(
            "openai_vibe_strength", 0.6
        )
        self.openai_director_strength: float = _load_ref_strength(
            "openai_director_strength", 1.0
        )
        self.openai_director_secondary_strength: float = _load_ref_strength(
            "openai_director_secondary_strength", 0.5
        )
        # 精准参考兜底参考图：director 模式下请求未携带参考图（聊天指令/陪伴
        # 联动等场景）时，取此处第一个存在的文件。配置值为文件路径列表。
        _fb_cfg = config.get("openai_director_fallback_images") or []
        if isinstance(_fb_cfg, str):
            _fb_cfg = [_fb_cfg]
        self.openai_director_fallback_images: list[str] = [
            str(p).strip() for p in _fb_cfg if str(p or "").strip()
        ]
        # 随机种子：-1 表示每次随机（不发送 seed 字段）。
        try:
            _seed_cfg = config.get("openai_seed")
            self.openai_seed: int = (
                int(_seed_cfg) if _seed_cfg not in (None, "") else -1
            )
        except (TypeError, ValueError):
            self.openai_seed = -1
        try:
            # v2.7.3 起「请求超时」两种调用模式共用；兼容 v2.7.2 及更早的
            # openai_timeout 旧键，旧值自动迁移，无需用户重新配置。
            _timeout_cfg = config.get("request_timeout") or config.get("openai_timeout")
            self.request_timeout: int = max(30, min(600, int(_timeout_cfg or 180)))
        except (TypeError, ValueError):
            self.request_timeout = 180
        try:
            _retries_cfg = config.get("openai_max_retries")
            self.openai_max_retries: int = max(
                0, min(3, int(_retries_cfg) if _retries_cfg not in (None, "") else 2)
            )
        except (TypeError, ValueError):
            self.openai_max_retries = 2
        self._session: aiohttp.ClientSession | None = None
        self.proxy_runner: web.AppRunner | None = None
        try:
            self.proxy_port: int = max(
                1024, min(65535, int(config.get("proxy_port") or PROXY_PORT))
            )
        except (TypeError, ValueError):
            self.proxy_port = PROXY_PORT
        # 旧配置迁移：v2.2.4 及更早版本的布尔 enable_translate、被污染或非法
        # 的 call_mode（如 openai 但未配置接口地址）。保留旧值会导致 dashboard
        # 保存配置时类型校验失败或生图请求打到空地址，这里加载时归一化并尝试
        # 写回磁盘；写回失败不影响本次运行。
        if (
            migrate_legacy_translate_config(config) is not None
            or healed_call_mode is not None
        ):
            try:
                self._persist_config_migrations()
            except Exception as exc:
                logger.warning(f"{LOG_TAG} 插件配置迁移写回失败: {exc}")
        self.translate_mode: str = normalize_translate_mode(
            config.get("enable_translate", TRANSLATE_MODE_OFF)
        )
        # 保留旧版布尔属性，避免仍读取 enable_translate 的外部联动失效。
        self.enable_translate: bool = self.translate_mode != TRANSLATE_MODE_OFF
        self.translate_provider: str = (config.get("translate_provider") or "").strip()
        self.interrogate_provider: str = (
            config.get("interrogate_provider") or ""
        ).strip()
        try:
            self.interrogate_max_tokens: int = max(
                128, min(4096, int(config.get("interrogate_max_tokens") or 700))
            )
        except (TypeError, ValueError):
            self.interrogate_max_tokens = 700

        # ==== Outfit 缓存池配置 ====
        self.default_outfit: str = (config.get("default_outfit") or "").strip()
        # 「启用服装缓存池」总开关：关闭后不写入也不读取缓存（ttl=0 亦同）。
        self.enable_outfit_cache: bool = bool(config.get("enable_outfit_cache", True))
        try:
            self.outfit_cache_ttl_seconds: int = max(
                0, min(86400, int(config.get("outfit_cache_ttl_seconds") or 3600))
            )
        except (TypeError, ValueError):
            self.outfit_cache_ttl_seconds = 3600
        # 单槽位 outfit 缓存：纯内存，重载插件即清空。
        self.outfit_cache_text: str | None = None
        self.outfit_cache_expires_at: float | None = None

        # 读取配置是否启用自主生图工具
        self.enable_llm_tool: bool = bool(config.get("enable_llm_tool", False))
        # 「开启画风参数」：控制 LLM 生图工具是否向模型暴露 style 参数；
        # 关闭时工具自动使用「默认风格」设置里选好的画风。
        self.enable_tool_style: bool = bool(config.get("enable_tool_style", True))
        # ==== 增强功能开关（一律默认关闭，不改变升级前的任何行为）====
        # 多角色块：开启后工具才向模型暴露 char1_prompt / char2_prompt。
        self.enable_character_blocks: bool = bool(
            config.get("enable_character_blocks", False)
        )
        # 法典词库：开启后才做检索、宏展开与转译参考注入。
        self.enable_grimoire: bool = bool(config.get("enable_grimoire", False))
        # 画风槽位：开启后槽位绑定的模型/画师串才覆盖原来的配置。
        self.enable_artist_slots: bool = bool(
            config.get("enable_artist_slots", False)
        )

        # ==== 陪伴插件直连（extension API）与本地代理开关 ====
        self.enable_companion_link: bool = bool(
            config.get("enable_companion_link", True)
        )
        self.companion_prompt_format: str = (
            config.get("companion_prompt_format") or "自然语言模式（en）"
        ).strip()
        if (
            "nai" not in self.companion_prompt_format.casefold()
            and "自然语言" not in self.companion_prompt_format
        ):
            self.companion_prompt_format = "自然语言模式（en）"
        try:
            self.companion_image_retention_days: int = max(
                0, min(3650, int(config.get("companion_image_retention_days") or 30))
            )
        except (TypeError, ValueError):
            self.companion_image_retention_days = 30
        # 「绕过系统代理直连生图站」默认开启：请求 nai.sta1n.cn 时忽略
        # 系统/环境代理强制直连，避免梯子代理出口导致连不上生图站。
        self.bypass_system_proxy: bool = bool(config.get("bypass_system_proxy", True))
        self.enable_proxy: bool = bool(config.get("enable_proxy", False))
        # 直连与本地代理二选一：旧版或手改配置可能两者同时开启，此时以陪伴
        # 直连为准，自动关闭本地代理并写回，保证落盘配置始终互斥。
        if self.enable_companion_link and self.enable_proxy:
            self.enable_proxy = False
            config["enable_proxy"] = False
            _save_config = getattr(config, "save_config", None)
            if callable(_save_config):
                _save_config()
            logger.info(
                f"{LOG_TAG} [init] 检测到陪伴直连与本地代理同时开启，"
                f"已按二选一规则自动关闭本地代理（enable_proxy=false）并写回配置"
            )
        # 陪伴系列插件通过 get_nai_image_api() / extension_api 直连本插件。
        self.extension_api = NAIImageCompanionExtensionAPI(self)
        _active_plugin = self

        # ==== 试用生成状态 ====
        self._trial_key: str | None = None  # 代码内解密，仅存内存
        self._trial_usage_count: int = 0  # 本地文件追踪
        self._trial_usage_file: str | None = None

        logger.info(
            f"{LOG_TAG} [init] 配置加载完成 | "
            f"token={'已配置' if self.image_gen_key else '未配置'} | "
            f"base_url={self.base_url} | "
            f"style={self.image_style} | size={self.image_size} | "
            f"count={self.image_count} reply={self.bot_reply_mode} | model={self.model} | "
            f"steps={self.steps} scale={self.scale} cfg={self.cfg_value} | "
            f"history={'ON' if self.save_image_history else 'OFF'} "
            f"parameters={'ON' if self.save_generation_parameters else 'OFF'} "
            f"limit={self.image_history_limit} | "
            f"template={'启用' if self.enable_template and self.character_preset else '未启用'} | "
            f"translate={self.translate_mode} "
            f"provider='{self.translate_provider or '默认'}' | "
            f"outfit: default={'已设' if self.default_outfit else '未设'} "
            f"cache={'ON' if self.enable_outfit_cache and self.outfit_cache_ttl_seconds > 0 else 'OFF'} "
            f"cache_ttl={self.outfit_cache_ttl_seconds}s | "
            f"companion_link={'ON' if self.enable_companion_link else 'OFF'} "
            f"companion_format={self.companion_prompt_format} "
            f"companion_retention={self.companion_image_retention_days}d | "
            f"proxy={'ON' if self.enable_proxy else 'OFF'} "
            f"bypass_system_proxy={'ON' if self.bypass_system_proxy else 'OFF'} "
            f"proxy_port={self.proxy_port} | "
            f"usage_stats={'ON' if self.enable_usage_stats else 'OFF'} "
            f"billing_sync={'ON' if self.enable_upstream_billing_sync else 'OFF'}"
            f"({self.upstream_billing_sync_interval}m) | "
            f"call_mode={self.call_mode} "
            f"openai: url={self.openai_api_base_url or '(未配置)'} "
            f"key={'已配置' if self.openai_api_key else '未配置'} "
            f"model={self.openai_api_model} "
            f"ref_mode={self.openai_reference_mode} seed={self.openai_seed} "
            f"timeout={self.request_timeout}s retries={self.openai_max_retries}"
        )
        # 按加载后的配置刷新 LLM 生图工具的参数 schema（画风开关 / 尺寸格式）。
        # 工具由装饰器在模块导入时注册，实例化发生在其后，此时改写即时生效。
        self._refresh_llm_tool_schema()

    def _build_full_prompt(
        self,
        user_prompt: str,
        *,
        enable_template: bool | None = None,
    ) -> str:
        """按单次覆盖或全局配置决定是否在用户提示词前拼接角色模板。"""

        template_enabled = (
            self.enable_template if enable_template is None else enable_template
        )
        if not template_enabled or not self.character_preset:
            return user_prompt.strip()
        return f"{self.character_preset}, {user_prompt.strip()}"

    @staticmethod
    def _image_history_extension(img_bytes: bytes) -> str:
        """根据文件头判断图片扩展名，不依赖上游响应的 Content-Type。"""

        if img_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if img_bytes.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if (
            len(img_bytes) >= 12
            and img_bytes[:4] == b"RIFF"
            and img_bytes[8:12] == b"WEBP"
        ):
            return ".webp"
        return ".img"

    def _persist_config_migrations(self) -> None:
        """把磁盘插件配置中的旧版/被污染配置项迁移为当前格式并保存。

        目前包含：旧版布尔 enable_translate -> 字符串三态；被污染或非法的
        call_mode -> direct。写回失败不影响本次运行。
        """
        import json as _json
        import os as _os

        from astrbot.core.config.astrbot_config import AstrBotConfig
        from astrbot.core.utils.astrbot_path import get_astrbot_config_path

        schema_path = _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)), "_conf_schema.json"
        )
        with open(schema_path, encoding="utf-8") as _f:
            schema = _json.load(_f)
        plugin_cfg = AstrBotConfig(
            config_path=_os.path.join(
                get_astrbot_config_path(), f"{PLUGIN_NAME}_config.json"
            ),
            schema=schema,
        )
        migrated = False
        if isinstance(plugin_cfg.get("enable_translate"), bool):
            plugin_cfg["enable_translate"] = normalize_translate_mode(
                plugin_cfg["enable_translate"]
            )
            migrated = True
            logger.info(
                f"{LOG_TAG} 已自动迁移旧版布尔 enable_translate 配置 -> "
                f"{plugin_cfg['enable_translate']}"
            )
        healed_mode = migrate_invalid_call_mode(plugin_cfg)
        if healed_mode is not None:
            migrated = True
            logger.info(f"{LOG_TAG} 已自动修正无效的 call_mode 配置 -> {healed_mode}")
        if migrated:
            plugin_cfg.save_config()

    def _get_image_history_dir(self) -> Path:
        """延迟解析插件数据目录，避免模块加载阶段依赖 AstrBot 运行环境。"""

        if self._image_history_dir is None:
            self._image_history_dir = self._get_plugin_data_dir() / "image_history"
        return self._image_history_dir

    @staticmethod
    def _get_plugin_data_dir() -> Path:
        """返回 AstrBot 为本插件分配的数据目录。"""

        return StarTools.get_data_dir(PLUGIN_NAME)

    # ------------------------------------------------------------------
    # 画风槽位（名称 + 绑定模型 + 专属画师串）
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 画风槽位（画风名 + 适用模型 + 对应画师串）
    # 数据存在插件配置的 artist_slots 里（设置页的 template_list，可直接编辑）；
    # 旧的 artist_slots.json 只在首次加载时做一次性迁移。
    # ------------------------------------------------------------------
    def _config_path(self) -> Path:
        """返回本插件配置文件的绝对路径。"""

        from astrbot.core.utils.astrbot_path import get_astrbot_config_path

        return Path(get_astrbot_config_path()) / f"{PLUGIN_NAME}_config.json"

    def _schema_path(self) -> Path:
        return Path(__file__).resolve().parent / "_conf_schema.json"

    def _read_plugin_config(self) -> Any:
        """读取插件配置（带 schema，保证缺省键有值）。"""

        from astrbot.core.config.astrbot_config import AstrBotConfig

        schema = json.loads(self._schema_path().read_text(encoding="utf-8-sig"))
        return AstrBotConfig(config_path=str(self._config_path()), schema=schema)

    def _write_plugin_config(self, updates: dict[str, Any]) -> None:
        """把若干键写回插件配置；未提供的键保持不变。"""

        cfg = self._read_plugin_config()
        for key, value in updates.items():
            cfg[key] = value
        cfg.save_config()

    def _slots_cache_key(self) -> float:
        try:
            return self._config_path().stat().st_mtime
        except OSError:
            return 0.0

    def _default_artist_slots(self) -> list[dict[str, Any]]:
        """内置画风预设：每个内置画风一个槽位，默认绑定 V4.5 full。

        当前生效画风排到第一位，保证「开启画风槽位」后第一次出图的结果与
        开启前完全一致（用户不会因为开了开关而突然换了画风）。
        """

        current = str(getattr(self, "image_style", "") or "").strip()
        # 配置里可能存的是中文标签（如「自定义」），统一归一化成内部风格键。
        if current not in IMAGE_STYLES:
            current = normalize_image_style(current) or current
        keys = list(IMAGE_STYLES.keys())
        if current not in keys:
            current = "vertical"
        ordered = [current] + [key for key in keys if key != current]
        slots: list[dict[str, Any]] = []
        for index, key in enumerate(ordered, 1):
            if key == "custom":
                artists = str(getattr(self, "custom_artists", "") or "")
            else:
                artists = DEFAULT_ARTISTS.get(key, "")
            slots.append(
                {
                    "id": index,
                    "name": IMAGE_STYLES.get(key, key),
                    "model": "nai-diffusion-4-5-full",
                    "artists": artists,
                }
            )
        return slots

    @staticmethod
    def _normalize_slot(item: Any) -> dict[str, Any] | None:
        """把一条槽位记录规整成固定结构；完全空的记录返回 None。"""

        if not isinstance(item, dict):
            return None
        name = str(item.get("name") or "").strip()
        model = str(item.get("model") or "").strip()
        artists = str(item.get("artists") or "").strip()
        if not (name or model or artists):
            return None
        return {
            "name": name or "未命名画风",
            "model": model,
            "artists": artists,
        }

    def _normalize_slots(self, raw: Any) -> list[dict[str, Any]]:
        """规整整个槽位表；id 按列表顺序重新编号（1 起）。"""

        if not isinstance(raw, list):
            return []
        slots: list[dict[str, Any]] = []
        for item in raw:
            slot = self._normalize_slot(item)
            if slot is None:
                continue
            slot["id"] = len(slots) + 1
            slots.append(slot)
        return slots

    @staticmethod
    def _looks_like_legacy_defaults(slots: list[dict[str, Any]]) -> bool:
        """判断槽位表是否还是旧版的「槽位 N · xx 默认」两条出厂值。"""

        if len(slots) != 2:
            return False
        for slot in slots:
            name = str(slot.get("name") or "")
            if not name.startswith("槽位 ") or "默认" not in name:
                return False
        return True

    @staticmethod
    def _to_config_items(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """转成 template_list 需要的结构（每项带 __template_key）。"""

        return [
            {
                "__template_key": "style",
                "name": str(slot.get("name") or ""),
                "model": str(slot.get("model") or ""),
                "artists": str(slot.get("artists") or ""),
            }
            for slot in slots
        ]

    def _migrate_legacy_slot_file(self) -> list[dict[str, Any]]:
        """把旧版 artist_slots.json 迁移成槽位表（一次性，成功后改名备份）。"""

        try:
            path = self._get_plugin_data_dir() / "artist_slots.json"
            if not path.is_file():
                return []
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            items = data.get("slots") if isinstance(data, dict) else data
            slots = self._normalize_slots(items)
            backup = path.with_name(f"{path.name}.migrated-{int(time.time())}")
            path.replace(backup)
            if slots and self._looks_like_legacy_defaults(slots):
                # 出厂的两条默认值没有迁移价值，直接用内置画风预设。
                logger.info(f"{LOG_TAG} 旧槽位文件为出厂默认值，改用内置画风预设")
                return []
            if slots:
                logger.info(
                    f"{LOG_TAG} 已把旧槽位文件迁移到插件配置 | count={len(slots)}"
                )
            return slots
        except Exception as exc:
            logger.warning(f"{LOG_TAG} 迁移旧槽位文件失败: {exc!r}")
            return []

    def _load_artist_slots(self) -> list[dict[str, Any]]:
        """读取画风槽位表（数据源是插件配置，设置页与面板共用同一份）。"""

        mtime = self._slots_cache_key()
        if self._slots_cache is not None and mtime == self._slots_cache_mtime:
            return self._slots_cache

        raw_slots: Any = None
        try:
            raw_slots = self._read_plugin_config().get("artist_slots")
        except Exception as exc:
            logger.warning(f"{LOG_TAG} 读取画风槽位配置失败: {exc!r}")

        slots = self._normalize_slots(raw_slots)

        if not slots:
            migrated = self._migrate_legacy_slot_file()
            slots = migrated or self._default_artist_slots()
            try:
                self._write_plugin_config(
                    {"artist_slots": self._to_config_items(slots)}
                )
                logger.info(f"{LOG_TAG} 已初始化画风槽位 | count={len(slots)}")
            except Exception as exc:
                logger.warning(f"{LOG_TAG} 写入画风槽位配置失败: {exc!r}")

        self._slots_cache = slots
        self._slots_cache_mtime = self._slots_cache_key()
        return slots

    def _save_artist_slots(self, slots: list[dict[str, Any]]) -> None:
        """保存画风槽位表，并同步「正式生图启用槽位」下拉里显示的名字。"""

        normalized = self._normalize_slots(slots)
        if not normalized:
            raise ValueError("槽位表不能为空")

        self._write_plugin_config({"artist_slots": self._to_config_items(normalized)})
        self._slots_cache = normalized
        self._slots_cache_mtime = self._slots_cache_key()
        self._sync_slot_labels(normalized)

    def _sync_slot_labels(self, slots: list[dict[str, Any]]) -> None:
        """把槽位名写进 schema 下拉的 labels，让设置页显示改后的画风名。

        AstrBot 的设置页由静态 schema 生成，下拉的选项文字只能靠改写 schema
        同步。这里做原子写，且只在内容变化时才落盘；只读部署下失败只提醒一次。
        """

        try:
            schema_path = self._schema_path()
            schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
            node = schema.get("active_artist_slot")
            if not isinstance(node, dict):
                return
            options = [str(slot["id"]) for slot in slots]
            labels = [f"{slot['id']}. {slot['name']}" for slot in slots]
            if node.get("options") == options and node.get("labels") == labels:
                return
            node["options"] = options
            node["labels"] = labels
            if str(node.get("default")) not in options and options:
                node["default"] = options[0]
            tmp_path = schema_path.with_name(schema_path.name + ".tmp")
            tmp_path.write_text(
                json.dumps(schema, ensure_ascii=False, indent=4) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(schema_path)
            logger.debug(f"{LOG_TAG} 已同步槽位下拉标签 | {labels}")
        except Exception as exc:
            if not getattr(self, "_slot_label_warned", False):
                self._slot_label_warned = True
                logger.warning(
                    f"{LOG_TAG} 同步槽位下拉标签失败（插件目录可能只读），"
                    f"设置页将显示槽位编号而不是画风名: {exc!r}"
                )

    def _ensure_artist_slots(self) -> None:
        """启动时确保槽位配置存在，并把画风名同步到设置页下拉。"""

        slots = self._load_artist_slots()
        self._sync_slot_labels(slots)

    # ------------------------------------------------------------------
    # 提示词转译：关闭时相关配置整块隐藏、且运行时完全不参与请求
    # ------------------------------------------------------------------
    def _translate_on(self) -> bool:
        """提示词转译当前是否启用（开启 / 自动）。"""

        mode = getattr(self, "translate_mode", None)
        if mode is not None:
            return normalize_translate_mode(mode) != TRANSLATE_MODE_OFF
        # 兼容只设置旧版布尔属性的实例与测试桩。
        return bool(getattr(self, "enable_translate", False))

    def _translate_condition_value(self) -> str:
        """返回转译相关条目在 schema 里应比较的转译模式值。

        开启/自动时返回该模式本身（条目显示）；关闭时没有可显示的启用模式，
        这里返回「开启」——与当前值「关闭」不相等，条目即被隐藏。
        """

        mode = normalize_translate_mode(
            getattr(self, "translate_mode", TRANSLATE_MODE_OFF)
        )
        if mode == TRANSLATE_MODE_OFF:
            return TRANSLATE_MODE_ON
        return mode

    def _sync_translate_visibility(self) -> None:
        """按当前转译模式改写 schema，让转译专属配置在关闭时整块隐藏。

        设置页用静态 schema 渲染，条件只支持「单值相等」，因此无法直接写
        「开启 或 自动」。这里在每次插件加载时把条件值改成当前模式：关闭时
        改成「开启」（不相等 → 隐藏），开启/自动时改成自身（相等 → 显示）。
        只读部署写失败时只提醒一次，退化为"条目常显但运行时不生效"。
        """

        value = self._translate_condition_value()
        try:
            schema_path = self._schema_path()
            schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
            changed = False
            for key in TRANSLATE_GATED_KEYS:
                node = schema.get(key)
                if not isinstance(node, dict):
                    continue
                condition = node.get("condition")
                if not isinstance(condition, dict):
                    condition = {}
                if condition.get("enable_translate") == value:
                    continue
                condition["enable_translate"] = value
                node["condition"] = condition
                changed = True
            if not changed:
                return
            tmp_path = schema_path.with_name(schema_path.name + ".tmp")
            tmp_path.write_text(
                json.dumps(schema, ensure_ascii=False, indent=4) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(schema_path)
            logger.debug(
                f"{LOG_TAG} 转译专属配置显隐已同步 | translate={value} "
                f"keys={list(TRANSLATE_GATED_KEYS)}"
            )
        except Exception as exc:
            if not getattr(self, "_translate_visibility_warned", False):
                self._translate_visibility_warned = True
                logger.warning(
                    f"{LOG_TAG} 同步转译配置显隐失败（插件目录可能只读），"
                    f"转译专属配置将常显但关闭时不生效: {exc!r}"
                )

    def _active_artist_slot(self) -> dict[str, Any] | None:
        """返回正式生图当前启用的槽位；配置指向不存在的槽位时回退第一个。"""

        slots = self._load_artist_slots()
        try:
            wanted = int(str(getattr(self, "active_artist_slot", "1") or "1"))
        except (TypeError, ValueError):
            wanted = 1
        for slot in slots:
            if slot["id"] == wanted:
                return slot
        if slots:
            logger.debug(
                f"{LOG_TAG} 配置的槽位 {wanted} 不存在，回退到槽位 {slots[0]['id']}"
            )
            return slots[0]
        return None

    def _slot_model(self) -> str:
        """当前槽位绑定的模型（未启用槽位或未绑定则为空串）。"""

        if not self._artist_slots_enabled():
            return ""
        if not hasattr(self, "active_artist_slot"):
            # 测试桩/半初始化实例：不触发配置读写。
            return ""
        slot = self._active_artist_slot()
        return str((slot or {}).get("model") or "").strip()

    def _slot_artists(self) -> str:
        """当前槽位绑定的画师串（未启用槽位或未绑定则为空串）。"""

        if not self._artist_slots_enabled():
            return ""
        if not hasattr(self, "active_artist_slot"):
            return ""
        slot = self._active_artist_slot()
        return str((slot or {}).get("artists") or "").strip()

    def _match_style_slot(self, value: str) -> dict[str, Any] | None:
        """按「画风名」匹配槽位，匹配不到返回 None。

        容忍常见噪音：前后空格、大小写、"1. " 这类编号前缀、全半角中点，
        这样模型传「2 韩漫小清新风」或用户传「韩漫小清新风」都能命中。
        """

        if not self._artist_slots_enabled():
            return None
        name = str(value or "").strip()
        if not name:
            return None
        slots = self._load_artist_slots()
        for slot in slots:
            if slot["name"] == name:
                return slot

        def compact(text: str) -> str:
            return re.sub(r"[\s\d.、·．]+", "", text).casefold()

        target = compact(name)
        if not target:
            return None
        for slot in slots:
            if compact(slot["name"]) == target:
                return slot
        return None

    def _persist_active_slot(self, slot_id: str) -> None:
        """把「正式生图启用槽位」写回插件配置；失败只告警。"""

        try:
            self._write_plugin_config({"active_artist_slot": str(slot_id)})
            self.active_artist_slot = str(slot_id)
            logger.info(f"{LOG_TAG} 正式生图槽位已切换为 {slot_id}")
        except Exception as exc:
            logger.warning(f"{LOG_TAG} 写入生效槽位失败: {exc!r}")
    def _migrate_legacy_data_dir(self) -> None:
        """把旧版工作目录下的数据整体迁移到 AstrBot 插件数据目录。"""

        legacy_dir = (Path.cwd() / "data" / PLUGIN_NAME).resolve(strict=False)
        target_dir = self._get_plugin_data_dir().resolve(strict=False)
        if legacy_dir == target_dir or not legacy_dir.exists():
            return
        if not legacy_dir.is_dir():
            logger.warning(
                f"{LOG_TAG} 旧版数据路径不是目录，跳过迁移 | path={legacy_dir}"
            )
            return

        target_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        conflicts: list[str] = []
        for source in sorted(legacy_dir.iterdir(), key=lambda path: path.name):
            destination = target_dir / source.name
            if destination.exists() or destination.is_symlink():
                conflicts.append(source.name)
                continue
            try:
                shutil.move(source, destination)
                moved += 1
            except OSError as exc:
                logger.warning(
                    f"{LOG_TAG} 旧版数据项迁移失败 | source={source} "
                    f"target={destination} error={exc!r}"
                )

        removed = False
        try:
            legacy_dir.rmdir()
            removed = True
        except OSError as exc:
            logger.warning(
                f"{LOG_TAG} 旧版数据目录仍有未迁移内容，保留原目录 | "
                f"path={legacy_dir} conflicts={conflicts} error={exc!r}"
            )

        logger.info(
            f"{LOG_TAG} 旧版数据目录迁移完成 | source={legacy_dir} "
            f"target={target_dir} moved={moved} conflicts={len(conflicts)} "
            f"source_removed={removed}"
        )

    @staticmethod
    def _write_and_cleanup_image_history(
        history_dir: Path,
        img_bytes: bytes,
        generation_parameters: dict[str, Any] | None,
        history_limit: int,
    ) -> tuple[Path, Path | None, int]:
        """同步写入图片和可选 YAML 参数，并按数量清理最旧记录。

        文件先写入同目录临时文件再原子替换，避免异常中断留下半份记录。
        只统计本插件生成的 ``nai_`` 图片；``history_limit=0`` 表示不清理。
        """
        history_dir.mkdir(parents=True, exist_ok=True)
        extension = NAIGenerateImagePlugin._image_history_extension(img_bytes)
        image_path = history_dir / f"nai_{time.time_ns()}{extension}"
        image_temp_path = history_dir / f".{image_path.name}.tmp"
        try:
            image_temp_path.write_bytes(img_bytes)
            image_temp_path.replace(image_path)
        finally:
            image_temp_path.unlink(missing_ok=True)

        parameters_path: Path | None = None
        if generation_parameters is not None:
            parameters_path = image_path.with_suffix(".yaml")
            parameters_temp_path = history_dir / f".{parameters_path.name}.tmp"
            try:
                parameters_temp_path.write_text(
                    yaml.dump(
                        _prepare_generation_parameters_for_yaml(generation_parameters),
                        Dumper=_GenerationParametersDumper,
                        allow_unicode=True,
                        default_flow_style=False,
                        sort_keys=False,
                        width=4096,
                    ),
                    encoding="utf-8",
                )
                parameters_temp_path.replace(parameters_path)
            finally:
                parameters_temp_path.unlink(missing_ok=True)

        removed = 0
        if history_limit > 0:
            managed_files: list[tuple[int, str, Path]] = []
            for path in history_dir.iterdir():
                if not path.is_file() or not path.name.startswith("nai_"):
                    continue
                if path.suffix.lower() not in {".png", ".jpg", ".webp", ".img"}:
                    continue
                try:
                    managed_files.append((path.stat().st_mtime_ns, path.name, path))
                except FileNotFoundError:
                    continue
            managed_files.sort()
            for _, _, old_path in managed_files[
                : max(0, len(managed_files) - history_limit)
            ]:
                try:
                    old_path.unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
                old_path.with_suffix(".yaml").unlink(missing_ok=True)
                # 同时删除同名参数文件，并兼容清理旧版本生成的 JSON 伴随文件。
                old_path.with_suffix(".json").unlink(missing_ok=True)

        return image_path, parameters_path, removed

    async def _archive_generated_image(
        self,
        img_bytes: bytes,
        generation_parameters: dict[str, Any],
    ) -> None:
        """按配置归档一次成功生图，归档失败不影响图片返回。

        文件操作在线程中执行，并由实例锁串行化，避免阻塞事件循环或并发生图
        在清理数量上互相干扰。
        """
        # getattr：单测直接 object.__new__ 构造对象时可能没有该属性。
        if not getattr(self, "save_image_history", False):
            return
        try:
            parameters_to_save = (
                generation_parameters
                if getattr(self, "save_generation_parameters", False)
                else None
            )
            # 锁覆盖写入和清理的完整事务，保证并发请求看到一致的历史数量。
            async with self._image_history_lock:
                image_path, parameters_path, removed = await asyncio.to_thread(
                    self._write_and_cleanup_image_history,
                    self._get_image_history_dir(),
                    img_bytes,
                    parameters_to_save,
                    self.image_history_limit,
                )
            logger.info(
                f"{LOG_TAG} [history] 已保存 {image_path} | "
                f"parameters={parameters_path or 'OFF'} "
                f"cleanup={removed} limit={self.image_history_limit}"
            )
        except Exception as e:
            logger.warning(f"{LOG_TAG} [history] 保存或清理失败，不影响本次出图: {e!r}")

    def _usage_stats_store(self) -> UsageStatsStore | None:
        """返回用量统计仓库；插件数据目录不可用时返回 ``None``。

        仓库在首次调用时创建（数据目录要等插件实例化后才能解析），创建失败
        只记日志，绝不影响生图主流程。
        """

        # 用 getattr 读取：单测直接 object.__new__ 构造对象时没有该属性。
        if getattr(self, "_usage_stats", None) is None:
            try:
                self._usage_stats = UsageStatsStore(
                    self._get_plugin_data_dir() / STATS_FILE_NAME
                )
            except Exception as e:
                logger.warning(f"{LOG_TAG} [usage] 数据目录不可用，跳过统计: {e!r}")
                return None
        return self._usage_stats

    async def _record_usage_stat(
        self,
        cost: int,
        *,
        count: int = 1,
        failed: int = 0,
        transport: str = "",
        model: str = "",
        size: str = "",
        day: str | None = None,
    ) -> None:
        """累计一次生图结果的点数消耗，失败不影响出图流程。

        点数按 :mod:`pricing` 的档位表估算；``failed`` 只记次数不记点数
        （上游任务失败会退点）。``day`` 可显式指定归档日期（默认今天）。
        读写在线程中完成并由实例锁串行化。
        """

        if not getattr(self, "enable_usage_stats", True):
            return
        # 仓库未就绪（插件尚未完成初始化）时直接跳过，绝不因此写脏数据目录。
        if self._usage_stats_store() is None:
            return
        try:
            async with self._usage_stats_lock:
                store = self._usage_stats_store()
                await asyncio.to_thread(store.load)
                store.record(
                    cost,
                    count=count,
                    failed=failed,
                    transport=transport,
                    model=model,
                    size=size,
                    day=day,
                )
                await asyncio.to_thread(store.save)
        except Exception as e:
            logger.warning(f"{LOG_TAG} [usage] 累计用量失败，不影响本次出图: {e!r}")

    async def _record_usage_from_parameters(
        self, generation_parameters: dict[str, Any]
    ) -> None:
        """按归档参数里的 ``estimated_cost`` / ``transport`` 累计一次用量。"""

        await self._record_usage_stat(
            int(generation_parameters.get("estimated_cost") or 0),
            transport=str(generation_parameters.get("transport") or ""),
            model=str(generation_parameters.get("model") or ""),
            size=str(generation_parameters.get("size") or ""),
        )

    async def _save_upstream_billing_state(
        self,
        balance: float | None,
        delta: int,
        error: str,
    ) -> None:
        """把一次上游余额检测结果写入用量统计，失败不影响生图主流程。

        Args:
            balance: 本次查询到的余额；查询失败传 None 以保留上次余额。
            delta: 本次检测到的余额减少量（点），非正数表示无扣费。
            error: 查询失败时的错误摘要，成功传空串。
        """

        if not getattr(self, "enable_usage_stats", True):
            return
        try:
            async with self._usage_stats_lock:
                store = self._usage_stats_store()
                if store is None:
                    return
                await asyncio.to_thread(store.load)
                if delta > 0:
                    store.record_upstream(
                        delta,
                        balance=balance,
                        checked_at=time.time(),
                        last_error=error,
                    )
                else:
                    store.set_upstream_state(
                        balance=balance,
                        checked_at=time.time(),
                        last_delta=0,
                        last_error=error,
                    )
                await asyncio.to_thread(store.save)
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [billing] 保存余额同步结果失败: {exc!r}")

    async def _sync_upstream_billing_once(self, baseline: float | None) -> float | None:
        """查询一次上游余额，并把余额减少量记为上游实际扣费。

        上游没有按用户开放的逐笔扣费明细，只能按相邻两次余额查询的差值
        推算；首次查询只建立基线，不计费。

        Args:
            baseline: 上一次成功查询到的余额；None 表示首次建立基线。

        Returns:
            本次查询到的新余额；查询失败时返回原基线。
        """

        quota = await self._fetch_quota(quiet=True)
        if not quota.get("ok"):
            error = str(quota.get("message") or "unknown")
            if error != getattr(self, "_upstream_billing_last_error", ""):
                logger.warning(f"{LOG_TAG} [billing] 上游余额查询失败: {error}")
                self._upstream_billing_last_error = error
            await self._save_upstream_billing_state(None, 0, error)
            return baseline
        try:
            balance = float(quota.get("balance", quota.get("value", 0)) or 0)
        except (TypeError, ValueError):
            balance = 0.0
        self._upstream_billing_last_error = ""
        if baseline is None:
            logger.info(f"{LOG_TAG} [billing] 建立余额基线 | balance={balance:g}")
            await self._save_upstream_billing_state(balance, 0, "")
            return balance
        delta = int(round(baseline - balance))
        if delta > 0:
            logger.info(
                f"{LOG_TAG} [billing] 检测到上游扣费 | "
                f"{baseline:g} -> {balance:g} | {delta} 点"
            )
            await self._save_upstream_billing_state(balance, delta, "")
        else:
            if delta < 0:
                logger.info(
                    f"{LOG_TAG} [billing] 余额增加（充值/退点），更新基线 | "
                    f"{baseline:g} -> {balance:g}"
                )
            await self._save_upstream_billing_state(balance, 0, "")
        return balance

    async def _upstream_billing_loop(self) -> None:
        """按配置间隔轮询上游余额，记录两次查询之间的实际扣费。"""

        interval = self.upstream_billing_sync_interval * 60
        logger.info(f"{LOG_TAG} [billing] 余额同步任务启动 | interval={interval}s")
        baseline: float | None = None
        while True:
            try:
                baseline = await self._sync_upstream_billing_once(baseline)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"{LOG_TAG} [billing] 余额同步异常: {exc!r}")
            await asyncio.sleep(interval)

    async def _usage_stats_payload(self, days: int = 30) -> dict[str, Any]:
        """构造用量统计快照（聊天指令与面板图表共用）。"""

        try:
            async with self._usage_stats_lock:
                store = self._usage_stats_store()
                if store is None:
                    return _empty_usage_payload(
                        bool(getattr(self, "enable_usage_stats", True)), days
                    )
                await asyncio.to_thread(store.load)
                window = store.totals(days)
                overall = store.totals(0)
                series = store.series(days)
                today_rows = store.series(1)
                return {
                    "enabled": bool(getattr(self, "enable_usage_stats", True)),
                    "days": days,
                    "today": today_rows[-1]
                    if today_rows
                    else {
                        "date": "",
                        "cost": 0,
                        "count": 0,
                        "failed": 0,
                        "upstream_cost": 0,
                        "upstream_events": 0,
                    },
                    "window": window,
                    "overall": overall,
                    "series": series,
                    "upstream": store.upstream_state(),
                    "upstream_enabled": bool(
                        getattr(self, "enable_upstream_billing_sync", False)
                    ),
                    "breakdown": {
                        field: [
                            {"name": name, "cost": value}
                            for name, value in store.breakdown(days, field)
                        ]
                        for field in ("model", "size", "transport")
                    },
                    "first_day": store.first_day(),
                    "is_empty": store.is_empty(),
                }
        except Exception as e:
            logger.warning(f"{LOG_TAG} [usage] 读取用量统计失败: {e!r}")
            return {"enabled": False, "days": days, "error": repr(e)}

    async def initialize(self):
        logger.info(f"{LOG_TAG} [initialize] 阶段开始")
        # 1) 启动时迁移旧版误写到 data/<plugin_name> 的运行数据。
        try:
            await asyncio.to_thread(self._migrate_legacy_data_dir)
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [initialize] 旧版数据目录迁移异常: {exc!r}")

        # 1.5) 确保画风槽位存在，并把画风名同步到设置页的槽位下拉。
        try:
            await asyncio.to_thread(self._ensure_artist_slots)
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [initialize] 画风槽位初始化异常: {exc!r}")

        # 1.6) 转译关闭时，把转译专属配置从设置页整块隐藏。
        try:
            await asyncio.to_thread(self._sync_translate_visibility)
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [initialize] 转译配置显隐同步异常: {exc!r}")

        # 2) aiohttp session —— 失败也继续，至少把代理先起来
        #    trust_env：开启「绕过系统代理」时忽略环境变量代理，强制直连
        #    生图站，避免梯子的 HTTP 代理出口把请求带偏。
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=180),
                trust_env=not self.bypass_system_proxy,
                # force_close：每次请求新建连接。实测中转站偶发把复用的
                # keep-alive 连接挂死（上游已出图但响应永不返回，直到
                # 180s 超时），生图频率低，放弃连接复用换取稳定性。
                connector=aiohttp.TCPConnector(force_close=True),
            )
            logger.info(
                f"{LOG_TAG} [initialize] aiohttp session 创建成功 (timeout=180s, "
                f"trust_env={not self.bypass_system_proxy})"
            )
        except Exception as e:
            logger.error(
                f"{LOG_TAG} [initialize] aiohttp session 创建失败: {e!r}（将继续，远程出图会受影响）"
            )

        # 3) 本地代理 —— 由 enable_proxy 控制。开启时先停掉旧实例（热重载
        #    场景），再带 3 次 retry（间隔 1s）启动，应对 TIME_WAIT 等端口占用。
        if not self.enable_proxy:
            await self._stop_proxy_server()
            logger.info(
                f"{LOG_TAG} [initialize] 本地代理已关闭（enable_proxy=false），"
                f"陪伴插件可改用直连 extension API 生图"
            )
        else:
            # 先停掉旧实例（热重载场景），再启动新的。
            await self._stop_proxy_server()
            last_err = None
            for attempt in range(1, 4):
                try:
                    await self._start_proxy_server()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    logger.warning(
                        f"{LOG_TAG} [initialize] 代理启动失败 attempt={attempt}/3: {e!r}"
                    )
                    if attempt < 3:
                        await asyncio.sleep(1.0)
            if last_err is not None:
                logger.error(
                    f"{LOG_TAG} [initialize] 代理服务器最终启动失败，端口 {PROXY_HOST}:{self.proxy_port} "
                    f"不可用 —— 上游 healthcheck 会走 nai.sta1n.cn, 不会被本地 {self.proxy_port} 错误掩盖。last_err={last_err!r}"
                )

        # 4) 注册测试面板 Web API（路由需带插件名前缀才能被 Bridge SDK 匹配）
        try:
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/config",
                self._test_panel_get_config,
                ["GET"],
                "NAI 测试面板：获取当前配置",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/generate",
                self._test_panel_generate,
                ["POST"],
                "NAI 测试面板：生图测试",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/generate_openai",
                self._test_panel_generate_openai,
                ["POST"],
                "NAI 测试面板：OpenAI 兼容格式生图（支持本地上传参考图）",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/trial_status",
                self._test_panel_trial_status,
                ["GET"],
                "NAI 测试面板：试用状态",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/trial_generate",
                self._test_panel_trial_generate,
                ["POST"],
                "NAI 测试面板：试用生图",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/save_cache",
                self._test_panel_save_cache,
                ["POST"],
                "NAI 测试面板：保存面板缓存",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/load_cache",
                self._test_panel_load_cache,
                ["GET"],
                "NAI 测试面板：加载面板缓存",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/usage",
                self._test_panel_usage,
                ["GET"],
                "NAI 测试面板：点数用量统计（按日期）",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/slots",
                self._test_panel_slots,
                ["GET"],
                "NAI 测试面板：读取画风槽位表",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/slots_save",
                self._test_panel_slots_save,
                ["POST"],
                "NAI 测试面板：保存画风槽位表",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/slots_reset",
                self._test_panel_slots_reset,
                ["POST"],
                "NAI 测试面板：按内置画风重建槽位表",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/grimoire_search",
                self._test_panel_grimoire_search,
                ["POST"],
                "NAI 测试面板：检索内置法典词库",
            )
            logger.info(
                f"{LOG_TAG} [initialize] 测试面板 Web API 已注册 | prefix={PAGE_API_PREFIX}"
            )
        except Exception as e:
            logger.warning(f"{LOG_TAG} [initialize] 注册测试面板 Web API 失败: {e!r}")

        # 5) 试用生成：解密代码内混淆密钥 + 加载本地试用次数
        await self._init_trial_feature()

        # 6) 载入点数用量统计（损坏文件会自动备份并重置）
        try:
            store = self._usage_stats_store()
            if store is not None:
                async with self._usage_stats_lock:
                    await asyncio.to_thread(store.load)
        except Exception as e:
            logger.warning(f"{LOG_TAG} [initialize] 载入用量统计失败: {e!r}")

        # 7) 上游余额扣费同步：仅传统 GET 直连模式，且需要 token、session
        #    与用量统计同时可用；任务内部首次查询只建立基线，不计费。
        if self.enable_upstream_billing_sync:
            if (
                self.enable_usage_stats
                and self.call_mode == "direct"
                and self.image_gen_key
                and self._session
                and not self._session.closed
            ):
                if (
                    self._upstream_billing_task is None
                    or self._upstream_billing_task.done()
                ):
                    self._upstream_billing_task = asyncio.create_task(
                        self._upstream_billing_loop()
                    )
            else:
                logger.info(
                    f"{LOG_TAG} [initialize] 上游余额同步未启动："
                    "需要传统 GET 直连模式、已配置 image_gen_key 且开启用量统计"
                )

        logger.info(
            f"{LOG_TAG} [initialize] 阶段完成 | token={'OK' if self.image_gen_key else 'MISSING'} | "
            f"proxy={'UP' if self.proxy_runner else ('OFF' if not self.enable_proxy else 'DOWN')} | "
            f"billing_sync={'ON' if self._upstream_billing_task else 'OFF'} | "
            f"trial_key={'OK' if self._trial_key else 'N/A'} "
            f"trial_used={self._trial_usage_count}/{TRIAL_MAX_USES}"
        )

    async def terminate(self):
        global _active_plugin
        logger.info(f"{LOG_TAG} [terminate] 阶段开始")
        if _active_plugin is self:
            _active_plugin = None
        if self._upstream_billing_task is not None:
            self._upstream_billing_task.cancel()
            try:
                await self._upstream_billing_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"{LOG_TAG} [terminate] 停止余额同步任务异常: {e!r}")
            self._upstream_billing_task = None
        try:
            await self._stop_proxy_server()
        except Exception as e:
            logger.warning(f"{LOG_TAG} [terminate] 关闭代理异常: {e!r}")
        if self._session and not self._session.closed:
            try:
                await self._session.close()
                logger.info(f"{LOG_TAG} [terminate] aiohttp session 已关闭")
            except Exception as e:
                logger.warning(f"{LOG_TAG} [terminate] session 关闭异常: {e!r}")
        # outfit 缓存是纯内存，插件重载一定会清掉，这里主动清理一次
        if self.outfit_cache_text is not None:
            logger.info(f"{LOG_TAG} [terminate] 清理 outfit 缓存")
            self._outfit_cache_clear()
        logger.info(f"{LOG_TAG} [terminate] 阶段完成")

    def _resolve_artists(self, style: str) -> str:
        if style == "custom":
            return self.custom_artists if self.custom_artists is not None else ""
        return DEFAULT_ARTISTS.get(style, DEFAULT_ARTISTS["vertical"])

    # ==== Outfit 缓存池读写 ====
    def _outfit_cache_get(self) -> str | None:
        """读缓存。TTL 到期自动清除（直接返回 None）。"""
        if self.outfit_cache_text is None:
            return None
        if (
            self.outfit_cache_expires_at is not None
            and time.monotonic() > self.outfit_cache_expires_at
        ):
            self.outfit_cache_text = None
            self.outfit_cache_expires_at = None
            return None
        return self.outfit_cache_text

    def _outfit_cache_set(self, text: str) -> None:
        """写缓存。TTL 由 self.outfit_cache_ttl_seconds 决定；ttl<=0 时写但立即禁用读。"""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        self.outfit_cache_text = cleaned
        if self.outfit_cache_ttl_seconds > 0:
            self.outfit_cache_expires_at = (
                time.monotonic() + self.outfit_cache_ttl_seconds
            )
        else:
            self.outfit_cache_expires_at = None  # ttl<=0 时强制失活

    def _outfit_cache_clear(self) -> None:
        self.outfit_cache_text = None
        self.outfit_cache_expires_at = None

    def _resolve_outfit(self, user_prompt: str) -> tuple[str, str, bool]:
        """根据源 prompt 决定要追加的"服装上下文"文本，并维护缓存池。

        返回 (outfit_text_for_context, source, use_default_outfit)：
          - source ∈ {"prompt", "cache", "none"}
          - outfit_text_for_context 为空表示不需要追加任何东西。
          - use_default_outfit: True表示应该在模板合并时添加默认服装

        副作用：
          - 命中具体词 / 换装动词时，从源 prompt 抽出片段写进缓存（启动/刷新 TTL）。
        """
        is_specific = _has_specific_outfit(user_prompt)
        is_change = _detect_outfit_change(user_prompt)
        # 开关关闭或 ttl=0 时缓存整体停用：不写入、不读取。
        cache_enabled = self.enable_outfit_cache and self.outfit_cache_ttl_seconds > 0

        # 1) 命中具体词 / 换装动词 → 抽出片段写缓存，并把片段本身作为本次上下文
        if is_specific or is_change:
            excerpt = _extract_outfit_excerpt(user_prompt)
            if excerpt:
                if cache_enabled:
                    self._outfit_cache_set(excerpt)
                    logger.info(
                        f"{LOG_TAG} [outfit] 命中具体/换装 | "
                        f"trigger={'change' if is_change else 'specific'} | "
                        f"cached | excerpt='{excerpt[:60]}...' "
                        f"ttl={self.outfit_cache_ttl_seconds}s"
                    )
                else:
                    logger.info(
                        f"{LOG_TAG} [outfit] 命中具体/换装但缓存已停用 "
                        f"(enable={self.enable_outfit_cache} ttl={self.outfit_cache_ttl_seconds}s)"
                    )
                return excerpt, "prompt", False

        # 2) 源 prompt 模糊 → 只使用缓存（TTL 内），不再返回默认服装（留到模板合并阶段）
        if cache_enabled:
            cached = self._outfit_cache_get()
            if cached:
                logger.debug(
                    f"{LOG_TAG} [outfit] 使用缓存 | preview='{cached[:60]}...'"
                )
                return cached, "cache", False

        # 3) 既无具体也没缓存，标记使用默认服装
        if self.default_outfit:
            return "", "none", True

        return "", "none", False

    # ==== NAI 尺寸键 → OpenAI 兼容像素尺寸映射 ====
    _OPENAI_SIZE_MAP = {
        "方图": "1024x1024",
        "竖图": "832x1216",
        "横图": "1216x832",
        "2K方图": "1472x1472",
        "2K竖图": "1088x1920",
        "2K横图": "1920x1088",
        # OpenAI 兼容上游一般限制最大边 ≤1920、总面积 ≤3686400，
        # 4K 键降级为 2K 上限尺寸，避免被接口以 invalid size 拒绝。
        "4K方图": "1472x1472",
        "4K竖图": "1088x1920",
        "4K横图": "1920x1088",
    }
    _OPENAI_MAX_SIDE = 1920
    _OPENAI_MAX_AREA = 3686400

    def _openai_size(self, nai_size: str) -> str:
        """把 NAI 尺寸键（竖图/方图/...）映射为 OpenAI 兼容的像素尺寸。

        中英文分档名（portrait/landscape/square/2k...）都会先归一化为中文键；
        未映射的像素尺寸直接透传（如 "1024x1024" 或 "832x1216"）。
        4K 键因上游边长限制降级为 2K 上限尺寸。
        """
        normalized = normalize_image_size(nai_size)
        if normalized:
            nai_size = normalized
        if str(nai_size).startswith("4K"):
            logger.info(
                f"{LOG_TAG} [openai_generate] OpenAI 兼容接口不支持 4K 尺寸，"
                f"'{nai_size}' 已降级为 2K 上限尺寸"
            )
        return self._OPENAI_SIZE_MAP.get(nai_size, nai_size)

    def _normalize_openai_size(self, size: str) -> str:
        """把尺寸校正为满足接口契约的 ``WxH``。

        契约（对齐接口文档 §10）：宽高为 64 的倍数，最大边不超过 1920，
        总面积不超过 3686400。越界值按最近合法值收缩，无法识别的格式
        回退 1024x1024。

        Args:
            size: 形如 "1024x1536" 的像素尺寸字符串。

        Returns:
            校正后的 "WxH" 字符串。
        """
        match = re.match(
            r"^\s*(\d{1,5})\s*[x×*]\s*(\d{1,5})\s*$", str(size), re.IGNORECASE
        )
        if not match:
            logger.warning(
                f"{LOG_TAG} [openai_generate] 无法识别的尺寸 '{size}'，回退 1024x1024"
            )
            return "1024x1024"
        width = max(64, (int(match.group(1)) + 32) // 64 * 64)
        height = max(64, (int(match.group(2)) + 32) // 64 * 64)
        while max(width, height) > self._OPENAI_MAX_SIDE and min(width, height) > 64:
            if width >= height:
                width -= 64
            else:
                height -= 64
        while width * height > self._OPENAI_MAX_AREA and min(width, height) > 64:
            if width >= height:
                width -= 64
            else:
                height -= 64
        normalized = f"{width}x{height}"
        if normalized != str(size).strip():
            logger.info(f"{LOG_TAG} [openai_generate] 尺寸校正: {size} -> {normalized}")
        return normalized

    def _openai_endpoint(self, target: str) -> str:
        """拼接 OpenAI 兼容端点，兼容 base_url 是否带 ``/v1``。

        Args:
            target: "generations" 或 "edits"。

        Returns:
            完整端点 URL，例如 ``https://api1.syuan.org/v1/images/edits``。
        """
        base = self.openai_api_base_url.rstrip("/")
        target = "edits" if target == "edits" else "generations"
        if base.endswith(f"/images/{target}"):
            return base
        if re.search(r"/images/(?:generations|edits)$", base):
            return re.sub(r"/images/(?:generations|edits)$", f"/images/{target}", base)
        if re.search(r"/v1/?$", base) or "/v1/" in base:
            return f"{base}/images/{target}"
        return f"{base}/v1/images/{target}"

    def _openai_prompt(
        self,
        prompt: str,
        style: str,
        custom_artists: str | None = None,
    ) -> str:
        """把画师串作为风格前缀拼进 OpenAI 兼容格式的 prompt。

        OpenAI 兼容格式没有独立的 artist 参数，画师串需要并入 prompt 文本。
        ``custom`` 风格可用 ``custom_artists`` 覆盖（面板独立字段），否则用
        插件配置的 ``self.custom_artists``；其余风格回退到对应预设。

        Args:
            prompt: 已转译/合并后的主体提示词。
            style: NAI 风格键（vertical / custom / ...）。
            custom_artists: 自定义画师串覆盖（面板传入），可选。

        Returns:
            拼接了画师串的最终 prompt 文本。
        """
        if style == "custom":
            artists = (
                custom_artists if custom_artists is not None else self.custom_artists
            )
            artists = normalize_prompt(artists or "")
        else:
            artists = normalize_prompt(
                DEFAULT_ARTISTS.get(style, DEFAULT_ARTISTS["vertical"])
            )
        # 槽位绑定的画师串优先级高于风格预设，但低于本次显式指定的画师串。
        if custom_artists is None:
            try:
                slot_artists = self._slot_artists()
            except Exception:
                slot_artists = ""
            if slot_artists:
                artists = normalize_prompt(slot_artists)
        if not artists:
            return normalize_prompt(prompt)
        return normalize_prompt(f"{artists}, {prompt}")

    async def _prepare_openai_prompt(
        self,
        prompt: str,
        style: str,
        *,
        custom_artists: str | None = None,
        enable_template: bool | None = None,
        enable_translate: bool | str | None = None,
    ) -> tuple[str, str, str]:
        """按与直连模式一致的流程准备 OpenAI 兼容模式的最终提示词。

        直连模式在 ``_generate_one`` 内完成了转译、服装缓存、角色模板和
        画师串合并；OpenAI 兼容模式此前直接使用原始 prompt，导致
        ``--translate`` / ``--template`` / ``--artist`` 与服装缓存失效。
        这里复用同一套处理链，只在最后一步改为把画师串并入 prompt。

        Args:
            prompt: 用户原始提示词。
            style: NAI 画风键。
            custom_artists: 单次自定义画师串，仅 ``custom`` 风格生效。
            enable_template: 单次模板开关；None 时使用插件配置。
            enable_translate: 单次转译模式；None 时使用插件配置。

        Returns:
            ``(full_prompt, outfit_source, prompt_kind)``，后两项仅用于日志。
        """
        _translate_mode = normalize_translate_mode(
            enable_translate if enable_translate is not None else self.translate_mode
        )
        (
            translated_prompt,
            outfit_source,
            use_default_outfit,
            prompt_kind,
        ) = await self._prepare_translated_prompt(
            prompt,
            translate_mode=_translate_mode,
            apply_outfit=True,
        )
        full_prompt = self._build_full_prompt(
            translated_prompt,
            enable_template=enable_template,
        )
        if use_default_outfit and self.default_outfit:
            full_prompt = f"{full_prompt}, {self.default_outfit}"
        full_prompt = self._openai_prompt(full_prompt, style, custom_artists)
        _template_on = (
            self.enable_template if enable_template is None else enable_template
        )
        logger.info(
            f"{LOG_TAG} [openai_prompt] style={style} "
            f"translate={_translate_mode}/{prompt_kind} "
            f"template={'on' if _template_on else 'off'} outfit={outfit_source} | "
            f"prompt(原始)='{prompt[:60]}...' "
            f"prompt(最终)='{full_prompt[:60]}...'"
        )
        return full_prompt, outfit_source, prompt_kind

    async def _openai_generate(
        self,
        prompt: str,
        size: str,
        *,
        n: int = 1,
        reference_image_path: str | None = None,
        reference_image_bytes: bytes | None = None,
        reference_image_paths: Sequence[str] | None = None,
        reference_image_bytes_list: Sequence[bytes] | None = None,
        strength: float | None = None,
        reference_strengths: Sequence[float] | None = None,
        noise: float | None = None,
        steps: int | None = None,
        scale: float | None = None,
        sampler: str | None = None,
        noise_schedule: str | None = None,
        seed: int | None = None,
        negative: str | None = None,
        model: str | None = None,
        reference_mode: str | None = None,
        director_action: str | None = None,
        director_caption: str | None = None,
        director_captions: Sequence[str] | None = None,
        characters: Sequence[tuple[str, float, float]] | None = None,
    ) -> tuple[list[bytes], str]:
        """OpenAI 兼容格式生图（文生图 / 图生图 / 参考图 / 图片处理）。

        对齐接口文档：高级参数统一放入 ``parameters`` 对象（§3.2/§10.2）；
        带参考图时按 ``reference_mode`` 选择 ``vibe``（generations 的
        ``reference_image_multiple``，§5）、``img2img``（``/v1/images/edits``
        的 JSON ``image`` 字段，§4）或 ``director``（generations 的
        ``director_reference_*`` 精准参考，§6）。vibe / director 支持一次
        提交最多 8 张参考图（§5.2），五个数组按下标一一对应；``director_action``
        对应 director-tools 图片处理（§8）；``characters`` 非空时附加多角色
        坐标控制（§7）。对 408/429/502/503/504 按 2/4/8 秒指数退避选择性重试；
        超时不自动重试（上游可能仍在生成并扣费）；400/401/403 默认不重
        试，但错误文案提示"稍后重试"的瞬时故障除外。

        Args:
            prompt: 主体提示词（已合并画师串）。
            size: NAI 尺寸键（竖图/方图/...）或像素尺寸 "WxH"。
            n: 生成张数。
            reference_image_path: 单张参考图本地路径（旧参数，与多图列表合并）。
            reference_image_bytes: 单张参考图字节内容（旧参数，与多图列表合并）。
            reference_image_paths: 多张参考图本地路径列表，最多取前 8 张。
            reference_image_bytes_list: 多张参考图字节内容列表，优先于路径，
                最多取前 8 张；img2img 仅使用第一张（§5.3 主输入图）。
            strength: img2img 重绘强度、vibe/精准参考统一强度，0-1；被
                reference_strengths 覆盖。vibe 默认 0.6，精准参考默认 1.0。
            reference_strengths: 每张参考图的强度列表（vibe 的
                reference_strength_multiple / 精准参考的
                director_reference_strength_values），不足位数用 strength 默认值补齐。
            noise: img2img 附加噪声强度，0-1。
            steps / scale / sampler / noise_schedule / seed / negative:
                高级参数，留 None 时使用插件全局配置。
            model: 模型覆盖，默认 self.openai_api_model。
            reference_mode: "vibe" / "img2img" / "director"，默认
                self.openai_reference_mode。
            director_action: director-tools 动作（bg-removal/lineart/sketch/
                colorize/emotion/declutter），指定时 model 强制 director-tools。
            director_caption: 精准参考描述（新文档 §8.4 的 base_caption），仅
                允许 character / style / character&style，默认
                self.openai_director_caption；被 director_captions 逐项覆盖。
                精准参考仅支持 NAI 4.5 / NAI 5 模型，其他模型会自动切换为
                nai-diffusion-4-5-full。
            director_captions: 每张精准参考图的 base_caption 列表，非法值回退
                director_caption / 插件配置。
            characters: 多角色坐标列表 (提示词, x, y)，坐标 0-1（§7），
                最多取前 6 个；仅对 generations 类请求生效。

        Returns:
            (images_bytes, reason) — reason 为 "ok" 或错误码。
        """
        import json

        import aiohttp

        base = self.openai_api_base_url
        if not base:
            return [], "openai_not_configured"
        if not self._session:
            return [], "no_session"

        # 尺寸规范化：NAI 键 → 像素尺寸 → 64 倍数/边长/面积契约（§10）
        size = self._normalize_openai_size(self._openai_size(size))
        try:
            target_w, target_h = (int(v) for v in size.split("x", 1))
        except ValueError:
            target_w, target_h = 1024, 1024

        # director-tools 图片处理：model / action 由动作唯一决定（§8）
        is_director = director_action in OPENAI_DIRECTOR_ACTIONS
        starbridge = getattr(self, "openai_api_profile", "compatible") == "starbridge"
        starbridge_tool_model = (
            (model or self._slot_model() or self.openai_api_model or "").strip()
            if starbridge and is_director
            else None
        )
        model = (
            "director-tools"
            if is_director
            else (model or self._slot_model() or self.openai_api_model or "").strip()
        )
        api_key = self.openai_api_key
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)

        # ==== 参考图收集：多图列表优先，兼容旧单图参数（§5.2 上限 8 张） ====
        ref_bytes_list: list[bytes] = []
        if reference_image_bytes_list:
            ref_bytes_list.extend(reference_image_bytes_list)
        if reference_image_bytes:
            ref_bytes_list.insert(0, reference_image_bytes)
        _ref_paths = list(reference_image_paths or ())
        if reference_image_path:
            _ref_paths.insert(0, reference_image_path)
        for _path in _ref_paths:
            text = str(_path or "").strip()
            if not text:
                continue
            try:
                file = Path(text)
                if file.is_file():
                    ref_bytes_list.append(await asyncio.to_thread(file.read_bytes))
            except Exception as exc:
                logger.warning(
                    f"{LOG_TAG} [openai_generate] 读取参考图失败: "
                    f"{type(exc).__name__}: {exc}"
                )

        ref_mode = (reference_mode or self.openai_reference_mode or "vibe").casefold()
        if ref_mode not in ("img2img", "vibe", "director"):
            ref_mode = "vibe"
        if (is_director or ref_mode == "director") and not ref_bytes_list:
            # 兜底参考图：director 模式且请求未带图时（聊天指令/陪伴联动），
            # 按设置里的文件顺序全部启用直至上限；director-tools 仍需显式
            # 提供待处理图。设置页上传的文件存的是相对插件数据目录的路径，
            # 绝对路径（用户手填）则原样使用。
            if not is_director:
                _data_dir = StarTools.get_data_dir(PLUGIN_NAME)
                for _fb_path in self.openai_director_fallback_images:
                    if len(ref_bytes_list) >= OPENAI_MAX_REFERENCE_IMAGES:
                        break
                    try:
                        _fb_file = Path(_fb_path)
                        if not _fb_file.is_absolute():
                            _fb_file = _data_dir / _fb_file
                        if _fb_file.is_file():
                            ref_bytes_list.append(
                                await asyncio.to_thread(_fb_file.read_bytes)
                            )
                    except Exception:
                        continue
                if ref_bytes_list:
                    logger.info(
                        f"{LOG_TAG} [openai_generate] 精准参考使用兜底参考图 "
                        f"{len(ref_bytes_list)} 张"
                    )
                else:
                    # 无参考图也无兜底图：按文档契约回退纯文生图，只有
                    # director-tools 图片处理动作才强制要求源图。
                    logger.info(
                        f"{LOG_TAG} [openai_generate] 精准参考模式未带参考图且"
                        "未配置兜底参考图，回退纯文生图"
                    )
            if not ref_bytes_list and is_director:
                return [], "no_reference_image"

        if len(ref_bytes_list) > OPENAI_MAX_REFERENCE_IMAGES:
            logger.info(
                f"{LOG_TAG} [openai_generate] 参考图超过 {OPENAI_MAX_REFERENCE_IMAGES} "
                f"张上限，截断 {len(ref_bytes_list)} -> {OPENAI_MAX_REFERENCE_IMAGES}"
            )
            ref_bytes_list = ref_bytes_list[:OPENAI_MAX_REFERENCE_IMAGES]

        # img2img 要求源图与目标尺寸严格一致，否则上游直接拒绝，且数组仅以
        # 第一张作为主输入图（§5.3）；vibe / director 不强制比例，但上游适配
        # 器同样按 §10 契约校验参考图（最大边 1920、面积 3686400），超限会报
        # "image dimensions exceed the maximum allowed size"，因此超限图片先
        # 等比缩小（不放大、不重编码合规图）。
        is_edit = bool(ref_bytes_list) and ref_mode == "img2img" and not is_director
        if is_edit and len(ref_bytes_list) > 1:
            logger.info(
                f"{LOG_TAG} [openai_generate] img2img 仅支持单张主输入图，"
                f"已忽略多余 {len(ref_bytes_list) - 1} 张参考图"
            )
            ref_bytes_list = ref_bytes_list[:1]
        ref_uris: list[str] = []
        if ref_bytes_list:
            try:
                import io

                from PIL import Image, ImageOps

                def _fit_ref_image(raw_bytes: bytes) -> bytes:
                    with Image.open(io.BytesIO(raw_bytes)) as img:
                        w, h = img.size
                        if is_edit:
                            if (w, h) == (target_w, target_h):
                                return raw_bytes
                            img = ImageOps.fit(
                                img, (target_w, target_h), Image.Resampling.LANCZOS
                            )
                        else:
                            ratio = min(
                                1.0,
                                self._OPENAI_MAX_SIDE / max(w, h),
                                (self._OPENAI_MAX_AREA / max(1, w * h)) ** 0.5,
                            )
                            if ratio >= 1.0:
                                return raw_bytes
                            img = img.resize(
                                (max(1, int(w * ratio)), max(1, int(h * ratio))),
                                Image.Resampling.LANCZOS,
                            )
                            logger.info(
                                f"{LOG_TAG} [openai_generate] 参考图超限，等比缩小: "
                                f"{w}x{h} -> {img.size[0]}x{img.size[1]}"
                            )
                        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
                        out_buf = io.BytesIO()
                        img.save(out_buf, format="PNG")
                        return out_buf.getvalue()

                def _prepare_refs() -> list[bytes]:
                    fitted: list[bytes] = []
                    for raw in ref_bytes_list:
                        try:
                            fitted.append(_fit_ref_image(raw))
                        except Exception as exc:
                            logger.warning(
                                f"{LOG_TAG} [openai_generate] 参考图尺寸适配失败: {exc}"
                            )
                            fitted.append(raw)
                    return fitted

                ref_bytes_list = await asyncio.to_thread(_prepare_refs)
                # Data URI 格式（与文档 §5 参考图示例一致）；MIME 按文件魔数
                # 判断，尺寸适配后的字节固定为 PNG，未适配的原图保持自身格式。
                for raw in ref_bytes_list:
                    if raw.startswith(b"\xff\xd8\xff"):
                        _mime = "image/jpeg"
                    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
                        _mime = "image/webp"
                    elif raw.startswith(b"GIF8"):
                        _mime = "image/gif"
                    else:
                        _mime = "image/png"
                    ref_uris.append(
                        f"data:{_mime};base64," + base64.b64encode(raw).decode()
                    )
            except Exception as exc:
                logger.warning(
                    f"{LOG_TAG} [openai_generate] 参考图处理失败: {type(exc).__name__}: {exc}"
                )
        image_b64 = ref_uris[0] if ref_uris else ""

        # ==== 高级参数组装（§3.2/§10.2），留 None 的项回退插件全局配置 ====
        try:
            _steps = max(1, min(50, int(steps if steps is not None else self.steps)))
        except (TypeError, ValueError):
            _steps = 28
        try:
            _scale = max(
                0.0, min(10.0, float(scale if scale is not None else self.scale))
            )
        except (TypeError, ValueError):
            _scale = 5.0
        _sampler = (sampler if sampler is not None else self.sampler or "").strip()
        _noise_schedule = (
            noise_schedule if noise_schedule is not None else self.noise_schedule or ""
        ).strip()
        _negative = negative if negative is not None else self.negative
        try:
            _seed = int(seed if seed is not None else self.openai_seed)
        except (TypeError, ValueError):
            _seed = -1

        parameters: dict[str, Any] = {"steps": _steps, "scale": _scale}
        if _sampler:
            parameters["sampler"] = _sampler
        if _noise_schedule:
            parameters["noise_schedule"] = _noise_schedule
        if _seed >= 0:
            parameters["seed"] = _seed
        if _negative and str(_negative).strip():
            parameters["negative_prompt"] = str(_negative).strip()

        # ==== 多角色坐标控制（§7）：仅对 generations 类请求生效 ====
        char_entries: list[tuple[str, float, float]] = []
        if characters:
            for entry in characters:
                try:
                    c_prompt, c_x, c_y = entry
                    c_prompt = str(c_prompt or "").strip()
                    if not c_prompt:
                        continue
                    char_entries.append(
                        (
                            c_prompt,
                            max(0.0, min(1.0, float(c_x))),
                            max(0.0, min(1.0, float(c_y))),
                        )
                    )
                except (TypeError, ValueError):
                    continue
            char_entries = char_entries[:6]
        if char_entries and (is_edit or is_director):
            logger.info(
                f"{LOG_TAG} [openai_generate] img2img/director-tools 不支持多角色坐标，"
                "已忽略本次 characters 参数"
            )
            char_entries = []

        # ==== 逐图参考参数（vibe / 精准参考共用） ====
        # 强度：reference_strengths 按下标逐张生效，缺位或非法值回退统一
        # strength 参数，仍缺失时用模式默认值补齐（vibe 默认 0.6、精准参考
        # 默认 1.0），保证数组与参考图严格等长。
        _strength_default = (
            self.openai_director_strength
            if ref_mode == "director"
            else self.openai_vibe_strength
        )
        _scalar_strength: float | None = None
        if strength is not None:
            try:
                _scalar_strength = round(max(0.0, min(1.0, float(strength))), 2)
            except (TypeError, ValueError):
                _scalar_strength = None
        ref_strength_values: list[float] = []
        for i in range(len(ref_uris)):
            value = (
                _scalar_strength if _scalar_strength is not None else _strength_default
            )
            if reference_strengths and i < len(reference_strengths):
                try:
                    per_value = float(reference_strengths[i])
                    if 0 <= per_value <= 1:
                        value = round(per_value, 2)
                except (TypeError, ValueError):
                    pass
            ref_strength_values.append(value)
        # 精准参考逐图描述（§8.4）：base_caption 仅三个枚举值合法，非法值
        # 回退 director_caption / 插件配置，最终回退 character&style。
        _caption_fallback = (
            director_caption or self.openai_director_caption or ""
        ).strip()
        if _caption_fallback not in OPENAI_DIRECTOR_CAPTIONS:
            _caption_fallback = "character&style"
        ref_director_captions: list[str] = []
        for i in range(len(ref_uris)):
            caption = ""
            if director_captions and i < len(director_captions):
                caption = str(director_captions[i] or "").strip()
            if caption not in OPENAI_DIRECTOR_CAPTIONS:
                caption = _caption_fallback
            ref_director_captions.append(caption)

        # ==== 请求体组装：文生图 / img2img / vibe / 精准参考 / director-tools ====
        payload: dict[str, Any] = {
            "prompt": prompt,
            "size": size,
            "n": max(1, int(n)),
        }
        if model:
            payload["model"] = model

        if is_director:
            payload["action"] = director_action
            parameters = {"image": image_b64, "width": target_w, "height": target_h}
        elif is_edit:
            payload["action"] = "img2img"
            payload["image"] = image_b64
            if _scalar_strength is not None:
                parameters["strength"] = _scalar_strength
            if noise is not None:
                parameters["noise"] = max(0.0, min(1.0, float(noise)))
        elif ref_mode == "director" and ref_uris:
            # 精准参考（新文档 §8）：五个 director_reference_* 数组严格等长、
            # 按下标一一对应；参考图由服务端自动规范化（§8.6），客户端无需
            # 预处理。information_extracted 固定 1.0：实测其他取值（如 0.7）
            # 会被上游参数校验拒绝（HTTP 400），与 §8 各示例保持一致。
            payload["action"] = "generate"
            # 精准参考仅支持 NAI 4.5 / NAI 5 模型（实测），其余模型会被服务端
            # 拒绝；当前模型不满足时自动切换到 4-5-full 以保证能力可用。
            if (
                not starbridge
                and (model or "").strip().casefold() not in OPENAI_DIRECTOR_MODELS
            ):
                logger.info(
                    f"{LOG_TAG} [openai_generate] 精准参考不支持当前模型 {model or '(none)'}"
                    "（仅 4.5 / 5 系列），自动切换为 nai-diffusion-4-5-full"
                )
                model = "nai-diffusion-4-5-full"
                payload["model"] = model
            parameters["director_reference_images"] = ref_uris
            parameters["director_reference_strength_values"] = ref_strength_values
            # 次级特征强度（§8.5 推荐 0.35-0.7），默认取插件设置
            parameters["director_reference_secondary_strength_values"] = [
                self.openai_director_secondary_strength
            ] * len(ref_uris)
            parameters["director_reference_information_extracted"] = [1.0] * len(
                ref_uris
            )
            parameters["director_reference_descriptions"] = [
                {
                    "caption": {
                        "base_caption": cap,
                        "char_captions": [],
                    },
                    "legacy_uc": False,
                }
                for cap in ref_director_captions
            ]
        elif ref_uris:
            # vibe 参考：数组按下标一一对应（§5）
            payload["action"] = "generate"
            parameters["reference_image_multiple"] = ref_uris
            parameters["reference_strength_multiple"] = ref_strength_values
            parameters["reference_information_extracted_multiple"] = [0.7] * len(
                ref_uris
            )
        else:
            payload["action"] = "generate"

        if char_entries:
            parameters["use_coords"] = True
            parameters["characterPrompts"] = [
                {"prompt": c_prompt, "center": {"x": c_x, "y": c_y}}
                for c_prompt, c_x, c_y in char_entries
            ]
            parameters["v4_prompt"] = {
                "caption": {
                    "base_caption": prompt,
                    "char_captions": [
                        {
                            "char_caption": c_prompt,
                            "centers": [{"x": c_x, "y": c_y}],
                        }
                        for c_prompt, c_x, c_y in char_entries
                    ],
                },
                "use_coords": True,
                "use_order": True,
            }
        payload["parameters"] = parameters

        endpoint = self._openai_endpoint("edits" if is_edit else "generations")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        requested_count = payload["n"]
        request_options = {}
        if starbridge:
            try:
                prepared = await prepare_starbridge_request(
                    self._session,
                    base,
                    payload,
                    headers=headers,
                    timeout=timeout,
                    image_model=starbridge_tool_model,
                )
            except asyncio.TimeoutError:
                return [], "starbridge | 参考图编码超时，未继续生图；编码可能已计费，请确认后再试。"
            except StarbridgeError as exc:
                return [], "starbridge | " + str(exc)
            except (aiohttp.ClientError, OSError):
                return [], "starbridge | 参考图处理请求中断，未自动重试；请确认编码费用后再试。"
            endpoint, payload = prepared.endpoint, prepared.payload
            headers["Accept"] = "application/json"
            request_options["allow_redirects"] = False

        if is_director:
            mode_desc = f"director/{director_action}"
        elif is_edit:
            mode_desc = "img2img"
        elif ref_uris and ref_mode == "director":
            mode_desc = "director_ref"
        elif ref_uris:
            mode_desc = "vibe"
        else:
            mode_desc = "t2i"
        logger.info(
            f"{LOG_TAG} [openai_generate] endpoint={endpoint} model={model or '(none)'} "
            f"mode={mode_desc} size={size} n={requested_count} steps={_steps} scale={_scale} "
            f"seed={_seed if _seed >= 0 else 'random'} chars={len(char_entries)} "
            f"refs={len(ref_uris)} ref_bytes={sum(len(b) for b in ref_bytes_list)}"
        )

        # ==== 发送请求：可重试状态码按 2/4/8 秒指数退避（§14）；超时不重试 ====
        status = 0
        text = ""
        # Native generation and Vibe encoding are billable POSTs. A failed
        # delivery must not silently submit another paid request.
        max_attempts = 1 if starbridge else self.openai_max_retries + 1
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                delay = OPENAI_RETRY_DELAYS[
                    min(attempt - 2, len(OPENAI_RETRY_DELAYS) - 1)
                ]
                logger.info(
                    f"{LOG_TAG} [openai_generate] 第 {attempt - 1}/{self.openai_max_retries} "
                    f"次重试，等待 {delay:g}s（last_status={status or 'timeout'}）"
                )
                await asyncio.sleep(delay)
            try:
                async with self._session.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                    **request_options,
                ) as resp:
                    text = await resp.text()
                    status = resp.status
            except asyncio.TimeoutError:
                # 超时不自动重试：生图请求超时后上游通常仍在继续生成并照常
                # 扣费，自动重试会导致一次需求被多次扣费。直接失败并提示
                # 用户稍后手动重试。
                status, text = 0, ""
                logger.warning(
                    f"{LOG_TAG} [openai_generate] 请求超时（第 {attempt}/{max_attempts} 次），"
                    "上游可能仍在生成（可能已扣费），不再自动重试"
                )
                break
            except Exception as exc:
                logger.warning(
                    f"{LOG_TAG} [openai_generate] 异常: {type(exc).__name__}: {exc}"
                )
                return [], "exception"
            if status < 400:
                break
            retryable = status in OPENAI_RETRYABLE_STATUS
            if not retryable:
                # 网关会把上游瞬时故障包装成 400（bad_response_status_code），
                # 错误文案明确提示"请稍后重试"时同样按可重试处理。
                _low = text.casefold()
                retryable = any(marker in _low for marker in OPENAI_TRANSIENT_MARKERS)
                if retryable:
                    logger.info(
                        f"{LOG_TAG} [openai_generate] HTTP {status} 错误文案提示稍后重试，"
                        "按可重试处理"
                    )
            if not retryable:
                break
            logger.warning(
                f"{LOG_TAG} [openai_generate] HTTP {status}（第 {attempt}/{max_attempts} 次）| "
                f"body={text[:300]}"
            )

        if status == 0:
            return [], "timeout"
        if starbridge and 300 <= status < 400:
            return [], "starbridge | 接口发生重定向，请填写最终的 HTTPS 站点地址。"
        if status >= 400:
            # 尽量提取上游 error.message，透传给用户（如"降低尺寸、步数"建议）
            err_msg = ""
            try:
                _err = json.loads(text)
                if isinstance(_err, dict):
                    _err_item = _err.get("error")
                    if isinstance(_err_item, dict):
                        err_msg = str(_err_item.get("message") or "").strip()
                    elif isinstance(_err_item, str):
                        err_msg = _err_item.strip()
                    if starbridge:
                        err_msg = native_error(_err, status)
            except Exception:
                pass
            reason = f"http_{status}"
            if err_msg:
                reason += f" | {err_msg[:200]}"
            # 精准参考（§6）按文档格式提交仍被上游 400 拒绝：通常是该 Key/中转
            # 未开通精准参考能力，单独标记以便给出针对性提示。
            if (
                status == 400
                and ref_uris
                and ref_mode == "director"
                and not director_action
                and not starbridge
            ):
                reason = "director_ref_rejected"
                if err_msg:
                    reason += f" | {err_msg[:200]}"
            logger.warning(
                f"{LOG_TAG} [openai_generate] HTTP {status} | body={text[:300]}"
            )
            return [], reason

        # 手动解析 JSON：上游异常时可能返回 text/html 页面，resp.json()
        # 会因 mimetype 不匹配抛 ContentTypeError，这里改为读取文本解析。
        try:
            _data = json.loads(text)
        except Exception:
            logger.warning(
                f"{LOG_TAG} [openai_generate] 响应非 JSON（status={status}） | "
                f"body={text[:300]}"
            )
            return [], "invalid_response"

        if starbridge:
            _data = normalize_starbridge_response(_data)
        items = _data.get("data") if isinstance(_data, dict) else []
        if not isinstance(items, list) or not items:
            return [], "empty_response"

        images: list[bytes] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            b64 = str(item.get("b64_json") or "").strip()
            url = str(item.get("url") or "").strip()
            if b64:
                # 文档要求 b64_json 不带 data URI 前缀（§12），但部分中转会
                # 原样透传 data:image/...;base64, 前缀，这里兼容两种情况。
                if b64.startswith("data:"):
                    b64 = b64.split(",", 1)[-1]
                try:
                    decoded = base64.b64decode(b64)
                    if decoded:
                        images.append(decoded)
                    else:
                        logger.warning(
                            f"{LOG_TAG} [openai_generate] b64_json 解码结果为空"
                        )
                except Exception as exc:
                    logger.warning(
                        f"{LOG_TAG} [openai_generate] b64_json 解码失败: {type(exc).__name__}"
                    )
            elif url:
                if url.startswith("data:"):
                    try:
                        decoded = base64.b64decode(url.split(",", 1)[-1])
                        if decoded:
                            images.append(decoded)
                        else:
                            logger.warning(
                                f"{LOG_TAG} [openai_generate] data URI 内容为空"
                            )
                    except Exception as exc:
                        logger.warning(
                            f"{LOG_TAG} [openai_generate] data URI 解码失败: "
                            f"{type(exc).__name__}"
                        )
                    continue
                try:
                    async with self._session.get(
                        url, timeout=aiohttp.ClientTimeout(total=60)
                    ) as dl_resp:
                        if dl_resp.status == 200:
                            downloaded = await dl_resp.read()
                            if downloaded:
                                images.append(downloaded)
                            else:
                                logger.warning(
                                    f"{LOG_TAG} [openai_generate] url 下载内容为空"
                                )
                except Exception as exc:
                    logger.warning(
                        f"{LOG_TAG} [openai_generate] 下载 url 失败: {type(exc).__name__}"
                    )
        if images:
            # OpenAI 兼容端点的实际计费由中转站决定，这里按像素画幅推断档位，
            # 仅作为用量统计的估算值（与直连档位表口径不同，分开记录）。
            _openai_size = str(payload.get("size") or size or "")
            _estimated_cost = estimate_openai_cost(_openai_size, model)
            # 历史归档与直连模式保持一致：所有成功出图的调用点（聊天指令、
            # LLM 工具、陪伴直连、测试面板、本地代理）都会走到这里，因此
            # 只需在 _openai_generate 内统一归档一次。
            generation_parameters: dict[str, Any] = {
                "tag": prompt,
                "model": str(model or ""),
                "size": _openai_size,
                "steps": _steps,
                "scale": _scale,
                "sampler": _sampler,
                "noise_schedule": _noise_schedule,
                "negative": _negative,
                "seed": _seed if _seed >= 0 else "random",
                "mode": mode_desc,
                "transport": "openai",
                "estimated_cost": _estimated_cost,
            }
            if starbridge:
                generation_parameters["api_profile"] = "starbridge"
                generation_parameters["seed"] = payload.get("parameters", {}).get(
                    "seed", "random"
                )
            if ref_uris:
                if is_director:
                    reference_mode_label = f"director-tools/{director_action}"
                elif is_edit:
                    reference_mode_label = "img2img"
                else:
                    reference_mode_label = ref_mode
                generation_parameters["reference_mode"] = reference_mode_label
                generation_parameters["reference_count"] = len(ref_uris)
                if not is_director:
                    generation_parameters["reference_strengths"] = ref_strength_values
            if char_entries:
                generation_parameters["characters"] = [
                    {"prompt": c_prompt, "x": c_x, "y": c_y}
                    for c_prompt, c_x, c_y in char_entries
                ]
            for img_bytes in images:
                await self._archive_generated_image(img_bytes, generation_parameters)
            await self._record_usage_stat(
                _estimated_cost * len(images),
                count=len(images),
                transport="openai",
                model=str(model or ""),
                size=_openai_size,
            )
            return images, "ok"
        return [], "empty_response"

    async def _generate_one_custom(
        self,
        prompt: str,
        style: str,
        size: str,
        *,
        steps: int | None = None,
        scale: float | None = None,
        cfg: float | None = None,
        sampler: str | None = None,
        noise_schedule: str | None = None,
        negative: str | None = None,
        model: str | None = None,
        custom_artists: str | None = None,
        character_preset: str | None = None,
        enable_template: bool | None = None,
        enable_translate: bool | str | None = None,
        token_override: str | None = None,
    ) -> tuple[bytes | None, str]:
        """生成单张图片（全参数可覆盖版本，供测试面板使用）。

        所有可选参数留 None 时使用插件默认配置。
        token_override 非空时用其代替 self.image_gen_key（试用生成）。
        """
        # “自定义”与 custom 等价（面板 / 配置可能传中文值）
        if style == "自定义":
            style = "custom"
        _token = token_override or self.image_gen_key
        if not _token:
            return None, "no_token"
        if not self._session:
            return None, "no_session"

        # 解析参数覆盖，并在入口处收敛到上游有效范围（与 _generate_one 一致）。
        try:
            _steps = max(1, min(50, int(steps if steps is not None else self.steps)))
        except (TypeError, ValueError):
            _steps = 24
        try:
            _scale = max(
                0.0, min(20.0, float(scale if scale is not None else self.scale))
            )
        except (TypeError, ValueError):
            _scale = 6.0
        try:
            _cfg = max(0.0, min(1.0, float(cfg if cfg is not None else self.cfg_value)))
        except (TypeError, ValueError):
            _cfg = 1.0
        _sampler = sampler if sampler is not None else self.sampler
        _noise = noise_schedule if noise_schedule is not None else self.noise_schedule
        _negative = negative if negative is not None else self.negative
        _model = model if model is not None else (self._slot_model() or self.model)
        _enable_template = (
            enable_template if enable_template is not None else self.enable_template
        )
        _translate_mode = normalize_translate_mode(
            enable_translate if enable_translate is not None else self.translate_mode
        )

        # artists 解析：自定义风格允许空画师串（不强制回退预设）
        if style == "custom":
            _artists = (
                custom_artists if custom_artists is not None else self.custom_artists
            )
            if _artists is None:
                _artists = ""
        else:
            _artists = DEFAULT_ARTISTS.get(style, DEFAULT_ARTISTS["vertical"])

        # character_preset
        _char_preset = (
            character_preset if character_preset is not None else self.character_preset
        )

        # 1) 可选转译：自动模式只转译自然语言片段。
        base_prompt, _, _, prompt_kind = await self._prepare_translated_prompt(
            prompt,
            translate_mode=_translate_mode,
            apply_outfit=False,
        )

        # 2) 与角色预设模板合并
        if _enable_template and _char_preset:
            full_prompt = f"{_char_preset}, {base_prompt}"
        else:
            full_prompt = base_prompt
        full_prompt = normalize_prompt(full_prompt)

        logger.info(
            f"{LOG_TAG} [generate:custom] style={style} size={size} "
            f"steps={_steps} scale={_scale} cfg={_cfg} sampler={_sampler} "
            f"model={_model} translate={_translate_mode}/{prompt_kind} "
            f"prompt='{full_prompt[:60]}...'"
        )

        generation_parameters = {
            "tag": full_prompt,
            "model": _model,
            "artist": _artists,
            "size": size,
            "steps": _steps,
            "scale": _scale,
            "cfg": _cfg,
            "sampler": _sampler,
            "negative": _negative,
            "nocache": 1,
            "noise_schedule": _noise,
        }

        # 与 _generate_one 保持一致：任务接口优先（1-50 步、档位计价、
        # 失败退点），上游不支持时自动回退 GET 直链。
        web_img, web_reason = await self._submit_web_job(
            full_prompt,
            _artists,
            size,
            steps=_steps,
            scale=_scale,
            cfg=_cfg,
            sampler=_sampler,
            noise_schedule=_noise,
            negative=_negative,
            model=_model,
            token=_token,
        )
        if web_img is not None:
            generation_parameters["steps"] = effective_transport_steps(
                _steps, TRANSPORT_WEB_JOBS
            )
            generation_parameters["transport"] = "web_jobs"
            generation_parameters["estimated_cost"] = web_job_generation_cost(
                size, _model, _steps
            )
            await self._archive_generated_image(web_img, generation_parameters)
            await self._record_usage_from_parameters(generation_parameters)
            return web_img, "ok"
        if web_reason != "web_jobs_unavailable":
            return None, web_reason
        logger.warning(
            f"{LOG_TAG} [generate:custom] 上游不支持任务接口，回退 GET 直链 | "
            f">{DIRECT_URL_STEPS_MAX} 步将按 {DIRECT_URL_STEPS_MAX} 步路由"
        )
        _steps = effective_transport_steps(_steps, TRANSPORT_GET)
        generation_parameters["steps"] = _steps

        url = (
            f"{self.base_url.rstrip('/')}/generate"
            f"?tag={quote(full_prompt)}"
            f"&token={_token}"
            f"&model={_model}"
            f"&artist={quote(_artists)}"
            f"&size={quote(size)}"
            f"&steps={_steps}"
            f"&scale={_scale}"
            f"&cfg={_cfg}"
            f"&sampler={_sampler}"
            f"&negative={quote(_negative)}"
            f"&nocache=1"
            f"&noise_schedule={_noise}"
        )
        # 脱敏日志：不输出明文 token
        safe_url = url.replace(f"&token={_token}", "&token=***")
        logger.debug(f"{LOG_TAG} [generate:custom] request url = {safe_url}")

        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=self.request_timeout)
            ) as resp:
                if resp.status != 200:
                    # 读取上游错误正文，方便排查
                    err_body = ""
                    try:
                        err_body = await resp.text()
                    except Exception:
                        pass
                    logger.warning(
                        f"{LOG_TAG} [generate:custom] 上游返回 {resp.status} | "
                        f"size={size} body='{err_body[:300]}'"
                    )
                    # 将状态码和错误摘要编入 reason，让前端可见
                    err_summary = (
                        err_body[:200].replace("\n", " ").strip() if err_body else ""
                    )
                    if 400 <= resp.status < 500:
                        reason = f"http_4xx (HTTP {resp.status})"
                    elif 500 <= resp.status < 600:
                        reason = f"http_5xx (HTTP {resp.status})"
                    else:
                        reason = f"http_other (HTTP {resp.status})"
                    if err_summary:
                        reason = f"{reason}: {err_summary}"
                    return None, reason
                img_bytes = await resp.read()
                if not img_bytes:
                    return None, "empty_response"
                # 解析 PNG 尺寸用于日志排查
                _w, _h = 0, 0
                if len(img_bytes) >= 24 and img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                    import struct

                    _w = struct.unpack(">I", img_bytes[16:20])[0]
                    _h = struct.unpack(">I", img_bytes[20:24])[0]
                logger.info(
                    f"{LOG_TAG} [generate:custom] 成功 | size={size} "
                    f"png={_w}x{_h} bytes={len(img_bytes)}"
                )
                generation_parameters["transport"] = TRANSPORT_GET
                generation_parameters["estimated_cost"] = direct_url_generation_cost(
                    size, _model
                )
                await self._archive_generated_image(
                    img_bytes,
                    generation_parameters,
                )
                await self._record_usage_from_parameters(generation_parameters)
                return img_bytes, "ok"
        except asyncio.TimeoutError:
            return None, "timeout"
        except Exception as e:
            logger.warning(f"{LOG_TAG} [generate:custom] 异常: {e!r}")
            return None, "exception"

    # ==== 试用生成：代码内 XOR 混淆方案 ====

    @staticmethod
    def _decrypt_trial_key(encrypted_b64: str) -> str:
        """XOR 解密代码内混淆的试用密钥。"""
        encrypted = base64.b64decode(encrypted_b64.strip())
        key_len = len(_TRIAL_OBF_KEY)
        decrypted = bytes(
            encrypted[i] ^ _TRIAL_OBF_KEY[i % key_len] for i in range(len(encrypted))
        )
        return decrypted.decode("utf-8").rstrip("\x00").strip()

    async def _init_trial_feature(self) -> None:
        """初始化试用功能：解密代码内混淆密钥 + 加载本地试用次数。"""
        import json

        # 1) 解密代码内混淆密钥（无网络依赖）
        if _TRIAL_KEY_ENC:
            try:
                self._trial_key = self._decrypt_trial_key(_TRIAL_KEY_ENC)
                logger.info(f"{LOG_TAG} [trial] 试用密钥解密成功")
            except Exception as e:
                logger.warning(f"{LOG_TAG} [trial] 试用密钥解密失败: {e!r}")
        else:
            logger.info(f"{LOG_TAG} [trial] 未配置试用密钥（_TRIAL_KEY_ENC 为空）")

        # 2) 加载本地试用次数
        try:
            trial_dir = self._get_plugin_data_dir()
            trial_dir.mkdir(parents=True, exist_ok=True)
            trial_file = trial_dir / "trial_usage.json"
            self._trial_usage_file = str(trial_file)
            if trial_file.exists():
                data = json.loads(trial_file.read_text(encoding="utf-8"))
                self._trial_usage_count = max(0, int(data.get("count", 0)))
                logger.info(
                    f"{LOG_TAG} [trial] 本地试用次数: {self._trial_usage_count}/{TRIAL_MAX_USES}"
                )
            else:
                logger.info(f"{LOG_TAG} [trial] 无本地试用记录，从 0 开始")
        except Exception as e:
            logger.warning(f"{LOG_TAG} [trial] 加载试用次数异常: {e!r}")

    def _save_trial_usage(self) -> None:
        """持久化试用次数到本地文件。"""
        import json
        from pathlib import Path

        if not self._trial_usage_file:
            return
        try:
            Path(self._trial_usage_file).write_text(
                json.dumps({"count": self._trial_usage_count}),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"{LOG_TAG} [trial] 保存试用次数异常: {e!r}")

    async def _test_panel_trial_status(self) -> Any:
        """Web API: 返回试用生成状态。"""
        return json_response(
            {
                "available": bool(self._trial_key)
                and self._trial_usage_count < TRIAL_MAX_USES,
                "key_loaded": bool(self._trial_key),
                "used": self._trial_usage_count,
                "max_uses": TRIAL_MAX_USES,
                "remaining": max(0, TRIAL_MAX_USES - self._trial_usage_count),
            }
        )

    async def _test_panel_trial_generate(self) -> Any:
        """Web API: 使用试用密钥生图（每次调用 +1 次数，达上限拒绝）。"""
        # 检查密钥
        if not self._trial_key:
            return json_response(
                {
                    "status": "error",
                    "message": "试用密钥未加载，请稍后重试或联系插件作者。",
                },
                status_code=503,
            )

        # 检查次数
        if self._trial_usage_count >= TRIAL_MAX_USES:
            return json_response(
                {
                    "status": "error",
                    "message": f"试用次数已达上限（{TRIAL_MAX_USES} 次）。请配置自己的密钥后使用正式生图。",
                    "reason": "trial_exhausted",
                },
                status_code=403,
            )

        # 解析请求
        try:
            body = await web_request.json(default={})
        except Exception:
            return error_response("请求体解析失败", status_code=400)

        nai_prompt = normalize_prompt(self._expand_grimoire(body.get("nai_prompt") or ""))
        nl_prompt = normalize_prompt(self._expand_grimoire(body.get("nl_prompt") or ""))
        if not nai_prompt and not nl_prompt:
            return error_response("请至少填写一个提示词框", status_code=400)

        style = body.get("style") or self.image_style
        size = self._resolve_direct_size(str(body.get("size") or self.image_size))

        # 解析可选覆盖参数（与正式生成一致）
        def _opt_int(key: str) -> int | None:
            val = body.get(key)
            if val is None or val == "":
                return None
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        def _opt_float(key: str) -> float | None:
            val = body.get(key)
            if val is None or val == "":
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        def _opt_str(key: str) -> str | None:
            val = body.get(key)
            if val is None:
                return None
            return str(val)

        # ==== 转译 + 合并 ====
        translated_nl = ""
        if nl_prompt:
            translated = await self._translate_prompt(nl_prompt)
            translated_nl = translated if translated else nl_prompt

        parts = [p for p in [nai_prompt, translated_nl] if p]
        full_prompt = ", ".join(parts)

        merge_info = {
            "nai_prompt": nai_prompt,
            "nl_prompt": nl_prompt,
            "translated_nl": translated_nl,
            "full_prompt": full_prompt,
        }

        # 试用生成固定 1 张，但传递面板上的所有参数
        try:
            img_bytes, reason = await self._generate_one_custom(
                full_prompt,
                style,
                size,
                steps=_opt_int("steps"),
                scale=_opt_float("scale"),
                cfg=_opt_float("cfg"),
                sampler=_opt_str("sampler"),
                noise_schedule=_opt_str("noise_schedule"),
                negative=_opt_str("negative"),
                model=_opt_str("model"),
                custom_artists=_opt_str("custom_artists"),
                token_override=self._trial_key,
                character_preset="",  # 面板独立，不合并 settings 的角色预设
                enable_template=False,  # 面板独立，不套用 settings 的模板
                enable_translate=False,  # 转译已在上方完成
            )
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [test_panel:trial] 生成异常: {exc!r}")
            img_bytes, reason = None, "exception"

        if not img_bytes:
            return json_response(
                {
                    "status": "error",
                    "message": _format_generate_error(reason),
                    "reason": reason,
                },
                status_code=502,
            )

        # 成功：次数 +1 并持久化
        self._trial_usage_count += 1
        self._save_trial_usage()
        logger.info(
            f"{LOG_TAG} [trial] 试用生成成功 | "
            f"used={self._trial_usage_count}/{TRIAL_MAX_USES}"
        )

        b64 = base64.b64encode(img_bytes).decode()
        return json_response(
            {
                "status": "ok",
                "data": [{"b64_json": b64}],
                "merge_info": merge_info,
                "trial_used": self._trial_usage_count,
                "trial_remaining": max(0, TRIAL_MAX_USES - self._trial_usage_count),
            }
        )

    async def _test_panel_save_cache(self) -> Any:
        """Web API: 保存面板状态缓存到本地文件（替代 localStorage，因为 iframe sandbox 限制）。"""
        import json

        try:
            body = await web_request.json(default={})
        except Exception:
            return error_response("请求体解析失败", status_code=400)

        try:
            cache_dir = self._get_plugin_data_dir()
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = cache_dir / "panel_cache.json"
            cache_file.write_text(
                json.dumps(body, ensure_ascii=False),
                encoding="utf-8",
            )
            return json_response({"status": "ok"})
        except Exception as e:
            logger.warning(f"{LOG_TAG} [panel_cache] 保存失败: {e!r}")
            return error_response(f"缓存保存失败: {e!r}", status_code=500)

    async def _test_panel_load_cache(self) -> Any:
        """Web API: 从本地文件加载面板状态缓存。"""
        import json

        try:
            cache_file = self._get_plugin_data_dir() / "panel_cache.json"
            if not cache_file.exists():
                return json_response({"status": "ok", "data": None})
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return json_response({"status": "ok", "data": data})
        except Exception as e:
            logger.warning(f"{LOG_TAG} [panel_cache] 加载失败: {e!r}")
            return json_response({"status": "ok", "data": None})

    async def _test_panel_usage(self) -> Any:
        """Web API: 点数用量统计（7/30/90 日窗口 + 90 日逐日序列）。

        前端按选中的窗口切片渲染柱状图/折线图，无需后端再接受查询参数。
        """

        windows: dict[str, Any] = {}
        breakdowns: dict[str, Any] = {}
        series: list[dict[str, Any]] = []
        overall: dict[str, Any] = {}
        today: dict[str, Any] = {}
        upstream: dict[str, Any] = {}
        upstream_enabled = False
        first_day = ""
        enabled = True
        for days in (7, 30, 90):
            payload = await self._usage_stats_payload(days)
            enabled = bool(payload.get("enabled", True))
            windows[str(days)] = payload.get("window", {})
            breakdowns[str(days)] = payload.get("breakdown", {})
            if days == 90:
                series = payload.get("series", [])
                overall = payload.get("overall", {})
                today = payload.get("today", {})
                upstream = payload.get("upstream", {})
                upstream_enabled = bool(payload.get("upstream_enabled", False))
                first_day = payload.get("first_day", "")
        return json_response(
            {
                "status": "ok",
                "data": {
                    "enabled": enabled,
                    "first_day": first_day,
                    "today": today,
                    "overall": overall,
                    "windows": windows,
                    "breakdowns": breakdowns,
                    "series": series,
                    "upstream": upstream,
                    "upstream_enabled": upstream_enabled,
                },
            }
        )

    async def _test_panel_slots(self) -> Any:
        """Web API: 读取画风槽位表与当前生效槽位。"""
        try:
            slots = await asyncio.to_thread(self._load_artist_slots)
            # 顺手把画风名同步到设置页下拉，用户打开面板即完成一次同步。
            await asyncio.to_thread(self._sync_slot_labels, slots)
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [panel:slots] 读取失败: {exc!r}")
            return error_response(f"读取槽位失败: {exc!r}", status_code=500)
        return json_response(
            {
                "status": "ok",
                "data": {
                    "slots": slots,
                    "active_slot_id": self.active_artist_slot,
                    "enabled": self._artist_slots_enabled(),
                    "model_options": OPENAI_COMPATIBLE_MODELS,
                },
            }
        )

    async def _test_panel_slots_save(self) -> Any:
        """Web API: 保存画风槽位表；可选同时切换正式生效槽位。"""
        try:
            body = await web_request.json(default={})
        except Exception:
            return error_response("请求体解析失败", status_code=400)
        if not isinstance(body, dict):
            return error_response("请求体格式错误", status_code=400)

        raw_slots = body.get("slots")
        if not isinstance(raw_slots, list) or not raw_slots:
            return error_response("slots 必须是非空数组", status_code=400)
        try:
            await asyncio.to_thread(self._save_artist_slots, raw_slots)
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [panel:slots] 保存失败: {exc!r}")
            return error_response(f"保存槽位失败: {exc!r}", status_code=400)

        active = body.get("active_slot_id")
        if active not in (None, ""):
            await asyncio.to_thread(self._persist_active_slot, str(active))

        slots = await asyncio.to_thread(self._load_artist_slots)
        logger.info(
            f"{LOG_TAG} [panel:slots] 已保存 | count={len(slots)} "
            f"active={self.active_artist_slot}"
        )
        return json_response(
            {
                "status": "ok",
                "data": {"slots": slots, "active_slot_id": self.active_artist_slot},
            }
        )

    async def _test_panel_slots_reset(self) -> Any:
        """Web API: 用内置画风重建槽位表（会覆盖现有槽位）。"""
        try:
            slots = await asyncio.to_thread(self._default_artist_slots)
            await asyncio.to_thread(self._save_artist_slots, slots)
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [panel:slots] 重置失败: {exc!r}")
            return error_response(f"恢复内置画风失败: {exc!r}", status_code=500)
        logger.info(f"{LOG_TAG} [panel:slots] 已按内置画风重建 | count={len(slots)}")
        return json_response(
            {
                "status": "ok",
                "data": {"slots": slots, "active_slot_id": self.active_artist_slot},
            }
        )

    async def _test_panel_grimoire_search(self) -> Any:
        """Web API: 按当前生效模型检索内置法典词库。"""
        if not self._grimoire_enabled():
            return json_response(
                {
                    "status": "disabled",
                    "data": {
                        "items": [],
                        "message": "内置法典词库未启用，请到插件设置里打开「启用内置法典词库」。",
                    },
                }
            )
        try:
            body = await web_request.json(default={})
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        query = str(body.get("query") or "").strip()
        try:
            limit = int(body.get("limit") or 12)
        except (TypeError, ValueError):
            limit = 12
        try:
            hits = await asyncio.to_thread(
                search_grimoire, query, limit, self._active_model_ver()
            )
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [panel:grimoire] 检索失败: {exc!r}")
            return error_response(f"检索失败: {exc!r}", status_code=500)
        return json_response(
            {
                "status": "ok",
                "data": {
                    "items": hits,
                    "model_ver": self._active_model_ver(),
                    "total_db": 19663,
                },
            }
        )


    async def _test_panel_get_config(self) -> Any:
        """Web API: 返回当前插件配置（脱敏 token）。"""
        return json_response(
            {
                "image_gen_key": "已配置" if self.image_gen_key else "未配置",
                "base_url": self.base_url,
                "show_cost_estimate": self.show_cost_estimate,
                "image_style": self.image_style,
                "image_size": self.image_size,
                "image_count": self.image_count,
                "bot_reply_mode": self.bot_reply_mode,
                "save_image_history": self.save_image_history,
                "save_generation_parameters": self.save_generation_parameters,
                "image_history_limit": self.image_history_limit,
                "enable_usage_stats": self.enable_usage_stats,
                "enable_upstream_billing_sync": self.enable_upstream_billing_sync,
                "upstream_billing_sync_interval": self.upstream_billing_sync_interval,
                "custom_artists": self.custom_artists,
                "model": self.model,
                "steps": self.steps,
                "scale": self.scale,
                "cfg": self.cfg_value,
                "sampler": self.sampler,
                "noise_schedule": self.noise_schedule,
                "negative": self.negative,
                "enable_template": self.enable_template,
                "character_preset": self.character_preset,
                "default_outfit": self.default_outfit,
                "enable_translate": self.translate_mode,
                "translate_provider": self.translate_provider,
                "proxy_port": self.proxy_port,
                "enable_proxy": self.enable_proxy,
                "bypass_system_proxy": self.bypass_system_proxy,
                "enable_companion_link": self.enable_companion_link,
                "companion_prompt_format": self.companion_prompt_format,
                "companion_image_retention_days": self.companion_image_retention_days,
                "image_styles_options": IMAGE_STYLES,
                "image_size_options": IMAGE_SIZES,
                "default_negative": DEFAULT_NEGATIVE,
                "openai_api_base_url": self.openai_api_base_url or "未配置",
                "openai_api_model": self.openai_api_model or "未配置",
                "openai_available": bool(
                    self.openai_api_base_url and self.openai_api_key
                ),
                "openai_reference_mode": self.openai_reference_mode,
                "openai_director_caption": self.openai_director_caption,
                "openai_vibe_strength": self.openai_vibe_strength,
                "openai_director_strength": self.openai_director_strength,
                "openai_director_secondary_strength": self.openai_director_secondary_strength,
                "openai_seed": self.openai_seed,
                "request_timeout": self.request_timeout,
                "openai_max_retries": self.openai_max_retries,
                "openai_models": OPENAI_COMPATIBLE_MODELS,
                "openai_director_actions": OPENAI_DIRECTOR_ACTIONS,
                "call_mode": self.call_mode,
            }
        )

    async def _test_panel_generate(self) -> Any:
        """Web API: 接收双提示词生图请求，后端转译+合并后生成，返回 base64 图片 + 合并步骤。"""
        try:
            body = await web_request.json(default={})
        except Exception:
            return error_response("请求体解析失败", status_code=400)

        nai_prompt = normalize_prompt(self._expand_grimoire(body.get("nai_prompt") or ""))
        nl_prompt = normalize_prompt(self._expand_grimoire(body.get("nl_prompt") or ""))
        if not nai_prompt and not nl_prompt:
            return error_response("请至少填写一个提示词框", status_code=400)

        style = body.get("style") or self.image_style
        size = self._resolve_direct_size(str(body.get("size") or self.image_size))

        # 解析可选覆盖参数
        def _opt_int(key: str) -> int | None:
            val = body.get(key)
            if val is None or val == "":
                return None
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        def _opt_float(key: str) -> float | None:
            val = body.get(key)
            if val is None or val == "":
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        def _opt_str(key: str) -> str | None:
            val = body.get(key)
            if val is None:
                return None
            return str(val)

        try:
            n = max(1, min(4, int(body.get("n") or 1)))
        except (TypeError, ValueError):
            n = 1

        # ==== 转译 + 合并 ====
        translated_nl = ""
        if nl_prompt:
            translated = await self._translate_prompt(nl_prompt)
            translated_nl = translated if translated else nl_prompt

        # 合并：两者都有用逗号连接，只有一方则直接用
        parts = [p for p in [nai_prompt, translated_nl] if p]
        full_prompt = ", ".join(parts)

        merge_info = {
            "nai_prompt": nai_prompt,
            "nl_prompt": nl_prompt,
            "translated_nl": translated_nl,
            "full_prompt": full_prompt,
        }

        logger.info(
            f"{LOG_TAG} [test_panel:generate] merge: "
            f"nai='{nai_prompt[:40]}' nl='{nl_prompt[:40]}' "
            f"translated='{translated_nl[:40]}' full='{full_prompt[:60]}'"
        )

        # ==== 逐张生成（每张独立调用，避免复制同一张图） ====
        images_b64: list[str] = []
        first_reason: str | None = None
        for i in range(n):
            logger.info(f"{LOG_TAG} [test_panel:generate] 生成第 {i + 1}/{n} 张")
            try:
                img_bytes, reason = await self._generate_one_custom(
                    full_prompt,
                    style,
                    size,
                    steps=_opt_int("steps"),
                    scale=_opt_float("scale"),
                    cfg=_opt_float("cfg"),
                    sampler=_opt_str("sampler"),
                    noise_schedule=_opt_str("noise_schedule"),
                    negative=_opt_str("negative"),
                    model=_opt_str("model"),
                    custom_artists=_opt_str("custom_artists"),
                    character_preset="",  # 面板独立，不合并 settings 的角色预设
                    enable_template=False,  # 面板独立，不套用 settings 的模板
                    enable_translate=False,  # 转译已在上方完成
                )
            except Exception as exc:
                logger.warning(
                    f"{LOG_TAG} [test_panel:generate] 第 {i + 1} 张异常: {exc!r}"
                )
                img_bytes, reason = None, "exception"
            if img_bytes:
                images_b64.append(base64.b64encode(img_bytes).decode())
            else:
                first_reason = first_reason or reason
                logger.warning(
                    f"{LOG_TAG} [test_panel:generate] 第 {i + 1}/{n} 张失败 | reason={reason}"
                )

        if not images_b64:
            return json_response(
                {
                    "status": "error",
                    "message": _format_generate_error(first_reason or "unknown"),
                    "reason": first_reason,
                },
                status_code=502,
            )

        return json_response(
            {
                "status": "ok",
                "data": [{"b64_json": b64} for b64 in images_b64],
                "merge_info": merge_info,
                "elapsed_info": f"{len(images_b64)} 张",
            }
        )

    async def _test_panel_generate_openai(self) -> Any:
        """Web API: OpenAI 兼容格式生图（支持本地上传多张参考图）。

        面板切换「OpenAI 兼容」后走此接口，请求体与 ``test_panel/generate``
        一致（双提示词 + 生成参数），额外支持 ``reference_image_b64_list``
        （data URL 或裸 base64 的数组，最多 8 张，兼容旧的单一
        ``reference_image_b64`` 字段）、逐图强度 ``reference_strengths``、
        逐图精准参考描述 ``director_captions`` 与 img2img 的 ``strength``。
        实际生图由 ``_openai_generate`` 完成，接口地址/密钥/模型由插件配置项
        openai_api_* 决定。
        """
        try:
            body = await web_request.json(default={})
        except Exception:
            return error_response("请求体解析失败", status_code=400)

        nai_prompt = normalize_prompt(self._expand_grimoire(body.get("nai_prompt") or ""))
        nl_prompt = normalize_prompt(self._expand_grimoire(body.get("nl_prompt") or ""))

        # director-tools 图片处理动作（§8）：指定时强制使用 director-tools 模型，
        # 提示词可为空，但必须附带一张源图片。
        director_action = str(body.get("director_action") or "").strip()
        if director_action and director_action not in OPENAI_DIRECTOR_ACTIONS:
            return error_response(
                f"不支持的处理动作: {director_action}", status_code=400
            )

        if not nai_prompt and not nl_prompt and not director_action:
            return error_response("请至少填写一个提示词框", status_code=400)

        if not self.openai_api_base_url:
            return error_response(
                "未配置 OpenAI 兼容格式地址（openai_api_base_url），请在插件设置中填写。",
                status_code=400,
            )

        size = body.get("size") or "方图"
        try:
            n = max(1, min(4, int(body.get("n") or 1)))
        except (TypeError, ValueError):
            n = 1

        # 本地上传参考图：优先 reference_image_b64_list 数组（最多 8 张，§5.2），
        # 兼容旧的单一 reference_image_b64 字段；各项为 data URL 或裸 base64
        raw_refs: list[str] = []
        raw_ref_list = body.get("reference_image_b64_list")
        if isinstance(raw_ref_list, str):
            raw_ref_list = [raw_ref_list]
        if isinstance(raw_ref_list, list):
            raw_refs.extend(str(item or "").strip() for item in raw_ref_list)
        legacy_ref = str(body.get("reference_image_b64") or "").strip()
        if legacy_ref:
            raw_refs.insert(0, legacy_ref)
        raw_refs = [item for item in raw_refs if item][:OPENAI_MAX_REFERENCE_IMAGES]
        reference_image_bytes_list: list[bytes] = []
        for idx, raw_ref in enumerate(raw_refs, start=1):
            try:
                if raw_ref.startswith("data:"):
                    raw_ref = raw_ref.split(",", 1)[1]
                decoded = base64.b64decode(raw_ref)
            except Exception as exc:
                logger.warning(
                    f"{LOG_TAG} [test_panel:generate_openai] 第 {idx} 张参考图 "
                    f"base64 解析失败: {exc!r}"
                )
                return error_response(
                    f"第 {idx} 张参考图数据解析失败，请重新上传", status_code=400
                )
            if not decoded:
                return error_response(
                    f"第 {idx} 张参考图数据为空，请重新上传", status_code=400
                )
            reference_image_bytes_list.append(decoded)
        if director_action and not reference_image_bytes_list:
            return error_response("图片处理动作需要上传一张源图片", status_code=400)

        strength: float | None = None
        raw_strength = body.get("strength")
        if raw_strength not in (None, ""):
            try:
                strength = float(raw_strength)
                if not (0 < strength <= 1):
                    strength = None
            except (TypeError, ValueError):
                strength = None

        # 逐图参考强度（vibe / 精准参考）：与参考图按下标对应，0-1。
        # 非法项保留 None 占位，避免后面的参考图强度整体前移。
        reference_strengths: list[float | None] | None = None
        raw_strengths = body.get("reference_strengths")
        if isinstance(raw_strengths, (list, tuple)):
            parsed_strengths: list[float | None] = []
            for item in raw_strengths:
                try:
                    value = float(item)
                except (TypeError, ValueError):
                    parsed_strengths.append(None)
                    continue
                parsed_strengths.append(round(value, 2) if 0 <= value <= 1 else None)
            reference_strengths = parsed_strengths or None

        noise: float | None = None
        raw_noise = body.get("noise")
        if raw_noise not in (None, ""):
            try:
                noise = float(raw_noise)
                if not (0 <= noise <= 1):
                    noise = None
            except (TypeError, ValueError):
                noise = None

        # 参考图使用模式：面板可覆盖插件配置（vibe / img2img / director 精准参考）
        reference_mode = str(body.get("reference_mode") or "").strip().casefold()
        if reference_mode not in ("vibe", "img2img", "director"):
            reference_mode = None
        if reference_mode == "director" and not reference_image_bytes_list:
            return error_response(
                "精准参考（director）需要至少上传一张参考图", status_code=400
            )

        # 解析可选覆盖参数
        def _opt_int(key: str) -> int | None:
            val = body.get(key)
            if val is None or val == "":
                return None
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        def _opt_str(key: str) -> str | None:
            val = body.get(key)
            if val is None:
                return None
            return str(val)

        steps = _opt_int("steps")
        seed = _opt_int("seed")
        negative = _opt_str("negative")
        sampler = _opt_str("sampler")
        noise_schedule = _opt_str("noise_schedule")
        raw_scale = body.get("scale")
        try:
            scale = float(raw_scale) if raw_scale not in (None, "") else None
        except (TypeError, ValueError):
            scale = None
        style = body.get("style") or self.image_style
        custom_artists = _opt_str("custom_artists")
        model = _opt_str("model")
        director_caption = _opt_str("director_caption")

        # 逐图精准参考描述（§8.4）：面板传 base_caption 数组，与参考图按下标
        # 对应；非法值由 _openai_generate 回退统一配置处理。
        director_captions: list[str] | None = None
        raw_captions = body.get("director_captions")
        if isinstance(raw_captions, (list, tuple)):
            # 保留空字符串占位，确保第 N 张参考图的描述不会错位到第 N-1 张。
            director_captions = [
                str(item or "").strip() for item in raw_captions
            ] or None

        # 多角色坐标（§7）：面板传 [{prompt, x, y}, ...]，坐标 0-1，最多 6 个
        characters: list[tuple[str, float, float]] | None = None
        raw_chars = body.get("characters")
        if isinstance(raw_chars, list) and raw_chars:
            characters = []
            for idx, item in enumerate(raw_chars[:6], start=1):
                if not isinstance(item, dict):
                    continue
                c_prompt = normalize_prompt(item.get("prompt"))
                if not c_prompt:
                    continue
                try:
                    c_x = float(item.get("x", 0.5))
                    c_y = float(item.get("y", 0.5))
                except (TypeError, ValueError):
                    return error_response(
                        f"第 {idx} 个角色的坐标必须是数字", status_code=400
                    )
                if not (0 <= c_x <= 1 and 0 <= c_y <= 1):
                    return error_response(
                        f"第 {idx} 个角色的坐标必须在 0-1 之间", status_code=400
                    )
                characters.append((c_prompt, c_x, c_y))
            characters = characters or None

        # ==== 转译 + 合并 ====
        translated_nl = ""
        if nl_prompt:
            translated = await self._translate_prompt(nl_prompt)
            translated_nl = translated if translated else nl_prompt

        base_prompt = normalize_prompt(
            ", ".join([p for p in [nai_prompt, translated_nl] if p])
        )
        # 画师串作为风格前缀拼进 OpenAI 兼容格式的 prompt
        full_prompt = self._openai_prompt(base_prompt, style, custom_artists)

        merge_info = {
            "nai_prompt": nai_prompt,
            "nl_prompt": nl_prompt,
            "translated_nl": translated_nl,
            "style": style,
            "model": model,
            "full_prompt": full_prompt,
        }

        # NAI 尺寸键 → OpenAI 兼容像素尺寸
        pixel_size = self._openai_size(size)

        logger.info(
            f"{LOG_TAG} [test_panel:generate_openai] "
            f"prompt='{full_prompt[:60]}' size={size}→{pixel_size} n={n} "
            f"reference={len(reference_image_bytes_list)}张 strength={strength} "
            f"steps={steps} seed={seed} model={model} style={style} "
            f"ref_mode={reference_mode or self.openai_reference_mode} "
            f"director={director_action or '-'} chars={len(characters or [])}"
        )

        # ==== 调用 OpenAI 兼容格式（一次调用生成 n 张） ====
        try:
            images, reason = await self._openai_generate(
                full_prompt,
                pixel_size,
                n=n,
                reference_image_bytes_list=reference_image_bytes_list or None,
                strength=strength,
                reference_strengths=reference_strengths,
                noise=noise,
                steps=steps,
                scale=scale,
                sampler=sampler,
                noise_schedule=noise_schedule,
                seed=seed,
                negative=negative,
                model=model,
                reference_mode=reference_mode,
                director_action=director_action or None,
                director_caption=director_caption,
                director_captions=director_captions,
                characters=characters,
            )
        except Exception as exc:
            logger.warning(
                f"{LOG_TAG} [test_panel:generate_openai] _openai_generate 异常: {exc!r}"
            )
            return json_response(
                {"status": "error", "message": f"生成异常: {exc!r}"},
                status_code=500,
            )

        if not images:
            return json_response(
                {
                    "status": "error",
                    "message": _format_generate_error(reason or "unknown"),
                    "reason": reason,
                },
                status_code=502,
            )

        images_b64 = [base64.b64encode(img).decode() for img in images]
        return json_response(
            {
                "status": "ok",
                "data": [{"b64_json": b64} for b64 in images_b64],
                "merge_info": merge_info,
                "elapsed_info": f"{len(images_b64)} 张",
                "call_format": "openai",
                "reference_used": bool(reference_image_bytes_list),
            }
        )

    async def _fetch_quota(self, *, quiet: bool = False) -> dict[str, Any]:
        """向上游查询当前 token 的剩余额度与状态。

        Args:
            quiet: 为 True 时只返回结果，不输出查询/失败日志；供后台余额
                同步避免每个轮询周期刷日志。

        Returns:
            成功时 ``{"ok": True, "value": int, "balance": float, "enabled": bool}``；
            失败时 ``{"ok": False, "message": str}``，message 为上游返回的
            业务错误（如 user not found）或本地网络/解析错误摘要。
        """
        if not self.image_gen_key or not self._session:
            if not quiet:
                logger.warning(f"{LOG_TAG} [quota] 跳过：token 或 session 缺失")
            return {
                "ok": False,
                "message": "插件未配置 image_gen_key 或 session 未初始化，请重载插件",
            }
        url = f"{self.base_url.rstrip('/')}/api/api/getUser"
        if not quiet:
            logger.info(f"{LOG_TAG} [quota] 查询中... | url={url}")
        try:
            async with self._session.post(
                url,
                json={"toUserId": self.image_gen_key},
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                logger.debug(f"{LOG_TAG} [quota] HTTP {resp.status}")
                if resp.status != 200:
                    if not quiet:
                        logger.warning(f"{LOG_TAG} [quota] 非 200 响应: {resp.status}")
                    return {"ok": False, "message": f"上游返回 HTTP {resp.status}"}
                data = await resp.json()
                logger.debug(f"{LOG_TAG} [quota] response data: {data}")
                if not isinstance(data, dict):
                    return {"ok": False, "message": "上游响应格式异常"}
                if data.get("status") != "ok":
                    message = str(data.get("message") or "上游返回异常状态")
                    if not quiet:
                        logger.warning(f"{LOG_TAG} [quota] 上游业务错误: {message}")
                    return {"ok": False, "message": message}
                quota_data = data.get("data") or {}
                if not isinstance(quota_data, dict):
                    return {"ok": False, "message": "上游响应格式异常"}
                result = {
                    "ok": True,
                    "value": int(quota_data.get("value", 0) or 0),
                    "balance": float(quota_data.get("balance", 0) or 0),
                    "enabled": bool(quota_data.get("enabled", True)),
                }
                if not quiet:
                    logger.info(
                        f"{LOG_TAG} [quota] 查询成功 | 剩余 {result['value']} "
                        f"enabled={result['enabled']}"
                    )
                return result
        except Exception as e:
            if not quiet:
                logger.warning(f"{LOG_TAG} [quota] 请求异常: {e!r}")
            return {"ok": False, "message": f"请求异常: {type(e).__name__}"}

    def _default_provider_id(self) -> str | None:
        """取 AstrBot 当前默认 provider 的 ID；取不到返回 None。"""

        try:
            # 默认 provider（v4.5.7+ context.get_using_provider() 可不传 umo）
            prov = self.context.get_using_provider()
            if prov is not None:
                # 用 provider 的 meta().id 作为 llm_generate 的 chat_provider_id
                try:
                    return prov.meta().id  # type: ignore[attr-defined]
                except Exception:
                    pass
                # 兜底：部分老 provider 没有 meta()，用 provider_config.id
                cfg = getattr(prov, "provider_config", None)
                if cfg and isinstance(cfg, dict):
                    return cfg.get("id")
            return None
        except Exception as e:
            logger.warning(f"{LOG_TAG} [provider] 获取默认 provider 异常: {e!r}")
            return None

    def _resolve_translate_provider_id(self) -> str | None:
        """根据配置和上下文，选出转译用的 provider ID。

        转译关闭时直接返回 None：此时「转译模型供应商」不参与任何请求，
        连 AstrBot 默认 provider 也不去解析。

        - self.translate_provider 留空 → 取 AstrBot 当前默认 provider
        - 自填 ID → 用 get_provider_by_id 校验；不通过则回退默认；默认也取不到则返回 None
        """
        if not self._translate_on():
            logger.debug(f"{LOG_TAG} [translate] 转译已关闭，不解析转译 provider")
            return None

        chosen = (self.translate_provider or "").strip()
        if not chosen:
            return self._default_provider_id()
        try:
            prov = self.context.get_provider_by_id(chosen)
            if prov:
                return chosen
            logger.warning(
                f"{LOG_TAG} [translate] provider '{chosen}' 不存在，回退默认"
            )
            return self._default_provider_id()
        except Exception as e:
            logger.warning(f"{LOG_TAG} [translate] 选择 provider 异常: {e!r}")
            return None

    def _resolve_interrogate_provider_id(self) -> str:
        """反推用的 provider ID。

        「图片反推多模态模型」独立于转译：未单独配置时，仅在转译开启时才
        借用「转译模型供应商」，否则交给 AstrBot 默认 provider，避免转译
        关闭后仍被一个看不见的配置项影响。
        """

        chosen = (self.interrogate_provider or "").strip()
        if chosen:
            return chosen
        if self._translate_on():
            borrowed = (self.translate_provider or "").strip()
            if borrowed:
                return borrowed
        return ""

    async def _translate_prompt(self, prompt: str, *, force: bool = False) -> str:
        """如果开启转译，把自然语言 prompt 转成 SD/NAI 标签风格。

        ``force=True`` 供单次参数覆盖和自动模式调用，表示已经由上层完成模式
        判断，不再受兼容属性 ``self.enable_translate`` 限制。
        失败时原样返回 prompt，不影响主流程。
        """
        import re as _re

        if not force and not self._translate_on():
            return prompt
        if not prompt or not prompt.strip():
            return prompt

        provider_id = self._resolve_translate_provider_id()
        if not provider_id:
            logger.warning(
                f"{LOG_TAG} [translate] 没有可用 provider，跳过转译，原样透传"
            )
            return prompt

        logger.info(
            f"{LOG_TAG} [translate] 开始 | provider='{provider_id}' "
            f"in_len={len(prompt)} preview='{prompt[:60]}...'"
        )

        # 命中内置法典时，把该模型版本下的实测词条作为参考一并交给转译模型。
        # 仅在「启用内置法典词库」开启时生效，关闭时不检索、不注入。
        grimoire_hint = ""
        if self._grimoire_enabled():
            grimoire_hint = build_grimoire_translation_hint(
                prompt, model_ver=self._active_model_ver()
            )
        llm_input_prompt = f"{prompt}{grimoire_hint}" if grimoire_hint else prompt
        if grimoire_hint:
            logger.info(
                f"{LOG_TAG} [translate] 法典命中参考词条 | hint_len={len(grimoire_hint)}"
            )

        response = None
        # === 优先使用 v4.5.7+ 推荐的 context.llm_generate ===
        try:
            llm_generate = getattr(self.context, "llm_generate", None)
            if llm_generate is not None:
                response = await llm_generate(
                    chat_provider_id=provider_id,
                    prompt=llm_input_prompt,
                    # TRANSLATE_SYSTEM_PROMPT 已内含可选的共享规范文本；
                    # 单独传 IMAGE_PROMPT_SPEC 会丢掉下面的转译指令。
                    system_prompt=TRANSLATE_SYSTEM_PROMPT,
                    temperature=0.4,
                )
        except AttributeError:
            llm_generate = None  # 老版本 AstrBot 没有这个方法
        except Exception as e:
            logger.warning(
                f"{LOG_TAG} [translate] context.llm_generate 异常: {e!r}，尝试 fallback"
            )

        # === fallback: 老版本 AstrBot 直接用 provider.text_chat ===
        if response is None:
            try:
                prov = self.context.get_provider_by_id(provider_id)
                if prov is None:
                    logger.warning(
                        f"{LOG_TAG} [translate] provider '{provider_id}' 不可用，原样透传"
                    )
                    return prompt
                try:
                    response = await prov.text_chat(
                        prompt=llm_input_prompt,
                        system_prompt=TRANSLATE_SYSTEM_PROMPT,
                        temperature=0.4,
                    )
                except TypeError:
                    # 极老 provider 不接受 system_prompt
                    response = await prov.text_chat(prompt=llm_input_prompt)
            except Exception as e:
                logger.warning(
                    f"{LOG_TAG} [translate] 调用 provider 异常: {e!r}，原样透传"
                )
                return prompt

        translated = ""
        if response is not None:
            translated = getattr(response, "completion_text", "") or ""
            if (
                not translated
                and hasattr(response, "result_chain")
                and response.result_chain
            ):
                buf = []
                for comp in response.result_chain:
                    txt = getattr(comp, "text", None)
                    if txt:
                        buf.append(txt)
                translated = "".join(buf)

        # ==== 思考块剥离正则：依据线上日志，转译模型（如 MiniMax-M3）会在正文前
        # 输出 <think>...</think> 推理内容，若不剥离会被原样拼进生图 tag。
        # 1) 成对闭合的 <think>/<thinking> 块整体移除（跨行、大小写不敏感、可多个）
        translated = _re.sub(
            r"<\s*think(?:ing)?\s*>.*?<\s*/\s*think(?:ing)?\s*>",
            "",
            translated,
            flags=_re.IGNORECASE | _re.DOTALL,
        )
        # 2) 只剩孤立闭合标签（开标签缺失或被上游截断）→ 取最后一个闭合标签之后的正文
        _parts = _re.split(
            r"<\s*/\s*think(?:ing)?\s*>", translated, flags=_re.IGNORECASE
        )
        if len(_parts) > 1:
            translated = _parts[-1]
        # 3) 孤立开标签未闭合 → 其后全部是推理内容，直接丢弃
        translated = _re.sub(
            r"<\s*think(?:ing)?\s*>.*$",
            "",
            translated,
            flags=_re.IGNORECASE | _re.DOTALL,
        )

        # 清理可能残留的 markdown 围栏 / 引号
        translated = translated.strip().strip("\"'` ")
        if translated.startswith("```"):
            translated = _re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", translated)
            translated = translated.rstrip("`").strip()
        # 把多行合并为单行（标签风格不应换行）
        translated = " ".join(translated.split())
        # 去掉可能的前缀废话，比如 "Output:" / "翻译结果：" / "Here is the translation:"
        translated = _re.sub(
            r"^\s*(Output|输出|翻译结果|Here is the translation)[^:：]*[:：]\s*",
            "",
            translated,
            flags=_re.IGNORECASE,
        )

        if not translated:
            logger.warning(f"{LOG_TAG} [translate] provider 返回空内容，原样透传")
            return prompt

        logger.info(
            f"{LOG_TAG} [translate] 完成 | out_len={len(translated)} "
            f"preview='{translated[:60]}...'"
        )
        return translated

    def _grimoire_enabled(self) -> bool:
        """法典词库是否启用。默认关闭：不查库、不展开宏、不注入转译参考。"""

        return bool(getattr(self, "enable_grimoire", False))

    def _character_blocks_enabled(self) -> bool:
        """多角色块是否启用。默认关闭：工具不向模型暴露角色块参数。"""

        return bool(getattr(self, "enable_character_blocks", False))

    def _artist_slots_enabled(self) -> bool:
        """画风槽位是否启用。默认关闭：完全忽略槽位，按原配置出图。"""

        return bool(getattr(self, "enable_artist_slots", False))

    def _expand_grimoire(self, prompt: str) -> str:
        """展开提示词里的【法典:词条名】宏；法典功能关闭时原样返回。"""

        if not self._grimoire_enabled() or not prompt:
            return prompt
        return expand_grimoire_macros(prompt, model_ver=self._active_model_ver())

    def _active_model_ver(self) -> str:
        """当前生效模型对应的词库版本（``NAI4.5`` / ``NAI5``）。

        两套调用通道用的模型配置不同：传统 GET 用 ``model``，OpenAI 兼容用
        ``openai_api_model``。词库必须锁定到当前通道真正会用的那个模型，
        避免 V4.5 检索到 V5 的词条。
        """

        call_mode = str(getattr(self, "call_mode", "direct") or "direct")
        # 槽位绑定的模型优先，其次是各通道自己的模型配置。
        try:
            slot_model = self._slot_model()
        except Exception:
            slot_model = ""
        if slot_model:
            return normalize_model_ver(slot_model)
        if call_mode == "openai":
            model = str(getattr(self, "openai_api_model", "") or "")
        else:
            model = str(getattr(self, "model", "") or "")
        if not model:
            # 测试桩/半初始化实例可能只设了其中一项。
            model = str(
                getattr(self, "openai_api_model", "")
                or getattr(self, "model", "")
                or ""
            )
        return normalize_model_ver(model)

    async def _prepare_translated_prompt(
        self,
        prompt: str,
        *,
        translate_mode: str | None = None,
        apply_outfit: bool,
    ) -> tuple[str, str, bool, str]:
        """按转译模式预处理提示词，并解析可选的服装上下文。

        返回 ``(处理后的提示词, 服装来源, 是否追加默认服装, 提示词类型)``。
        关闭模式直接透传规范化文本；开启模式整体转译；自动模式只抽取自然
        语言片段交给 LLM，再按原位置和现有 NAI 标签合并。``apply_outfit``
        用于区分启用服装缓存逻辑的 Bot 流程与不启用该逻辑的 WebUI 流程。
        """
        raw_prompt = normalize_prompt(
            self._expand_grimoire(prompt)
        )
        current_mode = getattr(self, "translate_mode", None)
        # 兼容只设置旧版 enable_translate 布尔属性的实例和测试桩。
        if current_mode is None:
            current_mode = (
                TRANSLATE_MODE_ON
                if getattr(self, "enable_translate", False)
                else TRANSLATE_MODE_OFF
            )
        mode = normalize_translate_mode(
            translate_mode if translate_mode is not None else current_mode
        )
        if mode == TRANSLATE_MODE_OFF:
            return raw_prompt, "none", False, "off"

        mixed_parts = None
        translation_input = raw_prompt
        prompt_kind = "natural"
        if mode == TRANSLATE_MODE_AUTO:
            # 自动模式只把自然语言片段送给 LLM，已有 NAI 标签保持原样。
            mixed_parts = extract_mixed_prompt(raw_prompt)
            if not mixed_parts.has_natural:
                logger.info(
                    f"{LOG_TAG} [translate:auto] 判定为 NAI 标签，原样透传 | "
                    f"preview='{raw_prompt[:60]}...'"
                )
                return raw_prompt, "none", False, "nai"
            translation_input = mixed_parts.natural_text
            prompt_kind = "mixed" if mixed_parts.nai_text else "natural"
            logger.info(
                f"{LOG_TAG} [translate:auto] kind={prompt_kind} "
                f"explicit={'yes' if mixed_parts.explicit_natural else 'no'} | "
                f"nai='{mixed_parts.nai_text[:60]}...' "
                f"natural='{translation_input[:60]}...'"
            )

        outfit_ctx, outfit_source, use_default_outfit = "", "none", False
        if apply_outfit:
            outfit_ctx, outfit_source, use_default_outfit = self._resolve_outfit(
                translation_input
            )
        if outfit_ctx:
            effective_translation_input = (
                f"{translation_input.rstrip()}\n\n"
                f"[延续上文穿搭或当前默认服装] {outfit_ctx}"
            )
        else:
            effective_translation_input = translation_input

        translated = await self._translate_prompt(
            effective_translation_input,
            force=True,
        )
        if mixed_parts is not None:
            # 译文回填到首个自然语言片段的位置，并去除与原标签重复的项。
            prepared_prompt = merge_translated_prompt(mixed_parts, translated)
        else:
            prepared_prompt = translated

        logger.debug(
            f"{LOG_TAG} [translate:{mode}] input={effective_translation_input!r} "
            f"prepared={prepared_prompt!r}"
        )
        return prepared_prompt, outfit_source, use_default_outfit, prompt_kind

    async def _submit_web_job(
        self,
        full_prompt: str,
        artists: str,
        size: str,
        *,
        steps: int,
        scale: float,
        cfg: float,
        sampler: str,
        noise_schedule: str,
        negative: str,
        model: str,
        token: str | None = None,
    ) -> tuple[bytes | None, str]:
        """经任务接口（POST /api/web/jobs）提交生图并轮询取回图片。

        上游 2026-09 计费模块：任务接口支持 1-50 步，按「步数 × 画质」
        档位计价，失败任务点数自动退回；GET 直链最高按 28 步路由且固定
        基础档价格。token 经 POST body 与轮询请求头（x-user-token）传递，
        不出现在 URL 中。

        Returns:
            (img_bytes_or_None, reason)
            reason 额外取值:
            "web_jobs_unavailable"（上游无任务接口，调用方可回退 GET 直链）、
            "web_job_failed"（任务失败，点数已退回）。
        """
        _token = token or self.image_gen_key
        if not _token:
            return None, "no_token"
        if not self._session:
            return None, "no_session"

        base = self.base_url.rstrip("/")
        # 步数钳制到任务接口上限 50，与上游 clamp 行为一致。
        _steps = max(1, min(int(steps), WEB_JOB_STEPS_MAX))
        payload = {
            "token": _token,
            "tag": full_prompt,
            "artist": artists or "",
            "model": model,
            "size": size,
            "steps": _steps,
            "scale": scale,
            "cfg": cfg,
            "sampler": sampler,
            "negative": negative,
            "noise_schedule": noise_schedule,
            "nocache": 1,
        }
        estimated_cost = web_job_generation_cost(size, model, _steps)
        logger.info(
            f"{LOG_TAG} [web_job] 提交任务 | size={size} steps={_steps} "
            f"model={model} 预计消耗={estimated_cost} 点"
        )
        try:
            async with self._session.post(
                f"{base}/api/web/jobs",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=WEB_JOB_SUBMIT_TIMEOUT),
            ) as resp:
                if resp.status == 404:
                    logger.info(
                        f"{LOG_TAG} [web_job] 上游无任务接口（404），"
                        f"调用方将回退 GET 直链"
                    )
                    return None, "web_jobs_unavailable"
                if resp.status not in (200, 202):
                    err_body = ""
                    try:
                        err_body = await resp.text()
                    except Exception:
                        pass
                    summary = _summarize_error_body(err_body)
                    logger.warning(
                        f"{LOG_TAG} [web_job] 提交失败 | status={resp.status} "
                        f"body='{err_body[:300]}'"
                    )
                    if 400 <= resp.status < 500:
                        reason = f"http_4xx (HTTP {resp.status})"
                    elif 500 <= resp.status < 600:
                        reason = f"http_5xx (HTTP {resp.status})"
                    else:
                        reason = f"http_other (HTTP {resp.status})"
                    if summary:
                        reason = f"{reason}: {summary}"
                    return None, reason
                try:
                    job = await resp.json()
                except Exception:
                    logger.warning(f"{LOG_TAG} [web_job] 提交响应不是 JSON")
                    return None, "invalid_response"
                if not isinstance(job, dict):
                    logger.warning(
                        f"{LOG_TAG} [web_job] 提交响应不是 JSON 对象: {type(job).__name__}"
                    )
                    return None, "invalid_response"
        except asyncio.TimeoutError:
            logger.warning(f"{LOG_TAG} [web_job] 提交超时 (>{WEB_JOB_SUBMIT_TIMEOUT}s)")
            return None, "timeout"
        except Exception as e:
            logger.warning(f"{LOG_TAG} [web_job] 提交异常: {e!r}")
            return None, "exception"

        job_id = str(job.get("id") or "")
        if not job_id:
            logger.warning(f"{LOG_TAG} [web_job] 提交响应缺少任务 id: {job!r}")
            return None, "invalid_response"
        actual_cost = job.get("cost")
        logger.info(
            f"{LOG_TAG} [web_job] 任务已入队 | id={job_id} status={job.get('status')} "
            f"cost={actual_cost if actual_cost is not None else estimated_cost}"
        )

        # 轮询直到 done / failed / 超时；单次轮询失败容忍 3 次再判异常。
        poll_failures = 0
        deadline = time.perf_counter() + self.request_timeout
        while True:
            job = None
            try:
                async with self._session.get(
                    f"{base}/api/jobs/{job_id}",
                    headers={"x-user-token": _token},
                    timeout=aiohttp.ClientTimeout(total=WEB_JOB_POLL_REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        polled = await resp.json()
                        if isinstance(polled, dict):
                            job = polled
                        else:
                            logger.warning(
                                f"{LOG_TAG} [web_job] 轮询响应不是 JSON 对象 | "
                                f"id={job_id} type={type(polled).__name__}"
                            )
                    else:
                        logger.warning(
                            f"{LOG_TAG} [web_job] 轮询非 200 | id={job_id} "
                            f"status={resp.status}"
                        )
            except Exception as e:
                logger.warning(f"{LOG_TAG} [web_job] 轮询异常 | id={job_id} err={e!r}")
            if job is not None:
                poll_failures = 0
                status = str(job.get("status") or "")
                if status == "done":
                    image_url = str(job.get("imageUrl") or "")
                    if not image_url:
                        logger.warning(
                            f"{LOG_TAG} [web_job] 任务完成但无 imageUrl: {job!r}"
                        )
                        return None, "invalid_response"
                    return await self._download_web_job_image(base, image_url)
                if status == "failed":
                    error_text = str(job.get("error") or "上游未返回原因")
                    logger.warning(
                        f"{LOG_TAG} [web_job] 任务失败 | id={job_id} "
                        f"error='{error_text}'（点数会自动退回）"
                    )
                    return None, "web_job_failed"
            else:
                poll_failures += 1
                if poll_failures >= 3:
                    logger.warning(
                        f"{LOG_TAG} [web_job] 连续 {poll_failures} 次轮询失败，"
                        f"放弃任务 | id={job_id}"
                    )
                    return None, "exception"
            if time.perf_counter() >= deadline:
                logger.warning(
                    f"{LOG_TAG} [web_job] 轮询超时 (>{self.request_timeout}s) | "
                    f"id={job_id}"
                )
                return None, "timeout"
            await asyncio.sleep(WEB_JOB_POLL_INTERVAL)

    async def _download_web_job_image(
        self, base: str, image_url: str
    ) -> tuple[bytes | None, str]:
        """下载任务接口返回的图片（imageUrl 为相对路径时拼上 base）。"""

        if image_url.startswith(("http://", "https://")):
            url = image_url
        else:
            url = f"{base}{'/' + image_url.lstrip('/') if not image_url.startswith('/') else image_url}"
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                # 上游用 x-error:1 标记错误占位图，避免把错误图当成功归档。
                if resp.headers.get("x-error") == "1":
                    logger.warning(f"{LOG_TAG} [web_job] 图片下载收到错误占位图")
                    return None, "web_job_failed"
                if resp.status != 200:
                    logger.warning(
                        f"{LOG_TAG} [web_job] 图片下载失败 | status={resp.status}"
                    )
                    if 400 <= resp.status < 500:
                        return None, f"http_4xx (HTTP {resp.status})"
                    if 500 <= resp.status < 600:
                        return None, f"http_5xx (HTTP {resp.status})"
                    return None, f"http_other (HTTP {resp.status})"
                img_bytes = await resp.read()
                if not img_bytes:
                    logger.warning(f"{LOG_TAG} [web_job] 图片下载空响应")
                    return None, "empty_response"
                logger.info(
                    f"{LOG_TAG} [web_job] 图片下载成功 | bytes={len(img_bytes)}"
                )
                return img_bytes, "ok"
        except asyncio.TimeoutError:
            logger.warning(f"{LOG_TAG} [web_job] 图片下载超时")
            return None, "timeout"
        except Exception as e:
            logger.warning(f"{LOG_TAG} [web_job] 图片下载异常: {e!r}")
            return None, "exception"

    async def _generate_one(
        self,
        prompt: str,
        style: str,
        size: str,
        *,
        steps: int | None = None,
        scale: float | None = None,
        cfg: float | None = None,
        sampler: str | None = None,
        noise_schedule: str | None = None,
        negative: str | None = None,
        model: str | None = None,
        custom_artists: str | None = None,
        enable_template: bool | None = None,
        enable_translate: bool | str | None = None,
    ) -> tuple[bytes | None, str]:
        """使用最终参数生成单张图片。

        所有关键字参数都是仅对本次请求生效的覆盖值；传入 ``None`` 时才使用
        插件全局配置，因此 ``0``、``False`` 和空反向提示词都能被显式传递。
        成功后会按当前历史配置归档图片及实际发送的接口参数。

        Returns:
            (img_bytes_or_None, reason)
            reason 取值: "ok" / "no_token" / "no_session" /
                        "timeout" / "http_4xx" / "http_5xx" / "http_other" /
                        "empty_response" / "invalid_response" /
                        "web_job_failed" / "exception"

        生图通道固定为任务接口优先（1-50 步，按步数档位计价，失败退点），
        上游无任务接口（404，旧版自建 Nai2API）时自动回退 GET 直链
        （>28 步按 28 路由，基础档计价）。
        """
        if not self.image_gen_key:
            logger.warning(f"{LOG_TAG} [generate] 跳过：token 未配置")
            return None, "no_token"
        if not self._session:
            logger.warning(f"{LOG_TAG} [generate] 跳过：session 未初始化")
            return None, "no_session"

        # 在局部变量中合并单次覆盖，不修改实例配置，后续请求仍使用原设置。
        # 所有数值参数在入口处统一收敛到上游有效范围，避免面板/指令/API
        # 传入越界值时请求与归档参数不一致。
        try:
            _steps = max(1, min(50, int(steps if steps is not None else self.steps)))
        except (TypeError, ValueError):
            _steps = 24
        try:
            _scale = max(
                0.0, min(20.0, float(scale if scale is not None else self.scale))
            )
        except (TypeError, ValueError):
            _scale = 6.0
        try:
            _cfg = max(0.0, min(1.0, float(cfg if cfg is not None else self.cfg_value)))
        except (TypeError, ValueError):
            _cfg = 1.0
        _sampler = sampler if sampler is not None else self.sampler
        _noise = noise_schedule if noise_schedule is not None else self.noise_schedule
        _negative = negative if negative is not None else self.negative
        _model = model if model is not None else (self._slot_model() or self.model)
        _enable_template = (
            enable_template if enable_template is not None else self.enable_template
        )
        _translate_mode = normalize_translate_mode(
            enable_translate if enable_translate is not None else self.translate_mode
        )

        # 1) 关闭时原样发送；开启时整体转译；自动时只转译自然语言片段。
        (
            translated_prompt,
            outfit_source,
            use_default_outfit,
            prompt_kind,
        ) = await self._prepare_translated_prompt(
            prompt,
            translate_mode=_translate_mode,
            apply_outfit=True,
        )

        # 2) 与预设模板合并
        full_prompt = self._build_full_prompt(
            translated_prompt,
            enable_template=_enable_template,
        )

        # 3) 如果需要，直接追加默认服装（假设default_outfit已经是SD tags格式）
        if use_default_outfit and self.default_outfit:
            full_prompt = f"{full_prompt}, {self.default_outfit}"
            logger.debug(
                f"{LOG_TAG} [outfit] 模板合并后直接添加默认服装SD tags | preview='{self.default_outfit[:60]}...'"
            )
        full_prompt = normalize_prompt(full_prompt)

        # 自定义风格允许空画师串（不强制回退预设）。
        # 优先级：本次显式指定 > 槽位绑定 > 自定义/风格预设。
        slot_artists = self._slot_artists()
        if custom_artists is not None and str(custom_artists).strip():
            artists = str(custom_artists)
        elif slot_artists:
            artists = slot_artists
        elif style == "custom":
            artists = (
                custom_artists if custom_artists is not None else self.custom_artists
            )
            if artists is None:
                artists = ""
        else:
            artists = self._resolve_artists(style)

        logger.info(
            f"{LOG_TAG} [generate] 开始 | style={style} size={size} | "
            f"steps={_steps} scale={_scale} cfg={_cfg} sampler={_sampler} "
            f"noise={_noise} model={_model} | "
            f"translate={_translate_mode}/{prompt_kind} "
            f"template={'on' if _enable_template and self.character_preset else 'off'} | "
            f"outfit={outfit_source}{'+default' if use_default_outfit and self.default_outfit else ''} | "
            f"prompt(原始)='{prompt[:60]}...' "
            f"prompt(转译后)='{translated_prompt[:60]}...' "
            f"prompt(模板后,前60字)='{full_prompt[:60]}...'"
        )
        logger.debug(
            f"{LOG_TAG} [generate] translated_prompt(完整) = {translated_prompt!r}"
        )
        logger.debug(f"{LOG_TAG} [generate] full_prompt(完整) = {full_prompt!r}")
        logger.debug(f"{LOG_TAG} [generate] artists = {artists!r}")

        # 历史文件记录最终发往接口的字段，不保存 token 等认证信息。
        generation_parameters = {
            "tag": full_prompt,
            "model": _model,
            "artist": artists,
            "size": size,
            "steps": _steps,
            "scale": _scale,
            "cfg": _cfg,
            "sampler": _sampler,
            "negative": _negative,
            "nocache": 1,
            "noise_schedule": _noise,
        }

        # 直连通道固定为任务接口（/api/web/jobs）优先：支持 1-50 步、
        # 按「步数 × 画质」档位计价、失败退点；上游无该接口（404，自建
        # 旧版 Nai2API）时自动回退 GET 直链（>28 步按 28 路由、基础档计价）。
        web_img, web_reason = await self._submit_web_job(
            full_prompt,
            artists,
            size,
            steps=_steps,
            scale=_scale,
            cfg=_cfg,
            sampler=_sampler,
            noise_schedule=_noise,
            negative=_negative,
            model=_model,
        )
        if web_img is not None:
            generation_parameters["steps"] = effective_transport_steps(
                _steps, TRANSPORT_WEB_JOBS
            )
            generation_parameters["transport"] = "web_jobs"
            generation_parameters["estimated_cost"] = web_job_generation_cost(
                size, _model, _steps
            )
            await self._archive_generated_image(web_img, generation_parameters)
            await self._record_usage_from_parameters(generation_parameters)
            return web_img, "ok"
        if web_reason != "web_jobs_unavailable":
            return None, web_reason
        logger.warning(
            f"{LOG_TAG} [generate] 上游不支持任务接口，回退 GET 直链 | "
            f">{DIRECT_URL_STEPS_MAX} 步将按 {DIRECT_URL_STEPS_MAX} 步路由、"
            f"按基础档计价"
        )
        # GET 直链只支持 28 步，实际请求与历史参数都按 28 记录，
        # 避免面板显示 50 步但上游只生成 28 步。
        _steps = effective_transport_steps(_steps, TRANSPORT_GET)
        generation_parameters["steps"] = _steps

        url = (
            f"{self.base_url.rstrip('/')}/generate"
            f"?tag={quote(full_prompt)}"
            f"&token={self.image_gen_key}"
            f"&model={quote(_model)}"
            f"&artist={quote(artists)}"
            f"&size={quote(size)}"
            f"&steps={_steps}"
            f"&scale={_scale}"
            f"&cfg={_cfg}"
            f"&sampler={quote(_sampler)}"
            f"&negative={quote(_negative)}"
            f"&nocache=1"
            f"&noise_schedule={quote(_noise)}"
        )
        # 脱敏：日志中不输出完整 URL（含明文 token）
        safe_url = url.replace(f"&token={self.image_gen_key}", "&token=***")
        logger.debug(f"{LOG_TAG} [generate] request url = {safe_url}")

        start = time.perf_counter()
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=self.request_timeout)
            ) as resp:
                elapsed = time.perf_counter() - start
                if resp.status != 200:
                    if 400 <= resp.status < 500:
                        reason = "http_4xx"
                    elif 500 <= resp.status < 600:
                        reason = "http_5xx"
                    else:
                        reason = "http_other"
                    logger.warning(
                        f"{LOG_TAG} [generate] 失败 | reason={reason} "
                        f"status={resp.status} elapsed={elapsed:.2f}s"
                    )
                    return None, reason
                img_bytes = await resp.read()
                if not img_bytes:
                    logger.warning(
                        f"{LOG_TAG} [generate] 空响应 | status=200 "
                        f"bytes=0 elapsed={elapsed:.2f}s"
                    )
                    return None, "empty_response"
                logger.info(
                    f"{LOG_TAG} [generate] 成功 | bytes={len(img_bytes)} "
                    f"elapsed={elapsed:.2f}s style={style} size={size}"
                )
                generation_parameters["transport"] = TRANSPORT_GET
                generation_parameters["estimated_cost"] = direct_url_generation_cost(
                    size, _model
                )
                await self._archive_generated_image(
                    img_bytes,
                    generation_parameters,
                )
                await self._record_usage_from_parameters(generation_parameters)
                return img_bytes, "ok"
        except asyncio.TimeoutError:
            logger.warning(
                f"{LOG_TAG} [generate] 超时 (>{self.request_timeout}s) | prompt='{full_prompt[:60]}...'"
            )
            return None, "timeout"
        except Exception as e:
            logger.warning(f"{LOG_TAG} [generate] 异常: {e!r}")
            return None, "exception"

    async def _start_proxy_server(self):
        logger.info(f"{LOG_TAG} [proxy:start] 准备启动 {PROXY_HOST}:{self.proxy_port}")
        app = web.Application()
        app.router.add_post("/v1/images/generations", self._proxy_handle_generations)
        app.router.add_post("/v1/images/edits", self._proxy_handle_edits)
        app.router.add_get("/v1/images/generations", self._proxy_handle_health)
        app.router.add_get("/v1/proxy_status", self._proxy_handle_health)
        self.proxy_runner = web.AppRunner(app)
        try:
            await self.proxy_runner.setup()
            site = web.TCPSite(self.proxy_runner, PROXY_HOST, self.proxy_port)
            await site.start()
        except Exception:
            # 端口占用等失败时先清理半启动的 runner，避免 initialize 的
            # 3 次重试不断泄漏监听 socket/事件循环资源。
            failed_runner = self.proxy_runner
            self.proxy_runner = None
            if failed_runner is not None:
                try:
                    await failed_runner.cleanup()
                except Exception:
                    pass
            raise
        logger.info(
            f"{LOG_TAG} [proxy:start] 启动成功 | "
            f"http://{PROXY_HOST}:{self.proxy_port}/v1/images/generations"
        )

    async def _stop_proxy_server(self):
        if not self.proxy_runner:
            logger.info(f"{LOG_TAG} [proxy:stop] 代理未运行，跳过")
            return
        logger.info(f"{LOG_TAG} [proxy:stop] 正在关闭代理")
        try:
            await self.proxy_runner.cleanup()
            logger.info(f"{LOG_TAG} [proxy:stop] 代理已停止")
        except Exception as e:
            logger.warning(f"{LOG_TAG} [proxy:stop] 停止异常: {e!r}")
        finally:
            self.proxy_runner = None

    async def _proxy_handle_health(self, request: web.Request):
        logger.debug(
            f"{LOG_TAG} [proxy:health] GET {request.path} from {request.remote}"
        )
        return web.json_response(
            {
                "status": "ok",
                "plugin": "astrbot_plugin_nai_image",
                "base_url": self.base_url,
                "token_configured": bool(self.image_gen_key),
            }
        )

    async def _proxy_handle_generations(self, request: web.Request):
        logger.info(
            f"{LOG_TAG} [proxy:gen] 收到 POST {request.path} from {request.remote}"
        )
        # 按调用模式校验所需凭据
        missing = ""
        if self.call_mode == "openai":
            if not self.openai_api_base_url:
                missing = "openai_api_base_url"
        else:
            if not self.image_gen_key:
                missing = "image_gen_key"
        if missing or not self._session:
            logger.warning(f"{LOG_TAG} [proxy:gen] 拒绝：缺少 {missing or 'session'}")
            return web.json_response(
                {
                    "error": {
                        "message": f"NAI 插件未配置 {missing or '会话'}",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            logger.debug(f"{LOG_TAG} [proxy:gen] body keys: {list(body.keys())}")
        except Exception as e:
            logger.warning(f"{LOG_TAG} [proxy:gen] JSON 解析失败: {e!r}")
            return web.json_response(
                {
                    "error": {
                        "message": f"invalid json: {e!r}",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        prompt = normalize_prompt(body.get("prompt") or "")
        if not prompt:
            logger.warning(f"{LOG_TAG} [proxy:gen] prompt 为空")
            return web.json_response(
                {
                    "error": {
                        "message": "prompt is required",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        size = body.get("size") or "1024x1024"
        try:
            n = max(1, min(4, int(body.get("n") or 1)))
        except (TypeError, ValueError):
            n = 1
        # 高级参数：接口文档允许放在 parameters 对象或请求顶层（§3.2），
        # parameters 优先；_openai_generate 内部再做类型/范围归一化。
        _params = body.get("parameters")
        if not isinstance(_params, dict):
            _params = {}
        client_overrides = {
            "steps": _params.get("steps", body.get("steps")),
            "scale": _params.get("scale", body.get("scale")),
            "cfg": _params.get("cfg", body.get("cfg")),
            "seed": _params.get("seed", body.get("seed")),
            "sampler": _params.get("sampler", body.get("sampler")),
            "noise_schedule": _params.get("noise_schedule", body.get("noise_schedule")),
            "negative": _params.get("negative_prompt", body.get("negative_prompt")),
        }
        model_override = str(body.get("model") or "").strip() or None
        logger.info(
            f"{LOG_TAG} [proxy:gen] 参数 | prompt='{prompt[:80]}' size={size} n={n}"
        )

        try:
            if self.call_mode == "openai":
                images, reason = await self._openai_generate(
                    prompt,
                    size,
                    n=n,
                    steps=client_overrides["steps"],
                    scale=client_overrides["scale"],
                    seed=client_overrides["seed"],
                    sampler=client_overrides["sampler"],
                    noise_schedule=client_overrides["noise_schedule"],
                    negative=client_overrides["negative"],
                    model=model_override,
                )
                img_list = images
            else:
                # 直连上游只认中文分档名：OpenAI 客户端传入的像素尺寸/
                # 英文分档名必须先归一化，否则会被上游拒绝或误判。
                size = self._resolve_direct_size(size)
                img_list = []
                reason = ""
                for _ in range(n):
                    img_bytes, reason = await self._generate_one(
                        prompt,
                        self.image_style,
                        size,
                        steps=client_overrides["steps"],
                        scale=client_overrides["scale"],
                        cfg=client_overrides["cfg"],
                        sampler=client_overrides["sampler"],
                        noise_schedule=client_overrides["noise_schedule"],
                        negative=client_overrides["negative"],
                        model=model_override,
                    )
                    if not img_bytes:
                        break
                    img_list.append(img_bytes)
        except Exception as e:
            logger.warning(f"{LOG_TAG} [proxy:gen] 生成异常: {e!r}")
            return web.json_response(
                {
                    "error": {
                        "message": f"generate exception: {e!r}",
                        "type": "internal_error",
                    }
                },
                status=500,
            )

        if not img_list:
            logger.warning(f"{LOG_TAG} [proxy:gen] 生图失败 | reason={reason}")
            user_msg = _format_generate_error(reason)
            status = 504 if reason == "timeout" else 502
            return web.json_response(
                {
                    "error": {
                        "message": f"generate failed: {reason}",
                        "user_message": user_msg,
                        "type": "upstream_error",
                    }
                },
                status=status,
            )

        b64_list = [base64.b64encode(img).decode() for img in img_list]
        logger.info(
            f"{LOG_TAG} [proxy:gen] 响应 | images={len(b64_list)} "
            f"total_bytes={sum(len(b) for b in img_list)} n={n}"
        )
        return web.json_response(
            {
                "created": int(time.time()),
                "data": [{"b64_json": b64} for b64 in b64_list],
            }
        )

    async def _proxy_handle_edits(self, request: web.Request):
        logger.info(
            f"{LOG_TAG} [proxy:edit] 收到 POST {request.path} from {request.remote}"
        )
        # 按调用模式校验所需凭据
        missing = ""
        if self.call_mode == "openai":
            if not self.openai_api_base_url:
                missing = "openai_api_base_url"
        else:
            if not self.image_gen_key:
                missing = "image_gen_key"
        if missing or not self._session:
            logger.warning(f"{LOG_TAG} [proxy:edit] 拒绝：缺少 {missing or 'session'}")
            return web.json_response(
                {
                    "error": {
                        "message": f"NAI 插件未配置 {missing or '会话'}",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        prompt = ""
        size = "1024x1024"
        n = 1
        strength: float | None = None
        noise: float | None = None
        image_bytes: bytes | None = None
        content_type = (request.content_type or "").casefold()

        if content_type == "application/json":
            # JSON 图生图（接口文档 §4）：image 支持 URL / Data URI / 纯 Base64。
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError("request body must be a JSON object")
            except Exception as e:
                logger.warning(f"{LOG_TAG} [proxy:edit] JSON 解析失败: {e!r}")
                return web.json_response(
                    {
                        "error": {
                            "message": f"invalid json: {e!r}",
                            "type": "invalid_request_error",
                        }
                    },
                    status=400,
                )
            prompt = normalize_prompt(body.get("prompt") or "")
            size = str(body.get("size") or size).strip() or size
            try:
                n = max(1, min(4, int(body.get("n") or 1)))
            except (TypeError, ValueError):
                n = 1
            _params = body.get("parameters")
            if not isinstance(_params, dict):
                _params = {}
            try:
                strength = float(_params.get("strength", body.get("strength")))
                if not 0 <= strength <= 1:
                    strength = None
            except (TypeError, ValueError):
                strength = None
            try:
                noise = float(_params.get("noise", body.get("noise")))
                if not 0 <= noise <= 1:
                    noise = None
            except (TypeError, ValueError):
                noise = None
            raw_image = str(_params.get("image", body.get("image")) or "").strip()
            if raw_image.startswith(("http://", "https://")):
                try:
                    async with self._session.get(
                        raw_image, timeout=aiohttp.ClientTimeout(total=60)
                    ) as dl_resp:
                        if dl_resp.status == 200:
                            image_bytes = await dl_resp.read()
                except Exception as exc:
                    logger.warning(
                        f"{LOG_TAG} [proxy:edit] 下载 image URL 失败: {type(exc).__name__}"
                    )
            elif raw_image:
                b64_part = (
                    raw_image.split(",", 1)[1]
                    if raw_image.startswith("data:")
                    else raw_image
                )
                try:
                    image_bytes = base64.b64decode(b64_part)
                except Exception:
                    logger.warning(f"{LOG_TAG} [proxy:edit] image Base64 解码失败")
            logger.debug(f"{LOG_TAG} [proxy:edit] JSON body keys: {list(body.keys())}")
        else:
            parts_seen: list[str] = []
            try:
                reader = await request.multipart()
                async for part in reader:
                    if part.name is None:
                        continue
                    parts_seen.append(part.name)
                    if part.name == "prompt":
                        prompt = normalize_prompt(await part.text())
                    elif part.name == "size":
                        raw_size = (await part.text() or "").strip()
                        if raw_size:
                            size = raw_size
                    elif part.name == "n":
                        try:
                            n = max(1, min(4, int((await part.text() or "").strip())))
                        except Exception:
                            n = 1
                    elif part.name == "strength":
                        try:
                            strength = float((await part.text() or "").strip())
                            if not (0 < strength <= 1):
                                strength = None
                        except Exception:
                            strength = None
                    elif part.name in ("image", "image[]"):
                        # 参考图：保留字节，交给 _generate_one 落盘托管。
                        image_bytes = await part.read()
                    elif part.name in ("mask", "mask[]"):
                        await part.read()
                logger.debug(f"{LOG_TAG} [proxy:edit] multipart parts: {parts_seen}")
            except Exception as e:
                logger.warning(f"{LOG_TAG} [proxy:edit] multipart 解析失败: {e!r}")
                return web.json_response(
                    {
                        "error": {
                            "message": f"invalid multipart: {e!r}",
                            "type": "invalid_request_error",
                        }
                    },
                    status=400,
                )
        if not prompt:
            logger.warning(f"{LOG_TAG} [proxy:edit] prompt 为空")
            return web.json_response(
                {
                    "error": {
                        "message": "prompt is required",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        if self.call_mode == "openai":
            # OpenAI 兼容模式：有参考图走图生图，否则文生图
            logger.info(
                f"{LOG_TAG} [proxy:edit] OpenAI 模式 | prompt='{prompt[:80]}' "
                f"size={size} n={n} strength={strength} "
                f"image_bytes={len(image_bytes) if image_bytes else 0}"
            )
            try:
                images, reason = await self._openai_generate(
                    prompt,
                    size,
                    n=n,
                    reference_image_bytes=image_bytes,
                    strength=strength,
                    noise=noise,
                    reference_mode="img2img",
                )
            except Exception as e:
                logger.warning(f"{LOG_TAG} [proxy:edit] _openai_generate 异常: {e!r}")
                return web.json_response(
                    {
                        "error": {
                            "message": f"generate exception: {e!r}",
                            "type": "internal_error",
                        }
                    },
                    status=500,
                )
        else:
            # 传统 GET 模式：文生图（参考图降级忽略）。直连上游只认中文
            # 分档名，先把 OpenAI 客户端传入的像素尺寸归一化。
            size = self._resolve_direct_size(size)
            if image_bytes:
                logger.info(
                    f"{LOG_TAG} [proxy:edit] 传统 GET 模式，忽略参考图按文生图 | "
                    f"prompt='{prompt[:80]}' size={size} n={n}"
                )
            else:
                logger.info(
                    f"{LOG_TAG} [proxy:edit] 传统 GET 文生图 | prompt='{prompt[:80]}' "
                    f"size={size} n={n}"
                )
            images = []
            reason = ""
            try:
                for _ in range(n):
                    img_bytes, reason = await self._generate_one(
                        prompt, self.image_style, size
                    )
                    if not img_bytes:
                        break
                    images.append(img_bytes)
            except Exception as e:
                logger.warning(f"{LOG_TAG} [proxy:edit] _generate_one 异常: {e!r}")
                return web.json_response(
                    {
                        "error": {
                            "message": f"generate exception: {e!r}",
                            "type": "internal_error",
                        }
                    },
                    status=500,
                )

        if not images:
            logger.warning(f"{LOG_TAG} [proxy:edit] 生图失败 | reason={reason}")
            user_msg = _format_generate_error(reason)
            status = 504 if reason == "timeout" else 502
            return web.json_response(
                {
                    "error": {
                        "message": f"generate failed: {reason}",
                        "user_message": user_msg,
                        "type": "upstream_error",
                    }
                },
                status=status,
            )

        b64_list = [base64.b64encode(img).decode() for img in images]
        logger.info(
            f"{LOG_TAG} [proxy:edit] 响应 | images={len(b64_list)} "
            f"total_bytes={sum(len(b) for b in images)}"
        )
        return web.json_response(
            {
                "created": int(time.time()),
                "data": [{"b64_json": b64} for b64 in b64_list],
            }
        )

    def _resolve_direct_size(self, size_cn: str) -> str:
        """传统 GET 模式的尺寸归一化：必须产出上游识别的中文分档名。

        像素分辨率（如 ``832x1216``）在该通道不支持，按长宽比回退为分档名；
        无法识别时回退插件默认尺寸。
        """

        size = size_cn
        if size not in IMAGE_SIZES:
            # 英文分档名（portrait/landscape/square）先归一化为中文键。
            size = normalize_image_size(size_cn) or size_cn
        if size not in IMAGE_SIZES:
            _px = _PIXEL_SIZE_RE.match(size)
            if _px:
                _w, _h = int(_px.group(1)), int(_px.group(2))
                if _w > 0 and _h > 0:
                    size = (
                        "方图"
                        if abs(_w - _h) <= max(_w, _h) * 0.08
                        else ("竖图" if _h > _w else "横图")
                    )
                    logger.info(
                        f"{LOG_TAG} [cmd] 传统 GET 模式不支持像素尺寸，"
                        f"{size_cn} 已按长宽比映射为 {size}"
                    )
            if size not in IMAGE_SIZES:
                size = self.image_size
        return size

    @filter.command("image")
    async def image(self, event: AstrMessageEvent):
        """处理 `/image` 指令，并将命名参数作为本次生图的临时覆盖值。

        参数解析在检查提示词前完成，确保提示词任意位置的 ``--名称=值`` 都能
        被移除和校验；未指定的字段继续沿用插件配置。
        """
        raw_text = event.message_str or ""
        text = strip_image_command_prefix(raw_text)
        sender = event.get_sender_id() if hasattr(event, "get_sender_id") else "?"
        logger.info(
            f"{LOG_TAG} [cmd:image] 收到指令 | sender={sender} | text='{text[:100]}'"
        )

        if not text.strip():
            logger.info(f"{LOG_TAG} [cmd:image] 提示用法 (空指令)")
            yield event.plain_result(
                "用法: /image <提示词> [--参数=值]\n"
                "基础: --n=1-6 --style=... --size=...\n"
                "生成: --steps=1-50 --scale=0-20 --cfg=0-1 "
                "--sampler=... --noise=karras|native|exponential|polyexponential\n"
                "覆盖: --translate=关闭|开启|自动 --template=关闭|开启 "
                '--model=... --artist="..." --negative="..."\n'
                '多角色(仅OpenAI兼容): --char="提示词|x|y"（可重复，坐标0-1）\n'
                "价格试算: /price [--steps=40 --size=2K竖图 --model=...] "
                "（不生图，按步数×画质档位试算并实时查余额）\n"
                "用量统计: /usage [天数|today|all]（按日期统计点数消耗）\n"
                "style/size/translate/template 的值均支持中英文。"
            )
            return

        try:
            args = parse_image_command(text, default_style=self.image_style)
        except ImageCommandArgumentError as e:
            logger.info(f"{LOG_TAG} [cmd:image] 参数错误: {e}")
            yield event.plain_result(f"参数错误：{e}")
            return

        logger.info(f"{LOG_TAG} [cmd:image] 解析参数: {args}")
        prompt = args.prompt
        if not prompt:
            logger.info(f"{LOG_TAG} [cmd:image] prompt 为空")
            yield event.plain_result("请提供提示词。")
            return

        if args.characters and self.call_mode != "openai":
            logger.info(f"{LOG_TAG} [cmd:image] --char 需要 OpenAI 兼容模式")
            yield event.plain_result(
                "参数 --char（多角色坐标）仅在调用模式为「OpenAI 兼容」时可用，"
                "请先在插件设置中切换 call_mode。"
            )
            return

        # 按调用模式校验凭据
        if self.call_mode == "openai":
            if not self.openai_api_base_url:
                logger.warning(f"{LOG_TAG} [cmd:image] OpenAI 接口未配置")
                yield event.plain_result(
                    "未配置 OpenAI 兼容生图接口（openai_api_base_url），请先在插件配置中填写。"
                )
                return
        else:
            if not self.image_gen_key:
                logger.warning(f"{LOG_TAG} [cmd:image] token 未配置")
                yield event.plain_result(
                    "未配置 image_gen_key，请先在插件配置中填写 token。"
                )
                return

        n = args.n if args.n is not None else self.image_count
        style = args.style or self.image_style
        size_cn = args.size or self.image_size
        if self.call_mode == "openai":
            # OpenAI 兼容模式：--size 直接使用像素分辨率（宽x高），契约修正由
            # _openai_generate 内部完成；分档名则映射为对应分辨率。
            size = self._openai_size(size_cn) if size_cn in IMAGE_SIZES else size_cn
        else:
            # 传统 GET 模式：上游 GET API 以中文名称区分普通、2K 和 4K 尺寸，
            # 必须原样传递；用户直接给像素分辨率时按长宽比回退到分档名。
            size = self._resolve_direct_size(size_cn)

        # 回复中展示的参数与 _generate_one 最终采用的覆盖/回退规则保持一致。
        effective_steps = (
            args.steps if args.steps is not None else getattr(self, "steps", 24)
        )
        effective_scale = (
            args.scale if args.scale is not None else getattr(self, "scale", 6)
        )
        effective_cfg = (
            args.cfg if args.cfg is not None else getattr(self, "cfg_value", 1.0)
        )
        effective_sampler = args.sampler or getattr(self, "sampler", "k_dpmpp_2m_sde")
        effective_noise = args.noise_schedule or getattr(
            self, "noise_schedule", "karras"
        )
        effective_translate = args.translate_mode or getattr(
            self, "translate_mode", TRANSLATE_MODE_OFF
        )
        effective_template = (
            args.enable_template
            if args.enable_template is not None
            else getattr(self, "enable_template", True)
        )
        effective_model = args.model or getattr(self, "model", "nai-diffusion-4-5-full")
        # 只传递用户明确指定的字段，避免 None 覆盖插件级默认配置。
        generation_overrides = args.generation_overrides()

        # 直连模式按「步数 × 画质」档位预估点数（任务接口计费见 pricing
        # 模块）；步数超过任务接口上限 50 时按 50 计。
        cost_note = ""
        if self.call_mode != "openai" and getattr(self, "show_cost_estimate", True):
            unit_cost = web_job_generation_cost(size, effective_model, effective_steps)
            cost_note = f"\n预计消耗: {unit_cost} 点/张"
            if n > 1:
                cost_note += f"（{n} 张共 {unit_cost * n} 点）"

        logger.info(
            f"{LOG_TAG} [cmd:image] 最终参数 | style={style} size_cn={size_cn} "
            f"size={size} n={n} steps={effective_steps} scale={effective_scale} "
            f"cfg={effective_cfg} sampler={effective_sampler} "
            f"noise={effective_noise} translate={effective_translate} "
            f"template={effective_template} model={effective_model}"
        )
        brief_parameters = (
            f"风格: {IMAGE_STYLES.get(style, style)}，比例: {size_cn}，数量: {n} 张\n"
            f"Steps: {effective_steps}，Scale: {effective_scale:g}，"
            f"CFG: {effective_cfg:g}\n"
            f"模型: {effective_model}\n"
            f"采样器: {effective_sampler}，噪声: {effective_noise}，"
            f"转译: {effective_translate}，模板: "
            f"{'开启' if effective_template else '关闭'}"
            f"{cost_note}"
        )
        # “仅图片”只省略成功前的状态文字，后续失败原因始终会正常回复。
        if self.bot_reply_mode == "完整":
            override_details = ""
            if args.artist is not None:
                override_details += f"\n画师串: {args.artist}"
            if args.negative is not None:
                override_details += f"\n反向提示词: {args.negative or '(空)'}"
            yield event.plain_result(
                f"提示词: {prompt}\n{brief_parameters}{override_details}"
            )
        elif self.bot_reply_mode == "简洁":
            yield event.plain_result(f"正在画图...\n{brief_parameters}")

        success = 0
        first_reason: str | None = None

        if self.call_mode == "openai":
            # OpenAI 兼容模式：一次请求返回 n 张。先按与直连一致的流程完成
            # 转译 / 服装缓存 / 角色模板，再把画师串并入 prompt。
            try:
                openai_prompt, _, _ = await self._prepare_openai_prompt(
                    prompt,
                    style,
                    custom_artists=args.artist,
                    enable_template=args.enable_template,
                    enable_translate=args.translate_mode,
                )
                logger.info(
                    f"{LOG_TAG} [cmd:image] OpenAI 兼容模式，一次生成 {n} 张 "
                    f"model={effective_model}"
                )
                images, reason = await self._openai_generate(
                    openai_prompt,
                    size,
                    n=n,
                    steps=effective_steps,
                    scale=args.scale,
                    sampler=args.sampler,
                    noise_schedule=args.noise_schedule,
                    negative=args.negative
                    if args.negative is not None
                    else self.negative,
                    model=effective_model,
                    characters=args.characters,
                )
            except Exception as exc:
                logger.warning(f"{LOG_TAG} [cmd:image] 生图流程异常: {exc!r}")
                images, reason = [], "exception"
            if images:
                for idx, img_bytes in enumerate(images, start=1):
                    success += 1
                    logger.info(
                        f"{LOG_TAG} [cmd:image] 第 {idx}/{n} 张发送 | bytes={len(img_bytes)}"
                    )
                    if self.bot_reply_mode == "仅图片":
                        yield event.chain_result([Img.fromBytes(img_bytes)])
                    else:
                        yield event.chain_result(
                            [Plain(f"[{idx}/{n}]"), Img.fromBytes(img_bytes)]
                        )
            else:
                first_reason = reason
                yield event.plain_result(f"生成失败：{_format_generate_error(reason)}")
        else:
            # 传统 GET 模式：每张图独立请求，单张失败不中止后续
            for i in range(n):
                logger.info(f"{LOG_TAG} [cmd:image] 生成第 {i + 1}/{n} 张")
                try:
                    img_bytes, reason = await self._generate_one(
                        prompt,
                        style,
                        size,
                        **generation_overrides,
                    )
                except Exception as exc:
                    logger.warning(
                        f"{LOG_TAG} [cmd:image] 第 {i + 1}/{n} 张异常: {exc!r}"
                    )
                    img_bytes, reason = None, "exception"
                if img_bytes:
                    success += 1
                    logger.info(
                        f"{LOG_TAG} [cmd:image] 第 {i + 1}/{n} 张发送 | bytes={len(img_bytes)}"
                    )
                    if self.bot_reply_mode == "仅图片":
                        yield event.chain_result([Img.fromBytes(img_bytes)])
                    else:
                        yield event.chain_result(
                            [Plain(f"[{i + 1}/{n}]"), Img.fromBytes(img_bytes)]
                        )
                else:
                    if first_reason is None:
                        first_reason = reason
                    logger.warning(
                        f"{LOG_TAG} [cmd:image] 第 {i + 1}/{n} 张失败 | reason={reason}"
                    )
                    yield event.plain_result(
                        f"第 {i + 1}/{n} 张生成失败：{_format_generate_error(reason)}"
                    )

        if success == 0:
            logger.error(
                f"{LOG_TAG} [cmd:image] 全部 {n} 张失败 | first_reason={first_reason}"
            )
            yield event.plain_result(
                f"全部 {n} 张图片生成失败。\n{_format_generate_error(first_reason or 'unknown')}"
            )
        else:
            logger.info(f"{LOG_TAG} [cmd:image] 完成 | 成功 {success}/{n}")

    def _interrogate_image_source(self, event: AstrMessageEvent, argument: str) -> str:
        """Resolve a reference image from an explicit path/URL or message content.

        Args:
            event: Current message event containing optional image segments.
            argument: Optional path or URL supplied after the command.

        Returns:
            A URL, data URL, or local path accepted by the multimodal provider.
        """
        explicit = str(argument or "").strip().strip("\"'")
        if explicit:
            return explicit
        message_obj = getattr(event, "message_obj", None)
        roots = [
            getattr(message_obj, "message", None),
            getattr(message_obj, "raw_message", None),
        ]

        def visit(value: Any) -> str:
            if isinstance(value, (list, tuple)):
                for item in value:
                    found = visit(item)
                    if found:
                        return found
                return ""
            if isinstance(value, dict):
                kind = str(value.get("type") or value.get("post_type") or "").lower()
                data = (
                    value.get("data") if isinstance(value.get("data"), dict) else value
                )
                if kind == "image" or any(
                    key in data for key in ("url", "path", "file", "src")
                ):
                    for key in (
                        "url",
                        "origin_url",
                        "path",
                        "file",
                        "src",
                        "image_path",
                        "file_path",
                    ):
                        candidate = str(data.get(key) or "").strip()
                        if candidate:
                            return candidate
                for key in ("message", "messages", "content", "data"):
                    nested = value.get(key)
                    if nested is not value:
                        found = visit(nested)
                        if found:
                            return found
                return ""
            text = str(value or "")
            match = re.search(r"\[CQ:image,([^\]]+)\]", text, re.IGNORECASE)
            if match:
                fields = dict(
                    part.split("=", 1)
                    for part in match.group(1).split(",")
                    if "=" in part
                )
                return str(
                    fields.get("url") or fields.get("path") or fields.get("file") or ""
                ).strip()
            return ""

        for root in roots:
            found = visit(root)
            if found:
                return found
        return ""

    async def _interrogate_image_url(self, source: str) -> str:
        """Convert a local reference image into a multimodal data URL when needed."""
        if re.match(r"^(?:https?|data:|base64://)", source, re.IGNORECASE):
            return source
        path = Path(source).expanduser()
        if not path.is_file():
            return source
        raw = await asyncio.to_thread(path.read_bytes)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    @filter.command("nai_interrogate", alias={"反推", "nai反推"})
    async def interrogate(self, event: AstrMessageEvent):
        """Reverse-engineer a reference image into NAI tags using a vision model.

        Args:
            event: Message containing an image or an explicit path/URL.
        """
        raw = str(event.message_str or "").strip()
        argument = re.sub(
            r"^(?:/)?(?:nai_interrogate|反推|nai反推)\s*", "", raw, flags=re.IGNORECASE
        ).strip()
        explicit_source, _, explicit_extra = argument.partition(" ")
        source = ""
        if explicit_source and (
            re.match(r"^(?:https?|data:|base64://)", explicit_source, re.IGNORECASE)
            or Path(explicit_source).expanduser().is_file()
        ):
            source = explicit_source
        else:
            source = self._interrogate_image_source(event, "")
            if not explicit_extra and explicit_source and source:
                explicit_extra = explicit_source
        if not source:
            yield event.plain_result(
                "请附带图片，或使用 /nai_interrogate <图片路径或URL> [补充要求]。"
            )
            return
        provider_id = self._resolve_interrogate_provider_id()
        provider = None
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            provider = self.context.get_using_provider(event.unified_msg_origin)
        if provider is None:
            yield event.plain_result(
                "未找到可用的多模态模型，请配置 interrogate_provider。"
            )
            return
        image_url = await self._interrogate_image_url(source)
        extra = explicit_extra.strip()
        prompt = (
            "Analyze the reference image and output a faithful NovelAI/Stable Diffusion tag prompt. "
            "Describe only visible content. Preserve the subject's identity, appearance, clothing, "
            "props, environment, camera/viewpoint, lighting, and composition. "
            "Do not invent hidden details. Output only comma-separated English tags.\n"
            f"Additional request: {extra or 'none'}"
        )
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=IMAGE_PROMPT_SPEC or TRANSLATE_SYSTEM_PROMPT,
                    image_urls=[image_url],
                    max_tokens=self.interrogate_max_tokens,
                ),
                timeout=120,
            )
            tags = str(getattr(response, "completion_text", "") or "").strip()
            tags = re.sub(
                r"<\s*think(?:ing)?\s*>.*?<\s*/\s*think(?:ing)?\s*>",
                "",
                tags,
                flags=re.I | re.S,
            )
            tags = re.sub(r"^```[a-zA-Z0-9_-]*\s*|```$", "", tags).strip().strip("\"'`")
            tags = re.sub(r"^\s*(?:output|tags?)\s*:\s*", "", tags, flags=re.I)
            tags = normalize_prompt(tags)
            if not tags:
                yield event.plain_result("多模态模型没有返回有效的 NAI tag。")
                return
            yield event.plain_result(f"反推结果：\n{tags}")
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [cmd:interrogate] failed: {exc!r}")
            yield event.plain_result(f"图片反推失败：{type(exc).__name__}: {exc}")

    def _refresh_llm_tool_schema(self) -> None:
        """按当前插件配置改写 NAI_Generate_Image 的参数 schema。

        注册时由 docstring 生成的 schema 是静态的，插件实例化（含配置热重载）
        后按设置同步：「开启画风参数」关闭时隐藏 style 字段，生图自动使用设置
        里的默认画风；size_cn 的说明按调用模式区分——OpenAI 兼容模式直接用像
        素分辨率（宽x高），传统 GET 模式保持中文分档名。首次调用时把注册生成
        的原始 properties 缓存在工具对象上，之后每次都从原始 schema 重建，保
        证开关来回切换时字段可以恢复。
        """
        try:
            manager = self.context.get_llm_tool_manager()
            tool = manager.get_func("NAI_Generate_Image") if manager else None
            if tool is None:
                return
            params = dict(getattr(tool, "parameters", None) or {})
            original = getattr(tool, "_nai_original_properties", None)
            if original is None:
                original = {
                    key: dict(value)
                    for key, value in (params.get("properties") or {}).items()
                    if isinstance(value, dict)
                }
                if not original:
                    return
                # pydantic dataclass 允许附加普通属性；缓存注册时的完整字段定义。
                object.__setattr__(tool, "_nai_original_properties", original)
            props = {key: dict(value) for key, value in original.items()}
            # 「开启画风参数」关闭时从 schema 隐藏 style 字段，模型不再传画风，
            # 生图回退到插件设置的默认画风。
            if not self.enable_tool_style:
                props.pop("style", None)
            elif self._artist_slots_enabled() and "style" in props:
                # 槽位开启时，把画风参数的取值限定为「槽位里的画风名」，
                # 模型就能直接认人话里的画风名（而不是那 7 个内部风格键）。
                try:
                    slot_names = [s["name"] for s in self._load_artist_slots()]
                except Exception:
                    slot_names = []
                if slot_names:
                    props["style"]["description"] = (
                        "画风名。只能从插件设置里的画风槽位中选一个："
                        + " / ".join(slot_names)
                        + "。选中后该槽位绑定的模型与画师串会一起生效。"
                        "留空则使用「正式生图启用槽位」里选中的画风。"
                    )
                    props["style"]["enum"] = slot_names
            # 「启用多角色块」关闭时隐藏角色块参数，模型看不到就不会去拆角色。
            if not self._character_blocks_enabled():
                props.pop("char1_prompt", None)
                props.pop("char2_prompt", None)
            if self.call_mode == "openai":
                default_size = self._openai_size(self.image_size)
                if "size_cn" in props:
                    props["size_cn"]["description"] = (
                        '图片尺寸，像素分辨率 "宽x高"，宽高须为 64 的倍数。'
                        f"常用值: {', '.join(OPENAI_TOOL_SIZES)}。"
                        f"留空则使用插件设置的默认尺寸（当前默认 {default_size}）。"
                    )
            elif "size_cn" in props:
                props["size_cn"]["description"] = (
                    "图片尺寸。可选：竖图 / 横图 / 方图 / 2K竖图 / 2K横图 / "
                    "2K方图 / 4K竖图 / 4K横图 / 4K方图。"
                    f"留空则使用插件设置的默认尺寸（当前默认 {self.image_size}）。"
                )
            params["properties"] = props
            params["type"] = "object"
            tool.parameters = params

            # 法典检索工具：未启用时把描述标成不可用（不改动 AstrBot 的全局
            # 停用列表，避免污染用户手动配置的工具开关），并在 handler 内直接拒绝。
            grimoire_tool = manager.get_func("NAI_Search_Grimoire") if manager else None
            if grimoire_tool is not None:
                original_desc = getattr(grimoire_tool, "_nai_original_desc", None)
                if original_desc is None:
                    original_desc = str(getattr(grimoire_tool, "description", "") or "")
                    object.__setattr__(
                        grimoire_tool, "_nai_original_desc", original_desc
                    )
                if self._grimoire_enabled():
                    object.__setattr__(grimoire_tool, "description", original_desc)
                else:
                    object.__setattr__(
                        grimoire_tool,
                        "description",
                        "（内置法典词库未启用，本工具不可用，请勿调用。）",
                    )

            logger.debug(
                f"{LOG_TAG} [tool] NAI_Generate_Image schema 已按配置刷新 | "
                f"style字段={'显示' if self.enable_tool_style else '隐藏'} "
                f"角色块={'显示' if self._character_blocks_enabled() else '隐藏'} "
                f"法典工具={'启用' if self._grimoire_enabled() else '停用'} "
                f"尺寸格式={'分辨率' if self.call_mode == 'openai' else '分档'}"
            )
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [tool] 工具参数 schema 刷新失败: {exc!r}")

    """
    提供tool让llm可以自主决定生成图片。为了防止暴走，每个消息事件最多请求1张。
    """

    @filter.llm_tool()
    async def NAI_Generate_Image(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        style: str = "",
        size_cn: str = "",
        char1_prompt: str = "",
        char2_prompt: str = "",
    ) -> AsyncGenerator[str, None]:
        """用NovelAI生成1张图片并直接发送给当前用户。

        每个用户消息最多调用一次本工具。不要在同一响应中重复调用，也不要同时
        调用send_message_to_user；生成成功后本工具会直接发送图片。
        画风与尺寸留空时，固定使用插件设置里选择的默认画风与默认尺寸。

        Args:
            prompt(string): NovelAI 4.5 提示词，标签化格式，标签间用英文逗号隔开。规范：
                - 质量词放前面：masterpiece, best quality, highly detailed
                - 角色描述：1girl/1boy, solo, 外貌特征；人物用 人物名(作品名) 形式（如 texas the omertosa (arknights)），特征不全就补充描述词
                - 画师风格引用：artist:画师名
                - 权重语法：{tag} 加强，[tag] 减弱；(tag:1.5) 为旧版权重；推荐新版 权重::标签:: 格式，可组合多标签如 1.5::red dress, long dress::，支持高权重（2、5、10 以上）
                - 负向权重（NAI4 精髓）：可移除物体或翻转概念，如 -2::标签::
                - 多角色：用 {人物 [tags], {位置}, ntags = [ntags] 人物} 包裹每个角色，最多 6 名，位置可选 左/中/右/上/下 组合；{人物 与 人物} 是占位符不可删除
                - 角色互动：source#动作 发起者 / target#动作 承受者 / mutual#动作 互相（如 source#hug, target#hug, mutual#hug）
                - 渲染文字：Text: HAVE FUN! 指定角色说出文字；no text 减少文本生成
                - 情绪词：可加入情绪描述增强表现力
                - 多风格混合：-2::artist collaboration:: 可融合 3 个以上画师风格
                - 精简：避免堆叠重复/无意义 tags，描述清楚构图即可
                示例：masterpiece, best quality, {人物 [1girl, solo, long hair, blue eyes, source#hug], {位置左}, ntags = [lowres, bad anatomy] 人物}, {人物 [1boy, short hair, target#hug], {位置右} 人物}, outdoor, sunset, artist:wlop
            style(string): 画风。可选：vertical(韩漫小清新风) / comicDoujin(漫画同人风) / r18(2.5D唯美风) / lolita25d(2.5D唯美风（萝）) / anime(本子里番风) / galgame(GalGame风) / custom(自定义)。留空使用插件设置的默认画风。
            size_cn(string): 图片尺寸，留空使用插件设置的默认尺寸。OpenAI 兼容调用模式下为像素分辨率 "宽x高"（如 832x1216，宽高须为 64 的倍数）；传统 GET 模式下为分档名：竖图 / 横图 / 方图 / 2K竖图 / 2K横图 / 2K方图 / 4K竖图 / 4K横图 / 4K方图
            char1_prompt(string): 【角色块1】只写第一个角色自身的标签：外貌、发色瞳色、表情、姿势动作、穿着及其局部状态（如 1girl, solo, long hair, blue eyes, smile, sitting）。留空表示画面只有一个角色。不要把场景、构图、画风写在这里。
            char2_prompt(string): 【角色块2】第二个角色自身的标签，写法同上。填写后两个角色会分别放到画面左右，各自独立不串色；双人互动时用 source#动作 / target#动作 表示发起方与承受方。
        """
        prompt = normalize_prompt(self._expand_grimoire(prompt))
        if not self.enable_llm_tool:
            logger.warning(
                f"{LOG_TAG} [tool:NAI_Generate_Image] 生图工具已禁用，请在插件设置中开启 enable_llm_tool"
            )
            yield "生图工具已被管理员禁用，请在插件设置中开启 enable_llm_tool"
            return

        logger.info(
            f"{LOG_TAG} [tool:NAI_Generate_Image] 调用NAI_Generate_Image, 参数： prompt: {prompt[:100]}, style: {style}, size_cn:{size_cn}"
        )
        raw_style = str(style or "").strip()
        if raw_style == "自定义":
            raw_style = "custom"
        # 优先把 style 当作「画风槽位名」来匹配：槽位开启时，模型可以直接按
        # 用户在设置页起的画风名选画风，该槽位绑定的模型与画师串一起生效。
        style_slot = self._match_style_slot(raw_style) if raw_style else None
        if style_slot is not None:
            style = normalize_image_style(self.image_style) or "vertical"
            slot_model = str(style_slot.get("model") or "")
            slot_artists = str(style_slot.get("artists") or "")
            logger.info(
                f"{LOG_TAG} [tool:NAI_Generate_Image] 命中画风槽位 "
                f"'{style_slot['name']}' | model={slot_model or '默认'} "
                f"artists={'有' if slot_artists else '无'}"
            )
        else:
            slot_model = ""
            slot_artists = ""
            # 「开启画风参数」关闭或未传画风时，固定使用插件设置里选好的默认画风；
            # 工具 schema 会同步隐藏 style 字段（见 _refresh_llm_tool_schema）。
            style = (
                raw_style
                if (self.enable_tool_style and raw_style)
                else normalize_image_style(self.image_style) or "vertical"
            )
        if not prompt:
            logger.info(f"{LOG_TAG} [tool:NAI_Generate_Image] prompt 为空")
            yield "生成失败，提示词不应为空"
            return

        # 按调用模式校验凭据
        if self.call_mode == "openai":
            if not self.openai_api_base_url:
                logger.warning(f"{LOG_TAG} [tool:NAI_Generate_Image] OpenAI 接口未配置")
                yield "生成失败，未配置 OpenAI 兼容生图接口，请告知用户先在插件配置中填写 openai_api_base_url。"
                return
        else:
            if not self.image_gen_key:
                logger.warning(f"{LOG_TAG} [tool:NAI_Generate_Image] token 未配置")
                yield "生成失败，未配置 image_gen_key，请告知用户先在插件配置中填写 token。"
                return

        if style_slot is None and style not in IMAGE_STYLES and style != "custom":
            logger.warning(f"{LOG_TAG} [tool:NAI_Generate_Image] 未知风格: {style}")
            slot_names = [s["name"] for s in self._load_artist_slots()] if self._artist_slots_enabled() else []
            choices = slot_names + list(IMAGE_STYLES.keys())
            yield f"未知风格: {style}\n可选: {', '.join(choices)}"
            return

        # 尺寸按调用模式归一化：OpenAI 兼容模式直接使用像素分辨率（宽x高），
        # 不再经由「竖图/2K/4K」分档名换算（4K 档在 OpenAI 接口会被静默降级，
        # 分档名与真实分辨率对不上）；传统 GET 模式保持中文分档名，上游只识别
        # 中文尺寸值（portrait/2k_portrait 等英文别名会导致 2K/4K 生成失败）。
        raw_size = str(size_cn or "").strip()
        if self.call_mode == "openai":
            if not raw_size:
                raw_size = self.image_size
            normalized_size = normalize_image_size(raw_size)
            if normalized_size:
                # 中文/英文分档名都映射为对应像素分辨率
                raw_size = self._openai_size(normalized_size)
            if not _PIXEL_SIZE_RE.match(raw_size):
                logger.warning(
                    f"{LOG_TAG} [tool:NAI_Generate_Image] 未知尺寸: {size_cn}"
                )
                yield (
                    f"未知尺寸: {size_cn}\n"
                    f"可选（像素分辨率 宽x高）: {', '.join(OPENAI_TOOL_SIZES)}"
                )
                return
            size = self._normalize_openai_size(raw_size)
        else:
            size = normalize_image_size(raw_size) if raw_size else self.image_size
            if not size and raw_size:
                # 兼容模型直接给像素分辨率：按长宽比映射到 GET 分档名。
                match = _PIXEL_SIZE_RE.match(raw_size)
                if match:
                    width, height = int(match.group(1)), int(match.group(2))
                    if width > 0 and height > 0:
                        if abs(width - height) <= max(width, height) * 0.08:
                            size = "方图"
                        else:
                            size = "竖图" if height > width else "横图"
            if not size:
                logger.warning(
                    f"{LOG_TAG} [tool:NAI_Generate_Image] 未知尺寸: {size_cn}"
                )
                yield (f"未知尺寸: {size_cn}\n可选: {', '.join(IMAGE_SIZES.keys())}")
                return

        generation_state = None
        if hasattr(event, "get_extra"):
            generation_state = event.get_extra("_nai_image_generation_state")
        if generation_state in {"running", "finished"}:
            logger.warning(
                f"{LOG_TAG} [tool:NAI_Generate_Image] 跳过同一事件内的额外请求 | "
                f"style={style} size_cn={size_cn}"
            )
            yield (
                "本轮消息已经执行过一次图片生成请求，请勿重复调用"
                "NAI_Generate_Image；图片成功时已由本工具直接发送，无需调用"
                "send_message_to_user。"
            )
            return

        if hasattr(event, "set_extra"):
            event.set_extra("_nai_image_generation_state", "running")

        # 多角色块：把 char1/char2 转成当前通道能表达的形式。
        # 未开启「启用多角色块」时忽略这两个参数，保持升级前的单 prompt 行为。
        char_blocks: list[str] = []
        if self._character_blocks_enabled():
            char_blocks = [
                normalize_prompt(self._expand_grimoire(c))
                for c in (char1_prompt, char2_prompt)
                if str(c or "").strip()
            ]
        elif str(char1_prompt or "").strip() or str(char2_prompt or "").strip():
            logger.info(
                f"{LOG_TAG} [tool:NAI_Generate_Image] 多角色块未启用，已忽略 "
                f"char1/char2 参数"
            )
        characters: list[tuple[str, float, float]] | None = None
        if char_blocks and self.call_mode == "openai":
            if len(char_blocks) == 1:
                characters = [(char_blocks[0], 0.5, 0.5)]
            else:
                # 双角色分别居中于左右半幅，避免两个角色块互相污染。
                characters = [
                    (char_blocks[0], 0.25, 0.5),
                    (char_blocks[1], 0.75, 0.5),
                ]
        elif char_blocks:
            # 传统 GET 通道没有坐标数组，改用上游支持的多角色块语法表达。
            wrapped: list[str] = []
            for idx, block in enumerate(char_blocks[:2]):
                if len(char_blocks) > 1:
                    pos = "{位置左}" if idx == 0 else "{位置右}"
                    wrapped.append("{人物 [" + block + "], " + pos + " 人物}")
                else:
                    wrapped.append("{人物 [" + block + "] 人物}")
            prompt = normalize_prompt(", ".join([prompt] + wrapped))
            logger.info(
                f"{LOG_TAG} [tool:NAI_Generate_Image] 直连模式多角色块已并入 prompt"
                f" | chars={len(wrapped)}"
            )

        logger.info(
            f"{LOG_TAG} [tool:NAI_Generate_Image] 最终参数 | style={style} size_cn={size_cn} "
            f"size={size} n=1"
        )

        logger.info(f"{LOG_TAG} [tool:NAI_Generate_Image] 生成第 1/1 张")
        try:
            if self.call_mode == "openai":
                openai_prompt, _, _ = await self._prepare_openai_prompt(
                    prompt, style, custom_artists=slot_artists or None
                )
                images, reason = await self._openai_generate(
                    openai_prompt,
                    size,
                    n=1,
                    characters=characters,
                    model=slot_model or None,
                )
                img_bytes = images[0] if images else None
            else:
                img_bytes, reason = await self._generate_one(
                    prompt,
                    style,
                    size,
                    model=slot_model or None,
                    custom_artists=slot_artists or None,
                )
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [tool:NAI_Generate_Image] 生成异常: {exc!r}")
            img_bytes, reason = None, "exception"
        finally:
            if hasattr(event, "set_extra"):
                event.set_extra("_nai_image_generation_state", "finished")
        if img_bytes:
            logger.info(
                f"{LOG_TAG} [tool:NAI_Generate_Image] 图片发送 | bytes={len(img_bytes)}"
            )
            await event.send(MessageChain(chain=[Img.fromBytes(img_bytes)]))
            logger.info(f"{LOG_TAG} [tool:NAI_Generate_Image] 完成 | 成功")
            yield "图片已生成并发送给用户，请根据本次请求继续回复。"
            return

        logger.warning(f"{LOG_TAG} [tool:NAI_Generate_Image] 失败 | reason={reason}")
        yield f"生成失败：{_format_generate_error(reason)}"
        return

    @filter.command("quota")
    async def quota(self, event: AstrMessageEvent):
        """查询上游生图站的剩余额度与 token 状态。"""
        sender = event.get_sender_id() if hasattr(event, "get_sender_id") else "?"
        logger.info(f"{LOG_TAG} [cmd:quota] 收到指令 | sender={sender}")
        if not self.image_gen_key:
            logger.warning(f"{LOG_TAG} [cmd:quota] token 未配置")
            yield event.plain_result("未配置 image_gen_key。")
            return
        yield event.plain_result("正在查询额度...")
        result = await self._fetch_quota()
        if not result.get("ok"):
            message = result.get("message", "未知原因")
            logger.warning(f"{LOG_TAG} [cmd:quota] 查询失败 | message={message}")
            yield event.plain_result(f"额度查询失败：{message}")
            return
        logger.info(f"{LOG_TAG} [cmd:quota] 返回 {result['value']}")
        lines = [f"剩余额度: {result['value']}"]
        if not result.get("enabled", True):
            lines.append("⚠️ 该 token 已被站点停用（enabled=false），生图可能失败")
        yield event.plain_result("\n".join(lines))

    @filter.command("price", alias={"价格", "点数"})
    async def price(self, event: AstrMessageEvent):
        """试算传统 GET 直连的点数消耗，并实时查询余额。

        不带参数时按插件默认配置试算；带 ``--steps``/``--size``/``--model``
        等参数时按指定值试算（参数与 /image 完全一致，但不会真的生图）。
        传统 GET 直连按「步数 × 画质」档位计价，>28 步需走任务接口。
        """
        sender = event.get_sender_id() if hasattr(event, "get_sender_id") else "?"
        logger.info(f"{LOG_TAG} [cmd:price] 收到指令 | sender={sender}")

        # 指令名剥离：与 /image 一样，AstrBot 不会改写 event.message_str。
        raw = str(event.message_str or "").strip()
        text = raw
        for prefix in ("price", "/price"):
            if raw == prefix:
                text = ""
                break
            if raw.startswith(prefix) and raw[len(prefix)].isspace():
                text = raw[len(prefix) :].lstrip()
                break

        try:
            args = parse_image_command(text, default_style=self.image_style)
        except ImageCommandArgumentError as e:
            logger.info(f"{LOG_TAG} [cmd:price] 参数错误: {e}")
            yield event.plain_result(f"参数错误：{e}")
            return

        if self.call_mode == "openai":
            yield event.plain_result(
                "当前调用模式为「OpenAI 兼容」，点数规则由该服务方决定，"
                "本价目表仅适用于「传统 GET」直连模式（nai.sta1n.cn）。\n"
                "可在插件设置中把调用模式切回「传统 GET」后再次查询。"
            )
            return

        size_cn = args.size or self.image_size
        size = self._resolve_direct_size(size_cn)
        steps = args.steps if args.steps is not None else self.steps
        model = args.model or self._slot_model() or self.model
        n = args.n if args.n is not None else self.image_count

        unit_cost = web_job_generation_cost(size, model, steps)
        routed_steps = effective_transport_steps(steps, TRANSPORT_WEB_JOBS)

        lines = [
            "💰 点数试算（传统 GET 直连）",
            f"当前参数: {size} · {routed_steps} 步 · {model}",
            "生图通道: 任务接口 /api/web/jobs（支持 1-50 步、档位计价、失败退点）\n"
            "  上游无任务接口（旧版自建）时自动回退 GET 直链（≤28 步、基础档计价）",
            f"预计消耗: {unit_cost} 点/张（{n} 张共 {unit_cost * n} 点）",
        ]
        # 实时余额：与 /quota 同源，失败不影响试算结果展示。
        if self.image_gen_key:
            quota = await self._fetch_quota()
            if quota.get("ok"):
                balance_line = f"实时余额: {quota['value']} 点"
                if not quota.get("enabled", True):
                    balance_line += "（⚠️ 该 token 已被站点停用）"
                lines.append(balance_line)
                if quota["value"] < unit_cost * n:
                    lines.append("⚠️ 余额不足以完成本次生成，请先充值或减少张数。")
            else:
                lines.append(
                    f"实时余额: 查询失败（{quota.get('message', '未知原因')}）"
                )
        else:
            lines.append("实时余额: 未配置 image_gen_key，跳过查询")

        lines.append("")
        lines.append("价目表（点/张，步数 × 画质）:")
        lines.extend(price_table_lines())
        lines.append(
            "说明: ≤28 步按基础档（V4.5 标准 1 点 / V5 标准 8 点 / 2K 15 点 / 4K 25 点）；"
            "29 步起走任务接口并加价（29-35 +4，36-45 +6，46-50 +8）。"
            "GET 直链最高按 28 步路由，不给超 28 步加价。"
        )
        yield event.plain_result("\n".join(lines))

    @filter.command("usage", alias={"用量", "统计", "点数用量"})
    async def usage(self, event: AstrMessageEvent):
        """查询本插件的点数消耗统计（按日期聚合）。

        用法: ``/usage``（默认近 7 日）/ ``/usage 30`` / ``/usage all``
        / ``/usage today``。点数按上游档位表估算，OpenAI 兼容模式按像素
        画幅推断档位，属估算值。
        """
        sender = event.get_sender_id() if hasattr(event, "get_sender_id") else "?"
        logger.info(f"{LOG_TAG} [cmd:usage] 收到指令 | sender={sender}")

        if not getattr(self, "enable_usage_stats", True):
            yield event.plain_result(
                "点数用量统计已关闭，可在插件设置中开启「点数用量统计」后使用。"
            )
            return

        raw = str(event.message_str or "").strip()
        text = raw
        for prefix in ("usage", "/usage", "用量", "/用量", "统计", "/统计"):
            if raw == prefix:
                text = ""
                break
            if (
                raw.startswith(prefix)
                and len(raw) > len(prefix)
                and raw[len(prefix)].isspace()
            ):
                text = raw[len(prefix) :].lstrip()
                break

        days = 7
        token = text.strip().casefold()
        if token in ("", "7", "7d", "week", "周"):
            days = 7
        elif token in ("today", "今日", "今天", "1", "1d"):
            days = 1
        elif token in ("all", "全部", "累计", "total", "0"):
            days = 0
        else:
            try:
                parsed = int(token.rstrip("d天日"))
            except ValueError:
                parsed = 0
            if parsed <= 0:
                yield event.plain_result(
                    "参数无法识别。用法: /usage（近 7 日）/ /usage 30 / "
                    "/usage today / /usage all"
                )
                return
            days = min(parsed, 730)

        payload = await self._usage_stats_payload(days)
        if payload.get("error"):
            yield event.plain_result(f"读取用量统计失败：{payload['error']}")
            return

        try:
            async with self._usage_stats_lock:
                store = self._usage_stats_store()
                if store is None:
                    yield event.plain_result(
                        "点数用量统计暂不可用（插件数据目录未就绪）。"
                    )
                    return
                await asyncio.to_thread(store.load)
                report = format_usage_report(store, days)
        except Exception as e:
            logger.warning(f"{LOG_TAG} [cmd:usage] 渲染报告失败: {e!r}")
            yield event.plain_result(f"读取用量统计失败：{e!r}")
            return

        yield event.plain_result(report)

    @filter.command("imgstatus")
    async def imgstatus(self, event: AstrMessageEvent):
        sender = event.get_sender_id() if hasattr(event, "get_sender_id") else "?"
        logger.info(f"{LOG_TAG} [cmd:imgstatus] 收到指令 | sender={sender}")
        yield event.plain_result("正在检查生图服务...")

        # 1) 本地代理是否在线 —— 关系到陪伴插件能不能调通
        proxy_ok = False
        proxy_msg = ""
        try:
            if not self._session:
                proxy_msg = "（aiohttp session 未初始化）"
            else:
                async with self._session.get(
                    f"http://{PROXY_HOST}:{self.proxy_port}/v1/proxy_status",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as r:
                    proxy_ok = r.status == 200
        except Exception as e:
            proxy_msg = f"（{type(e).__name__}）"

        # 2) 不再探测上游可达性（该检查较耗时，已移除；生图失败时以生图报错为准）
        lines = []
        lines.append(
            f"本地代理 127.0.0.1:{self.proxy_port}: {'✅ 在线' if proxy_ok else '❌ 离线'} {proxy_msg}"
        )
        if self.call_mode == "openai":
            lines.append(
                f"上游 OpenAI 兼容接口: {self.openai_api_base_url or '❌ 未配置'}"
                f" | key={'✅ 已配置' if self.openai_api_key else '⚠️ 未配置'}"
                f" | model={self.openai_api_model}"
            )
        else:
            lines.append(
                f"上游 {self.base_url}: token={'✅ 已配置' if self.image_gen_key else '❌ 未配置'}"
                "（已取消在线探测，实际可用性以生图结果为准）"
            )
        lines.append(
            f"绕过系统代理直连生图站: {'✅ 开启' if self.bypass_system_proxy else '❌ 关闭'}"
            "（开启时忽略系统/环境代理；TUN 类梯子需在梯子软件里给 nai.sta1n.cn 加直连规则）"
        )
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="NAI_Search_Grimoire")
    async def NAI_Search_Grimoire(
        self,
        event: AstrMessageEvent,
        query: str = "",
    ) -> str:
        """在内置 NovelAI 法典词库（19,663 条实测 Tag 组合，NAI4.5 与 NAI5 分卷）中检索画风、服饰、姿势、视角、特殊玩法等 Tag。

        检索范围自动跟随当前生图模型：V5 会同时查 V5 与 V4.5 两卷（新底模兼容旧词条，同名优先 V5）；V4.5 只查 V4.5 卷。

        需要特定画风、精致穿搭、复杂互动姿势时，可先调用本工具取现成的实测 Tag 组合；
        也可以直接在 NAI_Generate_Image 的 prompt 里写【法典:词条名】，生图时会自动展开。

        Args:
            query(string): 检索关键词（如：死库水、旗袍、自拍、丝袜、动画画风），支持空格分隔多个词。
        """
        if not self._grimoire_enabled():
            return "内置法典词库未启用，请在插件设置中开启「启用内置法典词库」后再使用。"
        if not getattr(self, "enable_llm_tool", False):
            return "生图工具已被管理员禁用，法典检索当前不可用。"
        hits = await asyncio.to_thread(
            search_grimoire, query, 8, self._active_model_ver()
        )
        if not hits:
            return (
                f"未在法典词库中找到与 '{query}' 匹配的词条，"
                f"请尝试缩短或更换中文关键词。"
            )
        lines = [
            f"📖 NovelAI 法典检索结果（共命中 {len(hits)} 条，"
            f"可直接把 Tags 用于 NAI_Generate_Image）："
        ]
        for i, h in enumerate(hits, 1):
            lines.append(
                f"{i}. 【{h['title']}】({h['volume']} · {h['category']})\n"
                f"   Tags: {h['clean_tags']}"
            )
        return "\n".join(lines)

    @filter.command("法典", alias={"grimoire", "查法典"})
    async def cmd_grimoire(self, event: AstrMessageEvent):
        """检索内置 NovelAI 法典词条。用法：/法典 关键词"""
        if not self._grimoire_enabled():
            yield event.plain_result(
                "内置法典词库未启用。请到插件设置里打开「启用内置法典词库」后再使用。"
            )
            return
        raw = str(event.message_str or "").strip()
        for prefix in ("/法典", "法典", "/grimoire", "grimoire", "/查法典", "查法典"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        if not raw:
            yield event.plain_result(
                "📖 用法：/法典 <关键词>\n"
                "已收录 19,663 条实测词条（NAI4.5 18,505 / NAI5 1,158），"
                "生图时可用【法典:词条名】直接展开。"
            )
            return
        hits = await asyncio.to_thread(
            search_grimoire, raw, 6, self._active_model_ver()
        )
        if not hits:
            yield event.plain_result(f"未在法典中找到包含「{raw}」的词条。")
            return
        lines = [f"📖 NovelAI 法典检索：「{raw}」（前 {len(hits)} 条）"]
        for i, h in enumerate(hits, 1):
            lines.append(
                f"\n{i}. 【{h['title']}】({h['volume']} · {h['category']})\n"
                f"{h['clean_tags'][:260]}"
            )
        yield event.plain_result("\n".join(lines))

