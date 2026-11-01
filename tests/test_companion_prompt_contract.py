"""Local companion prompt validation must precede every paid image request."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_nai_image.companion_api import NAIImageCompanionExtensionAPI


class CompanionPromptContractTests(unittest.TestCase):
    def setUp(self):
        self.plugin = SimpleNamespace(
            call_mode="openai",
            companion_prompt_format="nai tag模式",
            openai_reference_mode="director",
            enable_companion_link=True,
            _generate_one=AsyncMock(),
            _openai_generate=AsyncMock(),
        )
        self.api = NAIImageCompanionExtensionAPI(self.plugin)

    def test_chinese_or_prose_in_tag_mode_is_rejected_before_generation(self):
        for prompt in (
            "一个银发绿眼的女孩坐在窗边",
            "1girl, {{银发少女}}",
            "Draw a young woman wearing a dress by the window",
        ):
            with self.subTest(prompt=prompt):
                result = asyncio.run(
                    self.api.generate_for_companion(None, {"prompt_text": prompt})
                )
                self.assertEqual(result["reason"], "nai_prompt_format_invalid")
                self.assertEqual(result["image_path"], "")
        self.plugin._generate_one.assert_not_awaited()
        self.plugin._openai_generate.assert_not_awaited()

    def test_tag_weights_and_requested_image_text_remain_valid(self):
        result = self.api.validate_companion_prompt(
            {"prompt_text": '1girl, 1.3::silver hair, green eyes::, Text:"早上好"'}
        )
        self.assertTrue(result["valid"])

    def test_tag_background_retains_identity_outfit_and_scene(self):
        prompt = self.api._compose_companion_prompt(
            "selfie, window",
            ["silver hair, green eyes", "white coat", "quiet bedroom"],
            prompt_format="nai",
        )
        self.assertEqual(
            prompt, "silver hair, green eyes, white coat, quiet bedroom, selfie, window"
        )
        result = self.api.validate_companion_prompt(
            {
                "prompt_text": "selfie",
                "prompt_sections": {"character": "silver hair, green eyes"},
            }
        )
        self.assertTrue(result["valid"])

    def test_chinese_negative_requirements_cannot_bypass_the_tag_contract(self):
        result = self.api.validate_companion_prompt(
            {"prompt_text": "1girl, silver hair", "negative": "不要画成黑发"}
        )
        self.assertEqual(result["reason"], "nai_prompt_format_invalid")

    def test_vibe_cannot_silently_replace_identity_reference(self):
        self.plugin.openai_reference_mode = "vibe"
        result = self.api.validate_companion_prompt(
            {"prompt_text": "selfie", "reference_roles": ["identity"]}
        )
        self.assertEqual(result["reason"], "nai_identity_reference_mode")
        self.assertTrue(
            self.api.validate_companion_prompt(
                {"prompt_text": "garden", "reference_roles": ["style"]}
            )["valid"]
        )

    def test_explicit_natural_mode_is_not_forced_into_tags(self):
        self.plugin.companion_prompt_format = "自然语言模式（en）"
        result = self.api.validate_companion_prompt(
            {"prompt_text": "A silver-haired woman is sitting by the window."}
        )
        self.assertTrue(result["valid"])
        result = self.api.validate_companion_prompt({"prompt_text": "银发少女坐在窗边"})
        self.assertEqual(result["reason"], "nai_prompt_language_invalid")

    def test_companion_subject_bypasses_standalone_character_and_outfit_defaults(self):
        self.plugin.image_style = "custom"
        self.plugin.image_size = "竖图"
        self.plugin._prepare_openai_prompt = AsyncMock(
            return_value=("1girl, silver hair", "none", "nai")
        )
        self.plugin._openai_generate.return_value = ([b"synthetic-image"], "ok")
        self.api._save_companion_image = AsyncMock(
            return_value=Path("synthetic-output.png")
        )
        self.api._build_result_metadata = Mock(return_value={})
        self.api._note_generation = Mock()
        result = asyncio.run(
            self.api.generate_for_companion(
                None,
                {"prompt_text": "1girl, silver hair", "character_source": "companion"},
            )
        )
        self.assertEqual(result["image_path"], "synthetic-output.png")
        self.plugin._prepare_openai_prompt.assert_awaited_once_with(
            "1girl, silver hair", "custom", enable_template=False, apply_outfit=False
        )
