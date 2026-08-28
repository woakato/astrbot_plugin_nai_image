import pytest

from main import migrate_invalid_call_mode, migrate_legacy_translate_config


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        (False, "关闭"),
        (True, "开启"),
    ],
)
def test_migrate_legacy_translate_config_migrates_bool(legacy, expected):
    config = {"enable_translate": legacy}
    assert migrate_legacy_translate_config(config) == expected
    assert config["enable_translate"] == expected


@pytest.mark.parametrize("value", ["关闭", "开启", "自动"])
def test_migrate_legacy_translate_config_leaves_string_untouched(value):
    config = {"enable_translate": value}
    assert migrate_legacy_translate_config(config) is None
    assert config["enable_translate"] == value


def test_migrate_legacy_translate_config_missing_key_untouched():
    config = {}
    assert migrate_legacy_translate_config(config) is None
    assert "enable_translate" not in config


def test_migrate_invalid_call_mode_resets_openai_without_base_url():
    config = {"call_mode": "openai", "openai_api_base_url": "", "openai_api_key": "k"}
    assert migrate_invalid_call_mode(config) == "direct"
    assert config["call_mode"] == "direct"


def test_migrate_invalid_call_mode_keeps_configured_openai():
    config = {"call_mode": "openai", "openai_api_base_url": "https://example.com"}
    assert migrate_invalid_call_mode(config) is None
    assert config["call_mode"] == "openai"


@pytest.mark.parametrize("value", [None, "", "OpenAI", "未知模式"])
def test_migrate_invalid_call_mode_normalizes_unknown_value(value):
    config = {"call_mode": value} if value is not None else {}
    assert migrate_invalid_call_mode(config) == "direct"
    assert config["call_mode"] == "direct"


def test_migrate_invalid_call_mode_keeps_direct():
    config = {"call_mode": "direct"}
    assert migrate_invalid_call_mode(config) is None
    assert config["call_mode"] == "direct"
