import math
import re
from dataclasses import dataclass

if __package__:
    from .prompt_processing import normalize_prompt
else:
    from prompt_processing import normalize_prompt


SAMPLERS = {
    "k_dpmpp_2m_sde",
    "k_dpmpp_2m",
    "k_dpmpp_sde",
    "k_dpmpp_2s_ancestral",
    "k_euler_ancestral",
    "k_euler",
}
NOISE_SCHEDULES = {"karras", "native", "exponential"}

_ARGUMENT_NAMES = {
    "n",
    "style",
    "size",
    "steps",
    "scale",
    "cfg",
    "sampler",
    "noise",
    "noise_schedule",
    "translate",
    "negative",
    "model",
    "artist",
    "template",
}
_ARGUMENT_NAME_ALIASES = {"noise_schedule": "noise"}
_STYLE_ALIASES = {
    "vertical": "vertical",
    "comicdoujin": "comicDoujin",
    "comic_doujin": "comicDoujin",
    "r18": "r18",
    "lolita25d": "lolita25d",
    "anime": "anime",
    "galgame": "galgame",
    "custom": "custom",
    "自定义": "custom",
    "韩漫小清新风": "vertical",
    "漫画同人风": "comicDoujin",
    "2.5d唯美风": "r18",
    "2.5d唯美风（萝）": "lolita25d",
    "2.5d唯美风(萝)": "lolita25d",
    "本子里番风": "anime",
    "galgame风": "galgame",
}
_SIZE_ALIASES = {
    "竖图": "竖图",
    "portrait": "竖图",
    "vertical": "竖图",
    "横图": "横图",
    "landscape": "横图",
    "horizontal": "横图",
    "方图": "方图",
    "square": "方图",
    "2k竖图": "2K竖图",
    "2k_portrait": "2K竖图",
    "2k-portrait": "2K竖图",
    "2k_vertical": "2K竖图",
    "2k-vertical": "2K竖图",
    "2k横图": "2K横图",
    "2k_landscape": "2K横图",
    "2k-landscape": "2K横图",
    "2k_horizontal": "2K横图",
    "2k-horizontal": "2K横图",
    "2k方图": "2K方图",
    "2k_square": "2K方图",
    "2k-square": "2K方图",
    "4k竖图": "4K竖图",
    "4k_portrait": "4K竖图",
    "4k-portrait": "4K竖图",
    "4k_vertical": "4K竖图",
    "4k-vertical": "4K竖图",
    "4k横图": "4K横图",
    "4k_landscape": "4K横图",
    "4k-landscape": "4K横图",
    "4k_horizontal": "4K横图",
    "4k-horizontal": "4K横图",
    "4k方图": "4K方图",
    "4k_square": "4K方图",
    "4k-square": "4K方图",
}
_TRANSLATE_ALIASES = {
    "关闭": "关闭",
    "off": "关闭",
    "false": "关闭",
    "0": "关闭",
    "disabled": "关闭",
    "开启": "开启",
    "on": "开启",
    "true": "开启",
    "1": "开启",
    "enabled": "开启",
    "自动": "自动",
    "auto": "自动",
}
_TEMPLATE_ALIASES = {
    "关闭": False,
    "off": False,
    "false": False,
    "0": False,
    "disabled": False,
    "开启": True,
    "on": True,
    "true": True,
    "1": True,
    "enabled": True,
}
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def strip_image_command_prefix(text: object) -> str:
    """移除事件文本开头的 ``image`` 指令名，保留后续完整参数文本。

    AstrBot 的命令过滤器会使用去掉指令名后的局部文本匹配处理函数参数，
    但不会同步修改 ``event.message_str``。这里只匹配完整的首个词，避免误删
    提示词正文中的 ``image`` 或以它开头的其他单词。
    """
    stripped = str(text or "").strip()
    for prefix in ("image", "/image"):
        if stripped == prefix:
            return ""
        if stripped.startswith(prefix) and stripped[len(prefix)].isspace():
            return stripped[len(prefix) :].lstrip()
    return stripped


class ImageCommandArgumentError(ValueError):
    """表示 `/image` 指令中的参数格式或取值不合法。"""

    pass


@dataclass(frozen=True)
class ImageCommandArguments:
    """一次 `/image` 调用解析后的提示词与可选覆盖参数。"""

    prompt: str
    n: int | None = None
    style: str | None = None
    size: str | None = None
    steps: int | None = None
    scale: float | None = None
    cfg: float | None = None
    sampler: str | None = None
    noise_schedule: str | None = None
    translate_mode: str | None = None
    negative: str | None = None
    model: str | None = None
    artist: str | None = None
    enable_template: bool | None = None

    def generation_overrides(self) -> dict[str, object]:
        """转换为 `_generate_one` 可直接接收的单次覆盖参数。

        未指定的参数必须被排除，才能继续使用插件全局配置；显式传入的
        ``False``、空反向提示词和数值 ``0`` 则需要完整保留。
        """
        values = {
            "steps": self.steps,
            "scale": self.scale,
            "cfg": self.cfg,
            "sampler": self.sampler,
            "noise_schedule": self.noise_schedule,
            "negative": self.negative,
            "model": self.model,
            "custom_artists": self.artist,
            "enable_template": self.enable_template,
            "enable_translate": self.translate_mode,
        }
        return {key: value for key, value in values.items() if value is not None}


def normalize_image_style(value: object) -> str | None:
    """将中英文风格名称统一为插件内部使用的风格键。"""

    normalized = str(value or "").strip().casefold()
    return _STYLE_ALIASES.get(normalized)


def normalize_image_size(value: object) -> str | None:
    """将中英文尺寸名称统一为 ``IMAGE_SIZES`` 使用的中文键。"""

    normalized = str(value or "").strip().casefold()
    return _SIZE_ALIASES.get(normalized)


def _read_quoted_value(text: str, start: int, name: str) -> tuple[str, int]:
    """读取带引号的参数值，并返回解码后的值和引号后的下标。

    只对当前引号和反斜杠执行转义，避免无意改写提示词中的其他反斜杠。
    """
    quote_char = text[start]
    value: list[str] = []
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == quote_char:
            end = index + 1
            if end < len(text) and not text[end].isspace():
                raise ImageCommandArgumentError(
                    f"参数 --{name} 的引号后需要空格或作为指令结尾。"
                )
            return "".join(value), end
        if char == "\\" and index + 1 < len(text):
            next_char = text[index + 1]
            if next_char in {quote_char, "\\"}:
                value.append(next_char)
                index += 2
                continue
        value.append(char)
        index += 1
    raise ImageCommandArgumentError(f"参数 --{name} 的引号未闭合。")


def _extract_raw_arguments(text: str) -> tuple[str, dict[str, str]]:
    """从指令文本中抽取 ``--名称=值``，其余内容作为提示词返回。

    扫描器只识别位于空白边界、且不在提示词引号内的参数。参数先在这里
    完成词法解析，具体类型、范围和枚举值由 :func:`parse_image_command`
    统一校验。
    """
    values: dict[str, str] = {}
    prompt_parts: list[str] = []
    copy_from = 0
    index = 0
    prompt_quote = ""

    while index < len(text):
        char = text[index]
        # 提示词本身可以包含引号；引号内形似 --cfg=1 的内容仍属于提示词。
        if prompt_quote:
            if char == "\\" and index + 1 < len(text):
                index += 2
                continue
            if char == prompt_quote:
                prompt_quote = ""
            index += 1
            continue
        # 英文所有格中的单引号不是字符串边界，例如 girl's --steps=28。
        if (
            char == "'"
            and index > 0
            and index + 1 < len(text)
            and text[index - 1].isalnum()
            and text[index + 1].isalnum()
        ):
            index += 1
            continue
        if char in {'"', "'"}:
            prompt_quote = char
            index += 1
            continue
        if not (
            text.startswith("--", index)
            and (index == 0 or text[index - 1].isspace())
        ):
            index += 1
            continue

        # 到达候选参数后，严格检查名称、等号、重复项和引号闭合状态。
        name_start = index + 2
        name_end = name_start
        while name_end < len(text) and (
            text[name_end].isalnum() or text[name_end] == "_"
        ):
            name_end += 1
        raw_name = text[name_start:name_end]
        name = raw_name.casefold()
        if not raw_name:
            raise ImageCommandArgumentError("检测到无效的 -- 参数。")
        if name not in _ARGUMENT_NAMES:
            raise ImageCommandArgumentError(f"未知参数: --{raw_name}。")
        if name_end >= len(text) or text[name_end] != "=":
            raise ImageCommandArgumentError(
                f"参数 --{raw_name} 必须使用 --{name}=值 的格式。"
            )

        canonical_name = _ARGUMENT_NAME_ALIASES.get(name, name)
        if canonical_name in values:
            raise ImageCommandArgumentError(f"参数 --{canonical_name} 不能重复指定。")

        value_start = name_end + 1
        if value_start < len(text) and text[value_start] in {'"', "'"}:
            value, argument_end = _read_quoted_value(
                text, value_start, canonical_name
            )
        else:
            argument_end = value_start
            while argument_end < len(text) and not text[argument_end].isspace():
                argument_end += 1
            value = text[value_start:argument_end]

        # 用空格替换参数所在片段，防止参数前后的提示词意外粘连。
        prompt_parts.append(text[copy_from:index])
        prompt_parts.append(" ")
        copy_from = argument_end
        values[canonical_name] = value
        index = argument_end

    prompt_parts.append(text[copy_from:])
    return normalize_prompt("".join(prompt_parts)), values


def _parse_int(
    values: dict[str, str], name: str, minimum: int, maximum: int
) -> int | None:
    """解析一个可选整数参数，并执行闭区间校验。"""

    if name not in values:
        return None
    raw = values[name]
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ImageCommandArgumentError(f"参数 --{name} 必须是整数。") from exc
    if not minimum <= value <= maximum:
        raise ImageCommandArgumentError(
            f"参数 --{name} 取值范围为 {minimum}-{maximum}。"
        )
    return value


def _parse_float(
    values: dict[str, str], name: str, minimum: float, maximum: float
) -> float | None:
    """解析一个可选浮点参数，并拒绝 NaN、无穷大及越界值。"""

    if name not in values:
        return None
    raw = values[name]
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ImageCommandArgumentError(f"参数 --{name} 必须是数字。") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ImageCommandArgumentError(
            f"参数 --{name} 取值范围为 {minimum:g}-{maximum:g}。"
        )
    return value


def parse_image_command(
    text: str,
    *,
    default_style: str,
) -> ImageCommandArguments:
    """解析 `/image` 文本并生成规范化的单次生图参数。

    先从任意位置抽取命名参数，再按各字段的类型、取值范围和中英文别名
    做语义校验。``default_style`` 仅用于判断未显式指定风格时，画师串是否
    允许覆盖自定义风格。
    """
    prompt, values = _extract_raw_arguments(text)

    style = None
    if "style" in values:
        style = normalize_image_style(values["style"])
        if style is None:
            raise ImageCommandArgumentError(
                "参数 --style 无效，可使用英文风格名或中文显示名。"
            )

    size = None
    if "size" in values:
        size = normalize_image_size(values["size"])
        if size is None:
            raise ImageCommandArgumentError(
                "参数 --size 无效，例如竖图/portrait、横图/landscape、方图/square。"
            )

    sampler = None
    if "sampler" in values:
        sampler = values["sampler"].strip().casefold()
        if sampler not in SAMPLERS:
            raise ImageCommandArgumentError(
                f"参数 --sampler 无效，可选: {', '.join(sorted(SAMPLERS))}。"
            )

    noise_schedule = None
    if "noise" in values:
        noise_schedule = values["noise"].strip().casefold()
        if noise_schedule not in NOISE_SCHEDULES:
            raise ImageCommandArgumentError(
                "参数 --noise 无效，可选: karras, native, exponential。"
            )

    translate_mode = None
    if "translate" in values:
        translate_mode = _TRANSLATE_ALIASES.get(
            values["translate"].strip().casefold()
        )
        if translate_mode is None:
            raise ImageCommandArgumentError(
                "参数 --translate 无效，可选: 关闭/off、开启/on、自动/auto。"
            )

    enable_template = None
    if "template" in values:
        enable_template = _TEMPLATE_ALIASES.get(
            values["template"].strip().casefold()
        )
        if enable_template is None:
            raise ImageCommandArgumentError(
                "参数 --template 无效，可选: 关闭/off、开启/on。"
            )

    model = None
    if "model" in values:
        model = values["model"].strip()
        if not _MODEL_RE.fullmatch(model):
            raise ImageCommandArgumentError(
                "参数 --model 无效，仅允许英文字母、数字、点、下划线和连字符。"
            )

    artist = None
    if "artist" in values:
        artist = normalize_prompt(values["artist"])
        if not artist:
            raise ImageCommandArgumentError("参数 --artist 不能为空。")
        effective_style = style or normalize_image_style(default_style)
        if effective_style != "custom":
            raise ImageCommandArgumentError(
                "参数 --artist 仅能在 --style=custom（或默认风格为自定义）时使用。"
            )

    negative = None
    if "negative" in values:
        negative = normalize_prompt(values["negative"])

    return ImageCommandArguments(
        prompt=prompt,
        n=_parse_int(values, "n", 1, 6),
        style=style,
        size=size,
        steps=_parse_int(values, "steps", 1, 100),
        scale=_parse_float(values, "scale", 0, 20),
        cfg=_parse_float(values, "cfg", 0, 30),
        sampler=sampler,
        noise_schedule=noise_schedule,
        translate_mode=translate_mode,
        negative=negative,
        model=model,
        artist=artist,
        enable_template=enable_template,
    )
