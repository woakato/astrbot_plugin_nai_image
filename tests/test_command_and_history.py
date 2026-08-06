import asyncio
from unittest.mock import AsyncMock

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
    plugin.image_history_limit = 2
    plugin._image_history_dir = tmp_path / "image_history"
    plugin._image_history_lock = asyncio.Lock()
    unmanaged_file = plugin._image_history_dir / "notes.txt"
    plugin._image_history_dir.mkdir()
    unmanaged_file.write_text("keep", encoding="utf-8")

    async def save_images():
        await plugin._archive_generated_image(b"first")
        await plugin._archive_generated_image(b"second")
        await plugin._archive_generated_image(b"third")

    asyncio.run(save_images())

    saved_files = sorted(plugin._image_history_dir.glob("nai_*.img"))
    assert len(saved_files) == 2
    assert {path.read_bytes() for path in saved_files} == {b"second", b"third"}
    assert unmanaged_file.read_text(encoding="utf-8") == "keep"


def test_zero_history_limit_disables_cleanup(tmp_path):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.save_image_history = True
    plugin.image_history_limit = 0
    plugin._image_history_dir = tmp_path / "image_history"
    plugin._image_history_lock = asyncio.Lock()

    async def save_images():
        await plugin._archive_generated_image(b"first")
        await plugin._archive_generated_image(b"second")
        await plugin._archive_generated_image(b"third")

    asyncio.run(save_images())

    assert len(list(plugin._image_history_dir.glob("nai_*.img"))) == 3
