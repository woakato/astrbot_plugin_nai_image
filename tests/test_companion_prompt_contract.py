"""Companion prompts preserve their content without a format or identity gate."""

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
            image_style="custom",
            image_size="竖图",
            _generate_one=AsyncMock(),
            _prepare_openai_prompt=AsyncMock(
                side_effect=lambda prompt, style, **options: (prompt, "none", "nai")
            ),
            _openai_generate=AsyncMock(return_value=([b"synthetic-image"], "ok")),
        )
        self.api = NAIImageCompanionExtensionAPI(self.plugin)
        self.api._save_companion_image = AsyncMock(
            return_value=Path("synthetic-output.png")
        )
        self.api._build_result_metadata = Mock(return_value={})
        self.api._note_generation = Mock()

    def test_chinese_prose_and_negative_requirements_reach_generation(self):
        for mode, prompt in (
            ("nai tag模式", "一个银发绿眼的女孩坐在窗边"),
            ("nai tag模式", "Draw a young woman wearing a dress by the window"),
            ("nai tag模式", '1girl, 1.3::silver hair::, Text:"早上好"'),
            ("自然语言模式（en）", "银发少女坐在窗边"),
        ):
            with self.subTest(mode=mode, prompt=prompt):
                self.plugin.companion_prompt_format = mode
                result = asyncio.run(
                    self.api.generate_for_companion(
                        None, {"prompt_text": prompt, "negative": "不要画成黑发"}
                    )
                )
                self.assertEqual(result["image_path"], "synthetic-output.png")
                self.assertEqual(
                    self.plugin._openai_generate.await_args.kwargs["negative"],
                    "不要画成黑发",
                )
        self.assertEqual(self.plugin._openai_generate.await_count, 4)
        self.plugin._generate_one.assert_not_awaited()

    def test_tag_background_retains_identity_outfit_and_scene(self):
        prompt = self.api._compose_companion_prompt(
            "selfie, window",
            ["silver hair, green eyes", "white coat", "quiet bedroom"],
            prompt_format="nai",
        )
        self.assertEqual(
            prompt, "silver hair, green eyes, white coat, quiet bedroom, selfie, window"
        )

    def test_vibe_identity_requests_use_the_selected_reference_mode(self):
        self.plugin.openai_reference_mode = "vibe"
        self.api._resolve_reference_paths = Mock(
            return_value=["synthetic-reference.png"]
        )
        result = asyncio.run(
            self.api.generate_for_companion(
                None, {"prompt_text": "selfie", "reference_roles": ["identity"]}
            )
        )
        self.assertEqual(result["image_path"], "synthetic-output.png")
        self.assertEqual(
            self.plugin._openai_generate.await_args.kwargs["reference_image_paths"],
            ["synthetic-reference.png"],
        )
        self.assertEqual(self.plugin.openai_reference_mode, "vibe")

    def test_companion_subject_bypasses_standalone_character_and_outfit_defaults(self):
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
