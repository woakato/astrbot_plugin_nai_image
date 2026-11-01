"""法典词库与画风槽位的单元测试。

覆盖三类容易回归的逻辑：
1. 模型版本归一化——V4.5 的 slug ``nai-diffusion-4-5-full`` 含 ``-5-``，
   早期用子串判断会把它误判成 NAI5，导致 94% 的词库检索不到。
2. 槽位读写——数据存在插件配置里（设置页可编辑），需保证 id 重排、
   空记录丢弃、生效槽位回退、关闭时不写任何东西。
3. 增强功能开关——默认关闭时不改变升级前的任何行为。
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import grimoire_service as grimoire
import pytest
from grimoire_service import (
    expand_grimoire_macros,
    normalize_model_ver,
    search_grimoire,
)

from main import NAIGenerateImagePlugin


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("nai-diffusion-4-5-full", "NAI4.5"),
        ("nai-diffusion-4-5-curated", "NAI4.5"),
        ("nai-diffusion-4-full", "NAI4.5"),
        ("nai-diffusion-5-full", "NAI5"),
        ("nai-diffusion-5-curated", "NAI5"),
        ("NAI4.5", "NAI4.5"),
        ("NAI5", "NAI5"),
        ("", "NAI4.5"),
    ],
)
def test_normalize_model_ver_separates_v45_from_v5(model, expected):
    assert normalize_model_ver(model) == expected


def test_search_grimoire_is_locked_to_current_model_version():
    hits = search_grimoire("白丝", 5, "nai-diffusion-4-5-full")
    assert hits, "V4.5 词库应当能检索到词条"
    assert all(item["model_ver"] == "NAI4.5" for item in hits)


def test_expand_grimoire_macros_replaces_macro_with_tags():
    expanded = expand_grimoire_macros(
        "1girl, 【法典:死库水】", model_ver="nai-diffusion-4-5-full"
    )
    assert "【法典" not in expanded
    assert "1girl" in expanded


# ---------------------------------------------------------------------------
# 槽位：数据源是插件配置（设置页的 template_list）
# ---------------------------------------------------------------------------
def _make_slot_plugin(
    tmp_path,
    monkeypatch,
    active: str = "1",
    enabled: bool = True,
    sync_labels: bool = False,
):
    """构造一个只带槽位相关属性的插件实例（不跑 __init__）。

    配置路径与 schema 改写默认都被隔离到 tmp_path，避免测试污染真实插件。
    """

    monkeypatch.setattr("main.StarTools.get_data_dir", lambda _name: tmp_path)
    plugin = object.__new__(NAIGenerateImagePlugin)
    plugin.custom_artists = "artist:base"
    plugin.image_style = "vertical"
    plugin.active_artist_slot = active
    plugin.enable_artist_slots = enabled
    plugin.enable_grimoire = False
    plugin.enable_character_blocks = False
    plugin._slots_cache = None
    plugin._slots_cache_mtime = 0.0
    monkeypatch.setattr(plugin, "_config_path", lambda: tmp_path / "plugin_config.json")
    if not sync_labels:
        monkeypatch.setattr(plugin, "_sync_slot_labels", lambda _slots: None)
    return plugin


def test_artist_slots_roundtrip_and_empty_entries_dropped(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch)
    plugin._save_artist_slots(
        [
            {"name": "B", "model": "nai-diffusion-5-full", "artists": "artist:b"},
            {"name": "", "model": "", "artists": ""},
            {"name": "C", "model": "nai-diffusion-4-5-full", "artists": "artist:c"},
        ]
    )

    slots = plugin._load_artist_slots()
    assert [s["name"] for s in slots] == ["B", "C"]
    # id 按列表顺序重新编号
    assert [s["id"] for s in slots] == [1, 2]
    assert slots[0]["artists"] == "artist:b"


def test_active_slot_falls_back_to_first_when_id_missing(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch, active="99")
    slots = plugin._load_artist_slots()
    active = plugin._active_artist_slot()
    assert active is not None
    assert active["id"] == slots[0]["id"]


def test_slot_binding_provides_model_and_artists(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch, active="1")
    plugin._save_artist_slots(
        [
            {
                "name": "我的画风",
                "model": "nai-diffusion-4-5-full",
                "artists": "artist:slot",
            }
        ]
    )

    assert plugin._slot_model() == "nai-diffusion-4-5-full"
    assert plugin._slot_artists() == "artist:slot"


def test_openai_prompt_prefers_slot_artists(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch, active="1")
    plugin._save_artist_slots(
        [
            {
                "name": "我的画风",
                "model": "nai-diffusion-4-5-full",
                "artists": "artist:slot",
            }
        ]
    )

    assert plugin._openai_prompt("1girl", "vertical").startswith("artist:slot")


def test_openai_prompt_explicit_artists_beat_slot(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch, active="1")
    plugin._save_artist_slots(
        [
            {
                "name": "我的画风",
                "model": "nai-diffusion-4-5-full",
                "artists": "artist:slot",
            }
        ]
    )

    assert plugin._openai_prompt("1girl", "custom", "artist:once").startswith(
        "artist:once"
    )


def test_default_slots_are_builtin_art_styles(tmp_path, monkeypatch):
    """默认槽位应为内置画风：画风名 + 适用模型 + 对应画师串。"""

    from main import DEFAULT_ARTISTS, IMAGE_STYLES

    plugin = _make_slot_plugin(tmp_path, monkeypatch, enabled=True)
    plugin.image_style = "comicDoujin"
    slots = plugin._default_artist_slots()

    # 当前画风排第一，开启槽位后出图结果不变
    assert slots[0]["name"] == IMAGE_STYLES["comicDoujin"]
    assert slots[0]["artists"] == DEFAULT_ARTISTS["comicDoujin"]
    assert slots[0]["model"] == "nai-diffusion-4-5-full"

    names = [s["name"] for s in slots]
    assert len(slots) == len(IMAGE_STYLES)
    assert IMAGE_STYLES["vertical"] in names
    assert all(s["model"] == "nai-diffusion-4-5-full" for s in slots)


def test_legacy_default_slot_file_is_replaced_by_builtin_styles(tmp_path, monkeypatch):
    """旧的出厂两条默认 json 没有迁移价值，应改用内置画风预设。"""

    plugin = _make_slot_plugin(tmp_path, monkeypatch, enabled=True)
    legacy = {
        "version": 1,
        "slots": [
            {
                "id": 1,
                "name": "槽位 1 · V4.5 默认",
                "model": "nai-diffusion-4-5-full",
                "artists": "",
            },
            {
                "id": 2,
                "name": "槽位 2 · V5 默认",
                "model": "nai-diffusion-5-full",
                "artists": "",
            },
        ],
    }
    (tmp_path / "artist_slots.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )

    slots = plugin._load_artist_slots()

    assert len(slots) > 2, "应改用内置画风预设"
    assert not any("默认" in s["name"] for s in slots)
    assert list(tmp_path.glob("artist_slots.json.migrated-*")), "旧文件应留档"


def test_custom_legacy_slot_file_is_migrated(tmp_path, monkeypatch):
    """用户自己改过的旧槽位文件应被迁移保留，而不是丢掉。"""

    plugin = _make_slot_plugin(tmp_path, monkeypatch, enabled=True)
    legacy = {
        "version": 1,
        "slots": [
            {
                "id": 1,
                "name": "我的画风",
                "model": "nai-diffusion-5-full",
                "artists": "artist:mine",
            }
        ],
    }
    (tmp_path / "artist_slots.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )

    slots = plugin._load_artist_slots()

    assert len(slots) == 1
    assert slots[0]["name"] == "我的画风"
    assert slots[0]["artists"] == "artist:mine"


def test_slot_labels_sync_to_schema(tmp_path, monkeypatch):
    """改画风名后，设置页下拉的 label 应同步成新名字。"""

    schema_file = tmp_path / "_conf_schema.json"
    schema_file.write_text(
        Path("_conf_schema.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    plugin = _make_slot_plugin(tmp_path, monkeypatch, sync_labels=True)
    monkeypatch.setattr(plugin, "_schema_path", lambda: schema_file)

    plugin._sync_slot_labels(
        [
            {"id": 1, "name": "我的画风", "model": "m", "artists": ""},
            {"id": 2, "name": "第二画风", "model": "m", "artists": ""},
        ]
    )

    data = json.loads(schema_file.read_text(encoding="utf-8"))
    node = data["active_artist_slot"]
    assert node["options"] == ["1", "2"]
    assert node["labels"] == ["1. 我的画风", "2. 第二画风"]


def test_slot_labels_not_rewritten_when_unchanged(tmp_path, monkeypatch):
    """标签没变时不应反复重写 schema 文件（避免无意义的磁盘写）。"""

    schema_file = tmp_path / "_conf_schema.json"
    schema_file.write_text(
        Path("_conf_schema.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    plugin = _make_slot_plugin(tmp_path, monkeypatch, sync_labels=True)
    monkeypatch.setattr(plugin, "_schema_path", lambda: schema_file)

    slots = [{"id": 1, "name": "我的画风", "model": "m", "artists": ""}]
    plugin._sync_slot_labels(slots)
    first_mtime = schema_file.stat().st_mtime_ns
    plugin._sync_slot_labels(slots)

    assert schema_file.stat().st_mtime_ns == first_mtime


# ---------------------------------------------------------------------------
# 增强功能开关：默认关闭时不得改变升级前的任何行为
# ---------------------------------------------------------------------------
def test_artist_slots_ignored_when_disabled(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch, active="1", enabled=False)
    plugin._save_artist_slots(
        [
            {
                "name": "A",
                "model": "nai-diffusion-5-full",
                "artists": "artist:slot",
            }
        ]
    )

    assert plugin._slot_model() == ""
    assert plugin._slot_artists() == ""
    # 画师串回退到原有逻辑（自定义风格 + custom_artists）
    assert plugin._openai_prompt("1girl", "custom").startswith("artist:base")


def test_slot_disabled_does_not_write_config(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch, enabled=False)

    assert plugin._slot_model() == ""
    assert not (tmp_path / "plugin_config.json").exists()


def test_grimoire_macro_not_expanded_when_disabled(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch, enabled=False)
    plugin.enable_grimoire = False

    assert plugin._expand_grimoire("1girl, 【法典:死库水】") == "1girl, 【法典:死库水】"


def test_grimoire_macro_expanded_when_enabled(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch, enabled=False)
    plugin.enable_grimoire = True
    plugin.model = "nai-diffusion-4-5-full"
    plugin.call_mode = "direct"

    expanded = plugin._expand_grimoire("1girl, 【法典:死库水】")

    assert "【法典" not in expanded
    assert "1girl" in expanded


def test_feature_flag_defaults():
    """槽位默认开启（内置画风已预置成槽位），法典与多角色块默认关闭。"""

    schema = json.loads(open("_conf_schema.json", encoding="utf-8").read())

    assert schema["enable_artist_slots"]["default"] is True
    assert schema["enable_character_blocks"]["default"] is False
    assert schema["enable_grimoire"]["default"] is False


def test_slot_template_declares_three_columns():
    """设置页的槽位模板必须有三列：画风名 / 适用模型 / 画师串。"""

    schema = json.loads(open("_conf_schema.json", encoding="utf-8").read())

    node = schema["artist_slots"]
    assert node["type"] == "template_list"
    items = node["templates"]["style"]["items"]
    assert set(items) == {"name", "model", "artists"}
    assert items["name"]["description"] == "画风名"
    assert items["model"]["description"] == "适用模型"
    assert items["artists"]["description"] == "对应画师串"


def test_slots_are_ordered_before_legacy_style():
    """设置页里槽位三项应排在「回退画风」之前，画风配置集中在一起。"""

    import collections

    schema = json.loads(
        open("_conf_schema.json", encoding="utf-8").read(),
        object_pairs_hook=collections.OrderedDict,
    )
    keys = list(schema)
    assert keys.index("enable_artist_slots") < keys.index("image_style")
    assert keys.index("artist_slots") < keys.index("image_style")
    assert keys.index("active_artist_slot") < keys.index("image_style")


def test_legacy_style_hidden_when_slots_enabled():
    """槽位开启时，旧的「默认风格」不再出现在设置页。"""

    schema = json.loads(open("_conf_schema.json", encoding="utf-8").read())

    assert schema["image_style"]["condition"] == {"enable_artist_slots": False}
    assert schema["custom_artists"]["condition"] == {
        "enable_artist_slots": False,
        "image_style": "自定义",
    }
    assert schema["artist_slots"]["condition"] == {"enable_artist_slots": True}


def test_match_style_slot_accepts_noisy_names(tmp_path, monkeypatch):
    """模型传画风名时容忍编号前缀、空格、大小写。"""

    plugin = _make_slot_plugin(tmp_path, monkeypatch, enabled=True)
    plugin._save_artist_slots(
        [
            {"name": "韩漫小清新风", "model": "m1", "artists": "a1"},
            {"name": "My SciFi", "model": "m2", "artists": "a2"},
        ]
    )

    assert plugin._match_style_slot("韩漫小清新风")["model"] == "m1"
    assert plugin._match_style_slot("2. 韩漫小清新风")["model"] == "m1"
    assert plugin._match_style_slot("myscifi")["model"] == "m2"
    assert plugin._match_style_slot("不存在的画风") is None
    assert plugin._match_style_slot("") is None


def test_match_style_slot_disabled_when_slots_off(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch, enabled=False)
    assert plugin._match_style_slot("韩漫小清新风") is None


def test_grimoire_tool_has_no_scope_param():
    """检索工具不暴露 sfw/nsfw 分级参数：只按当前模型词库检索。"""

    import inspect

    params = inspect.signature(NAIGenerateImagePlugin.NAI_Search_Grimoire).parameters
    assert "scope" not in params
    assert "query" in params


# ---------------------------------------------------------------------------
# 版本回退：V5 兼容 V4.5 词条，V4.5 用不了 V5 专属词条
# ---------------------------------------------------------------------------
def test_allowed_model_vers_fallback_chain():
    assert grimoire.allowed_model_vers("nai-diffusion-5-full") == ["NAI5", "NAI4.5"]
    assert grimoire.allowed_model_vers("nai-diffusion-4-5-full") == ["NAI4.5"]
    assert grimoire.allowed_model_vers(None) == []


def test_v5_search_falls_back_to_v45_entries():
    # 「动画画风」只存在于 V4.5 卷；V5 模型检索时应能命中
    hits = search_grimoire("动画画风", 5, "nai-diffusion-5-full")
    assert hits, "V5 应能回退到 V4.5 词条"
    assert all(item["model_ver"] in ("NAI5", "NAI4.5") for item in hits)
    assert any(item["model_ver"] == "NAI4.5" for item in hits)


def test_v45_search_cannot_reach_v5_only_entries():
    # 「ZUN绘」只存在于 V5 卷；V4.5 模型检索不应返回任何 V5 词条
    hits = search_grimoire("ZUN绘", 5, "nai-diffusion-4-5-full")
    assert all(item["model_ver"] == "NAI4.5" for item in hits)


def test_v5_prefers_own_volume_on_duplicate_titles():
    import sqlite3

    db_path = grimoire.get_grimoire_db_path()
    assert db_path is not None
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        """
        SELECT title FROM grimoire_entries WHERE model_ver = 'NAI5'
        INTERSECT SELECT title FROM grimoire_entries WHERE model_ver = 'NAI4.5'
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    assert row, "词库应存在两卷同名词条"
    title = row[0]

    v5_first = search_grimoire(title, 5, "nai-diffusion-5-full")[0]
    v45_first = search_grimoire(title, 5, "nai-diffusion-4-5-full")[0]
    assert v5_first["model_ver"] == "NAI5", "同名时 V5 模型应优先自己那卷"
    assert v45_first["model_ver"] == "NAI4.5"


# ---------------------------------------------------------------------------
# 面板套用画风槽位
# ---------------------------------------------------------------------------
def test_slot_by_id_finds_slot_and_rejects_bad_input(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch, enabled=True)
    plugin._save_artist_slots(
        [
            {"name": "韩漫小清新风", "model": "m1", "artists": "a1"},
            {"name": "本子里番风", "model": "m2", "artists": "a2"},
        ]
    )

    assert plugin._slot_by_id("2")["name"] == "本子里番风"
    assert plugin._slot_by_id(1)["model"] == "m1"
    assert plugin._slot_by_id("9") is None
    assert plugin._slot_by_id("abc") is None
    assert plugin._slot_by_id(None) is None


def test_slot_by_id_disabled_when_slots_off(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch, enabled=False)
    plugin._save_artist_slots([{"name": "A", "model": "m1", "artists": "a1"}])
    assert plugin._slot_by_id("1") is None


class _FakeWebRequest:
    def __init__(self, body):
        self._body = body

    async def json(self, default=None):
        return self._body


def _panel_plugin(tmp_path, monkeypatch):
    plugin = _make_slot_plugin(tmp_path, monkeypatch, enabled=True)
    plugin._save_artist_slots(
        [
            {
                "name": "默认画风",
                "model": "nai-diffusion-4-5-full",
                "artists": "artist:a",
            },
            {"name": "科幻风", "model": "nai-diffusion-5-full", "artists": "artist:b"},
        ]
    )
    plugin.image_size = "竖图"
    plugin.image_style = "vertical"
    plugin._generate_one_custom = AsyncMock(return_value=(b"img", "ok"))
    return plugin


def test_panel_generate_applies_selected_slot(tmp_path, monkeypatch):
    plugin = _panel_plugin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "main.web_request",
        _FakeWebRequest(
            {
                "nai_prompt": "1girl",
                "style": "vertical",
                "model": "panel-model",
                "slot_id": 2,
            }
        ),
    )

    asyncio.run(plugin._test_panel_generate())

    kwargs = plugin._generate_one_custom.await_args.kwargs
    assert kwargs["model"] == "nai-diffusion-5-full"
    assert kwargs["custom_artists"] == "artist:b"
    # 套用槽位时统一走 custom 分支，让槽位画师串（含空串）说了算
    assert plugin._generate_one_custom.await_args.args[1] == "custom"


def test_panel_generate_without_slot_keeps_panel_values(tmp_path, monkeypatch):
    plugin = _panel_plugin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "main.web_request",
        _FakeWebRequest(
            {"nai_prompt": "1girl", "style": "vertical", "model": "panel-model"}
        ),
    )

    asyncio.run(plugin._test_panel_generate())

    kwargs = plugin._generate_one_custom.await_args.kwargs
    assert kwargs["model"] == "panel-model"
    assert kwargs["custom_artists"] is None
    assert plugin._generate_one_custom.await_args.args[1] == "vertical"


def test_panel_generate_ignores_slot_when_feature_off(tmp_path, monkeypatch):
    plugin = _panel_plugin(tmp_path, monkeypatch)
    plugin.enable_artist_slots = False
    monkeypatch.setattr(
        "main.web_request",
        _FakeWebRequest({"nai_prompt": "1girl", "model": "panel-model", "slot_id": 2}),
    )

    asyncio.run(plugin._test_panel_generate())

    kwargs = plugin._generate_one_custom.await_args.kwargs
    assert kwargs["model"] == "panel-model"


def test_panel_generate_keeps_explicit_empty_artists(tmp_path, monkeypatch):
    plugin = _panel_plugin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "main.web_request",
        _FakeWebRequest(
            {"nai_prompt": "1girl", "style": "custom", "custom_artists": ""}
        ),
    )

    asyncio.run(plugin._test_panel_generate())

    assert plugin._generate_one_custom.await_args.kwargs["custom_artists"] == ""


def test_openai_prompt_skips_global_slot_when_flag_off(tmp_path, monkeypatch):
    """面板已按选中的槽位传画师串时，不再回退到设置页的生效槽位。"""

    plugin = _make_slot_plugin(tmp_path, monkeypatch, enabled=True)
    plugin._save_artist_slots(
        [{"name": "全局画风", "model": "m1", "artists": "artist:global"}]
    )

    # 默认行为：没有显式画师串时回退到生效槽位
    assert "artist:global" in plugin._openai_prompt("1girl", "custom")

    # 面板已套用槽位（画师串可能为空）时关闭回退
    assert "artist:global" not in plugin._openai_prompt(
        "1girl", "custom", "", use_slot_artists=False
    )
