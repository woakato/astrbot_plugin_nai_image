"""NovelAI 提示词格式检查（SFW 版，只读检查 + 可选自动修正，纯标准库，不调用任何 API）。

用法：
  python lint_nai.py --prompt "..." [--negative "..."] [--char "..." --char "..."]
                     [--size 832x1216] [--steps 28] [--quality on|off] [--fix]

退出码：0 没有错误；1 有会毁图或导致语法失效的错误（警告不影响退出码）。
"""
from __future__ import annotations

import argparse
import re
import sys

COMFY_WEIGHT = re.compile(r"\([^()]+:\s*-?\d+(?:\.\d+)?\)")
ESCAPED_PAREN = re.compile(r"\\[()]")
QUALITY_WORDS = ("masterpiece", "very aesthetic", "amazing quality")
COUNT_TAG = re.compile(r"\b(\d+)(girls?|boys?|others?)\b")
TEXT_TAG = re.compile(r"(?<![a-z0-9_])text:")
PIXEL_SIZE = re.compile(r"^(\d+)x(\d+)$", re.I)
PRESET_SIZES = {
    "竖图",
    "横图",
    "方图",
    "2k竖图",
    "2k横图",
    "2k方图",
    "4k竖图",
    "4k横图",
    "4k方图",
    "portrait",
    "vertical",
    "landscape",
    "horizontal",
    "square",
    "2k_portrait",
    "2k-portrait",
    "2k_vertical",
    "2k-vertical",
    "2k_landscape",
    "2k-landscape",
    "2k_horizontal",
    "2k-horizontal",
    "2k_square",
    "2k-square",
    "4k_portrait",
    "4k-portrait",
    "4k_vertical",
    "4k-vertical",
    "4k_landscape",
    "4k-landscape",
    "4k_horizontal",
    "4k-horizontal",
    "4k_square",
    "4k-square",
}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _scan_double_colons(text: str) -> tuple[int, int, list[tuple[int, int, str]]]:
    """扫描所有 `::`，区分权重开头、数字结尾标签紧贴 `::` 与权重结尾。"""
    opens = 0
    closes = 0
    digit_before_close: list[tuple[int, int, str]] = []
    pos = 0
    while True:
        idx = text.find("::", pos)
        if idx == -1:
            break
        seg_start = 0
        for sep in (",", "|", "\n", "::"):
            found = text.rfind(sep, 0, idx)
            if found != -1:
                candidate = found + len(sep)
                if candidate > seg_start:
                    seg_start = candidate
        segment = text[seg_start:idx]
        stripped = segment.strip()
        if stripped and re.fullmatch(r"-?\d+(?:\.\d+)?", stripped):
            opens += 1
        elif stripped and re.search(r"\d$", stripped):
            digit_before_close.append((seg_start, idx + 2, stripped))
        else:
            closes += 1
        pos = idx + 2
    return opens, closes, digit_before_close


def check_weights(text: str, where: str, rep: Report, fix: bool) -> str:
    # 1. 数字结尾的名称紧贴 ::（含空格，如 `year 2024::` 或 `artist:as109::`）
    opens, closes, digit_hits = _scan_double_colons(text)
    for _, _, token in digit_hits:
        rep.err(
            f"[{where}] `{token}::`：名称以数字结尾且紧贴 `::`，会被解析成权重数值导致整图崩坏。"
            f"请改为 `{token}, ::`"
        )
    if fix and digit_hits:
        parts: list[str] = []
        last = 0
        for seg_start, end, _ in digit_hits:
            segment = text[seg_start : end - 2]
            leading = segment[: len(segment) - len(segment.lstrip())]
            core = segment.strip()
            parts.append(text[last:seg_start])
            parts.append(f"{leading}{core}, ::")
            last = end
        parts.append(text[last:])
        text = "".join(parts)
        opens, closes, _ = _scan_double_colons(text)

    # 2. 权重开了没关
    if opens > closes:
        rep.warn(
            f"[{where}] 有 {opens - closes} 个数字权重没有用 `::` 结束，"
            "它会一直作用到提示词末尾。请确认是否漏写结尾 `::`。"
        )

    # 3. ComfyUI / A1111 写法
    for m in COMFY_WEIGHT.finditer(text):
        rep.err(f"[{where}] `{m.group(0)}` 是 ComfyUI 写法，NAI 不认。改成 `数值::词::`")
    if ESCAPED_PAREN.search(text):
        rep.warn(f"[{where}] 有 `\\(` `\\)`：NAI 里圆括号不是权重语法，无需转义，直接写圆括号")
        if fix:
            text = ESCAPED_PAREN.sub(lambda m: m.group(0)[1], text)

    # 4. 括号配对
    if text.count("{") != text.count("}"):
        rep.warn(f"[{where}] `{{` 和 `}}` 数量不一致")
    if text.count("[") != text.count("]"):
        rep.warn(f"[{where}] `[` 和 `]` 数量不一致")
    return text


def lint(
    prompt: str,
    negative: str = "",
    chars: list[str] | None = None,
    size: str = "832x1216",
    steps: int = 28,
    quality: bool = True,
    fix: bool = False,
) -> tuple[list[str], list[str], dict]:
    """供外部直接调用 -> (错误列表, 提醒列表, 修正后的 {prompt, negative, chars})。"""
    rep = Report()
    prompt = check_weights(prompt, "主提示词", rep, fix)
    negative = check_weights(negative, "负面", rep, fix) if negative else ""
    chars = [check_weights(c, f"角色{i + 1}", rep, fix) for i, c in enumerate(chars or [])]
    low = prompt.lower()

    # Text: 必须在末尾
    text_matches = list(TEXT_TAG.finditer(low))
    if text_matches:
        tail = prompt[text_matches[-1].start() :]
        if "\n\n" not in tail and re.search(r"text:.*?,\s*[a-z]", tail, re.I | re.S) and tail.count(",") > 2:
            rep.warn("`Text:` 后面还有很多逗号分隔的词，它们可能会被当成文字画进图里。`Text:` 要放在最末尾")
        if quality:
            rep.warn("要画文字但开着 Add Quality Tags（它带 `no text`），文字出不来时请关掉")

    # 质量词重复
    if quality:
        dup = [w for w in QUALITY_WORDS if re.search(rf"\b{re.escape(w)}\b", low)]
        if dup:
            rep.warn(f"开着 Add Quality Tags 又手写了 {', '.join(dup)}，会叠两层。二选一")

    # 分镜 + 预设
    if any(re.search(rf"\b{re.escape(k)}\b", low) for k in ("comic", "multiple panels", "panel")):
        rep.warn("这是分镜页：负面预设选「不加」（Heavy/Light 带 `multiple views`，会压分镜）")

    # 多人
    m = COUNT_TAG.search(low)
    if chars:
        for i, c in enumerate(chars):
            if COUNT_TAG.search(c.lower()):
                rep.err(f"角色{i + 1} 里有人数标签（如 1girl），人数只能写在主提示词里")
        if "extra characters" in negative.lower():
            rep.err("多人图负面里有 `extra characters`，会和人数标签打架")
    if m and m.start() > 80:
        rep.warn(f"人数标签 `{m.group(0)}` 不在开头，建议挪到主提示词最前面")

    # 尺寸与步数基础校验（适配本插件：支持标准/2K/4K 及 1-50 步，不做官网 Opus 免费限制报错）
    size_norm = str(size or "").strip().lower()
    if size_norm and size_norm not in PRESET_SIZES:
        pm = PIXEL_SIZE.match(size_norm)
        if pm:
            w, h = int(pm.group(1)), int(pm.group(2))
            if w <= 0 or h <= 0:
                rep.err(f"尺寸 `{size}` 宽高必须大于 0")
            elif w % 64 or h % 64:
                rep.warn("像素尺寸宽高最好是 64 的倍数")
        else:
            rep.warn(f"未识别的尺寸 `{size}`，常用如 `832x1216` 或 `竖图`/`2K竖图`")

    if not 1 <= steps <= 50:
        rep.warn(f"步数 {steps} 超出常规范围（1-50），插件会自动收敛")

    return rep.errors, rep.warnings, {"prompt": prompt, "negative": negative, "chars": chars}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--negative", default="")
    ap.add_argument("--char", action="append", default=[], help="角色框，可多次")
    ap.add_argument("--size", default="832x1216")
    ap.add_argument("--steps", type=int, default=28)
    ap.add_argument("--quality", choices=["on", "off"], default="on")
    ap.add_argument("--fix", action="store_true", help="输出自动修正后的文本")
    a = ap.parse_args()

    errors, warnings, fixed = lint(a.prompt, a.negative, a.char, a.size, a.steps, a.quality == "on", a.fix)
    for e in errors:
        print("错误：" + e)
    for wmsg in warnings:
        print("提醒：" + wmsg)
    if not errors and not warnings:
        print("没发现问题")
    if a.fix:
        print("\n--- 自动修正后 ---")
        print("主提示词：\n" + fixed["prompt"])
        if fixed["negative"]:
            print("\n负面：\n" + fixed["negative"])
        for i, c in enumerate(fixed["chars"]):
            print(f"\n角色{i + 1}：\n{c}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
