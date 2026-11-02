"""Opt-in adapter for Starbridge's native NovelAI image endpoints."""

import asyncio
import base64
import copy
import io
import json
import math
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

MODELS = {
    "nai-diffusion-4-5-full",
    "nai-diffusion-4-5-curated",
    "nai-diffusion-5-full",
    "nai-diffusion-5-curated",
}
MODEL_ALIASES = {
    "nai45": "nai-diffusion-4-5-full",
    "nai45-curated": "nai-diffusion-4-5-curated",
    "nai5": "nai-diffusion-5-full",
    "nai5-curated": "nai-diffusion-5-curated",
}
REFERENCE_MODELS = {"nai-diffusion-4-5-full", "nai-diffusion-4-5-curated"}
SAMPLERS = {
    "k_euler_ancestral",
    "k_euler",
    "k_dpmpp_2s_ancestral",
    "k_dpmpp_2m_sde",
    "k_dpmpp_2m",
    "k_dpmpp_sde",
    "k_dpm_2",
    "k_dpm_2_ancestral",
    "k_lms",
}
NOISE_SCHEDULES = {"native", "karras", "exponential", "polyexponential"}
MAX_PIXELS = 3_145_728
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
TOOL_ACTIONS = {"bg-removal", "lineart", "sketch", "colorize", "emotion", "declutter"}
EMOTIONS = set(
    "neutral happy sad angry scared surprised tired excited nervous thinking confused shy disgusted smug bored laughing irritated aroused embarrassed worried love determined hurt playful".split()
)


class StarbridgeError(ValueError):
    """A request cannot be submitted safely using the native contract."""


@dataclass
class StarbridgeRequest:
    endpoint: str
    payload: dict


def native_endpoint(base_url: str, operation: str) -> str:
    parsed = urlsplit(base_url.strip())
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise StarbridgeError("请填写不含查询参数或登录信息的站点地址。")
    path = parsed.path.rstrip("/")
    for suffix in ("/v1/images/generations", "/v1/images/edits", "/v1"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/ai/" + operation, "", ""))


def native_error(body: object, status: int) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if body.get("message"):
            return str(body["message"])
    return f"HTTP {status}"


def _image(value: str, size: tuple[int, int] | None = None, *, precise=False) -> str:
    from PIL import Image

    try:
        if value.startswith("data:"):
            value = value.split(",", 1)[1]
        if len(value) > ((MAX_REFERENCE_BYTES + 2) // 3) * 4:
            raise ValueError("Reference exceeds 20 MB")
        raw = base64.b64decode(value, validate=True)
        if len(raw) > MAX_REFERENCE_BYTES:
            raise ValueError("Reference exceeds 20 MB")
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Unsupported reference format")
            if (
                getattr(source, "n_frames", 1) != 1
                or source.width * source.height > 36_000_000
            ):
                raise ValueError("Animated or oversized reference")
            source.load()
            image = source.convert("RGB")
        if precise:
            if image.width > image.height * 1.2:
                size = (1536, 1024)
            elif image.height > image.width * 1.2:
                size = (1024, 1536)
            else:
                size = (1472, 1472)
            canvas = Image.new("RGB", size, "black")
            scale = min(size[0] / image.width, size[1] / image.height)
            image = image.resize(
                (
                    max(1, round(image.width * scale)),
                    max(1, round(image.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )
            canvas.paste(
                image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2)
            )
            image = canvas
        elif size and image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="PNG")
        if output.tell() > MAX_REFERENCE_BYTES:
            raise ValueError("Prepared reference exceeds 20 MB")
        return base64.b64encode(output.getvalue()).decode("ascii")
    except (
        ValueError,
        IndexError,
        OSError,
        TypeError,
        AttributeError,
        Image.DecompressionBombError,
    ) as error:
        raise StarbridgeError(
            "参考图必须是有效的静态 PNG、JPEG 或 WebP 图片，且不超过 20 MB。"
        ) from error


def _validate_generation(params: dict, prompt: str, width: int, height: int) -> None:
    """Check the site's generation contract before paid reference encoding."""
    if width < 64 or height < 64 or width % 64 or height % 64:
        raise StarbridgeError("生图宽高必须至少为 64，且为 64 的整数倍。")
    if width * height > MAX_PIXELS:
        raise StarbridgeError("星桥生图面积不能超过 3145728 像素，请减小尺寸。")
    if params.get("sampler", "k_euler_ancestral") not in SAMPLERS:
        raise StarbridgeError(
            "当前采样器不受 V4.5/V5 支持，请选择 Euler、DPM++、DPM2 或 LMS。"
        )
    if params.get("noise_schedule", "karras") not in NOISE_SCHEDULES:
        raise StarbridgeError(
            "噪声计划请选择 native、karras、exponential 或 polyexponential。"
        )
    texts = [prompt, params.get("negative_prompt", "")]
    for key in ("v4_prompt", "v4_negative_prompt"):
        caption = params.get(key, {}).get("caption", {})
        texts.append(caption.get("base_caption", ""))
        texts.extend(
            item.get("char_caption", "") for item in caption.get("char_captions", [])
        )
    if any(
        isinstance(text, str) and len(text.encode("utf-8")) >= 51200 for text in texts
    ):
        raise StarbridgeError(
            "每段正负提示词和角色提示词必须小于 51200 个 UTF-8 字节。"
        )


async def prepare_starbridge_request(
    session,
    base_url: str,
    payload: dict,
    *,
    headers: dict,
    timeout,
    image_model: str | None = None,
    reference_model: str | None = None,
) -> StarbridgeRequest:
    """Validate all inputs before performing any billable Vibe encoding."""
    endpoint = native_endpoint(base_url, "generate-image")
    model = (
        image_model
        if payload.get("model") == "director-tools"
        else payload.get("model")
    )
    model = MODEL_ALIASES.get(model, model)
    if model not in MODELS:
        raise StarbridgeError(
            "星桥适配仅支持 V4.5/V5 的 Full、Curated 生图型号，请检查模型设置。"
        )
    try:
        width, height = (int(value) for value in payload["size"].split("x", 1))
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise StarbridgeError("请使用有效的像素尺寸，例如 832x1216。") from error
    count = payload.get("n", 1)
    maximum = 6 if width * height <= 393216 else 4
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 1 <= count <= maximum
    ):
        raise StarbridgeError(
            f"当前尺寸每次支持 1–{maximum} 张，请调整生成数量或尺寸。"
        )
    params = copy.deepcopy(payload.get("parameters", {}))
    action = payload.get("action", "generate")
    prompt = payload.get("prompt", "")
    if payload.get("model") == "director-tools":
        from PIL import Image

        if action not in TOOL_ACTIONS or count != 1:
            raise StarbridgeError("图片处理请选择支持的动作，并将生成数量设为 1。")
        image_data = await asyncio.to_thread(_image, params.get("image", ""))
        with Image.open(io.BytesIO(base64.b64decode(image_data))) as image:
            width, height = image.size
        if action == "emotion":
            prefix, _, detail = prompt.partition(";;")
            artists, comma, mood = prefix.rpartition(",")
            mood = (mood if comma else prefix).strip()
            if mood in EMOTIONS:
                prompt = (
                    mood
                    + ";;"
                    + ", ".join(part for part in (artists.strip(), detail) if part)
                )
            else:
                prompt = "neutral;;" + prompt
        body = {
            "model": model,
            "req_type": action,
            "image": image_data,
            "width": width,
            "height": height,
            "prompt": prompt,
        }
        if action in {"colorize", "emotion"}:
            body["defry"] = 0
        return StarbridgeRequest(native_endpoint(base_url, "augment-image"), body)

    precise = params.get("director_reference_images", [])
    vibes = params.get("reference_image_multiple", [])
    if precise and vibes:
        raise StarbridgeError("精准参考和 Vibe 不能在同一次请求中使用。")
    if model.startswith("nai-diffusion-5-") and (precise or vibes):
        if not reference_model:
            raise StarbridgeError(
                "V5 暂不支持精准参考或 Vibe；请选择 V4.5，或在设置中指定「V5 参考模型」。图生图可继续使用 V5。"
            )
        reference_model = MODEL_ALIASES.get(reference_model, reference_model)
        if reference_model not in REFERENCE_MODELS:
            raise StarbridgeError(
                "V5 参考模型请选择 nai-diffusion-4-5-full 或 nai-diffusion-4-5-curated。"
            )
        model = reference_model
    for required, optional in (
        (
            (
                "reference_image_multiple",
                "reference_strength_multiple",
                "reference_information_extracted_multiple",
            ),
            (),
        ),
        (
            (
                "director_reference_images",
                "director_reference_descriptions",
                "director_reference_strength_values",
                "director_reference_information_extracted",
            ),
            ("director_reference_secondary_strength_values",),
        ),
    ):
        keys = required + optional
        arrays = [params[key] for key in keys if key in params]
        if arrays and (
            any(not isinstance(value, list) for value in arrays)
            or len(arrays[0]) > 8
            or any(len(value) != len(arrays[0]) for value in arrays)
            or bool(arrays[0])
            and any(key not in params for key in required)
        ):
            raise StarbridgeError("参考图及逐图参数必须按顺序对应，且最多 8 张。")
        for key in keys:
            if "strength" in key or "information_extracted" in key:
                for amount in params.get(key, []):
                    if (
                        isinstance(amount, bool)
                        or not isinstance(amount, (int, float))
                        or not math.isfinite(amount)
                        or not 0 <= amount <= 1
                    ):
                        raise StarbridgeError(
                            "参考图的信息提取和强度必须是 0 到 1 之间的有效数值。"
                        )
    for description in params.get("director_reference_descriptions", []):
        caption = description.get("caption") if isinstance(description, dict) else None
        if (
            not isinstance(caption, dict)
            or not isinstance(caption.get("base_caption"), str)
            or not caption["base_caption"].strip()
        ):
            raise StarbridgeError("精准参考描述必须包含 caption.base_caption。")
    if any(
        amount != 1
        for amount in params.get("director_reference_information_extracted", [])
    ):
        raise StarbridgeError("精准参考的信息提取参数必须为 1.0。")
    _validate_generation(params, prompt, width, height)

    params.update(width=width, height=height, n_samples=count)
    params.setdefault("seed", secrets.randbits(32))
    params.setdefault("params_version", 3)
    params.setdefault("noise_schedule", "karras")
    params.setdefault(
        "v4_prompt",
        {
            "caption": {"base_caption": prompt, "char_captions": []},
            "use_coords": False,
            "use_order": True,
        },
    )
    caption = params["v4_prompt"].get("caption", {})
    characters = caption.get("char_captions", [])
    if model.startswith("nai-diffusion-4-5-") and len(characters) < 2:
        params["v4_prompt"]["use_coords"] = False
    params.setdefault(
        "v4_negative_prompt",
        {
            "caption": {
                "base_caption": params.get("negative_prompt", ""),
                "char_captions": [
                    {"char_caption": "", "centers": item.get("centers", [])}
                    for item in characters
                ],
            }
        },
    )
    params.pop("characterPrompts", None)
    params.pop("use_coords", None)
    if action == "img2img":
        params["image"] = await asyncio.to_thread(
            _image, payload.get("image", ""), (width, height)
        )
        params.setdefault("strength", 0.7)
        params.setdefault("noise", 0)
    elif action != "generate":
        raise StarbridgeError("当前操作未提供对应的原生请求参数。")
    if precise:
        params["director_reference_images"] = [
            await asyncio.to_thread(_image, value, precise=True) for value in precise
        ]
    if vibes:
        # Read every image before the first paid request; a malformed later
        # reference must not leave the user with earlier encoding charges.
        images = [await asyncio.to_thread(_image, value) for value in vibes]
        information = params.get(
            "reference_information_extracted_multiple", [0.7] * len(images)
        )
        encoded = []
        for image, amount in zip(images, information):
            body = {"model": model, "image": image, "information_extracted": amount}
            async with session.post(
                native_endpoint(base_url, "encode-vibe"),
                json=body,
                headers={**headers, "Accept": "application/binary"},
                timeout=timeout,
                allow_redirects=False,
            ) as response:
                raw = await response.read()
                if response.status >= 300:
                    try:
                        detail = json.loads(raw)
                    except (ValueError, UnicodeError):
                        detail = None
                    completed = (
                        f"（此前 {len(encoded)} 张编码已完成并可能计费）"
                        if encoded
                        else ""
                    )
                    raise StarbridgeError(
                        "Vibe 编码失败"
                        + completed
                        + "，未提交生图："
                        + native_error(detail, response.status)
                    )
                if not raw:
                    raise StarbridgeError("Vibe 编码结果为空，未继续生图。")
                encoded.append(base64.b64encode(raw).decode("ascii"))
        params["reference_image_multiple"] = encoded
    return StarbridgeRequest(
        endpoint,
        {"model": model, "input": prompt, "action": action, "parameters": params},
    )


def normalize_starbridge_response(body: object) -> dict:
    """Adapt official images[].image without downloading site-hosted URLs."""
    images = body.get("images", []) if isinstance(body, dict) else []
    if not isinstance(images, list):
        return {"data": []}
    return {
        "data": [
            {"b64_json": image["image"]}
            for image in images
            if isinstance(image, dict)
            and isinstance(image.get("image"), str)
            and image["image"]
        ]
    }
