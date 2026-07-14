/**
 * NAI 生图测试面板 - 前端逻辑 v2.0
 *
 * 改版要点：
 *   - 双提示词框：NAI 风格 + 自然语言
 *   - 不再从 settings 加载表单数据，改用 localStorage 缓存
 *   - 生成时后端自动转译 NL + 合并 NAI → 完整 prompt
 *   - 展示合并步骤
 */

(function () {
  "use strict";

  // ===== DOM 引用 =====
  const $ = (id) => document.getElementById(id);
  const els = {
    naiPrompt: $("naiPrompt"),
    nlPrompt: $("nlPrompt"),
    sampler: $("sampler"),
    size: $("size"),
    steps: $("steps"),
    scale: $("scale"),
    cfg: $("cfg"),
    noiseSchedule: $("noiseSchedule"),
    model: $("model"),
    count: $("count"),
    style: $("style"),
    customArtistsWrapper: $("customArtistsWrapper"),
    customArtists: $("customArtists"),
    negative: $("negative"),
    loadDefaultNegative: $("loadDefaultNegative"),
    generateBtn: $("generateBtn"),
    trialBtn: $("trialBtn"),
    trialStatus: $("trialStatus"),
    trialStatusText: $("trialStatusText"),
    resetBtn: $("resetBtn"),
    tokenBadge: $("tokenBadge"),
    baseUrlBadge: $("baseUrlBadge"),
    resultMeta: $("resultMeta"),
    emptyState: $("emptyState"),
    loadingState: $("loadingState"),
    loadingText: $("loadingText"),
    errorState: $("errorState"),
    errorMsg: $("errorMsg"),
    retryBtn: $("retryBtn"),
    resultGrid: $("resultGrid"),
    mergeInfo: $("mergeInfo"),
    mergeSteps: $("mergeSteps"),
  };

  // ===== 状态 =====
  let isGenerating = false;
  let lastRequestBody = null;
  const CACHE_KEY = "nai_test_panel_v2";

  // ===== 工具函数 =====
  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  function setBadge(el, text, type) {
    el.textContent = text;
    el.className = "badge " + (type || "badge-neutral");
  }

  /**
   * 等待 Bridge SDK 就绪
   */
  async function getBridge() {
    const deadline = Date.now() + 5000;
    while (!window.AstrBotPluginPage && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 100));
    }
    if (!window.AstrBotPluginPage) {
      throw new Error("Bridge SDK 未就绪，请从 AstrBot 后台的插件拓展页打开此面板");
    }
    await window.AstrBotPluginPage.ready();
    return window.AstrBotPluginPage;
  }

  // ===== localStorage 缓存 =====
  function getCachedFields() {
    return [
      "naiPrompt", "nlPrompt", "sampler", "size", "steps", "scale",
      "cfg", "noiseSchedule", "model", "count", "style", "customArtists", "negative"
    ];
  }

  function saveCache() {
    const data = {};
    getCachedFields().forEach((key) => {
      const el = els[key];
      if (el) data[key] = el.value;
    });
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(data));
    } catch (e) {
      // localStorage 不可用时静默忽略
    }
  }

  function loadCache() {
    try {
      const raw = localStorage.getItem(CACHE_KEY);
      if (!raw) return false;
      const data = JSON.parse(raw);
      let restored = false;
      getCachedFields().forEach((key) => {
        const el = els[key];
        if (el && data[key] != null) {
          el.value = data[key];
          restored = true;
        }
      });
      return restored;
    } catch (e) {
      return false;
    }
  }

  function clearCache() {
    try {
      localStorage.removeItem(CACHE_KEY);
    } catch (e) {
      // ignore
    }
  }

  // ===== 加载 Token 状态（仅 badge，不填表单） =====
  async function loadTokenStatus() {
    try {
      const bridge = await getBridge();
      const resp = await bridge.apiGet("test_panel/config");
      const config = (resp && resp.data) ? resp.data : resp;
      if (config.image_gen_key === "已配置") {
        setBadge(els.tokenBadge, "Token: 已配置", "badge-success");
      } else {
        setBadge(els.tokenBadge, "Token: 未配置", "badge-error");
      }
      setBadge(els.baseUrlBadge, config.base_url || "--", "badge-info");
    } catch (err) {
      setBadge(els.tokenBadge, "配置加载失败", "badge-error");
    }
  }

  // ===== 表单交互 =====
  function toggleCustomArtists() {
    if (els.style.value === "custom") {
      show(els.customArtistsWrapper);
    } else {
      hide(els.customArtistsWrapper);
    }
    saveCache();
  }

  /**
   * 构建生图请求体（双提示词版本）
   */
  function buildRequestBody() {
    const safeInt = (v, d) => { const n = parseInt(v, 10); return Number.isNaN(n) ? d : n; };
    const safeFloat = (v, d) => { const n = parseFloat(v); return Number.isNaN(n) ? d : n; };

    const body = {
      nai_prompt: els.naiPrompt.value.trim(),
      nl_prompt: els.nlPrompt.value.trim(),
      style: els.style.value,
      size: els.size.value,
      sampler: els.sampler.value,
      steps: safeInt(els.steps.value, 24),
      scale: safeFloat(els.scale.value, 6),
      cfg: safeFloat(els.cfg.value, 7),
      noise_schedule: els.noiseSchedule.value,
      model: els.model.value,
      n: safeInt(els.count.value, 1),
    };

    const neg = els.negative.value.trim();
    if (neg) body.negative = neg;

    if (body.style === "custom") {
      body.custom_artists = els.customArtists.value.trim();
    }

    return body;
  }

  // ===== 生成图片 =====
  async function generate() {
    if (isGenerating) return;

    const body = buildRequestBody();
    if (!body.nai_prompt && !body.nl_prompt) {
      showError("请至少填写一个提示词框（NAI 风格或自然语言）。");
      return;
    }

    lastRequestBody = body;
    isGenerating = true;
    setLoading(true);
    hideError();
    hideResults();
    hideMergeInfo();

    try {
      const bridge = await getBridge();
      const resp = await bridge.apiPost("test_panel/generate", body);

      let images, mergeInfo;
      if (Array.isArray(resp)) {
        images = resp;
      } else if (resp && Array.isArray(resp.data)) {
        images = resp.data;
        mergeInfo = resp.merge_info;
      } else if (resp && resp.data && Array.isArray(resp.data.data)) {
        images = resp.data.data;
        mergeInfo = resp.data.merge_info;
      } else {
        images = resp;
        mergeInfo = (resp && resp.merge_info) || (resp && resp.data && resp.data.merge_info);
      }

      if (!images || !Array.isArray(images) || images.length === 0) {
        // 可能是错误响应
        const errMsg = (resp && resp.message) || (resp && resp.data && resp.data.message) || JSON.stringify(resp).slice(0, 200);
        throw new Error(errMsg);
      }

      // 展示合并步骤
      if (mergeInfo) {
        displayMergeInfo(mergeInfo);
      }

      displayResults(images, body);
    } catch (err) {
      const msg = err?.message || String(err);
      showError(msg);
    } finally {
      isGenerating = false;
      setLoading(false);
    }
  }

  // ===== 合并步骤展示 =====
  function displayMergeInfo(info) {
    hide(els.emptyState);
    els.mergeSteps.innerHTML = "";

    const steps = [];

    // Step 1: NAI 提示词
    if (info.nai_prompt) {
      steps.push({ label: "NAI 风格提示词（原样保留）", value: info.nai_prompt });
    }

    // Step 2: 自然语言提示词
    if (info.nl_prompt) {
      steps.push({ label: "自然语言提示词（待转译）", value: info.nl_prompt });
    }

    // Step 3: 转译结果
    if (info.nl_prompt && info.translated_nl) {
      const same = info.translated_nl === info.nl_prompt;
      steps.push({
        label: same ? "转译结果（未配置转译模型，原样使用）" : "转译结果（模型转译为 NAI 标签）",
        value: info.translated_nl,
      });
    }

    // Step 4: 合并结果
    steps.push({ label: "完整 Prompt（发送至生图站点）", value: info.full_prompt, highlight: true });

    steps.forEach((step, idx) => {
      const row = document.createElement("div");
      row.className = "merge-step";

      const num = document.createElement("span");
      num.className = "merge-step-num";
      num.textContent = String(idx + 1);

      const body = document.createElement("div");
      body.className = "merge-step-body";

      const label = document.createElement("div");
      label.className = "merge-step-label";
      label.textContent = step.label;

      const value = document.createElement("div");
      value.className = "merge-step-value" + (step.highlight ? " highlight" : "");
      value.textContent = step.value;

      body.appendChild(label);
      body.appendChild(value);
      row.appendChild(num);
      row.appendChild(body);
      els.mergeSteps.appendChild(row);
    });

    show(els.mergeInfo);
  }

  function hideMergeInfo() {
    hide(els.mergeInfo);
  }

  // ===== UI 状态控制 =====
  function setLoading(loading) {
    if (loading) {
      hide(els.emptyState);
      hide(els.errorState);
      show(els.loadingState);
      els.generateBtn.disabled = true;
      els.trialBtn.disabled = true;
      els.loadingText.textContent = "正在转译 + 生成图片...";
    } else {
      hide(els.loadingState);
      els.generateBtn.disabled = false;
    }
  }

  function showError(msg) {
    hide(els.emptyState);
    hide(els.loadingState);
    hide(els.resultGrid);
    hideMergeInfo();
    show(els.errorState);
    els.errorMsg.textContent = msg || "未知错误";
  }

  function hideError() {
    hide(els.errorState);
  }

  function hideResults() {
    hide(els.resultGrid);
    hide(els.resultMeta);
    hideMergeInfo();
    show(els.emptyState);
  }

  function displayResults(images, requestBody) {
    hide(els.emptyState);
    hide(els.errorState);

    const styleNames = {
      vertical: "韩漫小清新", comicDoujin: "漫画同人", r18: "2.5D唯美",
      lolita25d: "2.5D唯美(萝)", anime: "本子里番", galgame: "GalGame", custom: "自定义",
    };
    const sizeNames = {
      portrait: "竖图", landscape: "横图", square: "方图",
    };
    const metaText = `${styleNames[requestBody.style] || requestBody.style} · ${sizeNames[requestBody.size] || requestBody.size} · ${images.length}张`;
    setBadge(els.resultMeta, metaText, "badge-success");
    show(els.resultMeta);

    els.resultGrid.innerHTML = "";
    els.resultGrid.className = "result-grid";

    if (images.length > 1) {
      els.resultGrid.classList.add(images.length === 2 ? "cols-2" : "cols-4");
    }

    images.forEach((item, idx) => {
      const b64 = item.b64_json || item.b64 || item;
      const wrap = document.createElement("div");
      wrap.className = "result-item";

      const label = document.createElement("span");
      label.className = "result-item-label";
      label.textContent = `${idx + 1} / ${images.length}`;
      wrap.appendChild(label);

      const img = document.createElement("img");
      img.src = "data:image/png;base64," + b64;
      img.alt = `生成结果 ${idx + 1}`;
      img.addEventListener("click", () => openLightbox(img.src));
      wrap.appendChild(img);

      const actions = document.createElement("div");
      actions.className = "result-item-actions";

      const dlBtn = document.createElement("button");
      dlBtn.className = "result-item-action";
      dlBtn.textContent = "下载";
      dlBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        downloadImage(b64, idx + 1);
      });
      actions.appendChild(dlBtn);

      wrap.appendChild(actions);
      els.resultGrid.appendChild(wrap);
    });

    show(els.resultGrid);
  }

  // ===== 图片放大 =====
  function openLightbox(src) {
    const lb = document.createElement("div");
    lb.className = "lightbox";
    const img = document.createElement("img");
    img.src = src;
    img.addEventListener("click", (e) => e.stopPropagation());
    lb.appendChild(img);
    lb.addEventListener("click", () => lb.remove());
    document.body.appendChild(lb);
  }

  function downloadImage(b64, index) {
    const link = document.createElement("a");
    link.href = "data:image/png;base64," + b64;
    link.download = `nai_test_${Date.now()}_${index}.png`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  // ===== 重置参数 =====
  function resetParams() {
    clearCache();
    els.naiPrompt.value = "";
    els.nlPrompt.value = "";
    els.sampler.value = "k_dpmpp_2m_sde";
    els.steps.value = "24";
    els.scale.value = "6";
    els.cfg.value = "7";
    els.noiseSchedule.value = "karras";
    els.model.value = "nai-diffusion-4-5-full";
    els.style.value = "vertical";
    els.size.value = "portrait";
    els.count.value = "1";
    els.negative.value = "";
    els.customArtists.value = "";
    toggleCustomArtists();
  }

  // ===== 载入默认负面词 =====
  async function loadDefaultNegative() {
    try {
      const bridge = await getBridge();
      const resp = await bridge.apiGet("test_panel/config");
      const config = (resp && resp.data) ? resp.data : resp;
      if (config.default_negative) {
        els.negative.value = config.default_negative;
      } else {
        fallbackDefaultNegative();
      }
    } catch (e) {
      fallbackDefaultNegative();
    }
    saveCache();
  }

  function fallbackDefaultNegative() {
    els.negative.value =
      "{{bad anatomy}},{bad feet},bad hands,{{{bad proportions}}},{blurry},cloned face,cropped," +
      "{{{deformed}}},{{{disfigured}}},error,{{{extra arms}}},{extra digit},{{{extra legs}}},extra limbs," +
      "{{extra limbs}},{fewer digits},{{{fused fingers}}},gross proportions,ink eyes,ink hair," +
      "jpeg artifacts,{{{{long neck}}}},low quality,{malformed limbs},{{missing arms}},{missing fingers}," +
      "{{missing legs}},{{{more than 2 nipples}}},mutated hands,{{{mutation}}},normal quality,owres," +
      "{{poorly drawn face}},{{poorly drawn hands}},reen eyes,signature,text,{{too many fingers}}," +
      "{{{ugly}}},username,uta,watermark,worst quality,{{{more than 2 legs}}}";
  }

  // ===== 试用生成 =====
  async function loadTrialStatus() {
    try {
      const bridge = await getBridge();
      const resp = await bridge.apiGet("test_panel/trial_status");
      const status = (resp && resp.data) ? resp.data : resp;
      updateTrialUI(status);
    } catch (err) {
      hide(els.trialStatus);
      els.trialBtn.disabled = true;
    }
  }

  function updateTrialUI(status) {
    if (!status) return;
    const remaining = status.remaining || 0;
    const used = status.used || 0;
    const max = status.max_uses || 3;

    if (status.available) {
      els.trialBtn.disabled = false;
      show(els.trialStatus);
      els.trialStatusText.textContent = `🌟 试用生成可用 · 已用 ${used}/${max} 次 · 剩余 ${remaining} 次`;
    } else if (status.key_loaded && used >= max) {
      els.trialBtn.disabled = true;
      show(els.trialStatus);
      els.trialStatusText.textContent = `⚠ 试用次数已达上限（${max} 次）。请配置自己的密钥后使用正式生图。`;
    } else {
      els.trialBtn.disabled = true;
      hide(els.trialStatus);
    }
  }

  async function trialGenerate() {
    if (isGenerating) return;

    const body = buildRequestBody();
    if (!body.nai_prompt && !body.nl_prompt) {
      showError("请至少填写一个提示词框（NAI 风格或自然语言）。");
      return;
    }

    body.n = 1;
    lastRequestBody = body;
    isGenerating = true;
    setLoading(true);
    hideError();
    hideResults();
    hideMergeInfo();
    els.loadingText.textContent = "正在使用试用密钥转译 + 生成...";

    try {
      const bridge = await getBridge();
      const resp = await bridge.apiPost("test_panel/trial_generate", body);

      let images, mergeInfo, trialUsed, trialRemaining;
      if (Array.isArray(resp)) {
        images = resp;
      } else if (resp && Array.isArray(resp.data)) {
        images = resp.data;
        mergeInfo = resp.merge_info;
        trialUsed = resp.trial_used;
        trialRemaining = resp.trial_remaining;
      } else if (resp && resp.data && Array.isArray(resp.data.data)) {
        images = resp.data.data;
        mergeInfo = resp.data.merge_info;
        trialUsed = resp.data.trial_used;
        trialRemaining = resp.data.trial_remaining;
      } else {
        images = resp;
      }

      if (!images || !Array.isArray(images) || images.length === 0) {
        const errMsg = (resp && resp.message) || (resp && resp.data && resp.data.message) || JSON.stringify(resp).slice(0, 200);
        throw new Error(errMsg);
      }

      if (mergeInfo) displayMergeInfo(mergeInfo);
      displayResults(images, body);

      if (trialUsed != null && trialRemaining != null) {
        updateTrialUI({ available: trialRemaining > 0, key_loaded: true, used: trialUsed, max_uses: 3, remaining: trialRemaining });
      } else {
        await loadTrialStatus();
      }
    } catch (err) {
      const msg = err?.message || String(err);
      showError(msg);
      await loadTrialStatus();
    } finally {
      isGenerating = false;
      setLoading(false);
    }
  }

  // ===== 事件绑定 =====
  function bindEvents() {
    els.style.addEventListener("change", toggleCustomArtists);
    els.generateBtn.addEventListener("click", generate);
    els.trialBtn.addEventListener("click", trialGenerate);
    els.resetBtn.addEventListener("click", resetParams);
    els.retryBtn.addEventListener("click", () => {
      if (lastRequestBody) {
        hideError();
        generate();
      }
    });
    els.loadDefaultNegative.addEventListener("click", loadDefaultNegative);

    // 所有表单字段变更时自动缓存
    getCachedFields().forEach((key) => {
      const el = els[key];
      if (el) {
        el.addEventListener("input", saveCache);
        el.addEventListener("change", saveCache);
      }
    });

    // Ctrl+Enter 快捷生成
    [els.naiPrompt, els.nlPrompt].forEach((ta) => {
      ta.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
          e.preventDefault();
          generate();
        }
      });
    });
  }

  // ===== 初始化 =====
  async function init() {
    bindEvents();
    // 从 localStorage 恢复表单状态
    loadCache();
    toggleCustomArtists();
    // 仅加载 token 状态 badge（不填充表单）
    await loadTokenStatus();
    // 加载试用状态
    await loadTrialStatus();
  }

  // DOM 就绪后启动
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
