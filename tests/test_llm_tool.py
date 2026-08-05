import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import mcp
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.entities import LLMResponse, ProviderRequest, TokenUsage
from astrbot.core.provider.provider import Provider

from main import NAIGenerateImagePlugin


class FakeEvent:
    def __init__(self):
        self.extras = {}
        self.sent_messages = []

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    async def send(self, message):
        self.sent_messages.append(message)

    def get_result(self):
        return None


def make_plugin(generate_result):
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.enable_llm_tool = True
    plugin.image_gen_key = "test-token"
    plugin._generate_one = AsyncMock(return_value=generate_result)
    return plugin


async def collect_results(generator):
    return [result async for result in generator]


class ToolThenReplyProvider(Provider):
    def __init__(self):
        super().__init__({"id": "test-provider", "model": "test-model"}, {})
        self.call_count = 0

    def get_current_key(self):
        return "test-key"

    def set_key(self, key):
        return None

    async def get_models(self):
        return ["test-model"]

    async def text_chat(self, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            return LLMResponse(
                role="assistant",
                completion_text="",
                tools_call_name=["NAI_Generate_Image"],
                tools_call_args=[
                    {"prompt": "1girl", "style": "anime", "size_cn": "方图"}
                ],
                tools_call_ids=["call_nai_image"],
                usage=TokenUsage(input_other=10, output=5),
            )
        return LLMResponse(
            role="assistant",
            completion_text="图片已经发给你了。",
            usage=TokenUsage(input_other=10, output=5),
        )


class RepeatedToolThenReplyProvider(ToolThenReplyProvider):
    async def text_chat(self, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            return LLMResponse(
                role="assistant",
                completion_text="",
                tools_call_name=["NAI_Generate_Image", "NAI_Generate_Image"],
                tools_call_args=[
                    {"prompt": "1girl", "style": "anime", "size_cn": "方图"},
                    {"prompt": "2girls", "style": "galgame", "size_cn": "横图"},
                ],
                tools_call_ids=["call_nai_image_1", "call_nai_image_2"],
                usage=TokenUsage(input_other=10, output=5),
            )
        return LLMResponse(
            role="assistant",
            completion_text="图片已经发给你了。",
            usage=TokenUsage(input_other=10, output=5),
        )


def test_llm_tool_sends_image_and_returns_one_text_result():
    plugin = make_plugin((b"test-image-bytes", "ok"))
    event = FakeEvent()

    results = asyncio.run(
        collect_results(plugin.NAI_Generate_Image(event, "1girl", "anime", "方图"))
    )

    assert len(results) == 1
    assert results[0] == "图片已生成并发送给用户，请根据本次请求继续回复。"
    assert len(event.sent_messages) == 1
    assert isinstance(event.sent_messages[0], MessageChain)
    assert isinstance(event.sent_messages[0].chain[0], Plain)
    assert isinstance(event.sent_messages[0].chain[1], Image)
    assert event.sent_messages[0].chain[1].file.startswith("base64://")
    plugin._generate_one.assert_awaited_once_with("1girl", "anime", "方图")


def test_llm_tool_passes_supported_sizes_to_api_unchanged():
    for size_cn in (
        "竖图",
        "横图",
        "方图",
        "2K竖图",
        "2K横图",
        "2K方图",
        "4K竖图",
        "4K横图",
        "4K方图",
    ):
        plugin = make_plugin((b"test-image-bytes", "ok"))
        event = FakeEvent()

        results = asyncio.run(
            collect_results(
                plugin.NAI_Generate_Image(event, "1girl", "anime", size_cn)
            )
        )

        assert results == ["图片已生成并发送给用户，请根据本次请求继续回复。"]
        plugin._generate_one.assert_awaited_once_with("1girl", "anime", size_cn)


def test_llm_tool_allows_only_one_generation_attempt_per_event():
    plugin = make_plugin((b"test-image-bytes", "ok"))
    event = FakeEvent()

    first_results = asyncio.run(
        collect_results(plugin.NAI_Generate_Image(event, "1girl", "anime", "方图"))
    )
    second_results = asyncio.run(
        collect_results(plugin.NAI_Generate_Image(event, "2girls", "galgame", "横图"))
    )

    assert len(first_results) == 1
    assert first_results == ["图片已生成并发送给用户，请根据本次请求继续回复。"]
    assert len(second_results) == 1
    assert "本轮消息已经执行过一次" in second_results[0]
    assert "send_message_to_user" in second_results[0]
    assert len(event.sent_messages) == 1
    plugin._generate_one.assert_awaited_once()


def test_llm_tool_does_not_retry_after_failed_attempt_in_same_event():
    plugin = make_plugin((None, "timeout"))
    event = FakeEvent()

    first_results = asyncio.run(
        collect_results(plugin.NAI_Generate_Image(event, "1girl", "anime", "方图"))
    )
    second_results = asyncio.run(
        collect_results(plugin.NAI_Generate_Image(event, "1girl", "anime", "方图"))
    )

    assert len(first_results) == 1
    assert "生成失败" in first_results[0]
    assert len(second_results) == 1
    assert "本轮消息已经执行过一次" in second_results[0]
    assert event.sent_messages == []
    plugin._generate_one.assert_awaited_once()


def test_llm_tool_blocks_concurrent_generation_in_same_event():
    plugin = make_plugin((b"unused", "ok"))
    event = FakeEvent()
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_generate(*args):
        started.set()
        await release.wait()
        return b"test-image-bytes", "ok"

    plugin._generate_one = AsyncMock(side_effect=delayed_generate)

    async def run_calls():
        first_task = asyncio.create_task(
            collect_results(
                plugin.NAI_Generate_Image(event, "1girl", "anime", "方图")
            )
        )
        await started.wait()
        second_results = await collect_results(
            plugin.NAI_Generate_Image(event, "2girls", "galgame", "横图")
        )
        release.set()
        first_results = await first_task
        return first_results, second_results

    first_results, second_results = asyncio.run(run_calls())

    assert first_results == ["图片已生成并发送给用户，请根据本次请求继续回复。"]
    assert len(second_results) == 1
    assert "本轮消息已经执行过一次" in second_results[0]
    assert len(event.sent_messages) == 1
    plugin._generate_one.assert_awaited_once()


def test_llm_tool_validation_error_does_not_consume_event_attempt():
    plugin = make_plugin((b"test-image-bytes", "ok"))
    event = FakeEvent()

    invalid_results = asyncio.run(
        collect_results(
            plugin.NAI_Generate_Image(event, "1girl", "unknown-style", "方图")
        )
    )
    valid_results = asyncio.run(
        collect_results(plugin.NAI_Generate_Image(event, "1girl", "anime", "方图"))
    )

    assert len(invalid_results) == 1
    assert "未知风格" in invalid_results[0]
    assert valid_results == ["图片已生成并发送给用户，请根据本次请求继续回复。"]
    assert len(event.sent_messages) == 1
    plugin._generate_one.assert_awaited_once()


def test_llm_tool_executor_returns_content_instead_of_ending_agent_loop():
    plugin = make_plugin((b"test-image-bytes", "ok"))
    event = FakeEvent()
    run_context = ContextWrapper(context=SimpleNamespace(event=event))
    tool = FunctionTool(
        name="NAI_Generate_Image",
        description="Generate an image",
        parameters={"type": "object", "properties": {}},
        handler=plugin.NAI_Generate_Image,
    )

    async def execute_tool():
        return [
            result
            async for result in FunctionToolExecutor.execute(
                tool,
                run_context,
                prompt="1girl",
                style="anime",
                size_cn="方图",
            )
        ]

    results = asyncio.run(execute_tool())

    assert len(results) == 1
    assert isinstance(results[0], mcp.types.CallToolResult)
    assert results[0].content[0].text == (
        "图片已生成并发送给用户，请根据本次请求继续回复。"
    )
    assert len(event.sent_messages) == 1


def test_llm_tool_keeps_agent_loop_open_and_preserves_complete_turn():
    plugin = make_plugin((b"test-image-bytes", "ok"))
    event = FakeEvent()
    provider = ToolThenReplyProvider()
    tool = FunctionTool(
        name="NAI_Generate_Image",
        description="Generate an image",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "style": {"type": "string"},
                "size_cn": {"type": "string"},
            },
        },
        handler=plugin.NAI_Generate_Image,
    )
    request = ProviderRequest(
        prompt="request 1",
        contexts=[],
        func_tool=ToolSet(tools=[tool]),
    )
    run_context = ContextWrapper(context=SimpleNamespace(event=event))
    runner = ToolLoopAgentRunner()

    async def run_agent():
        await runner.reset(
            provider=provider,
            request=request,
            run_context=run_context,
            tool_executor=FunctionToolExecutor(),
            agent_hooks=BaseAgentRunHooks(),
            streaming=False,
        )
        return [response async for response in runner.step_until_done(3)]

    asyncio.run(run_agent())

    assert provider.call_count == 2
    assert runner.done()
    assert runner.get_final_llm_resp().completion_text == "图片已经发给你了。"
    assert [message.role for message in runner.run_context.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert runner.run_context.messages[1].tool_calls[0].id == "call_nai_image"
    assert runner.run_context.messages[2].tool_call_id == "call_nai_image"
    assert len(event.sent_messages) == 1


def test_llm_tool_returns_results_for_repeated_calls_but_generates_once():
    plugin = make_plugin((b"test-image-bytes", "ok"))
    event = FakeEvent()
    provider = RepeatedToolThenReplyProvider()
    tool = FunctionTool(
        name="NAI_Generate_Image",
        description="Generate an image",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "style": {"type": "string"},
                "size_cn": {"type": "string"},
            },
        },
        handler=plugin.NAI_Generate_Image,
    )
    request = ProviderRequest(
        prompt="request repeated images",
        contexts=[],
        func_tool=ToolSet(tools=[tool]),
    )
    run_context = ContextWrapper(context=SimpleNamespace(event=event))
    runner = ToolLoopAgentRunner()

    async def run_agent():
        await runner.reset(
            provider=provider,
            request=request,
            run_context=run_context,
            tool_executor=FunctionToolExecutor(),
            agent_hooks=BaseAgentRunHooks(),
            streaming=False,
        )
        return [response async for response in runner.step_until_done(3)]

    asyncio.run(run_agent())

    assert provider.call_count == 2
    assert [message.role for message in runner.run_context.messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert [
        tool_call.id for tool_call in runner.run_context.messages[1].tool_calls
    ] == ["call_nai_image_1", "call_nai_image_2"]
    assert [
        message.tool_call_id
        for message in runner.run_context.messages
        if message.role == "tool"
    ] == ["call_nai_image_1", "call_nai_image_2"]
    assert len(event.sent_messages) == 1
    plugin._generate_one.assert_awaited_once_with("1girl", "anime", "方图")


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
