"""传统 GET 直连模式（nai.sta1n.cn / Nai2API）的点数计价。

对齐上游 2026-09 引入的「步数 × 画质」计费模块（上游仓库
``public/generation-pricing.js`` 的 ``frontendGenerationCost`` 与
``server/providers.js`` 的 ``sizeCostMap``）：

- 基础点数由画幅决定：普通画幅（竖/横/方图）1 点，2K 15 点，4K 25 点；
- V5 完整版（nai-diffusion-5-full）普通画幅起步 8 点（2K/4K 与 V4.5 同价）；
- 步数超过 28 后按档位加价：29-35 步 +4，36-45 步 +6，46-50 步 +8。

完整价目表（点/张）::

    步数      V4.5标准   V5标准   2K(两模型)   4K(两模型)
    1-28       1         8        15           25
    29-35      6         12       19           29
    36-45      8         14       21           31
    46-50      10        16       23           33

计价规则因提交通道而异：

- **任务接口**（``POST /api/web/jobs``）：支持 1-50 步，按上表计价，
  失败任务点数自动退回；
- **GET 直链**（``GET /generate``）：步数最高按 28 路由（超出部分上游
  静默截断），价格固定为基础档（1 / 8 / 15 / 25），不含步数加价。

本模块为纯函数、零依赖，``main.py`` 与测试直接导入使用。
"""

from __future__ import annotations

import re

# 上游 sizeCostMap：画幅分档名 → 基础点数（普通 1 / 2K 15 / 4K 25）。
SIZE_COST_MAP: dict[str, int] = {
    "竖图": 1,
    "横图": 1,
    "方图": 1,
    "2K竖图": 15,
    "2K横图": 15,
    "2K方图": 15,
    "4K竖图": 25,
    "4K横图": 25,
    "4K方图": 25,
}

# 任务接口（/api/web/jobs）支持的最高步数；超过会被上游钳制。
WEB_JOB_STEPS_MAX = 50
# GET 直链（/generate）的最高路由步数；超出部分按 28 生成、按基础档计价。
DIRECT_URL_STEPS_MAX = 28

# V5 完整版普通画幅的起步点数（上游 modelGenerationCost）。
_V5_MODEL_COST_FLOOR = 8

# 通道标识（仅用于用量统计/参数记录的标注；路由逻辑已固定为
# 任务接口优先、GET 兜底，不再有用户可选的通道配置）。
TRANSPORT_GET = "get"
TRANSPORT_WEB_JOBS = "web_jobs"


def effective_steps(steps: int, transport: str) -> int:
    """按通道返回对生成与计价实际生效的步数（钳制到通道上限）。"""

    steps = int(steps)
    cap = (
        DIRECT_URL_STEPS_MAX
        if transport == TRANSPORT_GET
        else WEB_JOB_STEPS_MAX
    )
    return max(1, min(steps, cap))


def steps_cost_extra(steps: int) -> int:
    """步数档位加价：≤28 步 +0，29-35 +4，36-45 +6，46-50 +8。"""

    steps = int(steps)
    if steps > 45:
        return 8
    if steps > 35:
        return 6
    if steps > 28:
        return 4
    return 0


def web_job_generation_cost(size: str, model: str, steps: int) -> int:
    """任务接口（/api/web/jobs）的单张点数，复刻上游 frontendGenerationCost。

    ``steps`` 超过 50 时按 50 计（与上游钳制行为一致）。
    """

    size_cost = SIZE_COST_MAP.get(str(size or "").strip(), 1)
    steps = max(1, min(int(steps), WEB_JOB_STEPS_MAX))
    extra = steps_cost_extra(steps)
    if size_cost > 1:
        return size_cost + extra
    if str(model or "").strip() == "nai-diffusion-5-full":
        return _V5_MODEL_COST_FLOOR + extra
    return 2 + extra if extra else 1


def direct_url_generation_cost(size: str, model: str) -> int:
    """GET 直链（/generate）的单张点数：基础档，不含步数加价。"""

    size_cost = SIZE_COST_MAP.get(str(size or "").strip(), 1)
    model_cost = (
        _V5_MODEL_COST_FLOOR
        if str(model or "").strip() == "nai-diffusion-5-full"
        else 1
    )
    return max(size_cost, model_cost)


# OpenAI 兼容端点以像素分辨率（如 ``1024x1536``）表达画幅，无法直接套用
# 上游的中文分档名，这里按最长边回推档位，仅用于用量统计的估算。
_PIXEL_SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[xX×]\s*(\d{2,5})\s*$")


def pixel_size_cost(size: str) -> int:
    """按像素分辨率推断画幅档位点数：≥3000px 记 4K(25)，≥1800px 记 2K(15)。"""

    match = _PIXEL_SIZE_RE.match(str(size or ""))
    if not match:
        return 1
    longest = max(int(match.group(1)), int(match.group(2)))
    if longest >= 3000:
        return 25
    if longest >= 1800:
        return 15
    return 1


def estimate_openai_cost(size: str, model: str) -> int:
    """OpenAI 兼容模式的单张点数估算（画幅按像素推断，再取模型起步价）。"""

    size_cost = SIZE_COST_MAP.get(str(size or "").strip(), 0) or pixel_size_cost(size)
    model_cost = (
        _V5_MODEL_COST_FLOOR
        if str(model or "").strip() == "nai-diffusion-5-full"
        else 1
    )
    return max(size_cost, model_cost)


def price_table_lines() -> list[str]:
    """渲染完整的步数 × 画质价目表（markdown 表格行，含表头）。"""

    tiers = [
        ("1-28", DIRECT_URL_STEPS_MAX),
        ("29-35", 35),
        ("36-45", 45),
        ("46-50", WEB_JOB_STEPS_MAX),
    ]
    lines = [
        "| 步数 | V4.5 标准 | V5 标准 | 2K（两模型） | 4K（两模型） |",
        "| --- | --- | --- | --- | --- |",
    ]
    for label, sample_steps in tiers:
        v45 = web_job_generation_cost("方图", "nai-diffusion-4-5-full", sample_steps)
        v5 = web_job_generation_cost("方图", "nai-diffusion-5-full", sample_steps)
        two_k = web_job_generation_cost("2K方图", "nai-diffusion-4-5-full", sample_steps)
        four_k = web_job_generation_cost("4K方图", "nai-diffusion-4-5-full", sample_steps)
        lines.append(
            f"| {label} | {v45} | {v5} | {two_k} | {four_k} |"
        )
    return lines
