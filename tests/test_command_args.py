import pytest

from command_args import ImageCommandArgumentError, parse_image_command


def test_parse_all_generation_overrides_with_quoted_values():
    parsed = parse_image_command(
        '1girl, solo --n=2 --style=custom --size=portrait --cfg=0.3 '
        '--scale=6.5 --steps=28 --sampler=k_euler_ancestral '
        '--noise=karras --translate=auto --template=off '
        '--model=nai-diffusion-4-5-full '
        '--artist="best quality, artist:foo" '
        '--negative="bad anatomy, blurry, text"',
        default_style="vertical",
    )

    assert parsed.prompt == "1girl, solo"
    assert parsed.n == 2
    assert parsed.style == "custom"
    assert parsed.size == "竖图"
    assert parsed.cfg == 0.3
    assert parsed.scale == 6.5
    assert parsed.steps == 28
    assert parsed.sampler == "k_euler_ancestral"
    assert parsed.noise_schedule == "karras"
    assert parsed.translate_mode == "自动"
    assert parsed.enable_template is False
    assert parsed.model == "nai-diffusion-4-5-full"
    assert parsed.artist == "best quality, artist:foo"
    assert parsed.negative == "bad anatomy, blurry, text"
    assert parsed.generation_overrides() == {
        "steps": 28,
        "scale": 6.5,
        "cfg": 0.3,
        "sampler": "k_euler_ancestral",
        "noise_schedule": "karras",
        "negative": "bad anatomy, blurry, text",
        "model": "nai-diffusion-4-5-full",
        "custom_artists": "best quality, artist:foo",
        "enable_template": False,
        "enable_translate": "自动",
    }


@pytest.mark.parametrize(
    ("text", "expected_style", "expected_size", "expected_translate", "expected_template"),
    [
        (
            "1girl --style=自定义 --size=2K横图 --translate=自动 --template=关闭",
            "custom",
            "2K横图",
            "自动",
            False,
        ),
        (
            "1girl --style=anime --size=2k_landscape --translate=on --template=true",
            "anime",
            "2K横图",
            "开启",
            True,
        ),
        (
            "1girl --style=韩漫小清新风 --size=square --translate=off --template=disabled",
            "vertical",
            "方图",
            "关闭",
            False,
        ),
    ],
)
def test_chinese_and_english_option_aliases(
    text,
    expected_style,
    expected_size,
    expected_translate,
    expected_template,
):
    parsed = parse_image_command(text, default_style="vertical")

    assert parsed.style == expected_style
    assert parsed.size == expected_size
    assert parsed.translate_mode == expected_translate
    assert parsed.enable_template is expected_template


def test_noise_schedule_alias_and_empty_negative_override():
    parsed = parse_image_command(
        '1girl --noise_schedule=exponential --negative=""',
        default_style="custom",
    )

    assert parsed.noise_schedule == "exponential"
    assert parsed.negative == ""
    assert parsed.generation_overrides()["negative"] == ""


def test_prompt_quotes_and_flag_like_text_inside_prompt_are_preserved():
    parsed = parse_image_command(
        '1girl, sign "text --not-a-flag=kept", solo --cfg=0.3',
        default_style="vertical",
    )

    assert parsed.prompt == '1girl, sign "text --not-a-flag=kept", solo'
    assert parsed.cfg == 0.3


def test_apostrophe_in_prompt_does_not_hide_following_arguments():
    parsed = parse_image_command(
        "1girl, girl's outfit --cfg=0.3",
        default_style="vertical",
    )

    assert parsed.prompt == "1girl, girl's outfit"
    assert parsed.cfg == 0.3


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("1girl --unknown=value", "未知参数"),
        ("1girl --cfg", "必须使用"),
        ("1girl --cfg=1 --cfg=2", "不能重复"),
        ('1girl --negative="bad anatomy', "引号未闭合"),
        ("1girl --n=0", "1-6"),
        ("1girl --steps=101", "1-100"),
        ("1girl --scale=21", "0-20"),
        ("1girl --cfg=nan", "0-30"),
        ("1girl --sampler=invalid", "--sampler 无效"),
        ("1girl --noise=invalid", "--noise 无效"),
        ("1girl --translate=maybe", "--translate 无效"),
        ("1girl --template=maybe", "--template 无效"),
        ('1girl --model="invalid model"', "--model 无效"),
        ('1girl --artist="artist:foo"', "--artist 仅能"),
    ],
)
def test_invalid_arguments_are_rejected(text, message):
    with pytest.raises(ImageCommandArgumentError, match=message):
        parse_image_command(text, default_style="vertical")


def test_artist_is_allowed_when_config_default_style_is_custom():
    parsed = parse_image_command(
        '1girl --artist="best quality, artist:foo"',
        default_style="自定义",
    )

    assert parsed.artist == "best quality, artist:foo"
