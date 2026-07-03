import asyncio
import base64
import time
from typing import Optional

import aiohttp
from aiohttp import web
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as Img, Plain
from astrbot.api.star import Context, Star, register

LOG_TAG = "[NAI-Image]"

IMAGE_GEN_BASE_URL_DEFAULT = "https://nai.sta1n.cn"
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8765

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
}

DEFAULT_ARTISTS = {
    "vertical": "masterpiece, best quality,[[[artist:dishwasher1910]]], {{yd_(orange_maru)}}, [artist:ciloranko], [artist:sho_(sho_lwlw)], [ningen mame], soft lighting,year 2024",
    "comicDoujin": "masterpiece,best quality,ultra detailed,by 小田武士,by 内尾和正,by あずーる,TV anime screencap,clean cel shading,soft lineart,subtle bloom glow",
    "r18": (
        "20::best quality, absurdres, very aesthetic, detailed, masterpiece::, 20::highly finished::, "
        "10::ultra detailed::, 5::masterpiece::, 5::best quality::, "
        "2.4::kidmo::, 1.2::omone hokoma agm::, 1.1::dino, wanke, liduke::, "
        "0.8::rurudo, mignon, artist:pottsness, artist:toosaka asagi::, 0.7::misaka_12003-gou::, "
        "0.6::artist:chocoan, artist:ciloranko, artist:rhasta, artist:sho_sho_lwlw::, "
        "dino_(dinoartforame), agoto, akakura, "
        "year 2025, textless version, no text, The image is highly intricate finished drawn. "
        "1.35::A highly finished photo-style artwork that has graphic texture, realistic skin surface, "
        "and lifelike flesh with little obliques::, smooth line, glossy skin, realistic, 4k, "
        "1.63::photorealistic::, 1.63::photo(medium)::, 3::simple background::, 2::depth of field::, "
        "1.5::vivid color, lively color::, desaturated, muted tones, cinematic desaturation, "
        "pale aesthetic, silver-toned, -2::green::, -1.5::vibrant, colorful, saturated::"
    ),
    "lolita25d": (
        "20::best quality, absurdres, very aesthetic, detailed, masterpiece::, 20::highly finished::, "
        "10::ultra detailed::, 5::masterpiece::, 5::best quality::, "
        "2.4::kidmo::, 1.2::omone hokoma agm::, 1.1::dino, wanke, liduke::, "
        "0.8::rurudo, mignon, artist:pottsness, artist:toosaka asagi::, 0.7::misaka_12003-gou::, "
        "0.6::artist:chocoan, artist:ciloranko, artist:rhasta, artist:sho_sho_lwlw::, "
        "dino_(dinoartforame), agoto, akakura, "
        "0.9::rurudo(Only body shape), mignon(Only body shape)::, "
        "year 2025, textless version, {{petite,loli}}, Petite figure, no text, "
        "1.35::A highly finished photo-style artwork that has graphic texture, realistic skin surface, "
        "and lifelike flesh with little obliques::, smooth line, glossy skin, realistic, 4k, "
        "1.63::photorealistic::, 1.63::photo(medium)::, 3::simple background::, 2::depth of field::, "
        "1.5::vivid color, lively color::, desaturated, muted tones, "
        "-2::green::, -1.5::vibrant, colorful, saturated::"
    ),
    "anime": (
        "1.4::asanagi::,{{{{{artist:asanagi}}}}},1.2::xiaoluo_xl::,1.3::Artist: misaka_12003-gou::,"
        "1.2::Artist:shexyo::,0.7::Artist:b.sa_(bbbs)::,1::Artist:qiandaiyiyu::,"
        "1.05::artist:natedecock::,1.05::artist:kunaboto::,0.75::artist:kandata_nijou::,"
        "1.05::artist:zer0.zer0::,1.05::artist:jasony::,0.75::misaka_12003-gou::, "
        "dino_(dinoartforame), wanke, liduke, year 2025, realistic, 4k, -2::green::, "
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


def _format_generate_error(reason: str) -> str:
    """把 _generate_one 返回的 reason 翻译成给用户的中文报错。"""
    return {
        "no_token": "❌ 插件未配置 image_gen_key，请先在插件管理面板填入 token。",
        "no_session": "❌ 插件 session 未初始化，请重载插件。",
        "timeout": "⏱ 生图超时（超过 180 秒）。可能原因：nai.sta1n.cn 服务繁忙、提示词过长、或网络不稳。",
        "http_4xx": "🚫 上游返回 4xx。常见原因：token 无效、提示词含敏感词、或参数不合法。",
        "http_5xx": "🔥 上游返回 5xx。nai.sta1n.cn 服务器内部错误，请稍后重试。",
        "http_other": "⚠️ 上游返回非预期状态码。",
        "empty_response": "📭 上游返回 200 但内容为空，可能是接口限流或临时异常。",
        "exception": "💥 生图过程发生未捕获异常，请查看 AstrBot 日志获取详情。",
    }.get(reason, f"❓ 生图失败（原因: {reason}）")


def _parse_args(text: str) -> dict:
    import re

    args = {"prompt": "", "n": None, "style": None, "size": None}
    flags = re.findall(r"--(\w+)=([^\s]+)", text)
    for k, v in flags:
        if k in args:
            args[k] = v
    prompt = re.sub(r"--\w+=[^\s]+", "", text).strip()
    args["prompt"] = prompt
    return args


@register("astrbot_plugin_nai_image", "缪缪的小水泡", "基于 nai.sta1n.cn 的 NovelAI 生图插件", "1.0.0")
class NAIGenerateImagePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        logger.info(f"{LOG_TAG} [init] 插件实例化开始")
        logger.debug(f"{LOG_TAG} [init] config keys: {list(config.keys())}")

        self.base_url: str = (config.get("base_url") or IMAGE_GEN_BASE_URL_DEFAULT).strip() or IMAGE_GEN_BASE_URL_DEFAULT
        self.image_gen_key: str = (config.get("image_gen_key") or "").strip()
        self.image_style: str = config.get("image_style") or "vertical"
        self.image_size: str = config.get("image_size") or "竖图"
        try:
            self.image_count: int = max(1, min(6, int(config.get("image_count") or 2)))
        except (TypeError, ValueError):
            self.image_count = 2
        self.custom_artists: str = config.get("custom_artists") or ""
        self.model: str = config.get("model") or "nai-diffusion-4-5-full"
        try:
            self.steps: int = int(config.get("steps") or 40)
        except (TypeError, ValueError):
            self.steps = 40
        try:
            self.scale: int = int(config.get("scale") or 6)
        except (TypeError, ValueError):
            self.scale = 6
        try:
            self.cfg_value: int = int(config.get("cfg") or 0)
        except (TypeError, ValueError):
            self.cfg_value = 0
        self.sampler: str = config.get("sampler") or "k_dpmpp_2m_sde"
        self.noise_schedule: str = config.get("noise_schedule") or "karras"
        neg = config.get("negative")
        self.negative: str = neg if neg else DEFAULT_NEGATIVE
        self.enable_template: bool = bool(config.get("enable_template", True))
        self.character_preset: str = (config.get("character_preset") or "").strip()
        self._session: Optional[aiohttp.ClientSession] = None
        self.proxy_runner: Optional[web.AppRunner] = None
        self.proxy_port: int = int(config.get("proxy_port") or PROXY_PORT)

        logger.info(
            f"{LOG_TAG} [init] 配置加载完成 | "
            f"token={'已配置' if self.image_gen_key else '未配置'} | "
            f"base_url={self.base_url} | "
            f"style={self.image_style} | size={self.image_size} | "
            f"count={self.image_count} | model={self.model} | "
            f"steps={self.steps} scale={self.scale} cfg={self.cfg_value} | "
            f"template={'启用' if self.enable_template and self.character_preset else '未启用'} | "
            f"proxy_port={self.proxy_port}"
        )

    def _build_full_prompt(self, user_prompt: str) -> str:
        if not self.enable_template or not self.character_preset:
            return user_prompt.strip()
        return f"{self.character_preset}, {user_prompt.strip()}"

    async def initialize(self):
        logger.info(f"{LOG_TAG} [initialize] 阶段开始")
        try:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180))
            logger.info(f"{LOG_TAG} [initialize] aiohttp session 创建成功 (timeout=180s)")
        except Exception as e:
            logger.error(f"{LOG_TAG} [initialize] aiohttp session 创建失败: {e!r}")
            return

        try:
            await self._start_proxy_server()
        except Exception as e:
            logger.error(f"{LOG_TAG} [initialize] 代理服务器启动失败: {e!r}")

        logger.info(
            f"{LOG_TAG} [initialize] 阶段完成 | token={'OK' if self.image_gen_key else 'MISSING'}"
        )

    async def terminate(self):
        logger.info(f"{LOG_TAG} [terminate] 阶段开始")
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
        logger.info(f"{LOG_TAG} [terminate] 阶段完成")

    def _resolve_artists(self, style: str) -> str:
        if style == "custom":
            return self.custom_artists or DEFAULT_ARTISTS.get("vertical", "")
        return DEFAULT_ARTISTS.get(style, DEFAULT_ARTISTS["vertical"])

    def _resolve_size(self, size: str) -> str:
        return IMAGE_SIZES.get(size, "portrait")

    async def _check_status(self) -> tuple[bool, int]:
        logger.info(f"{LOG_TAG} [status] 开始检查 {self.base_url}")
        if not self._session:
            logger.warning(f"{LOG_TAG} [status] session 未初始化")
            return False, -1
        start = time.perf_counter()
        try:
            async with self._session.get(
                self.base_url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                latency = int((time.perf_counter() - start) * 1000)
                logger.info(
                    f"{LOG_TAG} [status] 可用 | status={resp.status} latency={latency}ms"
                )
                return True, latency
        except Exception as e:
            logger.warning(f"{LOG_TAG} [status] 检查失败: {e!r}")
            return False, -1

    async def _fetch_quota(self) -> Optional[int]:
        if not self.image_gen_key or not self._session:
            logger.warning(f"{LOG_TAG} [quota] 跳过：token 或 session 缺失")
            return None
        url = f"{self.base_url.rstrip('/')}/api/api/getUser"
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
                    logger.warning(f"{LOG_TAG} [quota] 非 200 响应: {resp.status}")
                    return None
                data = await resp.json()
                logger.debug(f"{LOG_TAG} [quota] response data: {data}")
                if data.get("status") == "ok" and data.get("type") == "sta1n":
                    val = int(data.get("data", {}).get("value", 0))
                    logger.info(f"{LOG_TAG} [quota] 查询成功 | 剩余 {val}")
                    return val
                logger.warning(f"{LOG_TAG} [quota] 响应不符合预期: {data}")
                return None
        except Exception as e:
            logger.warning(f"{LOG_TAG} [quota] 请求异常: {e!r}")
            return None

    async def _generate_one(
        self, prompt: str, style: str, size: str
    ) -> tuple[Optional[bytes], str]:
        """生成单张图片。

        Returns:
            (img_bytes_or_None, reason)
            reason 取值: "ok" / "no_token" / "no_session" /
                        "timeout" / "http_4xx" / "http_5xx" / "http_other" /
                        "empty_response" / "exception"
        """
        if not self.image_gen_key:
            logger.warning(f"{LOG_TAG} [generate] 跳过：token 未配置")
            return None, "no_token"
        if not self._session:
            logger.warning(f"{LOG_TAG} [generate] 跳过：session 未初始化")
            return None, "no_session"
        artists = self._resolve_artists(style)
        from urllib.parse import quote

        full_prompt = self._build_full_prompt(prompt)
        logger.info(
            f"{LOG_TAG} [generate] 开始 | style={style} size={size} | "
            f"prompt(原始)='{prompt[:60]}...' "
            f"prompt(模板后,前60字)='{full_prompt[:60]}...'"
        )
        logger.debug(f"{LOG_TAG} [generate] full_prompt(完整) = {full_prompt!r}")
        logger.debug(f"{LOG_TAG} [generate] artists = {artists!r}")

        url = (
            f"{self.base_url.rstrip('/')}/generate"
            f"?tag={quote(full_prompt)}"
            f"&token={self.image_gen_key}"
            f"&model={self.model}"
            f"&artist={quote(artists)}"
            f"&size={size}"
            f"&steps={self.steps}"
            f"&scale={self.scale}"
            f"&cfg={self.cfg_value}"
            f"&sampler={self.sampler}"
            f"&negative={quote(self.negative)}"
            f"&nocache=0"
            f"&noise_schedule={self.noise_schedule}"
        )
        logger.debug(f"{LOG_TAG} [generate] request url = {url}")

        start = time.perf_counter()
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=180)
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
                return img_bytes, "ok"
        except asyncio.TimeoutError:
            logger.warning(
                f"{LOG_TAG} [generate] 超时 (>{180}s) | prompt='{full_prompt[:60]}...'"
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
        await self.proxy_runner.setup()
        site = web.TCPSite(self.proxy_runner, PROXY_HOST, self.proxy_port)
        await site.start()
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
        logger.info(f"{LOG_TAG} [proxy:gen] 收到 POST {request.path} from {request.remote}")
        if not self.image_gen_key or not self._session:
            logger.warning(f"{LOG_TAG} [proxy:gen] 拒绝：token 或 session 缺失")
            return web.json_response(
                {"error": {"message": "NAI 插件未配置 image_gen_key", "type": "invalid_request_error"}},
                status=400,
            )
        try:
            body = await request.json()
            logger.debug(f"{LOG_TAG} [proxy:gen] body keys: {list(body.keys())}")
        except Exception as e:
            logger.warning(f"{LOG_TAG} [proxy:gen] JSON 解析失败: {e!r}")
            return web.json_response(
                {"error": {"message": f"invalid json: {e!r}", "type": "invalid_request_error"}},
                status=400,
            )
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            logger.warning(f"{LOG_TAG} [proxy:gen] prompt 为空")
            return web.json_response(
                {"error": {"message": "prompt is required", "type": "invalid_request_error"}},
                status=400,
            )
        size = body.get("size") or "1024x1024"
        n = max(1, min(4, int(body.get("n") or 1)))
        logger.info(
            f"{LOG_TAG} [proxy:gen] 参数 | prompt='{prompt[:80]}' size={size} n={n}"
        )

        try:
            img_bytes, reason = await self._generate_one(prompt, self.image_style, size)
        except Exception as e:
            logger.warning(f"{LOG_TAG} [proxy:gen] _generate_one 异常: {e!r}")
            return web.json_response(
                {"error": {"message": f"generate exception: {e!r}", "type": "internal_error"}},
                status=500,
            )

        if not img_bytes:
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

        b64 = base64.b64encode(img_bytes).decode()
        logger.info(
            f"{LOG_TAG} [proxy:gen] 响应 | img_bytes={len(img_bytes)} "
            f"b64_chars={len(b64)} n={n}"
        )
        return web.json_response(
            {
                "created": int(time.time()),
                "data": [{"b64_json": b64} for _ in range(n)],
            }
        )

    async def _proxy_handle_edits(self, request: web.Request):
        logger.info(f"{LOG_TAG} [proxy:edit] 收到 POST {request.path} from {request.remote}")
        if not self.image_gen_key or not self._session:
            logger.warning(f"{LOG_TAG} [proxy:edit] 拒绝：token 或 session 缺失")
            return web.json_response(
                {"error": {"message": "NAI 插件未配置 image_gen_key", "type": "invalid_request_error"}},
                status=400,
            )
        prompt = ""
        size = "1024x1024"
        n = 1
        parts_seen: list[str] = []
        try:
            reader = await request.multipart()
            async for part in reader:
                if part.name is None:
                    continue
                parts_seen.append(part.name)
                if part.name == "prompt":
                    prompt = (await part.text()).strip()
                elif part.name == "size":
                    raw_size = (await part.text() or "").strip()
                    if raw_size:
                        size = raw_size
                elif part.name == "n":
                    try:
                        n = max(1, min(4, int((await part.text() or "").strip())))
                    except Exception:
                        n = 1
                elif part.name in ("image", "mask", "image[]", "mask[]"):
                    await part.read()
            logger.debug(f"{LOG_TAG} [proxy:edit] multipart parts: {parts_seen}")
        except Exception as e:
            logger.warning(f"{LOG_TAG} [proxy:edit] multipart 解析失败: {e!r}")
            return web.json_response(
                {"error": {"message": f"invalid multipart: {e!r}", "type": "invalid_request_error"}},
                status=400,
            )
        if not prompt:
            logger.warning(f"{LOG_TAG} [proxy:edit] prompt 为空")
            return web.json_response(
                {"error": {"message": "prompt is required", "type": "invalid_request_error"}},
                status=400,
            )
        logger.info(
            f"{LOG_TAG} [proxy:edit] 降级到纯文生图 | prompt='{prompt[:80]}' "
            f"size={size} n={n} (参考图已丢弃)"
        )

        try:
            img_bytes, reason = await self._generate_one(prompt, self.image_style, size)
        except Exception as e:
            logger.warning(f"{LOG_TAG} [proxy:edit] _generate_one 异常: {e!r}")
            return web.json_response(
                {"error": {"message": f"generate exception: {e!r}", "type": "internal_error"}},
                status=500,
            )
        if not img_bytes:
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

        b64 = base64.b64encode(img_bytes).decode()
        logger.info(
            f"{LOG_TAG} [proxy:edit] 响应 | img_bytes={len(img_bytes)} "
            f"b64_chars={len(b64)} n={n}"
        )
        return web.json_response(
            {
                "created": int(time.time()),
                "data": [{"b64_json": b64} for _ in range(n)],
            }
        )

    @filter.command("image")
    async def image(self, event: AstrMessageEvent):
        text = event.message_str or ""
        sender = event.get_sender_id() if hasattr(event, "get_sender_id") else "?"
        logger.info(f"{LOG_TAG} [cmd:image] 收到指令 | sender={sender} | text='{text[:100]}'")

        if not text.strip():
            logger.info(f"{LOG_TAG} [cmd:image] 提示用法 (空指令)")
            yield event.plain_result(
                "用法: /image <提示词> [--n=1-6] [--style=vertical|comicDoujin|r18|lolita25d|anime|galgame|custom] [--size=竖图|横图|方图]"
            )
            return

        args = _parse_args(text)
        logger.info(f"{LOG_TAG} [cmd:image] 解析参数: {args}")
        prompt = args["prompt"]
        if not prompt:
            logger.info(f"{LOG_TAG} [cmd:image] prompt 为空")
            yield event.plain_result("请提供提示词。")
            return

        if not self.image_gen_key:
            logger.warning(f"{LOG_TAG} [cmd:image] token 未配置")
            yield event.plain_result("未配置 image_gen_key，请先在插件配置中填写 token。")
            return

        try:
            n = int(args["n"]) if args["n"] else self.image_count
        except (TypeError, ValueError):
            n = self.image_count
        n = max(1, min(6, n))

        style = args["style"] or self.image_style
        size_cn = args["size"] or self.image_size
        size = self._resolve_size(size_cn)

        if style not in IMAGE_STYLES and style != "custom":
            logger.warning(f"{LOG_TAG} [cmd:image] 未知风格: {style}")
            yield event.plain_result(
                f"未知风格: {style}\n可选: {', '.join(IMAGE_STYLES.keys())}"
            )
            return

        logger.info(
            f"{LOG_TAG} [cmd:image] 最终参数 | style={style} size_cn={size_cn} "
            f"size_eng={size} n={n}"
        )
        yield event.plain_result(
            f"提示词: {prompt}\n风格: {IMAGE_STYLES.get(style, style)}，比例: {size_cn}，共 {n} 张"
        )

        success = 0
        first_reason: Optional[str] = None
        for i in range(n):
            logger.info(f"{LOG_TAG} [cmd:image] 生成第 {i + 1}/{n} 张")
            img_bytes, reason = await self._generate_one(prompt, style, size)
            if img_bytes:
                success += 1
                logger.info(
                    f"{LOG_TAG} [cmd:image] 第 {i + 1}/{n} 张发送 | bytes={len(img_bytes)}"
                )
                yield event.chain_result(
                    [
                        Plain(f"[{i + 1}/{n}]"),
                        Img.fromBytes(img_bytes),
                    ]
                )
            else:
                if first_reason is None:
                    first_reason = reason
                logger.warning(
                    f"{LOG_TAG} [cmd:image] 第 {i + 1}/{n} 张失败 | reason={reason}"
                )
                yield event.plain_result(f"第 {i + 1}/{n} 张生成失败：{_format_generate_error(reason)}")

        if success == 0:
            logger.error(
                f"{LOG_TAG} [cmd:image] 全部 {n} 张失败 | first_reason={first_reason}"
            )
            yield event.plain_result(
                f"全部 {n} 张图片生成失败。\n{_format_generate_error(first_reason or 'unknown')}"
            )
        else:
            logger.info(f"{LOG_TAG} [cmd:image] 完成 | 成功 {success}/{n}")

    @filter.command("quota")
    async def quota(self, event: AstrMessageEvent):
        sender = event.get_sender_id() if hasattr(event, "get_sender_id") else "?"
        logger.info(f"{LOG_TAG} [cmd:quota] 收到指令 | sender={sender}")
        if not self.image_gen_key:
            logger.warning(f"{LOG_TAG} [cmd:quota] token 未配置")
            yield event.plain_result("未配置 image_gen_key。")
            return
        yield event.plain_result("正在查询配额...")
        val = await self._fetch_quota()
        if val is None:
            logger.warning(f"{LOG_TAG} [cmd:quota] 查询失败")
            yield event.plain_result("配额查询失败，请检查 token 或网络。")
        else:
            logger.info(f"{LOG_TAG} [cmd:quota] 返回 {val}")
            yield event.plain_result(f"剩余配额: {val}")

    @filter.command("imgstatus")
    async def imgstatus(self, event: AstrMessageEvent):
        sender = event.get_sender_id() if hasattr(event, "get_sender_id") else "?"
        logger.info(f"{LOG_TAG} [cmd:imgstatus] 收到指令 | sender={sender}")
        yield event.plain_result("正在检查生图服务...")
        ok, latency = await self._check_status()
        if ok:
            yield event.plain_result(f"生图服务可用，延迟约 {latency}ms")
        else:
            yield event.plain_result("生图服务不可用，请稍后重试。")

    @filter.on_decorating_result()
    async def auto_generate_for_companion(self, event: AstrMessageEvent):
        logger.debug(f"{LOG_TAG} [hook:on_decorating_result] 进入 (空实现)")
        return

    def _save_companion_image(self, img_bytes: bytes, prompt: str) -> Optional[str]:
        try:
            import hashlib
            from datetime import datetime
            from pathlib import Path

            save_dir = Path("./data/companion_images")
            save_dir.mkdir(parents=True, exist_ok=True)

            name = (
                f"companion_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                f"_{hashlib.md5(prompt.encode()).hexdigest()[:8]}.jpg"
            )
            save_path = save_dir / name
            save_path.write_bytes(img_bytes)
            return str(save_path)
        except Exception as e:
            logger.warning(f"{LOG_TAG} [companion:save] 保存图片失败: {e}")
            return None
