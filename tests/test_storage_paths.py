import asyncio
from unittest.mock import patch

from main import NAIGenerateImagePlugin, PLUGIN_NAME


def test_plugin_data_dir_uses_astrbot_data_helper(monkeypatch, tmp_path):
    expected = tmp_path / "data" / "plugin_data" / PLUGIN_NAME
    monkeypatch.setattr(
        "main.StarTools.get_data_dir",
        lambda plugin_name: expected if plugin_name == PLUGIN_NAME else None,
    )

    plugin = object.__new__(NAIGenerateImagePlugin)

    assert plugin._get_plugin_data_dir() == expected


def test_legacy_data_dir_is_migrated_and_removed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / "data" / PLUGIN_NAME
    legacy.mkdir(parents=True)
    (legacy / "trial_usage.json").write_text('{"count": 2}', encoding="utf-8")
    (legacy / "panel_cache.json").write_text('{"prompt": "test"}', encoding="utf-8")
    (legacy / "nested").mkdir()
    (legacy / "nested" / "state.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "data" / "plugin_data" / PLUGIN_NAME

    plugin = object.__new__(NAIGenerateImagePlugin)
    with patch("main.StarTools.get_data_dir", return_value=target):
        plugin._migrate_legacy_data_dir()

    assert (target / "trial_usage.json").read_text(encoding="utf-8") == '{"count": 2}'
    assert (target / "panel_cache.json").read_text(encoding="utf-8") == (
        '{"prompt": "test"}'
    )
    assert (target / "nested" / "state.json").read_text(encoding="utf-8") == "{}"
    assert not legacy.exists()


def test_legacy_data_dir_preserves_conflicting_items(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / "data" / PLUGIN_NAME
    legacy.mkdir(parents=True)
    (legacy / "trial_usage.json").write_text('{"count": 4}', encoding="utf-8")
    target = tmp_path / "data" / "plugin_data" / PLUGIN_NAME
    target.mkdir(parents=True)
    target_file = target / "trial_usage.json"
    target_file.write_text('{"count": 3}', encoding="utf-8")

    plugin = object.__new__(NAIGenerateImagePlugin)
    with patch("main.StarTools.get_data_dir", return_value=target):
        plugin._migrate_legacy_data_dir()

    assert target_file.read_text(encoding="utf-8") == '{"count": 3}'
    assert (legacy / "trial_usage.json").read_text(encoding="utf-8") == '{"count": 4}'
    assert legacy.exists()


def test_trial_usage_initializes_inside_plugin_data_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    expected = tmp_path / "data" / "plugin_data" / PLUGIN_NAME
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin._trial_key = None
    plugin._trial_usage_count = 0
    plugin._trial_usage_file = None

    with patch("main._TRIAL_KEY_ENC", ""), patch(
        "main.StarTools.get_data_dir", return_value=expected
    ):
        asyncio.run(plugin._init_trial_feature())

    assert plugin._trial_usage_file == str(expected / "trial_usage.json")
    assert expected.is_dir()
    assert not (tmp_path / "data" / PLUGIN_NAME / "trial_usage.json").exists()
