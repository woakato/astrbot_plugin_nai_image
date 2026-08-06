import re
from dataclasses import dataclass
from typing import Literal

TRANSLATE_MODE_OFF = "关闭"
TRANSLATE_MODE_ON = "开启"
TRANSLATE_MODE_AUTO = "自动"
TRANSLATE_MODES = {
    TRANSLATE_MODE_OFF,
    TRANSLATE_MODE_ON,
    TRANSLATE_MODE_AUTO,
}

_PROMPT_LINE_BREAK_RE = re.compile(
    r"[^\S\r\n]*(?:[,，;；][^\S\r\n]*)?"
    r"(?:(?:\r\n?|\n)[^\S\r\n]*)+"
    r"(?:[,，;；][^\S\r\n]*)?"
)
_PROMPT_HORIZONTAL_WHITESPACE_RE = re.compile(r"[^\S\r\n]+")

PromptSegmentKind = Literal["nai", "natural"]


@dataclass(frozen=True)
class PromptSegment:
    """一个已判定为 NAI 标签或自然语言的提示词片段。"""

    text: str
    kind: PromptSegmentKind


@dataclass(frozen=True)
class MixedPrompt:
    """混合提示词的拆分结果，并保留各片段的原始排列顺序。"""

    original: str
    segments: tuple[PromptSegment, ...]
    explicit_natural: bool = False

    @property
    def has_natural(self) -> bool:
        return any(segment.kind == "natural" for segment in self.segments)

    @property
    def natural_text(self) -> str:
        return ", ".join(
            segment.text for segment in self.segments if segment.kind == "natural"
        )

    @property
    def nai_text(self) -> str:
        return ", ".join(
            segment.text for segment in self.segments if segment.kind == "nai"
        )


def normalize_translate_mode(value: object) -> str:
    """统一转译模式，并兼容旧版本配置中的布尔值。"""
    if isinstance(value, bool):
        return TRANSLATE_MODE_ON if value else TRANSLATE_MODE_OFF
    if value is None:
        return TRANSLATE_MODE_OFF

    normalized = str(value).strip().casefold()
    aliases = {
        "关闭": TRANSLATE_MODE_OFF,
        "off": TRANSLATE_MODE_OFF,
        "false": TRANSLATE_MODE_OFF,
        "0": TRANSLATE_MODE_OFF,
        "disabled": TRANSLATE_MODE_OFF,
        "开启": TRANSLATE_MODE_ON,
        "on": TRANSLATE_MODE_ON,
        "true": TRANSLATE_MODE_ON,
        "1": TRANSLATE_MODE_ON,
        "enabled": TRANSLATE_MODE_ON,
        "自动": TRANSLATE_MODE_AUTO,
        "auto": TRANSLATE_MODE_AUTO,
    }
    return aliases.get(normalized, TRANSLATE_MODE_OFF)


def normalize_prompt(prompt: str) -> str:
    """在分类、转译和生图前统一提示词中的空白与换行。

    一个或多个换行（以及换行两侧已有的分隔符）会归一化为一个英文逗号，
    连续横向空白则折叠为一个空格，避免 WebUI、Bot 和 API 入口行为不同。
    """
    normalized = _PROMPT_LINE_BREAK_RE.sub(", ", str(prompt or "").strip())
    normalized = _PROMPT_HORIZONTAL_WHITESPACE_RE.sub(" ", normalized)
    return normalized.strip()


def split_prompt_segments(prompt: str) -> list[str]:
    """按顶层分隔符切分提示词，同时保护 NAI 权重表达式。

    括号、引号、反引号或 ``::...::`` 内部的逗号属于当前标签，不能作为
    混合提示词的切分点。
    """
    segments: list[str] = []
    buffer: list[str] = []
    depths = {"(": 0, "[": 0, "{": 0}
    closing_to_opening = {")": "(", "]": "[", "}": "{"}
    quote_char = ""
    in_weighted_expression = False
    index = 0

    def flush() -> None:
        text = "".join(buffer).strip()
        if text:
            segments.append(text)
        buffer.clear()

    while index < len(prompt):
        # NAI 的数值权重写法可能含逗号，成对的 :: 之间视为一个整体。
        if prompt[index : index + 2] == "::" and not quote_char:
            in_weighted_expression = not in_weighted_expression
            buffer.append("::")
            index += 2
            continue

        char = prompt[index]
        if char in {'"', "`"}:
            if quote_char == char:
                quote_char = ""
            elif not quote_char:
                quote_char = char
            buffer.append(char)
            index += 1
            continue

        if not quote_char:
            if char in depths:
                depths[char] += 1
            elif char in closing_to_opening:
                opening = closing_to_opening[char]
                depths[opening] = max(0, depths[opening] - 1)

            if (
                char in {",", "，", ";", "；", "\n", "\r"}
                and not in_weighted_expression
                and not any(depths.values())
            ):
                flush()
                index += 1
                continue

        buffer.append(char)
        index += 1

    flush()
    return segments


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WORD_RE = re.compile(r"[a-zA-Z]+(?:'[a-zA-Z]+)?|\d+")
_NAI_COUNT_TAG_RE = re.compile(
    r"(?<![a-z0-9_])\d+(?:girl|boy|girls|boys|other)s?(?![a-z0-9_])",
    re.IGNORECASE,
)
_NAI_ARTIST_RE = re.compile(
    r"(?:^|\s)(?:artist|character|series)\s*:",
    re.IGNORECASE,
)
_EN_DIRECTIVE_RE = re.compile(
    r"\b(?:please|draw|generate|create|depict|show|make|render|illustrate)\b",
    re.IGNORECASE,
)
_EN_CONNECTOR_RE = re.compile(
    r"\b(?:while|who|which|that|because|where|whose)\b",
    re.IGNORECASE,
)
_EN_PREPOSITION_RE = re.compile(
    r"\b(?:with|without|under|inside|outside|beside|behind|between|through|"
    r"toward|towards|into|from|near|upon|during|across|around|on|in|at)\b",
    re.IGNORECASE,
)
_ZH_NATURAL_MARKERS = (
    "请",
    "画",
    "生成",
    "描绘",
    "表现",
    "正在",
    "站在",
    "坐在",
    "躺在",
    "走在",
    "跑在",
    "穿着",
    "拿着",
    "看着",
    "有一个",
    "一个",
    "她",
    "他",
    "它",
)
_COMMON_NAI_TAGS = {
    "solo",
    "best quality",
    "masterpiece",
    "absurdres",
    "highres",
    "looking at viewer",
    "full body",
    "upper body",
    "portrait",
    "landscape",
    "anime style",
    "cinematic lighting",
}


def classify_prompt_segment(segment: str) -> PromptSegmentKind:
    """启发式判断单个片段是 NAI 标签还是自然语言。

    NAI 语法特征和自然语言特征分别计分。只有自然语言得分达到阈值且严格
    高于 NAI 得分时才会送入 LLM；有歧义的短英文默认按 NAI 标签处理，
    以降低误译已有标签的风险。
    """
    text = segment.strip()
    lowered = text.casefold().replace("_", " ")
    nai_score = 0
    natural_score = 0

    # 权重、括号、角色/画师前缀等是强 NAI 语法信号。
    if text.count("::") >= 2:
        nai_score += 7
    if any(char in text for char in "{}[]"):
        nai_score += 6
    if _NAI_COUNT_TAG_RE.search(lowered):
        nai_score += 5
    if _NAI_ARTIST_RE.search(text):
        nai_score += 5
    if "\\(" in text or "\\)" in text:
        nai_score += 3
    if lowered in _COMMON_NAI_TAGS:
        nai_score += 4
    if "_" in text and " " not in text:
        nai_score += 2

    # 中文、完整句标点、祈使动词和英文连接词倾向于自然语言。
    if _CJK_RE.search(text):
        natural_score += 4
        if any(marker in text for marker in _ZH_NATURAL_MARKERS):
            natural_score += 2
    if any(mark in text for mark in "。！？!?：:"):
        natural_score += 2
    if _EN_DIRECTIVE_RE.search(text):
        natural_score += 5
    if _EN_CONNECTOR_RE.search(text):
        natural_score += 2

    # 对纯英文片段补充轻量语法判断，避免只靠关键词漏掉普通描述句。
    words = _WORD_RE.findall(text)
    word_count = len(words)
    word_set = {word.casefold() for word in words}
    starts_with_article = bool(words) and words[0].casefold() in {
        "a",
        "an",
        "the",
        "this",
        "that",
        "she",
        "he",
        "they",
        "it",
    }
    has_article = bool(word_set & {"a", "an", "the"})
    has_preposition = bool(_EN_PREPOSITION_RE.search(text))
    has_gerund = any(word.casefold().endswith("ing") for word in words)

    if starts_with_article:
        natural_score += 1
        if word_count >= 4:
            natural_score += 1
    if has_article and has_preposition:
        natural_score += 1
    if has_gerund and word_count >= 4:
        natural_score += 1
    if word_count >= 8:
        natural_score += 2
    elif word_count >= 6:
        natural_score += 1

    if natural_score >= 3 and natural_score > nai_score:
        return "natural"
    return "nai"


def extract_mixed_prompt(prompt: str) -> MixedPrompt:
    """提取混合提示词；显式 ``|nl|`` 分界优先于自动分类。

    标记左侧强制视为 NAI 标签，右侧整体视为待转译自然语言。没有标记时，
    才逐个顶层片段调用 :func:`classify_prompt_segment` 自动判断。
    """
    original = prompt.strip()
    marker = re.search(r"\|nl\|", original, flags=re.IGNORECASE)
    if marker is not None:
        nai_part = original[: marker.start()].strip(" ,，;；\r\n")
        natural_part = original[marker.end() :].strip(" ,，;；\r\n")
        segments = [
            PromptSegment(text, "nai") for text in split_prompt_segments(nai_part)
        ]
        if natural_part:
            segments.append(PromptSegment(natural_part, "natural"))
        return MixedPrompt(original, tuple(segments), explicit_natural=True)

    segments = tuple(
        PromptSegment(text, classify_prompt_segment(text))
        for text in split_prompt_segments(original)
    )
    return MixedPrompt(original, segments)


def _normalize_tag_for_deduplication(tag: str) -> str:
    """生成仅用于去重比较的标签，不改变最终输出中的原始写法。"""

    normalized = tag.strip().casefold().replace("_", " ")
    return " ".join(normalized.split())


def merge_translated_prompt(parts: MixedPrompt, translated: str) -> str:
    """将转译标签放回首个自然语言片段的位置，并保持 NAI 标签顺序。

    合并时会忽略大小写及下划线差异，去除译文中与原有 NAI 标签重复的项；
    多个自然语言片段统一由一份译文替换，防止重复插入。
    """
    if not parts.has_natural:
        return parts.original

    existing_tags = {
        _normalize_tag_for_deduplication(segment.text)
        for segment in parts.segments
        if segment.kind == "nai"
    }
    translated_segments: list[str] = []
    seen = set(existing_tags)
    for segment in split_prompt_segments(translated):
        normalized = _normalize_tag_for_deduplication(segment)
        if not normalized or normalized in seen:
            continue
        translated_segments.append(segment)
        seen.add(normalized)

    translated_text = ", ".join(translated_segments)
    if not translated_text and not translated.strip():
        translated_text = parts.natural_text

    merged: list[str] = []
    inserted = False
    for segment in parts.segments:
        if segment.kind == "natural":
            if not inserted:
                if translated_text:
                    merged.append(translated_text)
                inserted = True
            continue
        merged.append(segment.text)
    if not inserted and translated_text:
        merged.append(translated_text)
    return ", ".join(text for text in merged if text)
