"""站点优先级的故障转移：站点表解析、排序与可切换性判定。

本模块只做纯函数计算，不发起网络请求、不依赖 AstrBot 运行时，便于离线测试。
接入方式由每个站点自己声明（``direct`` / ``openai`` / ``novelai``），
因此同一张站点表可以覆盖三种协议，并允许按优先级跨协议降级。

故障转移有一条硬约束：只有「明确没有出图、也没有计费」的失败才允许换站。
请求超时、以及请求已被上游接受（2xx）之后的解析失败，都可能已经扣费，
一律不换站，避免一次需求被多次计费。
"""

from __future__ import annotations

import re
from typing import Any

# 匹配既有的 HTTP 失败原因写法：``http_4xx (HTTP 401)``、``http_500``。
_HTTP_REASON_RE = re.compile(r"^http_(4xx|5xx|\d{3})\b")

# 站点接入方式与 ``call_mode`` 取值一致；顺序即同一优先级下的稳定次序。
SITE_MODES = ("direct", "novelai", "openai")
SITE_MODE_LABELS = {
    "direct": "nai.sta1n.cn",
    "novelai": "NovelAI 原生",
    "openai": "OpenAI 兼容",
}
# NovelAI 原生站点还需区分官方与星桥，取值为 ``novelai_service``。
SITE_NOVELAI_SERVICES = ("starbride", "official")

SITE_NAME_MAX = 40


def normalize_mode(value: Any) -> str:
    """把站点接入方式归一到受支持的取值，非法值返回空串。

    Args:
        value: 配置里读到的原始值。

    Returns:
        ``direct`` / ``novelai`` / ``openai`` 之一；无法识别时返回 ``""``。
    """

    mode = str(value or "").strip().casefold()
    return mode if mode in SITE_MODES else ""


def normalize_novelai_service(value: Any) -> str:
    """把 NovelAI 服务名归一到 ``starbride`` / ``official``。

    Args:
        value: 配置里读到的原始值。

    Returns:
        归一后的服务名；空值或非法值回退 ``starbride``（与单站点默认一致）。
    """

    service = str(value or "").strip().casefold()
    return service if service in SITE_NOVELAI_SERVICES else "starbride"


def normalize_site_name(value: Any, *, fallback: str = "") -> str:
    """清理站点显示名，避免空名与超长名进入日志和回复。

    Args:
        value: 配置里读到的原始名称。
        fallback: 名称为空时使用的兜底名。

    Returns:
        去掉首尾空白、截断到 ``SITE_NAME_MAX`` 的名称。
    """

    name = str(value or "").strip()
    if not name:
        name = str(fallback or "").strip()
    return name[:SITE_NAME_MAX]


def normalize_base_url(value: Any) -> str:
    """清理站点地址：只接受 http(s)，去掉尾部斜杠。

    Args:
        value: 配置里读到的原始地址。

    Returns:
        可用的地址；协议不受支持时返回空串，表示该站点应被跳过。
    """

    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    lowered = url.casefold()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return ""
    return url


def normalize_site(item: Any, index: int) -> dict[str, Any] | None:
    """把一条站点记录规整成固定结构，无地址的记录返回 None。

    Args:
        item: ``site_failover_sites`` 模板列表里的一条记录。
        index: 该记录在列表中的位置（0 起），仅用于生成兜底名称。

    Returns:
        规整后的站点字典；缺地址或接入方式无法识别时返回 None。
    """

    if not isinstance(item, dict):
        return None
    mode = normalize_mode(item.get("mode"))
    url = normalize_base_url(item.get("base_url"))
    if not mode or not url:
        return None
    enabled_value = item.get("enabled", True)
    if isinstance(enabled_value, str):
        enabled = enabled_value.strip().casefold() not in ("false", "0", "")
    else:
        enabled = bool(enabled_value)
    try:
        priority = int(item.get("priority"))
    except (TypeError, ValueError):
        priority = index + 1
    site = {
        "name": normalize_site_name(item.get("name"), fallback=f"站点 {index + 1}"),
        "mode": mode,
        "base_url": url,
        "api_key": str(item.get("api_key") or "").strip(),
        "enabled": enabled,
        # 优先级数字越小越先用；非法/缺省值按列表顺序补齐，保持可预测。
        "priority": priority,
        "order": index,
    }
    if mode == "novelai":
        site["novelai_service"] = normalize_novelai_service(
            item.get("novelai_service")
        )
    return site


def normalize_sites(raw: Any) -> list[dict[str, Any]]:
    """规整整张站点表，并按优先级排序。

    Args:
        raw: 配置项 ``site_failover_sites`` 的原始值。

    Returns:
        排序后的站点列表：``priority`` 升序，同优先级保持配置里的先后顺序。
    """

    if not isinstance(raw, list):
        return []
    sites: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        site = normalize_site(item, index)
        if site is not None:
            sites.append(site)
    sites.sort(key=lambda site: (site["priority"], site["order"]))
    for position, site in enumerate(sites, start=1):
        # 重排后的生效次序，仅用于日志与回复展示，不写回配置。
        site["effective_order"] = position
    return sites


def enabled_sites(raw: Any) -> list[dict[str, Any]]:
    """取出已启用且字段完整的站点，供故障转移按优先级依次尝试。

    Args:
        raw: 配置项 ``site_failover_sites`` 的原始值。

    Returns:
        已启用站点的有序列表；配置为空时返回空列表，调用方据此回退单站点逻辑。
    """

    return [site for site in normalize_sites(raw) if site["enabled"]]


def describe_site(site: dict[str, Any]) -> str:
    """生成用于日志的站点描述，包含名称、接入方式与地址，不含密钥。

    Args:
        site: ``normalize_site`` 产出的站点字典。

    Returns:
        形如 ``主力站(nai.sta1n.cn) https://nai.example.com`` 的字符串。
    """

    mode = SITE_MODE_LABELS.get(site.get("mode", ""), site.get("mode", ""))
    if site.get("mode") == "novelai":
        mode = f"NovelAI/{normalize_novelai_service(site.get('novelai_service'))}"
    return f"{site.get('name', '')}({mode}) {site.get('base_url', '')}"


# 明确不允许换站的原因：可能已经出图或已计费，或与站点无关。
# 这些失败即使还有候选站点也必须直接返回，避免重复扣费。
_UNSWITCHABLE_REASONS = frozenset(
    {
        # 请求已发出但超时：上游可能仍在生成并照常扣费。
        "timeout",
        # 上游返回 2xx 之后才发现响应/图片不可用：生图可能已完成并计费。
        "empty_response",
        "invalid_response",
        # 会话未初始化属于本地环境问题，换站没有意义。
        "no_session",
        # 缺少本地输入（参考图）等调用方错误，换站同样没有意义。
        "no_reference_image",
        # 未分类异常可能发生在请求发出之后，按已计费处理，不换站。
        "exception",
    }
)

# 明确可以换站的原因：请求确定没有被上游受理，或上游声明失败并退点。
_SWITCHABLE_REASONS = frozenset(
    {
        # 连接阶段就失败：请求没有到达上游，不可能出图或计费。
        "connect_failed",
        # 任务接口 404：该站没有任务接口，属于站点能力差异，换站有意义。
        "web_jobs_unavailable",
        # 任务失败：上游声明失败并退点，未产生有效出图。
        "web_job_failed",
        # 本站点没配凭据/地址，换到有凭据的站点可能成功。
        "no_token",
        "openai_not_configured",
        # 上游明确拒绝该模型的参考能力，属站点能力差异。
        "reference_model_unsupported",
        "director_ref_rejected",
    }
)


def _reason_code(reason: str) -> str:
    """从 ``http_4xx (HTTP 401): detail`` 这类原因里取出可判定的错误码。

    既有的原因字符串有四种形态：纯错误码（``timeout``）、带状态码的
    粗分类（``http_4xx (HTTP 401)``）、具体状态码（``http_503``），以及
    以 ``novelai |`` 开头的原生错误。这里统一收敛成便于判定的稳定形式。

    Args:
        reason: 生图方法返回的原因字符串。

    Returns:
        归一后的错误标识：HTTP 类统一为 ``http_<状态码>``，原生错误保留
        ``novelai |`` 前缀，其它错误取冒号前的部分。
    """

    text = str(reason or "").strip()
    if not text:
        return ""
    http_match = _HTTP_REASON_RE.match(text)
    if http_match:
        status = http_match.group(1)
        if status in ("4xx", "5xx"):
            return "http_" + status
        return "http_" + str(int(status))
    return text.split(":", 1)[0].strip()


def _http_status(code: str) -> int | None:
    """从归一后的原因码里解析出 HTTP 状态码。

    兼容两种既有写法：粗分类 ``http_4xx`` / ``http_5xx``，以及具体状态
    ``http_401`` / ``http_503``。

    Args:
        code: ``_reason_code`` 的返回值。

    Returns:
        解析出的状态码；不是 HTTP 类原因时返回 None。
    """

    if not code.startswith("http_"):
        return None
    tail = code[len("http_") :].strip()
    if tail in ("4xx", "5xx"):
        return int(tail[0]) * 100
    if tail.isdigit():
        return int(tail)
    return None


def is_switchable(reason: str) -> bool:
    """判断某个失败原因是否允许换到下一个站点重试。

    黑名单优先于白名单：宁可少切一次，也不能重复计费。HTTP 类失败按状态码
    判断——4xx 表示请求未被受理，5xx 表示服务端未完成，两者都未产生有效出图。

    Args:
        reason: 生图方法返回的原因字符串，可能带 ``: 详情`` 后缀。

    Returns:
        允许换站返回 True，否则返回 False。
    """

    code = _reason_code(reason)
    if not code:
        return False
    if code in _UNSWITCHABLE_REASONS:
        return False
    if code.startswith("novelai |"):
        # NovelAI 站点缺密钥属于站点未配置，可换到配好密钥的站点；
        # 编码超时等可能已计费的错误必须留在本站点，不能换站重发。
        return "API Key" in code
    if code.startswith("reference_model_unsupported"):
        # 该站不支持所选模型的精准参考，换站可能可用。
        return True
    status = _http_status(code)
    if status is not None:
        # 4xx/5xx 都在请求受理阶段或服务端故障阶段终止，未产生有效出图。
        return 400 <= status < 600
    return code in _SWITCHABLE_REASONS


def switch_decision(reason: str, remaining: int) -> tuple[bool, str]:
    """给出「是否换站」的结论，并附上可直接写日志的理由。

    Args:
        reason: 当前站点的失败原因。
        remaining: 除当前站点外还剩几个候选站点。

    Returns:
        ``(是否换站, 说明)``。说明用于日志，既覆盖切换也覆盖不切换的原因。
    """

    if remaining <= 0:
        return False, "没有更多候选站点"
    if is_switchable(reason):
        return True, f"失败原因 {_reason_code(reason)} 可安全换站"
    return False, f"失败原因 {_reason_code(reason)} 可能已出图或已计费，不换站"


def build_site(  # noqa: PLR0913 - 站点字段与配置项一一对应，显式传参最清晰
    name: str,
    mode: str,
    base_url: str,
    *,
    api_key: str = "",
    priority: int = 1,
    enabled: bool = True,
    novelai_service: str = "starbride",
) -> dict[str, Any]:
    """构造一条站点记录，供单站点兼容路径与测试使用。

    Args:
        name: 站点显示名。
        mode: 接入方式，取值同 ``SITE_MODES``。
        base_url: 站点地址。
        api_key: 该站点凭据，留空表示沿用插件里该协议的既有配置。
        priority: 优先级，数字越小越先用。
        enabled: 是否参与故障转移。
        novelai_service: ``novelai`` 站点使用的服务类型。

    Returns:
        规整后的站点字典；字段非法时返回空字典。
    """

    return (
        normalize_site(
            {
                "name": name,
                "mode": mode,
                "base_url": base_url,
                "api_key": api_key,
                "priority": priority,
                "enabled": enabled,
                "novelai_service": novelai_service,
            },
            0,
        )
        or {}
    )
