from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATHS = [
    _PLUGIN_DIR.parent.parent / "plugin_data" / "astrbot_plugin_nai_image" / "grimoire.db",
    _PLUGIN_DIR / "data" / "grimoire.db",
]

_MACRO_RE = re.compile(r"[\[【]法典[:：]([^\]】]+)[\]】]")


def normalize_model_ver(model_name: str | None) -> str:
    """将模型名称（如 nai-diffusion-4-5-full / nai-diffusion-5-full）归一化为词库 model_ver ('NAI4.5' 或 'NAI5')。"""
    m = str(model_name or "").strip().lower()
    if "-5-" in m or "nai5" in m or m.endswith("-5") or m == "nai5":
        return "NAI5"
    return "NAI4.5"


def get_grimoire_db_path() -> Path | None:
    for p in DEFAULT_DB_PATHS:
        if p.exists():
            return p
    gz_path = _PLUGIN_DIR / "data" / "grimoire.db.gz"
    if gz_path.is_file():
        import gzip
        import shutil
        target_db = DEFAULT_DB_PATHS[0]
        try:
            target_db.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(gz_path, "rb") as f_in, open(target_db, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            return target_db
        except Exception:
            fallback_db = _PLUGIN_DIR / "data" / "grimoire.db"
            try:
                with gzip.open(gz_path, "rb") as f_in, open(fallback_db, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                return fallback_db
            except Exception:
                pass
    return None


def extract_clean_positive_tags(raw_tags: str) -> str:
    """从法典原始词条中提取干净的正向 Tag（剥离负面提示词、Steps设置行等）。"""
    lines = []
    for line in (raw_tags or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("负面", "negative", "Steps:", "Sampler:", "Prompt Guidance", "设置")):
            continue
        s = re.sub(r"^char\d+[:：]\s*", "", s, flags=re.IGNORECASE)
        lines.append(s.rstrip(",， "))
    return ", ".join(lines)


def search_grimoire(
    query: str = "",
    scope: str = "all",
    limit: int = 12,
    model_ver: str | None = None,
) -> list[dict[str, Any]]:
    """检索内置 NovelAI 法典词库，支持按当前生图模型版本 (NAI4.5 / NAI5) 强制物理隔离。"""
    db_path = get_grimoire_db_path()
    if not db_path:
        return []

    q = (query or "").strip()
    scope_norm = (scope or "all").strip().lower()
    limit = max(1, min(int(limit or 12), 50))
    locked_ver = normalize_model_ver(model_ver) if model_ver else None

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    where_clauses = []
    params: list[Any] = []

    # 1. 模型版本硬隔离：若传入了 locked_ver（后台当前模型），除非前端测试面板显式指定跨模型，否则强制锁定当前模型的词条
    if scope_norm in ("nai4.5", "nai4.5_all"):
        where_clauses.append("model_ver = 'NAI4.5'")
    elif scope_norm == "nai4.5_sfw":
        where_clauses.append("model_ver = 'NAI4.5' AND rating = 'sfw'")
    elif scope_norm == "nai4.5_nsfw":
        where_clauses.append("model_ver = 'NAI4.5' AND rating IN ('nsfw', 'nsfw_extreme')")
    elif scope_norm in ("nai5", "nai5_all"):
        where_clauses.append("model_ver = 'NAI5'")
    elif scope_norm == "nai5_sfw":
        where_clauses.append("model_ver = 'NAI5' AND rating = 'sfw'")
    elif scope_norm == "nai5_nsfw":
        where_clauses.append("model_ver = 'NAI5' AND rating IN ('nsfw', 'nsfw_extreme')")
    else:
        if locked_ver:
            where_clauses.append("model_ver = ?")
            params.append(locked_ver)
        if scope_norm == "sfw":
            where_clauses.append("rating = 'sfw'")
        elif scope_norm == "nsfw":
            where_clauses.append("rating IN ('nsfw', 'nsfw_extreme')")
        elif scope_norm not in ("all", "cross_all", ""):
            where_clauses.append("volume LIKE ?")
            params.append(f"%{scope}%")

    tokens = [t.strip() for t in re.split(r"[\s,，/]+", q) if t.strip()]
    for tok in tokens[:5]:
        where_clauses.append("(title LIKE ? OR category LIKE ? OR tags LIKE ?)")
        like_tok = f"%{tok}%"
        params.extend([like_tok, like_tok, like_tok])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    if tokens:
        first_tok = tokens[0]
        sql = f"""
            SELECT id, volume, rating, model_ver, category, title, tags,
                (CASE WHEN title = ? THEN 100
                      WHEN title LIKE ? THEN 60
                      WHEN title LIKE ? THEN 40
                      ELSE 0 END
                 + CASE WHEN category LIKE ? THEN 20 ELSE 0 END) AS relevance
            FROM grimoire_entries
            {where_sql}
            ORDER BY relevance DESC, length(title) ASC, id ASC
            LIMIT ?
        """
        score_params = [first_tok, f"{first_tok}%", f"%{first_tok}%", f"%{first_tok}%"]
        rows = cur.execute(sql, score_params + params + [limit]).fetchall()
    else:
        sql = f"""
            SELECT id, volume, rating, model_ver, category, title, tags, 10 AS relevance
            FROM grimoire_entries
            {where_sql}
            ORDER BY id ASC
            LIMIT ?
        """
        rows = cur.execute(sql, params + [limit]).fetchall()

    conn.close()
    results = []
    for r in rows:
        raw_tags = r["tags"] or ""
        results.append({
            "id": r["id"],
            "volume": r["volume"],
            "rating": r["rating"],
            "model_ver": r["model_ver"],
            "category": r["category"],
            "title": r["title"],
            "tags": raw_tags,
            "clean_tags": extract_clean_positive_tags(raw_tags),
        })
    return results


def expand_grimoire_macros(prompt: str, model_ver: str | None = None) -> str:
    """展开提示词中的 [法典:词条名] 或 【法典:词条名】 宏（严格限定在当前模型版本词库内）。"""
    if not prompt or ("法典:" not in prompt and "法典：" not in prompt):
        return prompt

    def _repl(m: re.Match) -> str:
        kw = m.group(1).strip()
        hits = search_grimoire(kw, scope="all", limit=1, model_ver=model_ver)
        if hits:
            return hits[0]["clean_tags"]
        return kw

    return _MACRO_RE.sub(_repl, prompt)


def build_grimoire_translation_hint(
    natural_text: str, max_matches: int = 5, model_ver: str | None = None
) -> str:
    """从自然语言描述中提取命中的当前模型专属法典词条，供转译模型参考。"""
    if not natural_text or len(natural_text.strip()) < 2:
        return ""
    db_path = get_grimoire_db_path()
    if not db_path:
        return ""

    text = natural_text.strip()
    cn_chunks = re.findall(r"[\u4e00-\u9fff]{2,12}", text)
    if not cn_chunks:
        return ""

    locked_ver = normalize_model_ver(model_ver) if model_ver else "NAI4.5"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    matched_rows = []
    seen_ids = set()

    rows = cur.execute(
        """
        SELECT id, volume, category, title, tags
        FROM grimoire_entries
        WHERE model_ver = ?
          AND length(title) BETWEEN 2 AND 14
          AND instr(?, title) > 0
        ORDER BY length(title) DESC, id ASC
        LIMIT 12
        """,
        (locked_ver, text),
    ).fetchall()

    for r in rows:
        t = r["title"]
        if t in ("其他", "杂项", "正面", "侧面", "背面", "室内", "室外", "日常", "内衣", "泳装", "睡衣"):
            continue
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            matched_rows.append(r)
            if len(matched_rows) >= max_matches:
                break

    conn.close()
    if not matched_rows:
        return ""

    lines = [f"\n\n【NovelAI ({locked_ver}) 专属法典命中词条参考（若与用户意图一致，请优先采用或融合以下经过实测的 {locked_ver} 标签）】："]
    for r in matched_rows:
        clean = extract_clean_positive_tags(r["tags"])
        if len(clean) > 260:
            clean = clean[:260] + "..."
        lines.append(f"- 《{r['title']}》({r['volume']} / {r['category']}): {clean}")
    return "\n".join(lines)
