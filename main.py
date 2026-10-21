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

import aiohttp
import yaml

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
    from .pricing import estimate_openai_cost
    from .usage_stats import (
        STATS_FILE_NAME,
        UsageStatsStore,
        format_usage_report,
    )
    from .grimoire_service import (
        build_grimoire_translation_hint,
        expand_grimoire_macros,
        normalize_model_ver,
        search_grimoire,
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
else:
    from command_args import (
        ImageCommandArgumentError,
        normalize_image_size,
        normalize_image_style,
        parse_image_command,
        strip_image_command_prefix,
    )
    from pricing import estimate_openai_cost
    from usage_stats import (
        STATS_FILE_NAME,
        UsageStatsStore,
        format_usage_report,
    )
    from grimoire_service import (
        build_grimoire_translation_hint,
        expand_grimoire_macros,
        normalize_model_ver,
        search_grimoire,
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

LOG_TAG = "[NAI-Image]"

_IMAGE_PROMPT_SPEC_PATH = Path(__file__).resolve().parents[1] / "image_prompt_spec.txt"


def _load_image_prompt_spec() -> str:
    try:
        return _IMAGE_PROMPT_SPEC_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning(f"{LOG_TAG} image prompt spec unavailable: {exc}")
        return ""


IMAGE_PROMPT_SPEC = _load_image_prompt_spec()

PLUGIN_NAME = "astrbot_plugin_nai_image"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/test_panel"
BOT_REPLY_MODES = {"仅图片", "简洁", "完整"}

DEFAULT_CHARACTER_BASE_TAGS = ""

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
        "artist:ningen_mame,, noyu_(noyu23386566),, toosaka asagi,, location,\\n"
        "20::best quality, absurdres, very aesthetic, detailed, masterpiece::,:,, "
        "very aesthetic, masterpiece, no text,"
    ),
}

DEFAULT_NEGATIVE = (
    "{{bad anatomy}},{bad feet},bad hands,{{{bad proportions}}},{blurry},cloned face,cropped,"
    "{{{deformed}}},{{{disfigured}}},error,{{{extra arms}}},{extra digit},{{{extra legs}}},extra limbs,"
    "{{extra limbs}},{fewer digits},{{{fused fingers}}},gross proportions,ink eyes,ink hair,"
    "jpeg artifacts,{{{{long neck}}}},low quality,{malformed limbs},{{missing arms}},{missing fingers},"
    "{{missing legs}},{{{more than 2 nipples}}},mutated hands,{{{mutation}}},normal quality,owres,"
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

    empty_row = {"date": "", "cost": 0, "count": 0, "failed": 0}
    empty_totals = {
        "days_in_window": 0,
        "active_days": 0,
        "cost": 0,
        "count": 0,
        "failed": 0,
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
            "🚫 上游生成任务失败（失败任务的点数会自动退回），"
            "请查看日志或稍后重试。"
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



@register(
    "astrbot_plugin_nai_image",
    "Evenstar259",
    "基于 NovelAI V4.5 / V5 的 AstrBot 生图插件——支持原生多角色块控制、法典检索、衣柜联动与相册归档。",
    "v2.2.0",
    "",
)
class NAIGenerateImagePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        logger.info(f"{LOG_TAG} [init] 插件实例化开始")

        self.call_mode: str = "openai"
        self.image_style: str = (
            normalize_image_style(config.get("image_style")) or "custom"
        )
        self.image_size: str = normalize_image_size(config.get("image_size")) or "竖图"
        try:
            self.image_count: int = max(1, min(6, int(config.get("image_count") or 1)))
        except (TypeError, ValueError):
            self.image_count = 1
        self.bot_reply_mode: str = config.get("bot_reply_mode") or "完整"
        if self.bot_reply_mode not in BOT_REPLY_MODES:
            self.bot_reply_mode = "完整"
        self.save_image_history: bool = bool(config.get("save_image_history", True))
        self.save_generation_parameters: bool = bool(
            config.get("save_generation_parameters", True)
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
        self.active_artist_slot: str = str(config.get("active_artist_slot") or "1").strip()
        self.custom_artists: str = config.get("custom_artists") or ""
        self.custom_artists_v5: str = config.get("custom_artists_v5") or ""
        self.model: str = config.get("model") or "nai-diffusion-4-5-full"
        try:
            self.steps: int = int(config.get("steps") or 24)
        except (TypeError, ValueError):
            self.steps = 24
        try:
            raw_scale = config.get("scale")
            self.scale: float = (
                float(raw_scale) if raw_scale is not None and raw_scale != "" else 6.0
            )
        except (TypeError, ValueError):
            self.scale = 6.0
        try:
            cfg = config.get("cfg")
            raw_cfg = float(cfg) if cfg is not None and cfg != "" else 0.0
            self.cfg_value: float = max(0.0, min(raw_cfg, 1.0))
        except (TypeError, ValueError):
            self.cfg_value = 0.0
        _sampler_cfg = (config.get("sampler") or "").strip()
        if _sampler_cfg == "ddim":
            _sampler_cfg = ""
        self.sampler: str = _sampler_cfg or "k_dpmpp_2m_sde"
        self.noise_schedule: str = "karras"
        neg = config.get("negative")
        self.negative: str = neg if neg else DEFAULT_NEGATIVE
        self.enable_template: bool = bool(config.get("enable_template", True))
        self.character_preset: str = (config.get("character_preset") or "").strip()
        self.character_preset_v5: str = (config.get("character_preset_v5") or "").strip()

        self.enable_usage_stats: bool = bool(config.get("enable_usage_stats", True))
        self._usage_stats_lock = asyncio.Lock()
        self._usage_stats: UsageStatsStore | None = None

        self.openai_api_base_url: str = (
            (config.get("openai_api_base_url") or config.get("base_url") or "").strip().rstrip("/")
        )
        self.openai_api_key: str = (
            config.get("openai_api_key") or config.get("image_gen_key") or ""
        ).strip()
        self.openai_api_model: str = (
            config.get("openai_api_model") or self.model or "nai-diffusion-4-5-full"
        ).strip() or "nai-diffusion-4-5-full"

        self.openai_reference_mode: str = (
            (config.get("openai_reference_mode") or "vibe").strip().casefold()
        )
        if self.openai_reference_mode not in ("img2img", "vibe", "director"):
            self.openai_reference_mode = "vibe"
        self.openai_director_caption: str = str(
            config.get("openai_director_caption") or "character&style"
        ).strip()
        if self.openai_director_caption not in OPENAI_DIRECTOR_CAPTIONS:
            self.openai_director_caption = "character&style"

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
        _fb_cfg = config.get("openai_director_fallback_images") or []
        if isinstance(_fb_cfg, str):
            _fb_cfg = [_fb_cfg]
        self.openai_director_fallback_images: list[str] = [
            str(p).strip() for p in _fb_cfg if str(p or "").strip()
        ]
        try:
            _seed_cfg = config.get("openai_seed")
            self.openai_seed: int = (
                int(_seed_cfg) if _seed_cfg not in (None, "") else -1
            )
        except (TypeError, ValueError):
            self.openai_seed = -1
        try:
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

        if migrate_legacy_translate_config(config) is not None:
            try:
                self._persist_config_migrations()
            except Exception as exc:
                logger.warning(f"{LOG_TAG} 插件配置迁移写回失败: {exc}")
        self.translate_mode: str = normalize_translate_mode(
            config.get("enable_translate", TRANSLATE_MODE_OFF)
        )
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

        self.enable_llm_tool: bool = bool(config.get("enable_llm_tool", True))
        self.enable_tool_style: bool = bool(config.get("enable_tool_style", True))
        self.bypass_system_proxy: bool = bool(config.get("bypass_system_proxy", True))

        logger.info(
            f"{LOG_TAG} [init] 配置加载完成 | "
            f"url={self.openai_api_base_url or '(未配置)'} "
            f"key={'已配置' if self.openai_api_key else '未配置'} "
            f"model={self.openai_api_model} style={self.image_style} size={self.image_size} "
            f"steps={self.steps} scale={self.scale} sampler={self.sampler}"
        )
        self._refresh_llm_tool_schema()

    def _get_backend_active_slot(self) -> dict[str, Any] | None:
        """读取后端设置中选定的正式生效槽位（与 UI 测试面板的临时测试槽位相互独立）。"""
        try:
            slots_data = self._load_artist_slots()
            slots = slots_data.get("slots") or []
            target_id = str(getattr(self, "active_artist_slot", "1") or "1").strip()
            for s in slots:
                if str(s.get("id")) == target_id:
                    return s
            return slots[0] if slots else None
        except Exception:
            return None

    def _active_model_name(self, model_override: str | None = None) -> str:
        """返回后端正式生效的模型名（优先取参数覆盖 -> 后端选定槽位绑定的模型 -> openai_api_model）。"""
        if model_override and str(model_override).strip():
            return str(model_override).strip()
        slot = self._get_backend_active_slot()
        if slot and str(slot.get("model") or "").strip():
            return str(slot["model"]).strip()
        return self.openai_api_model

    def _active_model_ver(self, model_override: str | None = None) -> str:
        """返回当前生效模型的词库版本标识 ('NAI4.5' 或 'NAI5')。"""
        return normalize_model_ver(self._active_model_name(model_override))

    def _active_custom_artists(self, model_override: str | None = None) -> str:
        """返回后端正式生效槽位的专属画师串。"""
        slot = self._get_backend_active_slot()
        if slot and str(slot.get("artists") or "").strip():
            return str(slot["artists"]).strip()
        if self._active_model_ver(model_override) == "NAI5" and self.custom_artists_v5.strip():
            return self.custom_artists_v5
        return self.custom_artists

    def _active_character_preset(self, model_override: str | None = None) -> str:
        """按当前模型版本返回对应的角色固定外貌底模。"""
        if self._active_model_ver(model_override) == "NAI5" and self.character_preset_v5.strip():
            return self.character_preset_v5
        return self.character_preset

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
        if not self.save_image_history:
            return
        try:
            parameters_to_save = (
                generation_parameters if self.save_generation_parameters else None
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
                    "today": today_rows[-1] if today_rows else {
                        "date": "",
                        "cost": 0,
                        "count": 0,
                        "failed": 0,
                    },
                    "window": window,
                    "overall": overall,
                    "series": series,
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
        try:
            from astrbot.core import sp
            sources = await sp.global_get("plugin_install_sources", {}) or {}
            if isinstance(sources, dict):
                sources["astrbot_plugin_nai_image"] = {"install_method": "upload"}
                await sp.global_put("plugin_install_sources", sources)
        except Exception:
            pass
        try:
            await asyncio.to_thread(self._migrate_legacy_data_dir)
            old_fav = Path(__file__).resolve().parent / "artist_favorites.json"
            new_fav = self._get_plugin_data_dir() / "artist_favorites.json"
            if old_fav.is_file() and not new_fav.is_file():
                new_fav.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_fav, new_fav)
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [initialize] 数据目录迁移异常: {exc!r}")

        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.request_timeout),
                trust_env=not self.bypass_system_proxy,
                connector=aiohttp.TCPConnector(force_close=True),
            )
            logger.info(
                f"{LOG_TAG} [initialize] aiohttp session 创建成功 (timeout={self.request_timeout}s, "
                f"trust_env={not self.bypass_system_proxy})"
            )
        except Exception as e:
            logger.error(f"{LOG_TAG} [initialize] aiohttp session 创建失败: {e!r}")

        try:
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/config",
                self._test_panel_get_config,
                ["GET"],
                "NAI 测试面板：获取当前配置",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/generate_openai",
                self._test_panel_generate_openai,
                ["POST"],
                "NAI 测试面板：OpenAI/NAI V4.5 原生格式生图",
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
                f"{PAGE_API_PREFIX}/grimoire_search",
                self._test_panel_grimoire_search,
                ["GET", "POST"],
                "NAI 测试面板：检索所长NovelAI个人法典（19108词条）",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/starry_context",
                self._test_panel_starry_context,
                ["GET"],
                "NAI生图插件：获取实时衣柜穿搭与作息场景",
            )
            self.context.register_web_api(
                f"{PAGE_API_PREFIX}/save_artists",
                self._test_panel_save_artists,
                ["POST"],
                "NAI生图插件：保存默认画师串与生成参数",
            )
            logger.info(
                f"{LOG_TAG} [initialize] 测试面板 Web API 已注册 | prefix={PAGE_API_PREFIX}"
            )
        except Exception as e:
            logger.warning(f"{LOG_TAG} [initialize] 注册测试面板 Web API 失败: {e!r}")

        try:
            store = self._usage_stats_store()
            if store is not None:
                async with self._usage_stats_lock:
                    await asyncio.to_thread(store.load)
        except Exception as e:
            logger.warning(f"{LOG_TAG} [initialize] 载入用量统计失败: {e!r}")

        try:
            self._sync_slots_to_schema(self._load_artist_slots().get("slots", []))
        except Exception:
            pass
        logger.info(f"{LOG_TAG} [initialize] 阶段完成")

    async def terminate(self):
        logger.info(f"{LOG_TAG} [terminate] 阶段开始")
        if self._session and not self._session.closed:
            try:
                await self._session.close()
                logger.info(f"{LOG_TAG} [terminate] aiohttp session 已关闭")
            except Exception as e:
                logger.warning(f"{LOG_TAG} [terminate] 关闭 session 异常: {e!r}")
        self._session = None
        logger.info(f"{LOG_TAG} [terminate] 阶段完成")

    def _resolve_artists(self, style: str) -> str:
        if style == "custom":
            return self.custom_artists if self.custom_artists is not None else ""
        return DEFAULT_ARTISTS.get(style, DEFAULT_ARTISTS["vertical"])



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

        未映射的原始尺寸直接透传（如 "1024x1024" 或 "832x1216"）。
        4K 键因上游边长限制降级为 2K 上限尺寸。
        """
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
            r"^\s*(\d{1,5})\s*[x×]\s*(\d{1,5})\s*$", str(size), re.IGNORECASE
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
            完整端点 URL。
        """
        base = self.openai_api_base_url.rstrip("/")
        if "image.novelai.net" in base.lower() or base.endswith("/ai/generate-image"):
            return base if base.endswith("/ai/generate-image") else f"{base}/ai/generate-image"
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
        model: str | None = None,
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
                custom_artists
                if custom_artists is not None
                else self._active_custom_artists(model)
            )
            artists = normalize_prompt(artists or "")
        else:
            artists = normalize_prompt(
                DEFAULT_ARTISTS.get(style, DEFAULT_ARTISTS["vertical"])
            )
        prompt_clean = (prompt or "").strip()
        if not artists:
            return normalize_prompt(prompt_clean)
        return normalize_prompt(f"{artists}, {prompt_clean}")

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
        model = (
            "director-tools"
            if is_director
            else self._active_model_name(model)
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
            if (model or "").strip().casefold() not in OPENAI_DIRECTOR_MODELS:
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

        payload["input"] = prompt
        _neg_str = str(_negative).strip() if (_negative and str(_negative).strip()) else "lowres, bad anatomy, bad hands, text, error, missing fingers"
        parameters.setdefault("params_version", 3)
        parameters.setdefault("width", target_w)
        parameters.setdefault("height", target_h)
        parameters.setdefault("n_samples", max(1, int(n)))
        parameters.setdefault("ucPreset", 0)
        parameters.setdefault("qualityToggle", True)
        parameters.setdefault("negative_prompt", _neg_str)

        if char_entries:
            parameters["use_coords"] = True
            parameters["characterPrompts"] = [
                {"prompt": c_prompt, "center": {"x": c_x, "y": c_y}, "uc": ""}
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
            parameters["v4_negative_prompt"] = {
                "caption": {
                    "base_caption": _neg_str,
                    "char_captions": [
                        {
                            "char_caption": "",
                            "centers": [{"x": c_x, "y": c_y}],
                        }
                        for _, c_x, c_y in char_entries
                    ],
                },
                "legacy_uc": False,
            }
        else:
            parameters.setdefault(
                "v4_prompt",
                {
                    "caption": {"base_caption": prompt, "char_captions": []},
                    "use_coords": False,
                    "use_order": True,
                },
            )
            parameters.setdefault(
                "v4_negative_prompt",
                {
                    "caption": {"base_caption": _neg_str, "char_captions": []},
                    "legacy_uc": False,
                },
            )
        payload["parameters"] = parameters

        endpoint = self._openai_endpoint("edits" if is_edit else "generations")
        if not endpoint.endswith("/ai/generate-image") and payload.get("action") == "generate":
            payload.pop("action", None)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

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
            f"mode={mode_desc} size={size} n={payload['n']} steps={_steps} scale={_scale} "
            f"seed={_seed if _seed >= 0 else 'random'} chars={len(char_entries)} "
            f"refs={len(ref_uris)} ref_bytes={sum(len(b) for b in ref_bytes_list)}"
        )

        # ==== 发送请求：可重试状态码按 2/4/8 秒指数退避（§14）；超时不重试 ====
        status = 0
        text = ""
        raw_resp_bytes = b""
        max_attempts = self.openai_max_retries + 1
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
                    endpoint, json=payload, headers=headers, timeout=timeout
                ) as resp:
                    raw_resp_bytes = await resp.read()
                    status = resp.status
                    text = (
                        raw_resp_bytes.decode("utf-8", errors="ignore")
                        if status >= 400 or raw_resp_bytes[:2] != b"PK"
                        else ""
                    )
            except asyncio.TimeoutError:
                # 超时不自动重试：生图请求超时后上游通常仍在继续生成并照常
                # 扣费，自动重试会导致一次需求被多次扣费。直接失败并提示
                # 用户稍后手动重试。
                status, text, raw_resp_bytes = 0, "", b""
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
            ):
                reason = "director_ref_rejected"
                if err_msg:
                    reason += f" | {err_msg[:200]}"
            logger.warning(
                f"{LOG_TAG} [openai_generate] HTTP {status} | body={text[:300]}"
            )
            return [], reason

        images: list[bytes] = []
        if raw_resp_bytes[:2] == b"PK":
            import io as _io
            import zipfile as _zipfile
            try:
                with _zipfile.ZipFile(_io.BytesIO(raw_resp_bytes)) as _zf:
                    for _zname in _zf.namelist():
                        if _zname.lower().endswith((".png", ".webp", ".jpg", ".jpeg")):
                            images.append(_zf.read(_zname))
            except Exception as _zexc:
                logger.warning(f"{LOG_TAG} [openai_generate] ZIP 解包失败: {_zexc}")
                return [], "invalid_response"
        elif raw_resp_bytes[:4] == b"\x89PNG" or raw_resp_bytes[:3] == b"\xff\xd8\xff":
            images.append(raw_resp_bytes)
        else:
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

            items = _data.get("data") if isinstance(_data, dict) else []
            if not isinstance(items, list) or not items:
                return [], "empty_response"

            for item in items:
                if not isinstance(item, dict):
                    continue
                b64 = str(item.get("b64_json") or "").strip()
                url = str(item.get("url") or "").strip()
                if b64:
                    # b64_json 不带 data:image/...;base64, 前缀（§12）
                    try:
                        images.append(base64.b64decode(b64))
                    except Exception as exc:
                        logger.warning(
                            f"{LOG_TAG} [openai_generate] b64_json 解码失败: {type(exc).__name__}"
                        )
                elif url:
                    try:
                        async with self._session.get(
                            url, timeout=aiohttp.ClientTimeout(total=60)
                        ) as dl_resp:
                            if dl_resp.status == 200:
                                images.append(await dl_resp.read())
                    except Exception as exc:
                        logger.warning(
                            f"{LOG_TAG} [openai_generate] 下载 url 失败: {type(exc).__name__}"
                        )
        if images:
            # OpenAI 兼容端点的实际计费由中转站决定，这里按像素画幅推断档位，
            # 仅作为用量统计的估算值（与直连档位表口径不同，分开记录）。
            _openai_size = str(payload.get("size") or size or "")
            await self._record_usage_stat(
                estimate_openai_cost(_openai_size, model) * len(images),
                count=len(images),
                transport="openai",
                model=str(model or ""),
                size=_openai_size,
            )
            _arch_params: dict[str, Any] = {
                "transport": "openai",
                "model": str(model or ""),
                "prompt": str(prompt or ""),
                "negative_prompt": str(negative or ""),
                "size": _openai_size,
            }
            if char_entries:
                _arch_params["characters"] = [
                    {"prompt": cp, "x": cx, "y": cy} for cp, cx, cy in char_entries
                ]
            for _img_b in images:
                await self._archive_generated_image(_img_b, _arch_params)
            return images, "ok"
        return [], "empty_response"


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
                },
            }
        )

    async def _test_panel_grimoire_search(self) -> Any:
        """Web API: 检索所长 NovelAI 个人法典（支持 GET query 参数或 POST JSON）。"""
        try:
            q = str(web_request.args.get("query", "") or "").strip()
            scope = str(web_request.args.get("scope", "all") or "all").strip()
            limit = int(web_request.args.get("limit", 12) or 12)
        except Exception:
            q, scope, limit = "", "all", 12

        if not q and web_request.method == "POST":
            try:
                body = await web_request.json(default={})
                if isinstance(body, dict):
                    q = str(body.get("query", "") or "").strip()
                    scope = str(body.get("scope", scope) or scope).strip()
                    limit = int(body.get("limit", limit) or limit)
            except Exception:
                pass

        items = await asyncio.to_thread(search_grimoire, q, scope, limit, self._active_model_ver())
        return json_response({"status": "ok", "data": {"items": items, "total_db": 19108}})

    async def _test_panel_starry_context(self) -> Any:
        """读取当前实时衣柜穿搭与作息场景，供画室一键载入。"""
        import datetime
        plugins_root = Path(__file__).resolve().parent.parent
        outfit_data: dict[str, Any] = {}
        timeline_data: dict[str, Any] = {}
        presets_data: list[dict[str, Any]] = []

        outfit_file = plugins_root / "astrbot_plugin_daily_outfit" / "data" / "outfit_state.json"
        if outfit_file.is_file():
            try:
                st = json.loads(outfit_file.read_text(encoding="utf-8"))
                of = st.get("outfit") or {}
                outfit_data = {
                    "name": of.get("name") or "今日穿搭",
                    "status_note": st.get("status_note") or "",
                    "location": st.get("location") or "",
                    "activity": st.get("activity") or "",
                    "nai_tags": of.get("nai_tags") or "",
                    "hair": of.get("hair") or "",
                    "top": of.get("top") or "",
                    "bottom": of.get("bottom") or "",
                    "legwear": of.get("legwear") or "",
                }
            except Exception:
                pass

        tl_file = plugins_root / "astrbot_plugin_starry_timeline" / "data" / "timeline_state.json"
        if tl_file.is_file():
            try:
                tl = json.loads(tl_file.read_text(encoding="utf-8"))
                slots = tl.get("daily_slots") or []
                now = datetime.datetime.now()
                cur_m = now.hour * 60 + now.minute
                active_slot = slots[0] if slots else {}
                for s in slots:
                    sm, em = int(s.get("start_min", 0)), int(s.get("end_min", 0))
                    if sm <= em:
                        if sm <= cur_m < em:
                            active_slot = s
                            break
                    else:
                        if cur_m >= sm or cur_m < em:
                            active_slot = s
                            break
                timeline_data = {
                    "slot_name": active_slot.get("name") or "日常陪伴",
                    "time_range": active_slot.get("time_range") or "",
                    "location": tl.get("override_location") or active_slot.get("location") or "",
                    "activity": tl.get("override_activity") or active_slot.get("activity") or "",
                    "selfie_tags": active_slot.get("selfie_tags") or "",
                }
            except Exception:
                pass

        slots_data = self._load_artist_slots()
        return json_response(
            {
                "status": "ok",
                "has_wardrobe": bool(outfit_data),
                "has_timeline": bool(timeline_data),
                "outfit": outfit_data,
                "timeline": timeline_data,
                "artist_slots": slots_data.get("slots", []),
                "ui_test_slot_id": slots_data.get("ui_test_slot_id", 1),
                "backend_active_slot_id": self.active_artist_slot,
            }
        )

    def _default_artist_slots(self) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "name": "槽位 1 · V4.5 主力画风",
                "model": self.openai_api_model if self._active_model_ver() == "NAI4.5" else "nai-diffusion-4-5-full",
                "artists": self.custom_artists or DEFAULT_ARTISTS.get("r18", ""),
            },
            {
                "id": 2,
                "name": "槽位 2 · V4.5 清新厚涂",
                "model": "nai-diffusion-4-5-full",
                "artists": DEFAULT_ARTISTS.get("vertical", ""),
            },
            {
                "id": 3,
                "name": "槽位 3 · V4.5 赛璐璐同人",
                "model": "nai-diffusion-4-5-full",
                "artists": DEFAULT_ARTISTS.get("comicDoujin", ""),
            },
            {
                "id": 4,
                "name": "槽位 4 · NAI V5 专属画风",
                "model": "nai-diffusion-5-full",
                "artists": self.custom_artists_v5 or "",
            },
            {
                "id": 5,
                "name": "槽位 5 · NAI V5 备用",
                "model": "nai-diffusion-5-curated",
                "artists": "",
            },
        ]

    def _sync_slots_to_schema(self, slots: list[dict[str, Any]]) -> None:
        """将所有预设槽位实时同步进 _conf_schema.json 的 active_artist_slot 下拉选项中，供后端插件设置独立选择。"""
        try:
            schema_path = Path(__file__).resolve().parent / "_conf_schema.json"
            if not schema_path.is_file():
                return
            schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
            opts = [str(s.get("id")) for s in slots]
            labels = [
                f"{s.get('name', '槽位 ' + str(s.get('id')))} [{s.get('model', 'nai-diffusion-4-5-full')}]"
                for s in slots
            ]
            if "active_artist_slot" in schema and isinstance(schema["active_artist_slot"], dict):
                if (
                    schema["active_artist_slot"].get("options") != opts
                    or schema["active_artist_slot"].get("labels") != labels
                ):
                    schema["active_artist_slot"]["options"] = opts
                    schema["active_artist_slot"]["labels"] = labels
                    if opts and schema["active_artist_slot"].get("default") not in opts:
                        schema["active_artist_slot"]["default"] = opts[0]
                    schema_path.write_text(
                        json.dumps(schema, ensure_ascii=False, indent=4) + "\n",
                        encoding="utf-8",
                    )
        except Exception as exc:
            logger.debug(f"{LOG_TAG} 同步槽位到 _conf_schema.json 跳过: {exc}")

    def _load_artist_slots(self) -> dict[str, Any]:
        slot_file = self._get_plugin_data_dir() / "artist_slots.json"
        if slot_file.is_file():
            try:
                data = json.loads(slot_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("slots"), list) and data["slots"]:
                    return data
            except Exception:
                pass
        default_slots = self._default_artist_slots()
        default_payload = {
            "ui_test_slot_id": 1,
            "slots": default_slots,
        }
        try:
            slot_file.parent.mkdir(parents=True, exist_ok=True)
            slot_file.write_text(
                json.dumps(default_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._sync_slots_to_schema(default_slots)
        except Exception:
            pass
        return default_payload

    async def _test_panel_save_artists(self) -> Any:
        """保存槽位库（支持新建/修改/删除槽位）并同步更新后端配置 schema 下拉选项；UI 测试槽位与后端正式启用槽位互不覆盖。"""
        try:
            body = await web_request.json(default={}) or {}
            slots_data = self._load_artist_slots()
            slots = slots_data.get("slots", [])
            ui_slot_id = int(body.get("ui_test_slot_id") or body.get("active_slot_id") or slots_data.get("ui_test_slot_id") or 1)

            if isinstance(body.get("slots"), list) and body["slots"]:
                cleaned_slots = []
                for idx, item in enumerate(body["slots"], 1):
                    if not isinstance(item, dict):
                        continue
                    sid = int(item.get("id") or idx)
                    sname = str(item.get("name") or f"槽位 {sid}").strip()
                    smodel = str(item.get("model") or "nai-diffusion-4-5-full").strip()
                    sartists = str(item.get("artists") or "").strip()
                    cleaned_slots.append({
                        "id": sid,
                        "name": sname,
                        "model": smodel,
                        "artists": sartists,
                    })
                if cleaned_slots:
                    slots = cleaned_slots

            save_payload = {"ui_test_slot_id": ui_slot_id, "slots": slots}
            slot_file = self._get_plugin_data_dir() / "artist_slots.json"
            slot_file.parent.mkdir(parents=True, exist_ok=True)
            slot_file.write_text(
                json.dumps(save_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # 实时把新建/改名/删除后的槽位列表写进 _conf_schema.json 的下拉框选项里！
            self._sync_slots_to_schema(slots)
            self._refresh_llm_tool_schema()

            return json_response({
                "status": "ok",
                "ui_test_slot_id": ui_slot_id,
                "backend_active_slot_id": self.active_artist_slot,
                "slots": slots,
            })
        except Exception as exc:
            return error_response(f"保存槽位失败: {exc}", status_code=500)

    async def _test_panel_get_config(self) -> Any:
        """Web API: 返回当前插件配置（脱敏 token）。"""
        return json_response(
            {
                "image_gen_key": "已配置" if self.openai_api_key else "未配置",
                "base_url": self.openai_api_base_url,
                "show_cost_estimate": False,
                "image_style": self.image_style,
                "image_size": self.image_size,
                "image_count": self.image_count,
                "bot_reply_mode": self.bot_reply_mode,
                "save_image_history": self.save_image_history,
                "save_generation_parameters": self.save_generation_parameters,
                "image_history_limit": self.image_history_limit,
                "custom_artists": self.custom_artists,
                "custom_artists_v5": self.custom_artists_v5,
                "model": self.model,
                "steps": self.steps,
                "scale": self.scale,
                "cfg": self.cfg_value,
                "sampler": self.sampler,
                "noise_schedule": self.noise_schedule,
                "negative": self.negative,
                "enable_template": self.enable_template,
                "character_preset": self.character_preset,
                "enable_translate": self.translate_mode,
                "translate_provider": self.translate_provider,
                "bypass_system_proxy": self.bypass_system_proxy,
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

        nai_prompt = normalize_prompt(body.get("nai_prompt") or "")
        nl_prompt = normalize_prompt(body.get("nl_prompt") or "")

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

        # 逐图参考强度（vibe / 精准参考）：与参考图按下标对应，0-1，非法值跳过
        reference_strengths: list[float] | None = None
        raw_strengths = body.get("reference_strengths")
        if isinstance(raw_strengths, (list, tuple)):
            parsed_strengths = []
            for item in raw_strengths:
                try:
                    value = float(item)
                except (TypeError, ValueError):
                    continue
                if 0 <= value <= 1:
                    parsed_strengths.append(round(value, 2))
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
            parsed_captions = [str(item or "").strip() for item in raw_captions]
            director_captions = [item for item in parsed_captions if item] or None

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


    def _resolve_translate_provider_id(self) -> str | None:
        """根据配置和上下文，选出转译用的 provider ID。

        - self.translate_provider 留空 → 取 AstrBot 当前默认 provider
        - 自填 ID → 用 get_provider_by_id 校验；不通过则回退默认；默认也取不到则返回 None
        """
        chosen = (self.translate_provider or "").strip()
        try:
            if chosen:
                prov = self.context.get_provider_by_id(chosen)
                if prov:
                    return chosen
                logger.warning(
                    f"{LOG_TAG} [translate] provider '{chosen}' 不存在，回退默认"
                )
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
            logger.warning(f"{LOG_TAG} [translate] 选择 provider 异常: {e!r}")
            return None

    async def _translate_prompt(self, prompt: str, *, force: bool = False) -> str:
        """如果开启转译，把自然语言 prompt 转成 SD/NAI 标签风格。

        ``force=True`` 供单次参数覆盖和自动模式调用，表示已经由上层完成模式
        判断，不再受兼容属性 ``self.enable_translate`` 限制。
        失败时原样返回 prompt，不影响主流程。
        """
        import re as _re

        if not force and not self.enable_translate:
            return prompt
        if not prompt or not prompt.strip():
            return prompt

        provider_id = self._resolve_translate_provider_id()
        if not provider_id:
            logger.warning(
                f"{LOG_TAG} [translate] 没有可用 provider，跳过转译，原样透传"
            )
            return prompt

        grimoire_hint = build_grimoire_translation_hint(prompt, model_ver=self._active_model_ver())
        llm_input_prompt = f"{prompt}{grimoire_hint}" if grimoire_hint else prompt
        logger.info(
            f"{LOG_TAG} [translate] 开始 | provider='{provider_id}' "
            f"in_len={len(prompt)} grimoire_hint={'YES' if grimoire_hint else 'NO'} preview='{prompt[:60]}...'"
        )

        response = None
        # === 优先使用 v4.5.7+ 推荐的 context.llm_generate ===
        try:
            llm_generate = getattr(self.context, "llm_generate", None)
            if llm_generate is not None:
                response = await llm_generate(
                    chat_provider_id=provider_id,
                    prompt=llm_input_prompt,
                    system_prompt=IMAGE_PROMPT_SPEC or TRANSLATE_SYSTEM_PROMPT,
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
                        prompt=prompt,
                        system_prompt=IMAGE_PROMPT_SPEC or TRANSLATE_SYSTEM_PROMPT,
                        temperature=0.4,
                    )
                except TypeError:
                    # 极老 provider 不接受 system_prompt
                    response = await prov.text_chat(prompt=prompt)
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
        raw_prompt = normalize_prompt(expand_grimoire_macros(prompt, model_ver=self._active_model_ver()))
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

        outfit_source, use_default_outfit = "none", False
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


    @filter.command("image")
    async def image(self, event: AstrMessageEvent):
        """处理 `/image` 指令，支持命名参数覆盖与多角色坐标控制。"""
        raw_text = event.message_str or ""
        text = strip_image_command_prefix(raw_text)
        sender = event.get_sender_id() if hasattr(event, "get_sender_id") else "?"
        logger.info(
            f"{LOG_TAG} [cmd:image] 收到指令 | sender={sender} | text='{text[:100]}'"
        )

        if not text.strip():
            yield event.plain_result(
                "用法: /image <提示词> [--参数=值]\n"
                "基础: --n=1-6 --style=... --size=...\n"
                "生成: --steps=1-50 --scale=0-10 --sampler=... --noise=karras|native|exponential|polyexponential\n"
                "覆盖: --translate=关闭|开启|自动 --template=关闭|开启 "
                '--model=... --artist="..." --negative="..."\n'
                '多角色: --char="提示词|x|y"（可重复，坐标0-1）\n'
                "用量统计: /usage [天数|today|all]"
            )
            return

        try:
            args = parse_image_command(text, default_style=self.image_style)
        except ImageCommandArgumentError as e:
            yield event.plain_result(f"参数错误：{e}")
            return

        prompt = args.prompt
        if not prompt:
            yield event.plain_result("请提供提示词。")
            return

        if not self.openai_api_base_url:
            yield event.plain_result(
                "未配置生图接口（openai_api_base_url），请先在插件配置中填写。"
            )
            return

        n = args.n if args.n is not None else self.image_count
        style = args.style or self.image_style
        size_cn = args.size or self.image_size
        size = self._openai_size(size_cn) if size_cn in IMAGE_SIZES else size_cn

        effective_steps = args.steps if args.steps is not None else self.steps
        effective_scale = args.scale if args.scale is not None else self.scale
        effective_sampler = args.sampler or self.sampler
        effective_noise = args.noise_schedule or self.noise_schedule
        effective_translate = args.translate_mode or self.translate_mode
        effective_template = (
            args.enable_template if args.enable_template is not None else self.enable_template
        )
        effective_model = args.model or self.openai_api_model

        prepared_prompt, _, _, _ = await self._prepare_translated_prompt(
            prompt, translate_mode=effective_translate, apply_outfit=False
        )
        prepared_prompt = self._build_full_prompt(
            prepared_prompt, enable_template=effective_template
        )
        openai_prompt = self._openai_prompt(prepared_prompt, style, args.artist)

        brief_parameters = (
            f"风格: {IMAGE_STYLES.get(style, style)}，尺寸: {size}，数量: {n} 张\n"
            f"Steps: {effective_steps}，Scale: {effective_scale:g}，模型: {effective_model}\n"
            f"采样器: {effective_sampler}，噪声: {effective_noise}，转译: {effective_translate}"
        )
        if self.bot_reply_mode == "完整":
            yield event.plain_result(f"提示词: {prepared_prompt}\n{brief_parameters}")
        elif self.bot_reply_mode == "简洁":
            yield event.plain_result(f"正在画图...\n{brief_parameters}")

        try:
            images, reason = await self._openai_generate(
                openai_prompt,
                size,
                n=n,
                steps=effective_steps,
                scale=args.scale,
                sampler=args.sampler,
                noise_schedule=args.noise_schedule,
                negative=args.negative if args.negative is not None else self.negative,
                model=effective_model,
                characters=args.characters,
            )
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [cmd:image] _openai_generate 异常: {exc!r}")
            images, reason = [], "exception"

        if images:
            for idx, img_bytes in enumerate(images, start=1):
                if self.bot_reply_mode == "仅图片":
                    yield event.chain_result([Img.fromBytes(img_bytes)])
                else:
                    yield event.chain_result(
                        [Plain(f"[{idx}/{n}]"), Img.fromBytes(img_bytes)]
                    )
        else:
            yield event.plain_result(f"生成失败：{_format_generate_error(reason)}")

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
        provider_id = self.interrogate_provider or self.translate_provider
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
                    prompt=llm_input_prompt,
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
            wardrobe_installed = (
                Path(__file__).resolve().parent.parent / "astrbot_plugin_daily_outfit" / "data" / "outfit_state.json"
            ).is_file()
            if not wardrobe_installed:
                props.pop("use_wardrobe", None)
                if "char1_prompt" in props:
                    props["char1_prompt"]["description"] = (
                        "【角色块1：主角色专属块】填写主角色的外貌特征、服装穿搭、动作姿势与神态表情"
                        "（若已在插件设置中填写了 character_preset 角色固定外貌底模，底层会自动前置拼接）。"
                    )
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
            # 同步刷新法典检索工具 schema，明确告知 AI 当前锁定的模型词库版本，防止 AI 混用 4.5 与 5 的 Tag
            grim_tool = manager.get_func("NAI_Search_Grimoire") if manager else None
            if grim_tool is not None:
                m_ver = self._active_model_ver()
                grim_params = dict(getattr(grim_tool, "parameters", None) or {})
                grim_props = dict(grim_params.get("properties") or {})
                if "scope" in grim_props and isinstance(grim_props["scope"], dict):
                    grim_props["scope"] = dict(grim_props["scope"])
                    grim_props["scope"]["description"] = (
                        f"检索范围（当前后台模型已强制锁定为 {m_ver} 专属词库，自动屏蔽其他模型版本 Tag）："
                        "可选 all(当前模型全部卷) / sfw(当前模型常规卷) / nsfw(当前模型色色卷)，默认 all。"
                    )
                    grim_params["properties"] = grim_props
                    grim_tool.parameters = grim_params
            logger.debug(
                f"{LOG_TAG} [tool] NAI_Generate_Image schema 已按配置刷新 | "
                f"style字段={'显示' if self.enable_tool_style else '隐藏'} "
                f"尺寸格式={'分辨率' if self.call_mode == 'openai' else '分档'}"
            )
        except Exception as exc:
            logger.warning(f"{LOG_TAG} [tool] 工具参数 schema 刷新失败: {exc!r}")


    def _deduplicate_tags(self, tag_str: str) -> str:
        """优先调用衣柜插件模块化去重器净化合并后的 Tag 串；若不可用则按归一化键去重。"""
        raw = normalize_prompt(tag_str)
        if not raw:
            return ""
        try:
            wardrobe_dir = Path(__file__).resolve().parent.parent / "astrbot_plugin_daily_outfit"
            import sys as _sys
            if str(wardrobe_dir) not in _sys.path:
                _sys.path.insert(0, str(wardrobe_dir))
            import wardrobe as _wd
            return _wd.deduplicate_tag_string(raw)
        except Exception:
            seen: set[str] = set()
            out: list[str] = []
            for p in raw.split(","):
                s = p.strip()
                if not s:
                    continue
                k = re.sub(r"^-?\d+(?:\.\d+)?::|::$", "", s).strip().lower()
                if k and k not in seen:
                    seen.add(k)
                    out.append(s)
            return ", ".join(out)

    def _load_starry_wardrobe_base_tags(
        self, include_hidden: bool = True, with_clothes: bool = True
    ) -> str:
        """自动读取插件配置的「角色固定外貌底模 (character_preset)」及衣柜插件的「9部位实时穿搭词条包」，作为角色块1基底。"""
        base_tags = (self._active_character_preset() or "").strip(", ") if self.enable_template else ""
        if not with_clothes:
            return base_tags
        try:
            wardrobe_dir = Path(__file__).resolve().parent.parent / "astrbot_plugin_daily_outfit"
            outfit_file = wardrobe_dir / "data" / "outfit_state.json"
            if outfit_file.is_file():
                st = json.loads(outfit_file.read_text(encoding="utf-8"))
                of = st.get("outfit") or {}
                import sys as _sys
                if str(wardrobe_dir) not in _sys.path:
                    _sys.path.insert(0, str(wardrobe_dir))
                try:
                    import importlib as _il
                    import wardrobe as _wd
                    _wd = _il.reload(_wd)
                    modular_tags = _wd.build_modular_outfit_tags(
                        of, include_hidden_underwear=include_hidden
                    )
                    if modular_tags:
                        return _wd.deduplicate_tag_string(f"{base_tags}, {modular_tags}" if base_tags else modular_tags)
                except Exception:
                    pass
                outfit_tags = str(
                    of.get("outfit_nai_tags") or of.get("nai_tags") or ""
                ).strip(", ")
                if outfit_tags:
                    return self._deduplicate_tags(f"{base_tags}, {outfit_tags}" if base_tags else outfit_tags)
        except Exception as exc:
            logger.debug(f"{LOG_TAG} 读取衣柜角色块基底跳过: {exc}")
        return base_tags

    @filter.llm_tool()
    async def NAI_Generate_Image(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        char1_prompt: str = "",
        char2_prompt: str = "",
        use_wardrobe: bool = True,
        style: str = "",
        size_cn: str = "",
    ) -> AsyncGenerator[str, None]:
        """用NovelAI 4.5（原生角色块 characterPrompts 架构）生成1张图片并直接发送给当前用户。

        每个用户消息最多调用一次本工具。不要在同一响应中重复调用，也不要同时
        调用send_message_to_user；生成成功后本工具会直接发送图片。
        画风与尺寸留空时，固定使用插件设置里选择的默认画风与默认尺寸。

        Args:
            prompt(string): 【全局主提示词 base_caption】只填画面人数（如 1girl, solo 或 1girl, 1boy, heterosexual）、背景环境、光影氛围、镜头视角（如 pov, from below, selfie, depth of field）及整体构图。不要把角色外貌和穿搭塞进主块（主块底层会自动前置自定义画师串）。支持 权重::标签:: 与【法典:词条名】宏。
            char1_prompt(string): 【角色块1：主角色专属块】填写主角色当前的动作姿势、神态表情、互动标签（如 source#hug, looking at viewer, blushing, lifting skirt）及局部衣物状态微调。当 use_wardrobe=True（默认）时，底层会自动将配置的角色固定外貌底模与衣柜插件的实时穿搭 Tags 拼入本角色块，无需重复手抄外貌与日常穿搭；若 use_wardrobe=False（如临时换穿特定服装/Cosplay/全裸），则底层仅保留角色基础外貌，请在本参数中写出本次特定服装或裸露状态。
            char2_prompt(string): 【角色块2：第2角色/互动对象块（可选）】单人自拍或独处画面留空即可；双人亲密互动时填入第2角色的特征与互动动作（如 boy, faceless male, pov hands, target#hug, grabbing ass），与角色块1完全物理隔离防止串色。
            use_wardrobe(boolean): 是否在角色块1中自动挂载衣柜插件今日穿搭Tags，默认为 True；仅在临时换穿非衣柜特定服装或脱光衣物时设为 False。
            style(string): 画风。留空使用插件设置的默认画风（自定义画师串）。
            size_cn(string): 图片尺寸，留空使用插件设置的默认尺寸（832x1216）。
        """
        _m_ver = self._active_model_ver()
        prompt = normalize_prompt(expand_grimoire_macros(prompt, model_ver=_m_ver))
        char1_prompt = normalize_prompt(expand_grimoire_macros(char1_prompt, model_ver=_m_ver))
        char2_prompt = normalize_prompt(expand_grimoire_macros(char2_prompt, model_ver=_m_ver))
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
        style = (
            raw_style if (self.enable_tool_style and raw_style) else self.image_style
        )
        if not prompt:
            yield "生成失败，提示词不应为空"
            return

        if not self.openai_api_base_url:
            yield "生成失败，未配置生图接口，请告知用户先在插件配置中填写 openai_api_base_url。"
            return

        if style not in IMAGE_STYLES and style != "custom":
            yield f"未知风格: {style}\n可选: {', '.join(IMAGE_STYLES.keys())}"
            return

        raw_size = str(size_cn or "").strip() or self.image_size
        if raw_size in IMAGE_SIZES:
            raw_size = self._openai_size(raw_size)
        if not _PIXEL_SIZE_RE.match(raw_size):
            yield (
                f"未知尺寸: {size_cn}\n"
                f"可选（像素分辨率 宽x高）: {', '.join(OPENAI_TOOL_SIZES)}"
            )
            return
        size = self._normalize_openai_size(raw_size)

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

        tool_characters: list[tuple[str, float, float]] = []
        starry_base = self._load_starry_wardrobe_base_tags(
            include_hidden=True, with_clothes=bool(use_wardrobe)
        )
        full_char1 = (
            self._deduplicate_tags(f"{starry_base}, {char1_prompt}")
            if char1_prompt
            else self._deduplicate_tags(starry_base)
        )
        if char2_prompt:
            tool_characters.append((full_char1, 0.45, 0.5))
            tool_characters.append((self._deduplicate_tags(char2_prompt), 0.55, 0.5))
        else:
            tool_characters.append((full_char1, 0.5, 0.5))

        try:
            openai_prompt = self._openai_prompt(prompt, style)
            images, reason = await self._openai_generate(
                openai_prompt,
                size,
                n=1,
                characters=tool_characters,
            )
            img_bytes = images[0] if images else None
        finally:
            if hasattr(event, "set_extra"):
                event.set_extra("_nai_image_generation_state", "finished")
        if img_bytes:
            logger.info(
                f"{LOG_TAG} [tool:NAI_Generate_Image] 图片发送 | bytes={len(img_bytes)}"
            )
            await event.send(MessageChain(chain=[Img.fromBytes(img_bytes)]))
            logger.info(f"{LOG_TAG} [tool:NAI_Generate_Image] 完成 | 成功")
            yield "图片已生成并发送给用户，请顺着当前情境自然补充简短后续（严禁重复调用前说过的话，且句尾严禁出现包含“还是”的A或B二选一问句！）。"
            return

        logger.warning(f"{LOG_TAG} [tool:NAI_Generate_Image] 失败 | reason={reason}")
        yield f"生成失败：{_format_generate_error(reason)}"
        return

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
            if raw.startswith(prefix) and len(raw) > len(prefix) and raw[len(prefix)].isspace():
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
                    yield event.plain_result("点数用量统计暂不可用（插件数据目录未就绪）。")
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
        """检查当前 NAI 生图服务的配置与会话状态。"""
        lines = [
            f"🎨 NAI生图插件 (v2.2.0) 状态：",
            f"• 接口地址: {self.openai_api_base_url or '❌ 未配置'}",
            f"• API Key: {'✅ 已配置' if self.openai_api_key else '❌ 未配置'}",
            f"• 当前模型: {self.openai_api_model}",
            f"• 默认尺寸: {self.image_size} ({self._openai_size(self.image_size)})",
            f"• 会话状态: {'✅ 就绪' if (self._session and not self._session.closed) else '❌ 未初始化'}",
            f"• 绕过系统代理: {'✅ 开启' if self.bypass_system_proxy else '❌ 关闭'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="NAI_Search_Grimoire")
    async def NAI_Search_Grimoire(
        self,
        event: AstrMessageEvent,
        query: str = "",
        scope: str = "all",
    ) -> str:
        """在内置 NovelAI 法典词库（8卷共19,108条实测Tag词条，含N4.5常规、N4.5色色卷一~卷四、N5常规与色色）中检索精准的画风、服饰、姿势、视角、体位或特殊玩法Tag。

        当需要生成特定画风、精致穿搭、复杂互动姿势或NSFW画面时，可先调用本工具检索现成的实测Tag组合，或在NAI_Generate_Image的prompt中使用【法典:词条名】宏。

        Args:
            query(string): 检索关键词（如：死库水、传教士、碧蓝档案、自拍、丝袜、旗袍、动画画风），支持空格分隔多个词。
            scope(string): 检索范围，可选 all(全部8卷) / sfw(仅常规卷) / nsfw(仅色色卷) / nai4.5 / nai5，默认 all。
        """
        hits = await asyncio.to_thread(search_grimoire, query, scope, 8, self._active_model_ver())
        if not hits:
            return f"未在法典词库中找到与 '{query}' (scope={scope}) 匹配的词条，请尝试缩短或更换中文关键词。"
        lines = [f"📖 NovelAI 法典检索结果（共命中 {len(hits)} 条，可直接复制 clean_tags 用于 NAI_Generate_Image）："]
        for i, h in enumerate(hits, 1):
            lines.append(
                f"{i}. 【{h['title']}】({h['volume']} · {h['category']})\n"
                f"   Tags: {h['clean_tags']}"
            )
        return "\n".join(lines)

    @filter.command("法典", alias={"grimoire", "查法典"})
    async def cmd_grimoire(self, event: AstrMessageEvent):
        """检索内置 NovelAI 法典词条。用法：/法典 关键词"""
        raw = str(event.message_str or "").strip()
        for prefix in ("/法典", "法典", "/grimoire", "grimoire", "/查法典", "查法典"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        if not raw:
            yield event.plain_result("📖 用法：/法典 <关键词>（已收录 N4.5常规/色色四卷 & N5 全套共 19,108 条词条，支持在生图中使用 【法典:词条名】 直接展开）")
            return
        hits = await asyncio.to_thread(search_grimoire, raw, "all", 6, self._active_model_ver())
        if not hits:
            yield event.plain_result(f"未在法典中找到包含「{raw}」的词条。")
            return
        lines = [f"📖 NovelAI 法典检索：「{raw}」（前 {len(hits)} 条）"]
        for i, h in enumerate(hits, 1):
            lines.append(f"\n{i}. 【{h['title']}】({h['volume']} · {h['category']})\n{h['clean_tags'][:260]}")
        yield event.plain_result("\n".join(lines))

