"""Smoke test for NAIImageCompanionExtensionAPI (no network, no AstrBot runtime)."""
import asyncio
import pathlib
import sys
import types

sys.path.insert(0, r"E:\AstrBot\data\plugins")

from astrbot_plugin_nai_image.companion_api import NAIImageCompanionExtensionAPI


class FakeSession:
    closed = False


class FakePlugin:
    image_gen_key = "fake-token"
    image_style = "lolita25d"
    image_size = "竖图"
    base_url = "https://nai.sta1n.cn"
    proxy_runner = None
    proxy_port = 8766
    translate_mode = "关闭"
    enable_companion_link = True
    companion_prompt_format = "自然语言模式（en）"
    companion_image_retention_days = 30
    enable_proxy = True
    _session = FakeSession()
    last_prompt = None
    last_negative = None

    async def _generate_one(self, prompt, style, size, **kwargs):
        FakePlugin.last_prompt = prompt
        FakePlugin.last_negative = kwargs.get("negative")
        # return a 1x1 PNG
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
        )
        return png, "ok"

    @staticmethod
    def _image_history_extension(img_bytes):
        return ".png" if img_bytes.startswith(b"\x89PNG") else ".img"


async def main() -> None:
    api = NAIImageCompanionExtensionAPI(FakePlugin())

    # size coercion
    assert api._coerce_size("1024x1024") == "方图", api._coerce_size("1024x1024")
    assert api._coerce_size("9:16") == "竖图", api._coerce_size("9:16")
    assert api._coerce_size("竖图") == "竖图"
    assert api._coerce_size("2048x2048") == "2K方图", api._coerce_size("2048x2048")
    assert api._coerce_size("1920x1080") == "横图", api._coerce_size("1920x1080")
    assert api._coerce_size("3840x2160") == "2K横图", api._coerce_size("3840x2160")
    assert api._coerce_size("garbage") == "竖图"

    # style coercion (unknown styles fall back to plugin default)
    assert api._coerce_style("vertical") == "vertical"
    assert api._coerce_style("二次元") == "lolita25d"
    assert api._coerce_style("unknown") == "lolita25d"

    # prompt format resolution
    assert api._resolve_prompt_format({"prompt_format": "nai"}) == "nai"
    assert api._resolve_prompt_format({"prompt_format": "natural_language"}) == "natural_language"
    assert api._resolve_prompt_format({}) == "natural_language"
    FakePlugin.companion_prompt_format = "nai tag模式"
    assert api._resolve_prompt_format({}) == "nai"
    FakePlugin.companion_prompt_format = "自然语言模式（en）"

    # capability status
    status = api.capability_status(None)
    assert status["installed"] and status["available"] and status["selected_backend"] == "nai", status
    assert status["backends"]["external"] is True

    # status snapshot
    snap = api.status()
    assert snap["token_configured"] is True and snap["proxy_enabled"] is True

    # natural language mode: background sections + requirement, no translation
    result = await api.generate_for_companion(
        None,
        {
            "workflow_kind": "selfie",
            "prompt_text": "wear a school uniform and take a selfie in the bedroom",
            "session_key": "test_session",
            "prompt_format": "natural_language",
            "size": "1024x1024",
            "prompt_sections": [
                {"source": "wardrobe_decision", "positive": "white sailor school uniform", "negative": "pajamas"},
                {"source": "scene_context", "positive": "cozy bedroom at night"},
                {"source": "user_request", "positive": "duplicate requirement text"},
            ],
        },
    )
    assert result["handled"] is True, result
    prompt = FakePlugin.last_prompt
    assert "Background:" in prompt and "white sailor school uniform" in prompt, prompt
    assert "cozy bedroom at night" in prompt, prompt
    assert "duplicate requirement text" not in prompt, prompt
    assert "Requirements:" in prompt and "wear a school uniform" in prompt, prompt
    assert FakePlugin.last_negative == "pajamas", FakePlugin.last_negative
    assert result["image_path"].endswith(".png")
    assert result["metadata"]["kind"] == "selfie"
    assert pathlib.Path(result["image_path"]).exists()

    # natural language mode without sections: bare requirement, newlines collapsed, no commas injected
    await api.generate_for_companion(
        None,
        {
            "prompt_text": "a girl sitting by the window\nsoft morning light",
            "prompt_format": "natural_language",
        },
    )
    assert FakePlugin.last_prompt == "a girl sitting by the window soft morning light", FakePlugin.last_prompt

    # nai tag mode: tags passthrough (newlines folded into commas)
    await api.generate_for_companion(None, {"prompt_text": "1girl, red dress", "prompt_format": "nai"})
    assert FakePlugin.last_prompt == "1girl, red dress", FakePlugin.last_prompt

    # plain mapping sections shape: {"wardrobe": "...", "negative": "..."}
    await api.generate_for_companion(
        None,
        {"prompt_text": "take a photo", "prompt_sections": {"wardrobe": "school uniform", "negative": "no rain"}},
    )
    assert "school uniform" in FakePlugin.last_prompt, FakePlugin.last_prompt
    assert FakePlugin.last_negative == "no rain"

    # dataclass-like object sections shape
    section = types.SimpleNamespace(source="scene_context", positive="park with flowers", negative="")
    await api.generate_for_companion(
        None,
        {"prompt_text": "stand under a tree", "prompt_sections": [section]},
    )
    assert "park with flowers" in FakePlugin.last_prompt, FakePlugin.last_prompt

    # maintenance
    maintenance = await api.maintenance(None)
    assert "removed_files" in maintenance, maintenance

    # test_endpoint (direct pass, no translation)
    test = await api.test_endpoint(None, {"style": "vertical"}, "a test image")
    assert test["ok"] is True and test["image_path"], test
    assert FakePlugin.last_prompt == "a test image", FakePlugin.last_prompt

    # disabled plugin -> handled False
    FakePlugin.enable_companion_link = False
    disabled = await api.generate_for_companion(None, {"prompt_text": "x"})
    assert disabled == {"handled": False, "reason": "disabled"}, disabled
    disabled_status = api.capability_status(None)
    assert disabled_status["available"] is False and disabled_status["reason"] == "disabled"

    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
