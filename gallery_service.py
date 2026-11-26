"""Manage the plugin's saved images for the authenticated panel gallery."""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path
from typing import Any

import yaml
from PIL import Image, ImageOps

_IMAGE_NAME = re.compile(r"nai_\d+\.(?:png|jpg|webp|img)")
_MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_SAFE_METADATA_KEYS = (
    "model",
    "size",
    "steps",
    "scale",
    "cfg",
    "sampler",
    "seed",
    "negative",
    "artist",
    "mode",
    "transport",
    "reference_mode",
    "reference_count",
)
_SIDECAR_SUFFIXES = (".prompt.txt", ".yaml", ".json")


def _safe_history_child(history_dir: Path, path: Path) -> bool:
    """Return True when path is an existing non-symlink file inside history_dir."""
    if path.is_symlink():
        return False
    try:
        return path.resolve().parent == history_dir.resolve() and path.is_file()
    except OSError:
        return False


def _read_saved_yaml(history_dir: Path, image_name: str) -> dict[str, Any]:
    """Read legacy or parameter YAML sidecar if valid."""
    parameters_path = (history_dir / image_name).with_suffix(".yaml")
    if not _safe_history_child(history_dir, parameters_path):
        return {}
    try:
        parameters = yaml.safe_load(parameters_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}
    return parameters if isinstance(parameters, dict) else {}


def _read_saved_prompt(history_dir: Path, image_name: str) -> str:
    """Read the prompt sidecar, falling back to legacy generation YAML."""
    prompt_path = (history_dir / image_name).with_suffix(".prompt.txt")
    if _safe_history_child(history_dir, prompt_path):
        try:
            return prompt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""
    parameters = _read_saved_yaml(history_dir, image_name)
    return str(parameters.get("tag") or "")


def _read_saved_metadata(history_dir: Path, image_name: str) -> dict[str, Any]:
    """Extract safe display metadata from the YAML sidecar when present."""
    parameters = _read_saved_yaml(history_dir, image_name)
    if not parameters:
        return {}
    meta: dict[str, Any] = {}
    for key in _SAFE_METADATA_KEYS:
        if key not in parameters:
            continue
        value = parameters[key]
        if value is None or value == "":
            continue
        meta[key] = value
    return meta


def list_gallery_images(
    history_dir: Path, offset: int, limit: int, query: str = ""
) -> dict[str, Any]:
    """List one page of saved images with small JPEG thumbnails.

    Args:
        history_dir: Directory managed by the plugin's image history.
        offset: Number of newest records to skip.
        limit: Maximum number of records to return.
        query: Optional case-insensitive filter matched against prompt or filename.

    Returns:
        Gallery records, filtered/total record counts and pagination values.
    """
    records = []
    if history_dir.is_dir():
        for image_path in history_dir.iterdir():
            if not _IMAGE_NAME.fullmatch(image_path.name) or image_path.is_symlink():
                continue
            try:
                if image_path.is_file():
                    records.append((image_path.stat(), image_path))
            except FileNotFoundError:
                continue
    records.sort(key=lambda item: (item[0].st_mtime_ns, item[1].name), reverse=True)
    all_total = len(records)

    clean_query = str(query or "").strip()
    tokens = [tok.casefold() for tok in clean_query.split() if tok]

    filtered: list[tuple[Any, Path, str]] = []
    if tokens:
        for stat, image_path in records:
            prompt = _read_saved_prompt(history_dir, image_path.name)
            haystack = f"{image_path.name}\n{prompt}".casefold()
            if all(tok in haystack for tok in tokens):
                filtered.append((stat, image_path, prompt))
    else:
        filtered = [(stat, image_path, "") for stat, image_path in records]

    total = len(filtered)
    if not filtered:
        offset = 0
    elif offset >= total:
        offset = ((total - 1) // limit) * limit

    items = []
    for stat, image_path, cached_prompt in filtered[offset : offset + limit]:
        prompt = (
            cached_prompt
            if tokens
            else _read_saved_prompt(history_dir, image_path.name)
        )
        meta = _read_saved_metadata(history_dir, image_path.name)
        item = {
            "name": image_path.name,
            "created_at": int(stat.st_mtime * 1000),
            "size_bytes": stat.st_size,
            "width": 0,
            "height": 0,
            "thumbnail_b64": "",
            "prompt_preview": (
                prompt[:240].rstrip() + ("..." if len(prompt) > 240 else "")
            ),
            "model": str(meta.get("model") or ""),
            "size": str(meta.get("size") or ""),
        }
        try:
            with Image.open(image_path) as image:
                if image.format not in _MIME_TYPES:
                    raise ValueError("Unsupported gallery image format")
                item["width"], item["height"] = image.size
                image.draft("RGB", (320, 320))
                thumbnail = ImageOps.exif_transpose(image)
                thumbnail.thumbnail((320, 320))
                with io.BytesIO() as buffer:
                    thumbnail.convert("RGB").save(buffer, format="JPEG", quality=80)
                    item["thumbnail_b64"] = base64.b64encode(buffer.getvalue()).decode(
                        "ascii"
                    )
        except (OSError, ValueError, Image.DecompressionBombError):
            pass
        items.append(item)
    return {
        "items": items,
        "total": total,
        "all_total": all_total,
        "offset": offset,
        "limit": limit,
        "query": clean_query,
    }


def read_gallery_image(history_dir: Path, name: str) -> dict[str, Any]:
    """Read one original image without permitting paths outside the history.

    Args:
        history_dir: Directory managed by the plugin's image history.
        name: Exact basename of a generated image.

    Returns:
        Original image bytes encoded as base64, MIME type, filename, prompt and metadata.

    Raises:
        ValueError: The filename or image format is invalid.
        FileNotFoundError: The image is missing or points outside the history.
    """
    if not _IMAGE_NAME.fullmatch(name):
        raise ValueError("Invalid gallery filename")
    image_path = history_dir / name
    if image_path.is_symlink() or image_path.resolve().parent != history_dir.resolve():
        raise FileNotFoundError(name)
    raw = image_path.read_bytes()
    try:
        with Image.open(io.BytesIO(raw)) as image:
            mime_type = _MIME_TYPES.get(image.format or "")
            if not mime_type:
                raise ValueError("Unsupported gallery image format")
    except Image.DecompressionBombError as exc:
        raise ValueError("Gallery image dimensions are too large") from exc
    return {
        "name": name,
        "mime_type": mime_type,
        "b64_json": base64.b64encode(raw).decode("ascii"),
        "prompt": _read_saved_prompt(history_dir, name),
        "metadata": _read_saved_metadata(history_dir, name),
    }


def delete_gallery_image(history_dir: Path, name: str) -> str:
    """Delete one gallery image and its managed sidecar files.

    Args:
        history_dir: Directory managed by the plugin's image history.
        name: Exact basename of a generated image.

    Returns:
        The deleted filename.

    Raises:
        ValueError: The filename is invalid.
        FileNotFoundError: The image is missing or points outside the history.
    """
    if not _IMAGE_NAME.fullmatch(name):
        raise ValueError("Invalid gallery filename")
    image_path = history_dir / name
    if (
        image_path.is_symlink()
        or image_path.resolve().parent != history_dir.resolve()
        or not image_path.is_file()
    ):
        raise FileNotFoundError(name)
    image_path.unlink()
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = image_path.with_suffix(suffix)
        if not sidecar.is_symlink() and sidecar.resolve().parent == history_dir.resolve():
            sidecar.unlink(missing_ok=True)
    return name
