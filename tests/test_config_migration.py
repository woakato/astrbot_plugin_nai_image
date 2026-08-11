import pytest

from main import migrate_legacy_translate_config


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
