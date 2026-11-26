import importlib.util
import json
import shutil
from pathlib import Path

from astrbot.core.skills.skill_manager import _parse_frontmatter_description
from main import NAIGenerateImagePlugin

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_lint_module(variant: str):
    path = PLUGIN_ROOT / "skill_variants" / variant / "scripts" / "lint_nai.py"
    spec = importlib.util.spec_from_file_location(f"lint_nai_{variant}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_frontmatter_and_default_variant():
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["enable_nsfw_skill"]["default"] is False

    bundled_md = PLUGIN_ROOT / "skills" / "nai-prompting" / "SKILL.md"
    sfw_md = PLUGIN_ROOT / "skill_variants" / "sfw" / "SKILL.md"
    nsfw_md = PLUGIN_ROOT / "skill_variants" / "nsfw" / "SKILL.md"
    assert bundled_md.is_file()
    assert bundled_md.read_text(encoding="utf-8") == sfw_md.read_text(encoding="utf-8")

    for md in (bundled_md, sfw_md, nsfw_md):
        text = md.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "Anlas" not in text
        assert "Opus" not in text
        assert _parse_frontmatter_description(text) != ""


def test_sync_prompt_skill_variant_switches_and_is_idempotent(tmp_path):
    shutil.copytree(PLUGIN_ROOT / "skill_variants", tmp_path / "skill_variants")
    shutil.copytree(PLUGIN_ROOT / "skills", tmp_path / "skills")

    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin._plugin_root_dir = lambda: tmp_path  # type: ignore[method-assign]

    plugin.enable_nsfw_skill = False
    assert plugin._sync_prompt_skill_variant() is False

    plugin.enable_nsfw_skill = True
    assert plugin._sync_prompt_skill_variant() is True
    assert (
        (tmp_path / "skills" / "nai-prompting" / "SKILL.md").read_text(encoding="utf-8")
        == (tmp_path / "skill_variants" / "nsfw" / "SKILL.md").read_text(encoding="utf-8")
    )
    assert plugin._sync_prompt_skill_variant() is False

    plugin.enable_nsfw_skill = False
    assert plugin._sync_prompt_skill_variant() is True
    assert (
        (tmp_path / "skills" / "nai-prompting" / "SKILL.md").read_text(encoding="utf-8")
        == (tmp_path / "skill_variants" / "sfw" / "SKILL.md").read_text(encoding="utf-8")
    )


def test_lint_fixes_digit_suffix_with_spaces_and_checks_brackets_alongside_weights():
    for variant in ("sfw", "nsfw"):
        mod = _load_lint_module(variant)
        prompt = "1.2::year 2024::, 0.7::artist:as109::, {smile"
        errors, warnings, fixed = mod.lint(
            prompt, size="1024x1536", steps=40, quality=True, fix=True
        )
        assert len(errors) == 2
        assert "1.2::year 2024, ::" in fixed["prompt"]
        assert "0.7::artist:as109, ::" in fixed["prompt"]
        assert any("数量不一致" in w for w in warnings)
        assert not any("Opus" in e or "Anlas" in e for e in errors)


def test_lint_text_tag_avoids_context_false_positive_and_detects_presets():
    sfw = _load_lint_module("sfw")
    errors, warnings, _ = sfw.lint(
        "1girl, context: cozy cafe, amazing quality",
        size="2K竖图",
        steps=35,
        quality=True,
    )
    assert errors == []
    assert not any("Text:" in w for w in warnings)
    assert any("amazing quality" in w for w in warnings)


def test_nsfw_lint_enforces_adult_guard_on_explicit_prompts():
    nsfw = _load_lint_module("nsfw")
    errors, _, _ = nsfw.lint("1girl, nsfw, nude", negative="bad hands")
    assert len(errors) == 2
    errors_ok, _, _ = nsfw.lint(
        "adult, 1woman, nsfw, nude",
        negative="loli, child, bad hands, censored",
    )
    assert errors_ok == []


def test_llm_tool_docstring_delegates_prompt_rules_to_skill():
    doc = NAIGenerateImagePlugin.NAI_Generate_Image.__doc__ or ""
    assert "nai-prompting" in doc
    assert "(tag:1.5)" not in doc
