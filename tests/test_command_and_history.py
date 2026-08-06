import asyncio
from unittest.mock import AsyncMock

import yaml
from astrbot.api.message_components import Image, Plain

from main import NAIGenerateImagePlugin


async def collect_results(generator):
    return [result async for result in generator]


class FakeCommandEvent:
    def __init__(self, message_str: str):
        self.message_str = message_str

    def get_sender_id(self):
        return "test-user"

    def plain_result(self, text: str):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


def make_command_plugin(reply_mode: str, generate_result):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.bot_reply_mode = reply_mode
    plugin.image_gen_key = "test-token"
    plugin.image_count = 1
    plugin.image_style = "custom"
    plugin.image_size = "竖图"
    plugin._generate_one = AsyncMock(return_value=generate_result)
    return plugin


def test_image_only_mode_sends_only_the_generated_image():
    plugin = make_command_plugin("仅图片", (b"test-image-bytes", "ok"))
    event = FakeCommandEvent("a very long prompt")

    results = asyncio.run(collect_results(plugin.image(event)))

    assert len(results) == 1
    result_type, chain = results[0]
    assert result_type == "chain"
    assert len(chain) == 1
    assert isinstance(chain[0], Image)


def test_concise_mode_omits_prompt_and_keeps_brief_parameters():
    prompt = "a very long private prompt"
    plugin = make_command_plugin("简洁", (b"test-image-bytes", "ok"))
    event = FakeCommandEvent(prompt)

    results = asyncio.run(collect_results(plugin.image(event)))

    assert len(results) == 2
    result_type, text = results[0]
    assert result_type == "plain"
    assert "正在画图" in text
    assert "风格:" in text
    assert prompt not in text
    result_type, chain = results[1]
    assert result_type == "chain"
    assert isinstance(chain[0], Plain)
    assert isinstance(chain[1], Image)


def test_image_only_mode_keeps_generation_errors():
    plugin = make_command_plugin("仅图片", (None, "timeout"))
    event = FakeCommandEvent("1girl")

    results = asyncio.run(collect_results(plugin.image(event)))

    assert len(results) == 2
    assert all(result[0] == "plain" for result in results)
    assert "超时" in results[0][1]
    assert "全部 1 张图片生成失败" in results[1][1]


def test_image_history_keeps_newest_managed_files(tmp_path):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.save_image_history = True
    plugin.save_generation_parameters = True
    plugin.image_history_limit = 2
    plugin._image_history_dir = tmp_path / "image_history"
    plugin._image_history_lock = asyncio.Lock()
    unmanaged_file = plugin._image_history_dir / "notes.txt"
    plugin._image_history_dir.mkdir()
    unmanaged_file.write_text("keep", encoding="utf-8")

    async def save_images():
        await plugin._archive_generated_image(
            b"first",
            {"tag": "first prompt line 1\nfirst prompt line 2"},
        )
        first_image = next(plugin._image_history_dir.glob("nai_*.img"))
        first_image.with_suffix(".json").write_text(
            '{"tag": "legacy parameters"}',
            encoding="utf-8",
        )
        await plugin._archive_generated_image(b"second", {"tag": "second prompt"})
        await plugin._archive_generated_image(b"third", {"tag": "third prompt"})

    asyncio.run(save_images())

    saved_files = sorted(plugin._image_history_dir.glob("nai_*.img"))
    parameter_files = sorted(plugin._image_history_dir.glob("nai_*.yaml"))
    assert len(saved_files) == 2
    assert len(parameter_files) == 2
    assert {path.read_bytes() for path in saved_files} == {b"second", b"third"}
    saved_prompts = {
        yaml.safe_load(path.read_text(encoding="utf-8"))["tag"]
        for path in parameter_files
    }
    assert saved_prompts == {
        "second prompt",
        "third prompt",
    }
    saved_stems = {path.stem for path in saved_files}
    parameter_stems = {path.stem for path in parameter_files}
    assert saved_stems == parameter_stems
    assert unmanaged_file.read_text(encoding="utf-8") == "keep"
    assert not list(plugin._image_history_dir.glob("nai_*.json"))


def test_zero_history_limit_disables_cleanup(tmp_path):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.save_image_history = True
    plugin.save_generation_parameters = True
    plugin.image_history_limit = 0
    plugin._image_history_dir = tmp_path / "image_history"
    plugin._image_history_lock = asyncio.Lock()

    async def save_images():
        await plugin._archive_generated_image(b"first", {"tag": "first prompt"})
        await plugin._archive_generated_image(b"second", {"tag": "second prompt"})
        await plugin._archive_generated_image(b"third", {"tag": "third prompt"})

    asyncio.run(save_images())

    assert len(list(plugin._image_history_dir.glob("nai_*.img"))) == 3
    assert len(list(plugin._image_history_dir.glob("nai_*.yaml"))) == 3


def test_parameter_history_can_be_disabled(tmp_path):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.save_image_history = True
    plugin.save_generation_parameters = False
    plugin.image_history_limit = 0
    plugin._image_history_dir = tmp_path / "image_history"
    plugin._image_history_lock = asyncio.Lock()

    asyncio.run(plugin._archive_generated_image(b"image", {"tag": "prompt"}))

    assert len(list(plugin._image_history_dir.glob("nai_*.img"))) == 1
    assert not list(plugin._image_history_dir.glob("nai_*.yaml"))


def test_parameter_history_uses_yaml_literal_block_for_multiline_prompt(tmp_path):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.save_image_history = True
    plugin.save_generation_parameters = True
    plugin.image_history_limit = 0
    plugin._image_history_dir = tmp_path / "image_history"
    plugin._image_history_lock = asyncio.Lock()
    parameters = {
        "tag": "1girl, solo\ncute\nglare nude",
        "steps": 28,
        "cfg": 0.3,
    }

    asyncio.run(plugin._archive_generated_image(b"image", parameters))

    parameters_path = next(plugin._image_history_dir.glob("nai_*.yaml"))
    content = parameters_path.read_text(encoding="utf-8")
    assert "tag: |-\n  1girl, solo\n  cute\n  glare nude\n" in content
    assert "\\n" not in content
    assert yaml.safe_load(content) == parameters


def test_auto_translate_skips_pure_nai_prompt():
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.translate_mode = "自动"
    plugin.enable_translate = True
    plugin._translate_prompt = AsyncMock(return_value="should not be used")
    plugin._resolve_outfit = lambda prompt: ("unexpected", "prompt", False)
    prompt = "1girl, solo, 1.2::blue hair::, {{looking at viewer}}"

    prepared, outfit_source, use_default, prompt_kind = asyncio.run(
        plugin._prepare_translated_prompt(
            prompt,
            translate_mode="自动",
            apply_outfit=True,
        )
    )

    assert prepared == prompt
    assert outfit_source == "none"
    assert not use_default
    assert prompt_kind == "nai"
    plugin._translate_prompt.assert_not_awaited()


def test_prompt_is_normalized_before_disabled_translation_pipeline():
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.translate_mode = "关闭"
    plugin.enable_translate = False

    prepared, outfit_source, use_default, prompt_kind = asyncio.run(
        plugin._prepare_translated_prompt(
            "1girl,\n\n  solo\t  best quality",
            translate_mode="关闭",
            apply_outfit=True,
        )
    )

    assert prepared == "1girl, solo best quality"
    assert outfit_source == "none"
    assert not use_default
    assert prompt_kind == "nai"


def test_auto_translate_only_sends_natural_segments_to_llm():
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.translate_mode = "自动"
    plugin.enable_translate = True
    plugin._translate_prompt = AsyncMock(
        return_value="1girl, black dress, standing, rain"
    )
    plugin._resolve_outfit = lambda prompt: ("", "none", False)

    prepared, _, _, prompt_kind = asyncio.run(
        plugin._prepare_translated_prompt(
            "1girl, solo, 她穿着黑色连衣裙站在雨里, best quality",
            translate_mode="自动",
            apply_outfit=True,
        )
    )

    assert prompt_kind == "mixed"
    assert prepared == "1girl, solo, black dress, standing, rain, best quality"
    plugin._translate_prompt.assert_awaited_once_with(
        "她穿着黑色连衣裙站在雨里",
        force=True,
    )
