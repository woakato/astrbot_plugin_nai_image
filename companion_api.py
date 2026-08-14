"""陪伴系列插件直连扩展 API（对齐 astrbot_plugin_image_companion 的接口契约）。

陪伴插件通过 ``sys.modules`` 里的 ``get_nai_image_api()`` 或
``context.get_registered_star("astrbot_plugin_nai_image").star_cls.extension_api``
发现本接口，调用方式与 image_companion 完全一致，因此无需经过本地
OpenAI 兼容代理即可完成生图。

本模块只依赖插件实例暴露的既有方法（``_generate_one`` 等），不修改任何
核心生图逻辑，也不做上游可达性预检。
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.star import StarTools

from .command_args import normalize_image_size, normalize_image_style
from .prompt_processing import normalize_prompt

if TYPE_CHECKING:
    from .main import NAIGenerateImagePlugin

_LOG_TAG = "[NAI-Image][companion]"

# 陪伴请求中的提示词模式归一化结果
_PROMPT_FORMAT_NATURAL = "natural_language"
_PROMPT_FORMAT_NAI = "nai"

# 陪伴插件常见比例写法 → NAI 尺寸键。
_COMPANION_RATIO_SIZES = {
    "1:1": "方图",
    "2:3": "竖图",
    "3:4": "竖图",
    "9:16": "竖图",
    "3:2": "横图",
    "4:3": "横图",
    "16:9": "横图",
}

# OpenAI 风格的 "1024x1024" 尺寸写法。
_WH_SIZE_RE = re.compile(r"^\s*(\d{1,5})\s*[x×*]\s*(\d{1,5})\s*$", re.IGNORECASE)

# 自然语言模式下的空白折叠（保留句子结构，不注入逗号）。
_WHITESPACE_RE = re.compile(r"\s+")


def _first_text(*values: Any) -> str:
    """返回第一个非空字符串，全部为空时返回空串。"""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


class NAIImageCompanionExtensionAPI:
    """供陪伴系列插件调用的直连生图接口。

    方法签名与 ``astrbot_plugin_image_companion`` 的 extension API 保持一致，
    仅 ``local_load_state`` 按约定不提供（NAI 为远端生图服务，无本地负载）。
    """

    def __init__(self, plugin: NAIGenerateImagePlugin) -> None:
        self._plugin = plugin
        self.generation_count = 0
        self.last_generation: dict[str, Any] = {}

    # ==== 状态查询 ====

    def status(self) -> dict[str, Any]:
        """返回插件直连状态快照，供陪伴插件面板与状态查询使用。"""
        plugin = self._plugin
        return {
            "enabled": bool(getattr(plugin, "enable_companion_link", True)),
            "plugin": "astrbot_plugin_nai_image",
            "token_configured": bool(plugin.image_gen_key),
            "base_url": plugin.base_url,
            "default_style": plugin.image_style,
            "default_size": plugin.image_size,
            "prompt_format": getattr(plugin, "companion_prompt_format", "") or "自然语言模式（en）",
            "proxy_enabled": bool(getattr(plugin, "enable_proxy", True)),
            "proxy_force_disabled": bool(getattr(plugin, "force_disable_proxy", True)),
            "proxy_online": plugin.proxy_runner is not None,
            "proxy_port": plugin.proxy_port,
            "generation_count": self.generation_count,
            "last_generation": dict(self.last_generation),
        }

    def capability_status(self, owner: Any) -> dict[str, Any]:
        """返回与 image_companion 同构的能力状态。

        陪伴插件据此判断 ``available`` 与各后端是否就绪；这里把 NAI 直连同时
        映射为 ``nai`` 与 ``external`` 两个后端名，保证陪伴插件里针对
        ``external`` 的可用性检查在 NAI 直连模式下同样成立。
        """
        plugin = self._plugin
        if not bool(getattr(plugin, "enable_companion_link", True)):
            return {
                "installed": True,
                "enabled": False,
                "available": False,
                "reason": "disabled",
                "selected_backend": "nai",
                "backup_external_note": "disabled",
                "backends": {},
            }
        # 只做本地就绪检查，不探测上游：上游可达性探测每次约 1 秒以上，
        # 而陪伴插件会高频轮询本接口，预检会拖慢主动链路；真实失败会由
        # 生图结果以明确的错误信息返回。
        token_ok = bool(plugin.image_gen_key)
        session_ok = plugin._session is not None and not plugin._session.closed
        nai_ready = token_ok and session_ok
        if not token_ok:
            reason = "no_token"
        elif not session_ok:
            reason = "no_session"
        else:
            reason = "ready"
        return {
            "installed": True,
            "enabled": True,
            "available": nai_ready,
            "reason": reason,
            "selected_backend": "nai",
            "backup_external_note": "" if nai_ready else reason,
            "backends": {
                "nai": nai_ready,
                "external": nai_ready,
                "proxy": plugin.proxy_runner is not None,
            },
        }

    async def maintenance(self, owner: Any) -> dict[str, Any]:
        """清理超过保留天数的直连生图文件，返回清理数量。"""
        plugin = self._plugin
        try:
            retention = int(getattr(plugin, "companion_image_retention_days", 30) or 0)
        except (TypeError, ValueError):
            retention = 30
        if retention <= 0:
            return {"removed_files": 0, "note": "未启用按天清理"}
        image_dir = self._companion_image_dir()
        cutoff = time.time() - retention * 86400
        try:
            removed = await asyncio.to_thread(self._cleanup_expired_images, image_dir, cutoff)
        except Exception as exc:
            logger.warning(f"{_LOG_TAG} 直连生图清理失败: {type(exc).__name__}: {exc}")
            return {"removed_files": 0, "note": f"清理失败: {type(exc).__name__}"}
        return {
            "removed_files": removed,
            "note": f"已清理 {removed} 张超期直连生图" if removed else "无超期文件",
        }

    @staticmethod
    def _cleanup_expired_images(image_dir: Path, cutoff: float) -> int:
        """同步遍历目录并删除早于 cuttoff 的直连生图文件。"""
        if not image_dir.exists():
            return 0
        removed = 0
        for path in image_dir.iterdir():
            if not path.is_file() or not path.name.startswith("nai_"):
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except FileNotFoundError:
                continue
        return removed

    # ==== 生图 ====

    async def generate_for_companion(self, owner: Any, request: dict[str, Any]) -> dict[str, Any]:
        """按陪伴请求契约生成一张图片并返回文件路径。

        对陪伴插件传过来的请求做本土化适配：``prompt_text`` 是需求正文，
        ``prompt_sections`` 是结构化背景信息（穿搭/场景/记忆/构图等，正负向
        分开），两者会被合并成最终提示词直接提交给 NAI；``workflow_kind``
        只用于元数据记录（上游为纯文生图），``size`` / ``ratio`` / ``style``
        按 NAI 参数体系归一化，``reference_image_path`` 会被忽略。

        Returns:
            与 image_companion 同构的 ``{handled, backend, image_path, note, metadata}``。
        """
        plugin = self._plugin
        if not bool(getattr(plugin, "enable_companion_link", True)):
            return {"handled": False, "reason": "disabled"}
        req = dict(request) if isinstance(request, dict) else {}
        # prompt_text 是需求正文；prompt_sections 是陪伴插件整理的背景信息
        # （穿搭、场景、视觉记忆、构图要求等，正/负向分开）。
        raw_prompt = _first_text(req.get("prompt_text"), req.get("prompt"))
        workflow_kind = _first_text(req.get("workflow_kind"), "text2img")[:40]
        session_key = _first_text(req.get("session_key"), req.get("continuity_key"))[:340]
        if _first_text(req.get("reference_image_path")):
            logger.info(
                f"{_LOG_TAG} 上游为纯文生图，忽略参考图 | kind={workflow_kind}"
            )

        prompt_format = self._resolve_prompt_format(req)
        style = self._coerce_style(req.get("style"))
        size = self._coerce_size(_first_text(req.get("size"), req.get("ratio")))

        # 把背景信息与需求合并成最终提示词。两种模式都直接提交给 NAI：
        # nai tag 模式按标签规则归一化；自然语言模式保留英文句子原样，
        # 不做 LLM 转译（新版 NAI 已支持英文自然语言直接生图）。
        background, section_negative = self._collect_prompt_sections(
            req.get("prompt_sections")
        )
        prepared_prompt = self._compose_companion_prompt(
            raw_prompt,
            background,
            prompt_format=prompt_format,
        )
        if not prepared_prompt:
            return {
                "handled": True,
                "backend": "NAI 生图",
                "image_path": "",
                "note": "提示词为空，未发起生图。",
                "metadata": {},
            }
        logger.info(
            f"{_LOG_TAG} 直连生图({prompt_format}) | kind={workflow_kind} "
            f"style={style} size={size} background={len(background)} "
            f"prompt='{prepared_prompt[:60]}...'"
        )

        negative = req.get("negative")
        if not isinstance(negative, str) or not negative.strip():
            negative = section_negative or None

        try:
            # enable_translate=False：提示词已按模式处理完毕，禁止二次转译。
            img_bytes, reason = await plugin._generate_one(
                prepared_prompt,
                style,
                size,
                negative=negative,
                enable_translate=False,
            )
        except Exception as exc:
            logger.exception(
                f"{_LOG_TAG} 直连生图执行失败 | error_type={type(exc).__name__}"
            )
            return {
                "handled": True,
                "backend": "NAI 生图",
                "image_path": "",
                "note": "NAI 生图服务执行失败，请查看 AstrBot 日志。",
                "metadata": {},
            }

        if not img_bytes:
            from .main import _format_generate_error

            note = _format_generate_error(reason)
            self._note_generation(workflow_kind, prompt_format, style, size, "", False, note)
            return {
                "handled": True,
                "backend": "NAI 生图",
                "image_path": "",
                "note": note,
                "metadata": {},
            }

        try:
            image_path = await self._save_companion_image(img_bytes)
        except Exception as exc:
            logger.warning(f"{_LOG_TAG} 直连生图写盘失败: {type(exc).__name__}: {exc}")
            return {
                "handled": True,
                "backend": "NAI 生图",
                "image_path": "",
                "note": "图片已生成但保存失败，请检查插件数据目录权限。",
                "metadata": {},
            }

        metadata = self._build_result_metadata(
            workflow_kind=workflow_kind,
            session_key=session_key,
            prompt=prepared_prompt,
            prompt_format=prompt_format,
            style=style,
            size=size,
            image_path=str(image_path),
        )
        note = "生成完成"
        self._note_generation(workflow_kind, prompt_format, style, size, str(image_path), True, note)
        return {
            "handled": True,
            "backend": "NAI 生图",
            "image_path": str(image_path),
            "note": note,
            "metadata": metadata,
        }

    async def test_endpoint(self, owner: Any, endpoint: dict[str, Any], prompt: str) -> dict[str, Any]:
        """跑一次真实上游生图作为端点诊断，返回 ``{ok, image_path, message}``。

        NAI 上游地址固定，``endpoint`` 中的 ``style`` / ``size`` 会被用于本次
        测试，其余字段忽略。
        """
        plugin = self._plugin
        if not bool(getattr(plugin, "enable_companion_link", True)):
            return {"ok": False, "message": "NAI 直连未启用"}
        text = str(prompt or "").strip()
        if not text:
            return {"ok": False, "message": "测试提示词为空"}
        endpoint_dict = endpoint if isinstance(endpoint, dict) else {}
        style = self._coerce_style(endpoint_dict.get("style"))
        size = self._coerce_size(_first_text(endpoint_dict.get("size"), endpoint_dict.get("ratio")))
        try:
            img_bytes, reason = await plugin._generate_one(
                normalize_prompt(text),
                style,
                size,
                enable_translate=False,
            )
        except Exception as exc:
            logger.warning(f"{_LOG_TAG} 端点测试生图异常: {type(exc).__name__}: {exc}")
            return {"ok": False, "message": f"测试生图异常: {type(exc).__name__}"}
        if not img_bytes:
            from .main import _format_generate_error

            return {"ok": False, "message": _format_generate_error(reason)}
        image_path = await self._save_companion_image(img_bytes)
        return {"ok": True, "image_path": str(image_path), "message": "测试生图成功"}

    # ==== 内部工具 ====

    def _collect_prompt_sections(self, value: Any) -> tuple[list[str], str]:
        """把陪伴插件的背景信息拆成正向背景列表与负向要求串。

        兼容三种形态：结构化分节列表（每个元素带 positive/negative 字段的
        对象或字典）、``{名称: 文本}`` 平铺字典、以及纯字符串。``source``
        或 ``name`` 为 ``user_request`` 的分节是需求本身（已由 prompt_text
        承担），跳过以免重复。
        """
        positives: list[str] = []
        negatives: list[str] = []

        def take(section: Any) -> None:
            if isinstance(section, str):
                text = section.strip()
                if text:
                    positives.append(text)
                return
            source = ""
            positive = ""
            negative = ""
            if isinstance(section, dict):
                if "positive" in section:
                    source = str(section.get("source") or section.get("name") or "")
                    positive = str(section.get("positive") or "").strip()
                    negative = str(section.get("negative") or "").strip()
                else:
                    for key, text in section.items():
                        text = str(text or "").strip()
                        if not text:
                            continue
                        if key == "negative":
                            negatives.append(text)
                        else:
                            positives.append(text)
                    return
            elif section is not None:
                source = str(
                    getattr(section, "source", "")
                    or getattr(section, "name", "")
                    or ""
                )
                positive = str(getattr(section, "positive", "") or "").strip()
                negative = str(getattr(section, "negative", "") or "").strip()
            if positive and source != "user_request":
                positives.append(positive)
            if negative:
                negatives.append(negative)

        if isinstance(value, (list, tuple)):
            for section in value:
                take(section)
        else:
            take(value)
        return positives, ", ".join(negatives)

    def _compose_companion_prompt(
        self,
        requirement: str,
        background: list[str],
        *,
        prompt_format: str,
    ) -> str:
        """把背景信息与需求合并为直接提交 NAI 的提示词。

        nai tag 模式按标签规则归一化（换行折成逗号）；自然语言模式保留英文
        句子结构，仅折叠空白，不做 LLM 转译。有背景时用 Background /
        Requirements 分段，帮助模型区分上下文与要求；没有背景时需求原样提交。
        """
        if prompt_format == _PROMPT_FORMAT_NAI:
            if requirement:
                return normalize_prompt(requirement)
            return normalize_prompt(", ".join(text for text in background if text))
        if background:
            parts = ["Background: " + ". ".join(text.rstrip(".") for text in background)]
            if requirement:
                parts.append("Requirements: " + requirement.rstrip("."))
            return _WHITESPACE_RE.sub(" ", ". ".join(parts) + ".").strip()
        if requirement:
            return _WHITESPACE_RE.sub(" ", requirement).strip()
        return ""

    def _resolve_prompt_format(self, request: dict[str, Any]) -> str:
        """决定本次直连请求的提示词模式。

        请求里的 ``prompt_format`` 优先：含 ``nai`` 视为标签模式，其余非空值
        （natural_language / traditional 等）视为自然语言模式；未传时使用插件
        配置的 ``companion_prompt_format``。
        """
        raw = _first_text(request.get("prompt_format")).casefold()
        if raw:
            return _PROMPT_FORMAT_NAI if "nai" in raw else _PROMPT_FORMAT_NATURAL
        configured = str(getattr(self._plugin, "companion_prompt_format", "") or "").casefold()
        if "nai" in configured:
            return _PROMPT_FORMAT_NAI
        return _PROMPT_FORMAT_NATURAL

    def _coerce_style(self, value: Any) -> str:
        """把陪伴风格值归一化为 NAI 风格键，未命中回退插件默认风格。"""
        plugin = self._plugin
        text = str(value or "").strip()
        if not text:
            return plugin.image_style
        key = normalize_image_style(text)
        if key:
            return key
        return plugin.image_style

    def _coerce_size(self, value: str) -> str:
        """把陪伴尺寸/比例值归一化为 NAI 尺寸键，未命中回退插件默认尺寸。"""
        plugin = self._plugin
        text = str(value or "").strip()
        if not text:
            return plugin.image_size
        key = normalize_image_size(text)
        if key:
            return key
        ratio = _COMPANION_RATIO_SIZES.get(text.replace(" ", "").casefold())
        if ratio:
            return ratio
        match = _WH_SIZE_RE.match(text)
        if match:
            width = int(match.group(1))
            height = int(match.group(2))
            if width > 0 and height > 0:
                base = (
                    "方图"
                    if abs(width - height) <= max(width, height) * 0.08
                    else ("竖图" if height > width else "横图")
                )
                # 数值尺寸只升到 2K，避免陪伴流程误触高成本档位。
                if min(width, height) >= 1920:
                    base = "2K" + base
                return base
        return plugin.image_size

    def _companion_image_dir(self) -> Path:
        """直连生图文件保存目录。"""
        return StarTools.get_data_dir("astrbot_plugin_nai_image") / "companion_images"

    async def _save_companion_image(self, img_bytes: bytes) -> Path:
        """把生成结果写入直连目录（临时文件 + 原子替换）。"""
        image_dir = self._companion_image_dir()
        await asyncio.to_thread(image_dir.mkdir, parents=True, exist_ok=True)
        extension = self._plugin._image_history_extension(img_bytes)
        image_path = image_dir / f"nai_{time.time_ns()}{extension}"
        temp_path = image_dir / f".{image_path.name}.tmp"
        try:
            await asyncio.to_thread(temp_path.write_bytes, img_bytes)
            await asyncio.to_thread(temp_path.replace, image_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        logger.info(f"{_LOG_TAG} 直连生图已保存 | path={image_path} bytes={len(img_bytes)}")
        return image_path

    def _build_result_metadata(
        self,
        *,
        workflow_kind: str,
        session_key: str,
        prompt: str,
        prompt_format: str,
        style: str,
        size: str,
        image_path: str,
    ) -> dict[str, Any]:
        """构造陪伴侧可读的结果元数据（与 image_companion 常用字段对齐）。"""
        return {
            "schema_version": 1,
            "session": session_key,
            "kind": workflow_kind,
            "prompt": prompt[:900],
            "prompt_format": prompt_format,
            "style": style,
            "size": size,
            "path": image_path,
            "ts": time.time(),
            "trace": f"nai-{time.time_ns():x}"[:40],
            "generation_completed": True,
        }

    def _note_generation(
        self,
        workflow_kind: str,
        prompt_format: str,
        style: str,
        size: str,
        image_path: str,
        success: bool,
        note: str,
    ) -> None:
        """更新直连生图计数与最近一次生图快照。"""
        self.generation_count += 1
        self.last_generation = {
            "workflow_kind": workflow_kind,
            "prompt_format": prompt_format,
            "style": style,
            "size": size,
            "success": bool(success),
            "image_path": image_path,
            "note": note[:500],
            "ts": time.time(),
        }
