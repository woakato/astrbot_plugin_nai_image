import asyncio
from unittest.mock import AsyncMock

from astrbot.api.event import MessageEventResult
from astrbot.api.message_components import Image, Plain

from main import NAIGenerateImagePlugin


class FakeEvent:
    def __init__(self):
        self.extras = {}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


def make_plugin(generate_result):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.enable_llm_tool = True
    plugin.image_gen_key = "test-token"
    plugin._generate_one = AsyncMock(return_value=generate_result)
    return plugin


async def collect_results(generator):
    return [result async for result in generator]


def test_llm_tool_returns_one_direct_image_result():
    plugin = make_plugin((b"test-image-bytes", "ok"))
    event = FakeEvent()

    results = asyncio.run(
        collect_results(plugin.NAI_Generate_Image(event, "1girl", "anime", "方图"))
    )

    assert len(results) == 1
    assert isinstance(results[0], MessageEventResult)
    assert isinstance(results[0].chain[0], Plain)
    assert isinstance(results[0].chain[1], Image)
    assert results[0].chain[1].file.startswith("base64://")
    plugin._generate_one.assert_awaited_once_with("1girl", "anime", "square")


def test_llm_tool_skips_duplicate_completed_request_in_same_event():
    plugin = make_plugin((b"test-image-bytes", "ok"))
    event = FakeEvent()

    first_results = asyncio.run(
        collect_results(plugin.NAI_Generate_Image(event, "1girl", "anime", "方图"))
    )
    second_results = asyncio.run(
        collect_results(plugin.NAI_Generate_Image(event, "1girl", "anime", "方图"))
    )

    assert len(first_results) == 1
    assert isinstance(first_results[0], MessageEventResult)
    assert second_results == ["相同的图片生成请求已完成，请勿重复调用。"]
    plugin._generate_one.assert_awaited_once()


def test_llm_tool_returns_one_error_result():
    plugin = make_plugin((None, "timeout"))
    event = FakeEvent()

    results = asyncio.run(
        collect_results(plugin.NAI_Generate_Image(event, "1girl", "anime", "方图"))
    )

    assert len(results) == 1
    assert isinstance(results[0], str)
    assert "生成失败" in results[0]
    assert "超时" in results[0]
