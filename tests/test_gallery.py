import asyncio
import base64
import io
import json
import os

import pytest
from gallery_service import (
    delete_gallery_image,
    list_gallery_images,
    read_gallery_image,
)
from PIL import Image

from main import NAIGenerateImagePlugin


def saved_image(path, image_format="PNG", size=(640, 480)):
    image = Image.new("RGB", size, (20, 208, 208))
    with io.BytesIO() as buffer:
        image.save(buffer, format=image_format)
        content = buffer.getvalue()
    path.write_bytes(content)
    return content


def test_gallery_paginates_newest_images_and_only_returns_thumbnails(tmp_path):
    for index in range(1, 4):
        path = tmp_path / f"nai_{index}.png"
        saved_image(path)
        os.utime(path, (index, index))
    saved_image(tmp_path / "unmanaged.png")
    (tmp_path / "nai_4.yaml").write_text("private: metadata")
    (tmp_path / ".nai_5.png.tmp").write_bytes(b"incomplete")

    page = list_gallery_images(tmp_path, 1, 1)

    assert page["total"] == 3
    assert page["offset"] == 1
    assert [item["name"] for item in page["items"]] == ["nai_2.png"]
    item = page["items"][0]
    assert (item["width"], item["height"]) == (640, 480)
    assert "b64_json" not in item
    with Image.open(io.BytesIO(base64.b64decode(item["thumbnail_b64"]))) as thumb:
        assert thumb.format == "JPEG"
        assert thumb.size == (320, 240)


def test_gallery_exposes_prompt_preview_and_full_prompt(tmp_path):
    image = tmp_path / "nai_1.png"
    saved_image(image)
    prompt = "1girl, solo, blue hair"
    image.with_suffix(".prompt.txt").write_text(prompt, encoding="utf-8")

    item = list_gallery_images(tmp_path, 0, 12)["items"][0]
    original = read_gallery_image(tmp_path, image.name)

    assert item["prompt_preview"] == prompt
    assert original["prompt"] == prompt


def test_gallery_reads_prompt_from_legacy_yaml_parameters(tmp_path):
    image = tmp_path / "nai_1.png"
    saved_image(image)
    image.with_suffix(".yaml").write_text(
        "tag: 1girl, solo, legacy prompt\nsteps: 28\n", encoding="utf-8"
    )

    assert list_gallery_images(tmp_path, 0, 12)["items"][0]["prompt_preview"] == (
        "1girl, solo, legacy prompt"
    )
    assert read_gallery_image(tmp_path, image.name)["prompt"] == (
        "1girl, solo, legacy prompt"
    )


def test_gallery_missing_directory_and_removed_image_are_handled(tmp_path):
    assert list_gallery_images(tmp_path / "missing", 0, 12)["items"] == []
    assert list_gallery_images(tmp_path, 10, 12)["total"] == 0
    with pytest.raises(FileNotFoundError):
        read_gallery_image(tmp_path, "nai_1.png")


def test_gallery_clamps_stale_offset_to_last_page(tmp_path):
    for index in range(13):
        saved_image(tmp_path / f"nai_{index}.png")

    page = list_gallery_images(tmp_path, 24, 12)

    assert page["offset"] == 12
    assert len(page["items"]) == 1


@pytest.mark.parametrize(
    ("extension", "image_format", "mime_type"),
    [
        ("png", "PNG", "image/png"),
        ("jpg", "JPEG", "image/jpeg"),
        ("webp", "WEBP", "image/webp"),
        ("img", "PNG", "image/png"),
    ],
)
def test_gallery_reads_original_bytes_and_detects_mime(
    tmp_path, extension, image_format, mime_type
):
    name = f"nai_1.{extension}"
    content = saved_image(tmp_path / name, image_format)

    result = read_gallery_image(tmp_path, name)

    assert result["name"] == name
    assert result["mime_type"] == mime_type
    assert base64.b64decode(result["b64_json"]) == content


@pytest.mark.parametrize(
    "name",
    [
        "../nai_1.png",
        "..\\nai_1.png",
        "/nai_1.png",
        "C:\\nai_1.png",
        "nai_1.png/../secret",
        "nai_1.yaml",
        "notes.txt",
    ],
)
def test_gallery_rejects_non_image_filenames_and_path_traversal(tmp_path, name):
    with pytest.raises(ValueError):
        read_gallery_image(tmp_path, name)


def test_gallery_excludes_links_outside_the_history(tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    source = tmp_path / "private.png"
    saved_image(source)
    link = history / "nai_1.png"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("Symbolic links are unavailable")

    assert list_gallery_images(history, 0, 12)["total"] == 0
    with pytest.raises(FileNotFoundError):
        read_gallery_image(history, link.name)


def test_gallery_keeps_corrupt_records_without_breaking_the_page(tmp_path):
    (tmp_path / "nai_1.png").write_bytes(b"invalid image")
    result = list_gallery_images(tmp_path, 0, 12)
    assert len(result["items"]) == 1
    assert result["items"][0]["thumbnail_b64"] == ""


class GalleryRequest:
    def __init__(self, body):
        self.body = body

    async def json(self, default=None):
        return self.body


def test_gallery_api_browses_existing_images_when_saving_is_disabled(
    tmp_path, monkeypatch
):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin._image_history_dir = tmp_path
    plugin.save_image_history = False
    saved_image(tmp_path / "nai_1.png")
    monkeypatch.setattr("main.web_request", GalleryRequest({"limit": 999}))

    response = asyncio.run(plugin._test_panel_gallery())

    assert response.status_code == 200
    data = json.loads(response.body)["data"]
    assert data["saving_enabled"] is False
    assert data["limit"] == 24
    assert len(data["items"]) == 1


@pytest.mark.parametrize("body", [[], {"offset": None}, {"limit": "invalid"}])
def test_gallery_api_rejects_invalid_pagination(monkeypatch, body):
    plugin = object.__new__(NAIGenerateImagePlugin)
    monkeypatch.setattr("main.web_request", GalleryRequest(body))
    assert asyncio.run(plugin._test_panel_gallery()).status_code == 400


@pytest.mark.parametrize(
    ("name", "status_code"),
    [("nai_1.png", 200), ("nai_2.png", 404), ("../nai_1.png", 400)],
)
def test_gallery_image_api_reports_success_or_invalid_files(
    tmp_path, monkeypatch, name, status_code
):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin._image_history_dir = tmp_path
    saved_image(tmp_path / "nai_1.png")
    monkeypatch.setattr("main.web_request", GalleryRequest({"name": name}))
    response = asyncio.run(plugin._test_panel_gallery_image())
    assert response.status_code == status_code


def test_gallery_filters_by_query_and_exposes_safe_metadata(tmp_path):
    first = tmp_path / "nai_1.png"
    second = tmp_path / "nai_2.png"
    saved_image(first)
    saved_image(second)
    os.utime(first, (1, 1))
    os.utime(second, (2, 2))
    first.with_suffix(".prompt.txt").write_text("1girl, blue hair", encoding="utf-8")
    second.with_suffix(".prompt.txt").write_text("1boy, red jacket", encoding="utf-8")
    first.with_suffix(".yaml").write_text(
        "tag: 1girl, blue hair\nmodel: nai-diffusion-5-full\nsteps: 28\ntoken: secret\n",
        encoding="utf-8",
    )

    page = list_gallery_images(tmp_path, 0, 12, query="BLUE")
    assert page["total"] == 1
    assert page["all_total"] == 2
    assert [item["name"] for item in page["items"]] == ["nai_1.png"]
    assert page["items"][0]["model"] == "nai-diffusion-5-full"

    original = read_gallery_image(tmp_path, "nai_1.png")
    assert original["metadata"] == {"model": "nai-diffusion-5-full", "steps": 28}
    assert "token" not in original["metadata"]


def test_gallery_delete_removes_image_and_sidecars(tmp_path, monkeypatch):
    image = tmp_path / "nai_1.png"
    saved_image(image)
    image.with_suffix(".prompt.txt").write_text("1girl", encoding="utf-8")
    image.with_suffix(".yaml").write_text("tag: 1girl\n", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")

    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin._image_history_dir = tmp_path
    plugin._image_history_lock = asyncio.Lock()
    monkeypatch.setattr("main.web_request", GalleryRequest({"name": "nai_1.png"}))

    response = asyncio.run(plugin._test_panel_gallery_delete())
    assert response.status_code == 200
    assert json.loads(response.body)["data"] == {"deleted": "nai_1.png"}
    assert not image.exists()
    assert not image.with_suffix(".prompt.txt").exists()
    assert not image.with_suffix(".yaml").exists()
    assert (tmp_path / "keep.txt").exists()

    with pytest.raises(FileNotFoundError):
        delete_gallery_image(tmp_path, "nai_1.png")
    with pytest.raises(ValueError):
        delete_gallery_image(tmp_path, "../nai_1.png")

