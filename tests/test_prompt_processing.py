from prompt_processing import (
    TRANSLATE_MODE_AUTO,
    TRANSLATE_MODE_OFF,
    TRANSLATE_MODE_ON,
    classify_prompt_segment,
    extract_mixed_prompt,
    merge_translated_prompt,
    normalize_prompt,
    normalize_translate_mode,
    split_prompt_segments,
)


def test_prompt_normalization_replaces_line_breaks_and_collapses_spaces():
    prompt = "  1girl,\r\n \r\n  solo\t  best quality\n, blue eyes  "

    assert normalize_prompt(prompt) == "1girl, solo best quality, blue eyes"


def test_prompt_normalization_keeps_single_line_tag_syntax():
    prompt = "1.2::red dress, white bow::, {{blue hair, long hair}}, solo"

    assert normalize_prompt(prompt) == prompt


def test_explicit_natural_marker_can_be_on_its_own_line():
    normalized = normalize_prompt("1girl, solo\n|nl|\n她站在雨中")
    parts = extract_mixed_prompt(normalized)

    assert parts.nai_text == "1girl, solo"
    assert parts.natural_text == "她站在雨中"


def test_translate_mode_accepts_new_values_and_legacy_booleans():
    assert normalize_translate_mode("关闭") == TRANSLATE_MODE_OFF
    assert normalize_translate_mode("开启") == TRANSLATE_MODE_ON
    assert normalize_translate_mode("自动") == TRANSLATE_MODE_AUTO
    assert normalize_translate_mode(False) == TRANSLATE_MODE_OFF
    assert normalize_translate_mode(True) == TRANSLATE_MODE_ON


def test_split_does_not_break_weighted_or_bracketed_expressions():
    prompt = "1girl, 1.2::red dress, white bow::, {{blue hair, long hair}}, solo"

    assert split_prompt_segments(prompt) == [
        "1girl",
        "1.2::red dress, white bow::",
        "{{blue hair, long hair}}",
        "solo",
    ]


def test_classifier_preserves_nai_and_extracts_natural_language():
    assert classify_prompt_segment("1girl") == "nai"
    assert classify_prompt_segment("looking at viewer") == "nai"
    assert classify_prompt_segment("1.2::蓝色长发::") == "nai"
    assert classify_prompt_segment("她穿着黑色连衣裙站在雨里") == "natural"
    assert classify_prompt_segment("a cat sleeping on the sofa") == "natural"


def test_pure_nai_prompt_is_returned_unchanged():
    prompt = "1girl, solo, 1.2::blue hair::, {{looking at viewer}}, best quality"
    parts = extract_mixed_prompt(prompt)

    assert not parts.has_natural
    assert merge_translated_prompt(parts, "unused") == prompt


def test_mixed_prompt_only_replaces_natural_segments():
    prompt = (
        "1girl, solo, best quality, 她穿着黑色连衣裙站在月光下的废墟里, "
        "cinematic lighting"
    )
    parts = extract_mixed_prompt(prompt)

    assert parts.natural_text == "她穿着黑色连衣裙站在月光下的废墟里"
    assert parts.nai_text == "1girl, solo, best quality, cinematic lighting"
    assert merge_translated_prompt(
        parts,
        "1girl, black dress, standing, ruins, moonlight",
    ) == (
        "1girl, solo, best quality, black dress, standing, ruins, moonlight, "
        "cinematic lighting"
    )


def test_explicit_natural_marker_overrides_ambiguous_detection():
    parts = extract_mixed_prompt(
        "1girl, solo, best quality |nl| standing in rain with a clear umbrella"
    )

    assert parts.explicit_natural
    assert parts.natural_text == "standing in rain with a clear umbrella"
    assert merge_translated_prompt(parts, "standing, rain, clear umbrella") == (
        "1girl, solo, best quality, standing, rain, clear umbrella"
    )


def test_merge_drops_translated_tags_that_already_exist():
    parts = extract_mixed_prompt("1girl, 她是一个女孩, solo")

    assert merge_translated_prompt(parts, "1girl") == "1girl, solo"
