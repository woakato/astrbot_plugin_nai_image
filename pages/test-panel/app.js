// NAI 生图测试面板前端逻辑（支持三合一槽位：自定义名称 + 绑定模型 + 专属画师串，切换自动同步提示词与词库）
(function () {
  "use strict";

  const BRIDGE_URLS = [
    "/assets/astrbot-page-bridge.js",
    "https://cdn.jsdelivr.net/npm/astrbot-page-bridge@latest/dist/astrbot-page-bridge.js",
  ];

  const MODEL_GROUPS = {
    "nai4.5": {
      label: "NAI V4.5",
      shortTag: "V4.5",
      grimTitle: "NAI V4.5 专属 Tag 词库（共 18,505 条）",
      defaultScope: "nai4.5_all",
      scopes: [
        { value: "nai4.5_all", text: "V4.5 全部词条 (18,505)" },
        { value: "nai4.5_sfw", text: "V4.5 常规词条 (6,448)" },
        { value: "nai4.5_nsfw", text: "V4.5 NSFW 词条 (12,057)" },
      ],
      quickCategories: ["画风", "光影", "视角", "表情", "制服", "连衣裙", "丝袜", "体位"],
      defaultQuery: "画风",
    },
    "nai5": {
      label: "NAI V5",
      shortTag: "V5",
      grimTitle: "NAI V5 专属 Tag 词库（共 1,158 条）",
      defaultScope: "nai5_all",
      scopes: [
        { value: "nai5_all", text: "V5 全部词条 (1,158)" },
        { value: "nai5_sfw", text: "V5 常规词条 (238)" },
        { value: "nai5_nsfw", text: "V5 NSFW 词条 (920)" },
      ],
      quickCategories: ["画风", "人体", "服装", "表情", "姿势", "视角", "玩法", "特写"],
      defaultQuery: "",
    },
  };

  function getModelGroupKey(modelName) {
    const m = String(modelName || "").toLowerCase();
    if (m.includes("-5-") || m.includes("nai5") || m.endsWith("-5")) {
      return "nai5";
    }
    return "nai4.5";
  }

  let bridge = null;
  const state = {
    config: {},
    slots: [],
    activeSlotId: 1,
    currentGroup: "nai4.5",
    groupPrompts: {
      "nai4.5": { naiPrompt: "", nlPrompt: "", grimQuery: "画风" },
      "nai5": { naiPrompt: "", nlPrompt: "", grimQuery: "" },
    },
    generating: false,
  };

  const $ = (id) => document.getElementById(id);

  const el = {
    connBadge: $("connBadge"),
    btnRefresh: $("btnRefresh"),
    activeSlotBadge: $("activeSlotBadge"),
    backendSlotBadge: $("backendSlotBadge"),
    btnAddSlot: $("btnAddSlot"),
    btnDeleteSlot: $("btnDeleteSlot"),
    btnSaveSlot: $("btnSaveSlot"),
    slotTabs: $("slotTabs"),
    inpSlotName: $("inpSlotName"),
    selModel: $("selModel"),
    inpArtists: $("inpArtists"),
    activeModelBadge: $("activeModelBadge"),
    btnClearPrompt: $("btnClearPrompt"),
    lblNaiPrompt: $("lblNaiPrompt"),
    naiPrompt: $("naiPrompt"),
    nlPrompt: $("nlPrompt"),
    selSize: $("selSize"),
    selStyle: $("selStyle"),
    inpSteps: $("inpSteps"),
    inpScale: $("inpScale"),
    selSampler: $("selSampler"),
    btnGenerate: $("btnGenerate"),
    titleGrimBox: $("titleGrimBox"),
    grimScope: $("grimScope"),
    grimQuery: $("grimQuery"),
    btnGrimSearch: $("btnGrimSearch"),
    grimQuickTags: $("grimQuickTags"),
    grimResults: $("grimResults"),
    grimCountBadge: $("grimCountBadge"),
    genTimer: $("genTimer"),
    canvasEmpty: $("canvasEmpty"),
    resultImg: $("resultImg"),
    resultActions: $("resultActions"),
    btnDownloadImg: $("btnDownloadImg"),
    btnCopyFinalPrompt: $("btnCopyFinalPrompt"),
    finalPromptPre: $("finalPromptPre"),
    toast: $("toast"),
  };

  let toastTimer = null;
  function showToast(msg) {
    if (!el.toast) return;
    el.toast.textContent = msg;
    el.toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.toast.classList.remove("show"), 2600);
  }

  async function getBridge() {
    if (bridge) return bridge;
    for (const url of BRIDGE_URLS) {
      try {
        const mod = await import(url);
        const BridgeCls = mod.AstrBotPageBridge || mod.default;
        if (BridgeCls) {
          bridge = new BridgeCls({ pluginName: "astrbot_plugin_nai_image" });
          return bridge;
        }
      } catch (_) {}
    }
    bridge = {
      async apiGet(path) {
        const r = await fetch("/astrbot_plugin_nai_image/" + path.replace(/^\/+/, ""));
        return r.json();
      },
      async apiPost(path, body) {
        const r = await fetch("/astrbot_plugin_nai_image/" + path.replace(/^\/+/, ""), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body || {}),
        });
        return r.json();
      },
    };
    return bridge;
  }

  function getActiveSlot() {
    return (
      state.slots.find((s) => Number(s.id) === Number(state.activeSlotId)) ||
      state.slots[0] ||
      null
    );
  }

  function syncActiveSlotFromInputs() {
    const slot = getActiveSlot();
    if (!slot) return;
    slot.name = el.inpSlotName.value.trim() || `槽位 ${slot.id}`;
    slot.model = el.selModel.value;
    slot.artists = el.inpArtists.value;
  }

  function saveCurrentGroupPrompts() {
    const g = state.groupPrompts[state.currentGroup];
    if (!g) return;
    g.naiPrompt = el.naiPrompt.value;
    g.nlPrompt = el.nlPrompt.value;
    g.grimQuery = el.grimQuery.value;
  }

  async function persistPanelCache() {
    saveCurrentGroupPrompts();
    try {
      const b = await getBridge();
      await b.apiPost("test_panel/save_cache", {
        activeSlotId: state.activeSlotId,
        groupPrompts: state.groupPrompts,
      });
    } catch (_) {}
  }

  function renderSlotTabs() {
    el.slotTabs.innerHTML = "";
    state.slots.forEach((slot) => {
      const isAct = Number(slot.id) === Number(state.activeSlotId);
      const gKey = getModelGroupKey(slot.model);
      const shortTag = (MODEL_GROUPS[gKey] || MODEL_GROUPS["nai4.5"]).shortTag;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "slot-tab" + (isAct ? " active" : "");
      btn.innerHTML = `<span>${slot.name || "槽位 " + slot.id}</span><span class="slot-model-tag">${shortTag}</span>`;
      btn.addEventListener("click", () => {
        if (Number(slot.id) === Number(state.activeSlotId)) return;
        syncActiveSlotFromInputs();
        selectSlot(slot.id, true);
      });
      el.slotTabs.appendChild(btn);
    });
  }

  function applyModelGroupUI(newGroupKey, isInit = false) {
    if (!isInit && state.currentGroup && state.currentGroup !== newGroupKey) {
      saveCurrentGroupPrompts();
    }
    state.currentGroup = newGroupKey;
    const gInfo = MODEL_GROUPS[newGroupKey] || MODEL_GROUPS["nai4.5"];
    const gData = state.groupPrompts[newGroupKey] || { naiPrompt: "", nlPrompt: "", grimQuery: "" };

    el.activeModelBadge.textContent = "模型组：" + gInfo.label;
    el.lblNaiPrompt.textContent = `正向提示词 (${gInfo.label} 组独立记忆 · 支持权重 1.2::tag:: 及【法典:词条名】)`;
    el.titleGrimBox.textContent = gInfo.grimTitle;

    el.naiPrompt.value = gData.naiPrompt || "";
    el.nlPrompt.value = gData.nlPrompt || "";

    el.grimScope.innerHTML = gInfo.scopes
      .map((s) => `<option value="${s.value}">${s.text}</option>`)
      .join("");
    el.grimScope.value = gInfo.defaultScope;

    el.grimQuickTags.innerHTML =
      '<span class="capsule-label">' + gInfo.label + ' 常用分类：</span>' +
      gInfo.quickCategories
        .map((q) => `<button class="grim-quick" data-q="${q}" type="button">${q}</button>`)
        .join("");

    el.grimQuickTags.querySelectorAll(".grim-quick").forEach((btn) => {
      btn.addEventListener("click", () => {
        el.grimQuery.value = btn.getAttribute("data-q") || "";
        searchGrimoire(el.grimQuery.value, true);
      });
    });

    el.grimQuery.value = gData.grimQuery !== undefined ? gData.grimQuery : gInfo.defaultQuery;
    searchGrimoire(el.grimQuery.value, true);
  }

  // 选中某个槽位：同步填入该槽位的名称、绑定模型与画师串，并同步切换模型组提示词与词库
  function selectSlot(slotId, autoSaveBackend = false, isInit = false) {
    const slot =
      state.slots.find((s) => Number(s.id) === Number(slotId)) || state.slots[0];
    if (!slot) return;
    state.activeSlotId = Number(slot.id);
    el.inpSlotName.value = slot.name || `槽位 ${slot.id}`;
    el.selModel.value = slot.model || "nai-diffusion-4-5-full";
    el.inpArtists.value = slot.artists || "";
    el.activeSlotBadge.textContent = `UI 测试中：${slot.name || "槽位 " + slot.id}`;
    el.connBadge.textContent = `已就绪 (${slot.model})`;

    renderSlotTabs();
    const gKey = getModelGroupKey(slot.model);
    applyModelGroupUI(gKey, isInit);

    if (autoSaveBackend) {
      persistPanelCache();
      showToast(`UI 测试槽位已切换至「${slot.name}」（不影响后端正式设置）`);
    }
  }

  async function saveSlotsToBackend(showSuccessToast = true) {
    syncActiveSlotFromInputs();
    renderSlotTabs();
    const slot = getActiveSlot();
    if (slot) {
      el.activeSlotBadge.textContent = `当前激活：${slot.name}`;
      el.connBadge.textContent = `已就绪 (${slot.model})`;
    }
    try {
      const b = await getBridge();
      await b.apiPost("test_panel/save_artists", {
        active_slot_id: state.activeSlotId,
        slots: state.slots,
      });
      persistPanelCache();
      if (showSuccessToast) {
        showToast("已保存槽位列表并同步至后端设置下拉框（后端正式槽位请在插件配置中选择）");
      }
    } catch (err) {
      showToast("保存槽位失败: " + (err.message || err));
    }
  }

  function appendTagsToBoard(tags) {
    const clean = (tags || "").trim().replace(/^,+|,+$/g, "").trim();
    if (!clean) return;
    const cur = el.naiPrompt.value.trim().replace(/,+$/g, "").trim();
    el.naiPrompt.value = cur ? cur + ", " + clean : clean;
    persistPanelCache();
  }

  async function loadAllContext() {
    el.connBadge.textContent = "同步中…";
    try {
      const b = await getBridge();
      const [cfg, ctx, cacheRes] = await Promise.all([
        b.apiGet("test_panel/config"),
        b.apiGet("test_panel/starry_context"),
        b.apiGet("test_panel/load_cache"),
      ]);
      state.config = cfg || {};
      state.slots = Array.isArray(ctx && ctx.artist_slots) && ctx.artist_slots.length
        ? ctx.artist_slots
        : [
            {
              id: 1,
              name: "槽位 1 · V4.5 主力画风",
              model: cfg.openai_api_model || "nai-diffusion-4-5-full",
              artists: cfg.custom_artists || "",
            },
          ];

      const cached = (cacheRes && cacheRes.data) || {};
      if (cached.groupPrompts && typeof cached.groupPrompts === "object") {
        if (cached.groupPrompts["nai4.5"]) {
          state.groupPrompts["nai4.5"] = Object.assign(state.groupPrompts["nai4.5"], cached.groupPrompts["nai4.5"]);
        }
        if (cached.groupPrompts["nai5"]) {
          state.groupPrompts["nai5"] = Object.assign(state.groupPrompts["nai5"], cached.groupPrompts["nai5"]);
        }
      }

      const backendId = (ctx && ctx.backend_active_slot_id) || "1";
      const backendSlot = state.slots.find((s) => String(s.id) === String(backendId));
      if (el.backendSlotBadge) {
        el.backendSlotBadge.textContent = "后端正式槽位：" + (backendSlot ? backendSlot.name : "槽位 " + backendId);
      }
      const initSlotId = cached.activeSlotId || (ctx && ctx.ui_test_slot_id) || state.slots[0].id;
      if (cfg.steps) el.inpSteps.value = cfg.steps;
      if (cfg.scale !== undefined) el.inpScale.value = cfg.scale;
      if (cfg.sampler) el.selSampler.value = cfg.sampler;

      selectSlot(initSlotId, false, true);
    } catch (err) {
      el.connBadge.textContent = "连接异常: " + (err.message || err);
    }
  }

  async function searchGrimoire(queryOverride, allowEmpty = false) {
    const q = (queryOverride !== undefined ? queryOverride : el.grimQuery.value).trim();
    const scope = el.grimScope.value;
    el.grimCountBadge.textContent = "检索中…";
    try {
      const b = await getBridge();
      const res = await b.apiPost("test_panel/grimoire_search", {
        query: q,
        scope: scope,
        limit: 18,
      });
      const items = (res && res.data && res.data.items) || [];
      el.grimCountBadge.textContent = `显示 ${items.length} 条`;
      el.grimResults.innerHTML = "";
      if (!items.length) {
        el.grimResults.innerHTML = `<div style="grid-column:1/-1;text-align:center;color:#8b95a5;padding:18px;">当前模型词库下未找到包含「${q}」的词条</div>`;
        return;
      }
      items.forEach((it) => {
        const card = document.createElement("div");
        card.className = "grim-card";
        card.innerHTML = `
          <div class="grim-card-head">
            <span class="grim-title">${it.title}</span>
            <span class="grim-vol">${it.volume || ""} · ${it.category || ""}</span>
          </div>
          <div class="grim-tags">${(it.clean_tags || "").slice(0, 180)}</div>
          <div class="grim-card-actions">
            <button class="btn btn-sm btn-accent act-append" type="button">+ 追加 Tags</button>
            <button class="btn btn-sm btn-ghost act-macro" type="button">插入【法典:${it.title}】</button>
          </div>
        `;
        card.querySelector(".act-append").addEventListener("click", () => {
          appendTagsToBoard(it.clean_tags);
          showToast(`已追加「${it.title}」的 Tags`);
        });
        card.querySelector(".act-macro").addEventListener("click", () => {
          appendTagsToBoard(`【法典:${it.title}】`);
          showToast(`已插入宏【法典:${it.title}】`);
        });
        el.grimResults.appendChild(card);
      });
    } catch (err) {
      el.grimCountBadge.textContent = "检索失败";
      showToast("检索异常: " + (err.message || err));
    }
  }

  async function generateArtwork() {
    if (state.generating) return;
    const naiPrompt = el.naiPrompt.value.trim();
    const nlPrompt = el.nlPrompt.value.trim();
    if (!naiPrompt && !nlPrompt) {
      showToast("请先填写提示词");
      return;
    }

    syncActiveSlotFromInputs();
    persistPanelCache();
    state.generating = true;
    el.btnGenerate.disabled = true;
    el.btnGenerate.textContent = "正在生成中…";
    const t0 = Date.now();
    const tick = setInterval(() => {
      el.genTimer.textContent = ((Date.now() - t0) / 1000).toFixed(1) + "s";
    }, 200);

    try {
      const b = await getBridge();
      const payload = {
        model: el.selModel.value,
        nai_prompt: naiPrompt,
        nl_prompt: nlPrompt,
        size: el.selSize.value,
        style: el.selStyle.value,
        custom_artists: el.inpArtists.value.trim(),
        steps: parseInt(el.inpSteps.value, 10) || 24,
        scale: parseFloat(el.inpScale.value) || 6.0,
        sampler: el.selSampler.value,
        n: 1,
      };
      const res = await b.apiPost("test_panel/generate_openai", payload);
      clearInterval(tick);
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1) + "s";
      if (!res || res.status !== "ok" || !res.data || !res.data.length) {
        el.genTimer.textContent = "生成失败";
        showToast((res && res.message) || "生图失败，请检查日志");
        return;
      }

      const b64 = res.data[0].b64_json;
      const dataUrl = "data:image/png;base64," + b64;
      el.resultImg.src = dataUrl;
      el.resultImg.classList.remove("hidden");
      el.canvasEmpty.classList.add("hidden");
      el.resultActions.classList.remove("hidden");
      el.btnDownloadImg.href = dataUrl;
      el.btnDownloadImg.download = "nai_" + Date.now() + ".png";

      const fullPrompt = (res.merge_info && res.merge_info.full_prompt) || naiPrompt;
      el.finalPromptPre.textContent = fullPrompt;
      el.genTimer.textContent = "完成 (" + elapsed + ")";
      showToast("图片生成完成");
    } catch (err) {
      clearInterval(tick);
      el.genTimer.textContent = "请求异常";
      showToast("请求失败: " + (err.message || err));
    } finally {
      state.generating = false;
      el.btnGenerate.disabled = false;
      el.btnGenerate.textContent = "立即测试生图（仅使用当前 UI 面板所选槽位与参数，不影响后端正式设置）";
    }
  }

  function bindEvents() {
    el.btnRefresh.addEventListener("click", loadAllContext);

    // 修改槽位名称或绑定模型时，实时更新当前槽位标签并同步词库
    el.inpSlotName.addEventListener("input", () => {
      syncActiveSlotFromInputs();
      renderSlotTabs();
    });

    el.selModel.addEventListener("change", () => {
      syncActiveSlotFromInputs();
      renderSlotTabs();
      const newGroup = getModelGroupKey(el.selModel.value);
      applyModelGroupUI(newGroup, false);
    });

    el.inpArtists.addEventListener("input", () => {
      syncActiveSlotFromInputs();
    });

    el.btnSaveSlot.addEventListener("click", () => saveSlotsToBackend(true));

    el.btnAddSlot.addEventListener("click", () => {
      syncActiveSlotFromInputs();
      const nextId = state.slots.reduce((max, s) => Math.max(max, Number(s.id) || 0), 0) + 1;
      const newSlot = {
        id: nextId,
        name: `自定义槽位 ${nextId}`,
        model: el.selModel.value || "nai-diffusion-4-5-full",
        artists: "",
      };
      state.slots.push(newSlot);
      selectSlot(nextId, true);
    });

    el.btnDeleteSlot.addEventListener("click", () => {
      if (state.slots.length <= 1) {
        showToast("至少需要保留 1 个预设槽位");
        return;
      }
      const cur = getActiveSlot();
      if (!window.confirm(`确定要删除「${cur ? cur.name : "当前槽位"}」吗？`)) return;
      state.slots = state.slots.filter((s) => Number(s.id) !== Number(state.activeSlotId));
      selectSlot(state.slots[0].id, true);
    });

    el.naiPrompt.addEventListener("change", persistPanelCache);
    el.nlPrompt.addEventListener("change", persistPanelCache);

    el.btnClearPrompt.addEventListener("click", () => {
      el.naiPrompt.value = "";
      el.nlPrompt.value = "";
      persistPanelCache();
      showToast("当前模型组提示词已清空");
    });

    el.grimScope.addEventListener("change", () => searchGrimoire(el.grimQuery.value, true));
    el.btnGrimSearch.addEventListener("click", () => searchGrimoire(undefined, true));
    el.grimQuery.addEventListener("keydown", (e) => {
      if (e.key === "Enter") searchGrimoire(undefined, true);
    });

    el.btnGenerate.addEventListener("click", generateArtwork);
    el.btnCopyFinalPrompt.addEventListener("click", () => {
      navigator.clipboard.writeText(el.finalPromptPre.textContent || "");
      showToast("已复制完整提示词");
    });
  }

  bindEvents();
  loadAllContext();
})();
