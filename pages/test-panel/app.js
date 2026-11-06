/**
 * NAI 生图测试面板 - 前端逻辑 v4.8 (Endfield Protocol)
 *
 * 包含：
 *   - 双提示词框：NAI 风格 + 自然语言
 *   - OpenAI 兼容格式：多张参考图（最多 8 张，Vibe / 精准参考支持逐图强度，
 *     精准参考可逐图指定 base_caption）、img2img 重绘、噪声、种子、
 *     director-tools 图片处理动作、多角色坐标控制与完整模型列表
 *     （对齐接口文档 §4-§9）
 *   - 终末地高可见度动态等高线拓扑地形引擎 (Multi-Peak Gaussian + Dense Marching Squares)
 *   - 柔和战术圆角与亚克力毛玻璃材质交互
 *   - 自动转译与生图流水线展示
 */

(function () {
  "use strict";

  // ===== DOM 引用 =====
  const $ = (id) => document.getElementById(id);
  const els = {
    contourCanvas: $("contourCanvas"),
    naiPrompt: $("naiPrompt"),
    nlPrompt: $("nlPrompt"),
    sampler: $("sampler"),
    size: $("size"),
    steps: $("steps"),
    costBadge: $("costBadge"),
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
    callFormat: $("callFormat"),
    callFormatHint: $("callFormatHint"),
    openaiConfigStatus: $("openaiConfigStatus"),
    openaiConfigStatusText: $("openaiConfigStatusText"),
    refUploadWrap: $("refUploadWrap"),
    refFile: $("refFile"),
    refCountTag: $("refCountTag"),
    refGridWrap: $("refGridWrap"),
    refGrid: $("refGrid"),
    refRemove: $("refRemove"),
    refStrengthWrap: $("refStrengthWrap"),
    refStrength: $("refStrength"),
    refModeWrap: $("refModeWrap"),
    refMode: $("refMode"),
    refModelWarning: $("refModelWarning"),
    refNoiseWrap: $("refNoiseWrap"),
    refNoise: $("refNoise"),
    openaiSeedWrap: $("openaiSeedWrap"),
    openaiSeed: $("openaiSeed"),
    directorActionWrap: $("directorActionWrap"),
    directorAction: $("directorAction"),
    charListWrap: $("charListWrap"),
    charList: $("charList"),
    charAdd: $("charAdd"),
    usageRangeSwitch: $("usageRangeSwitch"),
    usageRefreshBtn: $("usageRefreshBtn"),
    usageSummary: $("usageSummary"),
    usageChart: $("usageChart"),
    usageBreakdown: $("usageBreakdown"),
    usageEmpty: $("usageEmpty"),
    usageDisabled: $("usageDisabled"),
    usageError: $("usageError"),
  };

  // ===== 状态 =====
  let isGenerating = false;
  let lastRequestBody = null;
  // 点数用量统计：当前窗口天数与后端快照
  let usageDays = 7;
  let usageData = null;
  // 本地上传参考图列表（OpenAI 兼容格式）：{ b64, dataUrl, name, strength, caption }
  let referenceImages = [];
  // 精准参考描述默认值（后端配置下发，新参考图卡片初始化用）
  let defaultDirectorCaption = "character&style";
  // 参考图默认权重（后端配置下发，新参考图卡片初始化用）
  let defaultVibeStrength = 0.6;
  let defaultDirectorStrength = 1;
  // 当前调用格式："direct" | "openai"
  let currentCallFormat = "direct";
  let upstreamLabel = "OpenAI";
  let panelConfig = {};
  let novelaiSizeOptions = [];
  let hasCachedFormat = false;
  const modeSelections = {};
  // 分段切换按钮引用
  let formatToggleBtns = [];
  // OpenAI 兼容模型列表（后端配置接口下发，用于切换格式时替换模型下拉项）
  let openaiModels = [
    "nai-diffusion-5-full",
    "nai-diffusion-5-curated",
    "nai-diffusion-4-5-full",
    "nai-diffusion-4-5-curated",
    "nai-diffusion-4-full",
    "nai-diffusion-4-curated-preview",
    "nai-diffusion-3",
    "nai-diffusion-furry-3",
  ];
  // nai.sta1n.cn模式可用的模型下拉项
  const DIRECT_MODEL_OPTIONS = [
    { value: "nai-diffusion-4-5-full", label: "V4.5 完整版 [4.5_FULL]" },
    { value: "nai-diffusion-5-full", label: "V5 完整版 [5.0_FULL]" },
  ];
  // 是否展示点数预估徽标（后端下发 show_cost_estimate）
  let panelShowCostEstimate = true;

  // ===== 工具函数 =====
  function show(el) { if (el) el.classList.remove("hidden"); }
  function hide(el) { if (el) el.classList.add("hidden"); }

  function setBadge(el, text, type) {
    if (!el) return;
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

  // =========================================================================
  // ===== 终末地等高线拓扑地形引擎 (HIGH-VISIBILITY CONTOUR TOPOLOGY) =====
  // =========================================================================
  function initContourEngine() {
    const canvas = els.contourCanvas;
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let width = 0;
    let height = 0;
    let dpr = 1;

    // 保持优雅大气的山脉尺度，调大高斯半径并恢复缓和悠远的流动速度
    const NUM_SOURCES = 18;
    const sources = [];

    function initSources() {
      sources.length = 0;
      for (let i = 0; i < NUM_SOURCES; i++) {
        const isPeak = i % 3 !== 0;
        sources.push({
          relX: 0.05 + Math.random() * 0.9,
          relY: 0.05 + Math.random() * 0.9,
          baseRadius: 160 + Math.random() * 180, // 调大山丘尺度 (160~340px)，舒展大气
          amp: (isPeak ? 1.0 : -0.8) * (0.75 + Math.random() * 0.5),
          speedX: (Math.random() - 0.5) * 0.00012,
          speedY: (Math.random() - 0.5) * 0.00012,
          freq: 0.00035 + Math.random() * 0.00055, // 回到舒缓沉稳的波动频率
          phase: Math.random() * Math.PI * 2,
          radiusOscFreq: 0.00025 + Math.random() * 0.00045,
        });
      }
    }

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = window.innerWidth;
      height = window.innerHeight;
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    window.addEventListener("resize", resize);
    resize();
    initSources();

    // 16 层等高线切片，间隔适度自然
    const NUM_LEVELS = 16;
    const ISO_LEVELS = [];
    for (let i = 0; i < NUM_LEVELS; i++) {
      ISO_LEVELS.push(-1.35 + (2.7 / (NUM_LEVELS - 1)) * i);
    }

    // Marching Squares 网格步长
    const STEP = 26;

    // Marching squares 查找表
    const MS_EDGES = [
      [],
      [[3, 2]],
      [[2, 1]],
      [[3, 1]],
      [[0, 1]],
      [[3, 0], [2, 1]],
      [[0, 2]],
      [[3, 0]],
      [[0, 3]],
      [[0, 2]],
      [[0, 1], [3, 2]],
      [[0, 1]],
      [[1, 3]],
      [[1, 2]],
      [[2, 3]],
      []
    ];

    function evaluateField(x, y, time) {
      let val = 0;
      for (let i = 0; i < NUM_SOURCES; i++) {
        const s = sources[i];
        const driftX = Math.sin(time * s.freq + s.phase) * 45;
        const driftY = Math.cos(time * s.freq * 0.85 + s.phase) * 45;
        const sx = s.relX * width + driftX;
        const sy = s.relY * height + driftY;

        const dx = x - sx;
        const dy = y - sy;
        const distSq = dx * dx + dy * dy;

        const r = s.baseRadius + Math.sin(time * s.radiusOscFreq + s.phase) * 30;
        const twoSigmaSq = 2 * r * r;

        // 局部截断加速
        if (distSq < 6.5 * r * r) {
          val += s.amp * Math.exp(-distSq / twoSigmaSq);
        }
      }
      return val;
    }

    function getEdgePoint(edgeIndex, x0, y0, step, vTL, vTR, vBR, vBL, iso) {
      const interp = (vA, vB) => {
        const d = vB - vA;
        return Math.abs(d) < 1e-6 ? 0.5 : Math.max(0, Math.min(1, (iso - vA) / d));
      };

      switch (edgeIndex) {
        case 0:
          return { x: x0 + interp(vTL, vTR) * step, y: y0 };
        case 1:
          return { x: x0 + step, y: y0 + interp(vTR, vBR) * step };
        case 2:
          return { x: x0 + interp(vBL, vBR) * step, y: y0 + step };
        case 3:
          return { x: x0, y: y0 + interp(vTL, vBL) * step };
        default:
          return { x: x0, y: y0 };
      }
    }

    // 绘制等高线：保持清晰可见度同时线条更雅致
    function stitchAndRenderPolylines(segments, isoIndex, totalLevels) {
      if (segments.length === 0) return;

      const isKeyRidge = isoIndex === totalLevels - 2 || isoIndex === totalLevels - 5;
      const isMajorLine = isoIndex % 3 === 0;

      ctx.beginPath();
      for (let s = 0; s < segments.length; s++) {
        const seg = segments[s];
        ctx.moveTo(seg[0].x, seg[0].y);
        ctx.lineTo(seg[1].x, seg[1].y);
      }

      if (isKeyRidge) {
        ctx.strokeStyle = "rgba(217, 199, 0, 0.38)"; // 终末地黄色山脊线
        ctx.lineWidth = 1.4;
      } else if (isMajorLine) {
        ctx.strokeStyle = "rgba(16, 17, 16, 0.15)";  // 适度清晰的主等高线
        ctx.lineWidth = 1.1;
      } else {
        ctx.strokeStyle = "rgba(16, 17, 16, 0.06)";  // 柔和优雅的细等高线
        ctx.lineWidth = 0.8;
      }
      ctx.stroke();
    }

    let lastFrameTime = 0;
    const TARGET_INTERVAL = 1000 / 30;

    function render(timestamp) {
      requestAnimationFrame(render);

      if (document.hidden) return;
      if (timestamp - lastFrameTime < TARGET_INTERVAL) return;
      lastFrameTime = timestamp;

      ctx.clearRect(0, 0, width, height);

      const cols = Math.ceil(width / STEP) + 1;
      const rows = Math.ceil(height / STEP) + 1;
      const grid = new Float32Array(cols * rows);

      for (let r = 0; r < rows; r++) {
        const y = r * STEP;
        const rowOffset = r * cols;
        for (let c = 0; c < cols; c++) {
          const x = c * STEP;
          grid[rowOffset + c] = evaluateField(x, y, timestamp);
        }
      }

      for (let l = 0; l < ISO_LEVELS.length; l++) {
        const iso = ISO_LEVELS[l];
        const segments = [];

        for (let r = 0; r < rows - 1; r++) {
          const y0 = r * STEP;
          const r0 = r * cols;
          const r1 = (r + 1) * cols;

          for (let c = 0; c < cols - 1; c++) {
            const x0 = c * STEP;
            const vTL = grid[r0 + c];
            const vTR = grid[r0 + c + 1];
            const vBR = grid[r1 + c + 1];
            const vBL = grid[r1 + c];

            let cellIndex = 0;
            if (vTL >= iso) cellIndex |= 8;
            if (vTR >= iso) cellIndex |= 4;
            if (vBR >= iso) cellIndex |= 2;
            if (vBL >= iso) cellIndex |= 1;

            if (cellIndex === 0 || cellIndex === 15) continue;

            const edgePairs = MS_EDGES[cellIndex];
            for (let p = 0; p < edgePairs.length; p++) {
              const pA = getEdgePoint(edgePairs[p][0], x0, y0, STEP, vTL, vTR, vBR, vBL, iso);
              const pB = getEdgePoint(edgePairs[p][1], x0, y0, STEP, vTL, vTR, vBR, vBL, iso);
              segments.push([pA, pB]);
            }
          }
        }

        stitchAndRenderPolylines(segments, l, ISO_LEVELS.length);
      }
    }

    requestAnimationFrame(render);
  }

  // =========================================================================
  // ===== 面板状态缓存（通过后端 API） =====
  // =========================================================================
  function getCachedFields() {
    return [
      "naiPrompt", "nlPrompt", "sampler", "size", "steps", "scale",
      "cfg", "noiseSchedule", "model", "count", "style", "customArtists", "negative",
      "refStrength", "refMode", "refNoise", "openaiSeed", "directorAction"
    ];
  }

  let _saveCacheTimer = null;
  function saveCache() {
    if (_saveCacheTimer) clearTimeout(_saveCacheTimer);
    _saveCacheTimer = setTimeout(async () => {
      try {
        const data = {};
        getCachedFields().forEach((key) => {
          const el = els[key];
          if (el) data[key] = el.value;
        });
        data.callFormat = currentCallFormat;
        const chars = collectCharRows();
        if (chars.length) data.characters = chars;
        const bridge = await getBridge();
        await bridge.apiPost("test_panel/save_cache", data);
      } catch (e) {
        console.warn("[NAI Panel] 缓存保存失败:", e);
      }
    }, 500);
  }

  async function loadCache() {
    try {
      const bridge = await getBridge();
      const resp = await bridge.apiGet("test_panel/load_cache");
      const data = (resp && resp.data) ? resp.data : resp;
      if (!data || typeof data !== "object") return false;
      let restored = false;
      // 配置和选项已加载；先切到缓存的接口，再恢复其模型和尺寸。
      if (["openai", "direct", "novelai"].includes(data.callFormat)) {
        hasCachedFormat = true;
        setCallFormat(data.callFormat);
        restored = true;
      }
      getCachedFields().forEach((key) => {
        const el = els[key];
        if (el && data[key] != null) {
          if (key === "style" && String(data[key]).startsWith("slot:")) {
            const option = document.createElement("option");
            option.dataset.slotOption = "true";
            option.value = data[key];
            option.textContent = "正在读取画风槽位";
            el.appendChild(option);
          }
          if (el.tagName === "SELECT" && !Array.from(el.options).some(option => option.value === String(data[key]))) return;
          el.value = data[key];
          restored = true;
        }
      });
      modeSelections[currentCallFormat] = {size: els.size.value, model: els.model.value};
      applyRefModeUI();
      if (Array.isArray(data.characters) && data.characters.length) {
        clearCharRows();
        data.characters.slice(0, MAX_CHAR_ROWS).forEach((item) => {
          if (!item || typeof item.prompt !== "string" || !item.prompt.trim()) return;
          addCharRow(item.prompt, Number(item.x) || 0.5, Number(item.y) || 0.5);
        });
        restored = true;
      }
      return restored;
    } catch (e) {
      console.warn("[NAI Panel] 缓存加载失败:", e);
      return false;
    }
  }

  function clearCache() {
    saveCache();
  }

  // ===== 加载 Token 状态 =====
  async function loadTokenStatus() {
    try {
      const bridge = await getBridge();
      const resp = await bridge.apiGet("test_panel/config");
      const config = (resp && resp.data) ? resp.data : resp;
      panelConfig = config;
      novelaiSizeOptions = Array.isArray(config.novelai_size_options) ? config.novelai_size_options : [];
      if (config.image_gen_key === "已配置") {
        setBadge(els.tokenBadge, "Token: 已配置", "badge-success");
      } else {
        setBadge(els.tokenBadge, "Token: 未配置", "badge-error");
      }
      setBadge(els.baseUrlBadge, config.base_url || "--", "badge-info");
      // 点数预估开关（后端下发 show_cost_estimate）：徽标是否展示
      if (typeof config.show_cost_estimate === "boolean") {
        panelShowCostEstimate = config.show_cost_estimate;
      }
      updateCostBadge();
      // 后端下发的 OpenAI 兼容模型列表（对齐接口文档 §9）
      if (Array.isArray(config.openai_models) && config.openai_models.length) {
        openaiModels = config.openai_models;
        if (currentCallFormat !== "direct") updateModelOptionsUI();
      }
      // 精准参考描述默认值：后端配置下发，新参考图卡片以此初始化
      if (config.openai_director_caption) {
        defaultDirectorCaption = String(config.openai_director_caption);
      }
      // 参考图默认权重：后端配置下发，新参考图卡片以此初始化
      const vibeStrength = parseFloat(config.openai_vibe_strength);
      if (!Number.isNaN(vibeStrength) && vibeStrength >= 0 && vibeStrength <= 1) {
        defaultVibeStrength = vibeStrength;
      }
      const directorStrength = parseFloat(config.openai_director_strength);
      if (
        !Number.isNaN(directorStrength) &&
        directorStrength >= 0 &&
        directorStrength <= 1
      ) {
        defaultDirectorStrength = directorStrength;
      }
      if (!hasCachedFormat) {
        setCallFormat(["direct", "openai", "novelai"].includes(config.call_mode) ? config.call_mode : "direct");
      } else { updateSizeOptionsUI(); updateProviderStatus(); }
    } catch (err) {
      setBadge(els.tokenBadge, "配置加载失败", "badge-error");
    }
  }

  function updateProviderStatus() {
    const native = currentCallFormat === "novelai";
    upstreamLabel = native ? (panelConfig.novelai_service === "official" ? "NAI 官方" : "Starbride") : "OpenAI";
    if (currentCallFormat === "direct") { hide(els.openaiConfigStatus); setBadge(els.tokenBadge, "Token: " + (panelConfig.image_gen_key || "未配置"), panelConfig.image_gen_key === "已配置" ? "badge-success" : "badge-error"); setBadge(els.baseUrlBadge, panelConfig.base_url || "--", "badge-info"); return; }
    const configured = native ? panelConfig.novelai_available : panelConfig.openai_available;
    const address = native ? panelConfig.novelai_api_base_url : panelConfig.openai_api_base_url;
    setBadge(els.tokenBadge, configured ? "API Key: 已配置" : "API Key: 未配置", configured ? "badge-success" : "badge-error");
    setBadge(els.baseUrlBadge, address || "--", "badge-info");
    show(els.openaiConfigStatus);
    els.callFormatHint.textContent = "使用插件设置中的 " + upstreamLabel + " 地址和密钥。";
    els.openaiConfigStatusText.textContent = configured ? upstreamLabel + " 已配置：" + (address || "") : upstreamLabel + " 未配置：请在插件设置中填写该服务的 API Key。";
    els.openaiConfigStatus.querySelector(".status-dot").className = configured ? "status-dot ok" : "status-dot error";
  }

  // ===== 尺寸与点数消耗映射 =====
  const SIZE_BASE_COSTS = [
    { value: "竖图", baseCost: 1 },
    { value: "横图", baseCost: 1 },
    { value: "方图", baseCost: 1 },
    { value: "2K竖图", baseCost: 15 },
    { value: "2K横图", baseCost: 15 },
    { value: "2K方图", baseCost: 15 },
    { value: "4K竖图", baseCost: 25 },
    { value: "4K横图", baseCost: 25 },
    { value: "4K方图", baseCost: 25 },
  ];

  // OpenAI 兼容格式直接用像素分辨率（接口文档 §12.1），与 GET 分档名一一对应：
  // 分档名经后端映射后的实际分辨率即为此处的 value。
  const OPENAI_SIZE_OPTIONS = [
    { value: "832x1216", tier: "竖图", baseCost: 1 },
    { value: "1216x832", tier: "横图", baseCost: 1 },
    { value: "1024x1024", tier: "方图", baseCost: 1 },
    { value: "1088x1920", tier: "2K竖图", baseCost: 15 },
    { value: "1920x1088", tier: "2K横图", baseCost: 15 },
    { value: "1472x1472", tier: "2K方图", baseCost: 15 },
  ];

  // 分档名 ↔ OpenAI 像素分辨率互查（结果徽标显示用）。
  function sizeDisplayName(value) {
    const opt = (currentCallFormat === "novelai" ? novelaiSizeOptions : OPENAI_SIZE_OPTIONS).find((o) => o.value === value);
    return opt ? (opt.label || `${opt.value}（${opt.tier}）`) : value;
  }

  // 任务接口（/api/web/jobs）步数上限。
  const WEB_JOB_STEPS_MAX = 50;

  // V5 完整版普通画幅起步 8 点（2026-09 上游计费模块；旧版为 5 点）。
  function getModelCostFloor(modelName) {
    return modelName === "nai-diffusion-5-full" ? 8 : 1;
  }

  // 步数档位加价：≤28 步 +0，29-35 +4，36-45 +6，46-50 +8。
  function getStepsExtra(steps) {
    if (steps > 45) return 8;
    if (steps > 35) return 6;
    if (steps > 28) return 4;
    return 0;
  }

  // 单张点数（任务接口档位表）；Web 侧镜像 pricing.py 的计价规则：
  // 画幅基础档 > 1（2K/4K）→ base + extra；V5 标准画幅 → 8 + extra；
  // V4.5 标准画幅 → 有加价时 2 + extra，否则 1。
  function computeUnitCost(sizeValue, modelName, steps) {
    const baseCost = getSizeBaseCost(sizeValue);
    const capped = Math.max(1, Math.min(parseInt(steps, 10) || 1, WEB_JOB_STEPS_MAX));
    const extra = getStepsExtra(capped);
    if (baseCost > 1) return baseCost + extra;
    const floor = getModelCostFloor(modelName);
    if (floor > 1) return floor + extra;
    return extra ? 2 + extra : 1;
  }

  function getSizeBaseCost(sizeValue) {
    const opt =
      SIZE_BASE_COSTS.find((o) => o.value === sizeValue) ||
      OPENAI_SIZE_OPTIONS.find((o) => o.value === sizeValue);
    return opt ? opt.baseCost : 1;
  }

  // 实时刷新点数预估徽标（直连格式显示步数×画质档位；OpenAI 格式隐藏）。
  // 直连通道固定为任务接口优先（1-50 步档位计价），上游不支持时自动回退 GET 直链。
  function updateCostBadge() {
    const badge = els.costBadge;
    if (!badge) return;
    if (currentCallFormat !== "direct") {
      hide(badge);
      return;
    }
    if (panelShowCostEstimate === false) {
      hide(badge);
      return;
    }
    const steps = parseInt(els.steps.value, 10) || 1;
    const count = parseInt(els.count.value, 10) || 1;
    const capped = Math.max(1, Math.min(steps, WEB_JOB_STEPS_MAX));
    const unit = computeUnitCost(els.size.value, els.model.value, steps);
    let text = `💰 预计 ${unit} 点/张 · 任务接口`;
    if (count > 1) text += ` · ${count} 张共 ${unit * count} 点`;
    if (steps !== capped) text += `（步数按 ${capped} 计）`;
    badge.textContent = text;
    // 超过基础档（>1 点）时高亮，提示本次为加价档位。
    badge.classList.toggle("cost-badge-high", unit > 1);
    show(badge);
  }

  function updateSizeOptionsUI() {
    const curSize = els.size.value;
    const curModel = els.model.value;
    els.size.innerHTML = "";
    const options =
      currentCallFormat === "novelai"
        ? novelaiSizeOptions.map((opt) => ({value: opt.value, text: opt.label, baseCost: 0}))
        : currentCallFormat === "openai"
        ? OPENAI_SIZE_OPTIONS.map((opt) => ({
            value: opt.value,
            text: opt.value,
            baseCost: opt.baseCost,
          }))
        : SIZE_BASE_COSTS.map((opt) => ({
            value: opt.value,
            text: opt.value,
            baseCost: opt.baseCost,
          }));
    options.forEach((opt) => {
      const cost = Math.max(opt.baseCost, getModelCostFloor(curModel));
      const elOpt = document.createElement("option");
      elOpt.value = opt.value;
      elOpt.textContent = currentCallFormat === "novelai" ? opt.text : `${opt.text}(-${cost})`;
      if (opt.value === curSize) {
        elOpt.selected = true;
      }
      els.size.appendChild(elOpt);
    });
    if (!options.some((opt) => opt.value === curSize)) {
      els.size.value = currentCallFormat !== "direct" ? "832x1216" : "竖图";
    }
  }

  // 按当前调用格式替换模型下拉项：直连只有 V4.5/V5 完整版，
  // OpenAI 兼容格式展示接口文档 §9 的完整模型列表。
  function updateModelOptionsUI() {
    const curModel = els.model.value;
    els.model.innerHTML = "";
    if (currentCallFormat !== "direct") {
      const models = currentCallFormat === "novelai" ? (panelConfig.novelai_models || openaiModels.slice(0, 4)) : openaiModels;
      models.forEach((name) => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        els.model.appendChild(opt);
      });
    } else {
      DIRECT_MODEL_OPTIONS.forEach((item) => {
        const opt = document.createElement("option");
        opt.value = item.value;
        opt.textContent = item.label;
        els.model.appendChild(opt);
      });
    }
    if (Array.from(els.model.options).some((o) => o.value === curModel)) {
      els.model.value = curModel;
    }
  }

  // ===== 表单交互 =====
  function toggleCustomArtists() {
    if (els.style.value === "custom" || els.style.value.startsWith("slot:")) {
      show(els.customArtistsWrapper);
    } else {
      hide(els.customArtistsWrapper);
    }
    saveCache();
  }

  function buildRequestBody() {
    const safeInt = (v, d) => { const n = parseInt(v, 10); return Number.isNaN(n) ? d : n; };
    const safeFloat = (v, d) => { const n = parseFloat(v); return Number.isNaN(n) ? d : n; };

    const body = {
      nai_prompt: els.naiPrompt.value.trim(),
      nl_prompt: els.nlPrompt.value.trim(),
      style: els.style.value.startsWith("slot:") ? "custom" : els.style.value,
      size: els.size.value,
      sampler: els.sampler.value,
      steps: safeInt(els.steps.value, 24),
      scale: safeFloat(els.scale.value, 6),
      cfg: safeFloat(els.cfg.value, 1),
      noise_schedule: els.noiseSchedule.value,
      model: els.model.value,
      n: safeInt(els.count.value, 1),
      call_format: currentCallFormat,
    };

    const neg = els.negative.value.trim();
    if (neg) body.negative = neg;

    if (body.style === "custom") {
      body.custom_artists = els.customArtists.value.trim();
    }

    // OpenAI 兼容格式：附带本地上传的多张参考图（§5.2 最多 8 张）、逐图
    // 强度/精准参考描述、img2img 重绘参数、种子、参考图使用模式与
    // director-tools 动作（对齐接口文档 §4-§8）
    if (body.call_format !== "direct") {
      const refs = referenceImages;
      if (refs.length) {
        body.reference_image_b64_list = refs.map((r) => r.b64);
      }
      const mode = els.refMode ? els.refMode.value : "vibe";
      if (els.refMode) {
        body.reference_mode = mode;
      }
      if (mode === "img2img") {
        // img2img 仅取第一张主输入图，使用全局重绘强度与附加噪声；
        // 其余模式走逐图强度（后端按 vibe 默认 0.6 / 精准参考默认 1.0）
        if (els.refStrength) {
          const strength = parseFloat(els.refStrength.value);
          if (!Number.isNaN(strength) && strength > 0 && strength <= 1) {
            body.strength = strength;
          }
        }
        if (els.refNoise) {
          const noise = parseFloat(els.refNoise.value);
          if (!Number.isNaN(noise) && noise >= 0 && noise <= 1) {
            body.noise = noise;
          }
        }
      } else if (refs.length) {
        body.reference_strengths = refs.map((r) =>
          Math.min(1, Math.max(0, Number(r.strength) || 0))
        );
        if (mode === "director") {
          body.director_captions = refs.map((r) => r.caption);
        }
      }
      if (els.openaiSeed && els.openaiSeed.value !== "") {
        const seed = parseInt(els.openaiSeed.value, 10);
        if (!Number.isNaN(seed) && seed >= -1) {
          body.seed = seed;
        }
      }
      if (els.directorAction && els.directorAction.value) {
        body.director_action = els.directorAction.value;
      }
      // 多角色坐标控制（§7）：收集角色行，空提示词的行跳过
      const chars = collectCharRows();
      if (chars.length) {
        body.characters = chars;
      }
    }

    return body;
  }

  // ===== 多角色坐标行管理（§7） =====
  const MAX_CHAR_ROWS = 6;

  function addCharRow(prompt = "", x = 0.5, y = 0.5) {
    if (!els.charList || els.charList.children.length >= MAX_CHAR_ROWS) return;

    const row = document.createElement("div");
    row.className = "char-row";

    const promptInput = document.createElement("input");
    promptInput.type = "text";
    promptInput.className = "input acrylic-input char-prompt";
    promptInput.placeholder = "角色提示词，如 1girl, red dress";
    promptInput.value = prompt;

    const xInput = document.createElement("input");
    xInput.type = "number";
    xInput.className = "input acrylic-input char-coord";
    xInput.min = "0";
    xInput.max = "1";
    xInput.step = "0.05";
    xInput.value = String(x);
    xInput.title = "x 坐标（0-1，从左到右）";

    const yInput = document.createElement("input");
    yInput.type = "number";
    yInput.className = "input acrylic-input char-coord";
    yInput.min = "0";
    yInput.max = "1";
    yInput.step = "0.05";
    yInput.value = String(y);
    yInput.title = "y 坐标（0-1，从上到下）";

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "btn-link char-remove";
    removeBtn.title = "移除该角色";
    removeBtn.innerHTML = '<span class="link-icon">✕</span>';
    removeBtn.addEventListener("click", () => {
      row.remove();
      saveCache();
    });

    row.appendChild(promptInput);
    row.appendChild(xInput);
    row.appendChild(yInput);
    row.appendChild(removeBtn);
    els.charList.appendChild(row);

    promptInput.addEventListener("input", saveCache);
    xInput.addEventListener("change", saveCache);
    yInput.addEventListener("change", saveCache);
  }

  function clearCharRows() {
    if (els.charList) els.charList.innerHTML = "";
  }

  function collectCharRows() {
    if (!els.charList) return [];
    const chars = [];
    Array.from(els.charList.children).forEach((row) => {
      const inputs = row.querySelectorAll("input");
      if (inputs.length < 3) return;
      const prompt = String(inputs[0].value || "").trim();
      if (!prompt) return;
      let x = parseFloat(inputs[1].value);
      let y = parseFloat(inputs[2].value);
      if (Number.isNaN(x)) x = 0.5;
      if (Number.isNaN(y)) y = 0.5;
      x = Math.min(1, Math.max(0, x));
      y = Math.min(1, Math.max(0, y));
      chars.push({ prompt, x, y });
    });
    return chars;
  }

  // ===== 多张参考图管理（OpenAI 兼容格式 §5.2，最多 8 张） =====
  const MAX_REF_IMAGES = 8;
  const REF_CAPTIONS = ["character&style", "character", "style"];

  function currentRefMode() {
    return els.refMode ? els.refMode.value : "vibe";
  }

  // 新参考图卡片按模式取插件设置的默认权重：精准参考 1.0（§8.5）、
  // Vibe 参考 0.6，均可在设置中修改
  function defaultRefStrength() {
    return currentRefMode() === "director" ? defaultDirectorStrength : defaultVibeStrength;
  }

  function addRefImages(fileList) {
    let remaining = MAX_REF_IMAGES - referenceImages.length;
    Array.from(fileList || []).forEach((file) => {
      if (remaining <= 0) return;
      if (!file || file.type.indexOf("image/") !== 0) return;
      remaining -= 1;
      const reader = new FileReader();
      reader.onload = (e) => {
        const dataUrl = String(e.target.result || "");
        const comma = dataUrl.indexOf(",");
        referenceImages.push({
          b64: comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl,
          dataUrl,
          name: file.name || `image_${referenceImages.length + 1}`,
          strength: defaultRefStrength(),
          caption: defaultDirectorCaption,
        });
        renderRefGrid();
        saveCache();
      };
      reader.onerror = () => {
        showError(`参考图 ${file.name || ""} 读取失败，请重新选择。`);
      };
      reader.readAsDataURL(file);
    });
  }

  function removeRefImage(index) {
    referenceImages.splice(index, 1);
    renderRefGrid();
    saveCache();
  }

  function clearReferenceImage() {
    referenceImages = [];
    if (els.refFile) els.refFile.value = "";
    renderRefGrid();
  }

  function renderRefGrid() {
    if (!els.refGrid) return;
    const mode = currentRefMode();
    els.refGrid.className = "ref-grid mode-" + mode;
    els.refGrid.innerHTML = "";

    referenceImages.forEach((item, idx) => {
      const row = document.createElement("div");
      row.className = "ref-item";

      const thumb = document.createElement("img");
      thumb.className = "ref-thumb";
      thumb.src = item.dataUrl;
      thumb.alt = `参考图 ${idx + 1}`;
      thumb.title = item.name;
      thumb.addEventListener("click", () => openLightbox(item.dataUrl));
      row.appendChild(thumb);

      const body = document.createElement("div");
      body.className = "ref-item-body";

      const controls = document.createElement("div");
      controls.className = "ref-item-controls";

      // 逐图强度：vibe 写入 reference_strength_multiple、精准参考写入
      // director_reference_strength_values（均按下标对应）
      const strengthInput = document.createElement("input");
      strengthInput.type = "number";
      strengthInput.className =
        "input acrylic-input ref-strength-input";
      strengthInput.min = "0";
      strengthInput.max = "1";
      strengthInput.step = "0.05";
      strengthInput.value = String(item.strength);
      strengthInput.title = `第 ${idx + 1} 张参考图的强度（0-1）`;
      strengthInput.addEventListener("change", () => {
        const value = parseFloat(strengthInput.value);
        if (!Number.isNaN(value)) {
          item.strength = Math.min(1, Math.max(0, value));
        }
        strengthInput.value = String(item.strength);
        saveCache();
      });
      controls.appendChild(strengthInput);

      // 逐图精准参考描述（仅 director 模式显示）
      const captionSelect = document.createElement("select");
      captionSelect.className = "select acrylic-input ref-caption-select";
      REF_CAPTIONS.forEach((cap) => {
        const opt = document.createElement("option");
        opt.value = cap;
        opt.textContent = cap;
        captionSelect.appendChild(opt);
      });
      if (REF_CAPTIONS.indexOf(item.caption) < 0) {
        item.caption = defaultDirectorCaption;
      }
      captionSelect.value = item.caption;
      captionSelect.title = `第 ${idx + 1} 张参考图的 base_caption`;
      captionSelect.addEventListener("change", () => {
        item.caption = captionSelect.value;
        saveCache();
      });
      controls.appendChild(captionSelect);

      body.appendChild(controls);

      const meta = document.createElement("div");
      meta.className = "ref-item-meta";
      meta.textContent = `REF_${String(idx + 1).padStart(2, "0")} · ${item.name}`;
      body.appendChild(meta);

      row.appendChild(body);

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "btn-link ref-item-remove";
      removeBtn.title = "移除该参考图";
      removeBtn.innerHTML = '<span class="link-icon">✕</span>';
      removeBtn.addEventListener("click", () => removeRefImage(idx));
      row.appendChild(removeBtn);

      els.refGrid.appendChild(row);
    });

    if (els.refCountTag) {
      els.refCountTag.textContent = `${referenceImages.length} / ${MAX_REF_IMAGES}`;
    }
    if (els.refGridWrap) {
      if (referenceImages.length) show(els.refGridWrap);
      else hide(els.refGridWrap);
    }
    updateReferenceWarning();
  }

  // 按参考图使用模式切换可见性：全局重绘强度/附加噪声仅 img2img 使用，
  // vibe / 精准参考改用逐图强度（CSS 按 mode-* 类控制卡片内控件显隐）
  function openaiReferenceError(body) {
    if (body.call_format !== "openai" || body.director_action ||
        body.reference_mode !== "director" || !(body.reference_image_b64_list || []).length) return "";
    const supported = panelConfig.openai_director_models || [
      "nai-diffusion-4-5-full", "nai-diffusion-4-5-curated", "nai45", "nai45-curated",
    ];
    if (supported.includes(String(body.model || "").trim().toLowerCase())) return "";
    return `当前模型 ${body.model || "（未选择）"} 不支持精准参考，V5 暂不支持此功能。请明确选择 V4.5 Full 或 Curated 后重新提交；不会自动替换模型。OpenAI 兼容仅表示传输协议，不代表模型能力。`;
  }

  function updateReferenceWarning() {
    const message = openaiReferenceError({
      call_format: currentCallFormat, model: els.model.value,
      reference_mode: currentRefMode(), director_action: els.directorAction.value,
      reference_image_b64_list: referenceImages,
    });
    els.refModelWarning.textContent = message;
    if (message) show(els.refModelWarning);
    else hide(els.refModelWarning);
  }

  function applyRefModeUI() {
    const openai = currentCallFormat !== "direct";
    const mode = currentRefMode();
    if (els.refGrid) {
      els.refGrid.classList.remove("mode-vibe", "mode-img2img", "mode-director");
      els.refGrid.classList.add("mode-" + mode);
    }
    if (openai && mode === "img2img") {
      show(els.refStrengthWrap);
      show(els.refNoiseWrap);
    } else {
      hide(els.refStrengthWrap);
      hide(els.refNoiseWrap);
    }
    updateReferenceWarning();
  }

  // ===== 生成图片 =====
  async function generate() {
    if (isGenerating) return;

    const body = buildRequestBody();
    if (!body.nai_prompt && !body.nl_prompt && !body.director_action) {
      showError("请至少填写一个提示词框（NAI 风格或自然语言）。");
      return;
    }
    if (body.call_format !== "direct") {
      const refCount = (body.reference_image_b64_list || []).length;
      if (body.director_action && !refCount) {
        showError("图片处理动作需要先在「调用格式」卡片上传一张源图片。");
        return;
      }
      if (body.reference_mode === "director" && !refCount) {
        showError(
          "精准参考（director）需要先在「调用格式」卡片上传至少一张参考图（可多选，最多 8 张）。"
        );
        return;
      }
    }
    const capabilityError = openaiReferenceError(body);
    if (capabilityError) {
      showError(capabilityError);
      return;
    }

    lastRequestBody = body;
    isGenerating = true;
    const refCountText = (body.reference_image_b64_list || []).length
      ? `（含 ${(body.reference_image_b64_list || []).length} 张参考图）`
      : "";
    els.loadingText.textContent = body.director_action
      ? "正在以 director-tools 处理图片..."
      : body.call_format !== "direct"
        ? `正在通过 ${upstreamLabel} 生成${refCountText}...`
        : "正在转译 + 生成图片...";
    setLoading(true);
    hideError();
    hideResults();
    hideMergeInfo();

    try {
      const bridge = await getBridge();
      const endpoint =
        body.call_format !== "direct"
          ? "test_panel/generate_openai"
          : "test_panel/generate";
      const resp = await bridge.apiPost(endpoint, body);

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
        const errMsg = (resp && resp.message) || (resp && resp.data && resp.data.message) || JSON.stringify(resp).slice(0, 200);
        throw new Error(errMsg);
      }

      if (mergeInfo) {
        displayMergeInfo(mergeInfo);
      }

      displayResults(images, body);
      // 出图成功后刷新点数台账（新消耗已写入统计）
      await loadUsageStats();
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

    if (info.nai_prompt) {
      steps.push({ label: "NAI 风格提示词（原样保留）", value: info.nai_prompt });
    }

    if (info.nl_prompt) {
      steps.push({ label: "自然语言提示词（待转译）", value: info.nl_prompt });
    }

    if (info.nl_prompt && info.translated_nl) {
      const same = info.translated_nl === info.nl_prompt;
      steps.push({
        label: same ? "转译结果（未配置转译模型，原样使用）" : "转译结果（模型转译为 NAI 标签）",
        value: info.translated_nl,
      });
    }

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
      vertical: "韩漫小清新风",
      comicDoujin: "漫画同人风",
      r18: "2.5D唯美风",
      lolita25d: "2.5D唯美风（萝）",
      anime: "本子里番风",
      galgame: "GalGame风",
      custom: "自定义",
    };
    const formatLabel = requestBody.call_format !== "direct" ? upstreamLabel : "nai.sta1n.cn";
    const metaText = `${styleNames[requestBody.style] || requestBody.style} · ${sizeDisplayName(requestBody.size)} · ${images.length} UNIT · ${formatLabel}`;
    setBadge(els.resultMeta, metaText, "badge-tech-success");
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
    els.cfg.value = "1";
    els.noiseSchedule.value = "karras";
    els.model.value = "nai-diffusion-4-5-full";
    els.style.value = "vertical";
    updateSizeOptionsUI();
    els.size.value = "竖图";
    els.count.value = "1";
    els.negative.value = "";
    els.customArtists.value = "";
    clearReferenceImage();
    els.refStrength.value = "0.7";
    els.refMode.value = "vibe";
    els.refNoise.value = "0.7";
    els.openaiSeed.value = "-1";
    els.directorAction.value = "";
    clearCharRows();
    toggleCustomArtists();
    setCallFormat("direct");
    saveCache();
  }

  // ===== 调用格式切换（nai.sta1n.cn / OpenAI 兼容） =====
  function initFormatToggle() {
    formatToggleBtns = Array.from(
      els.callFormat.querySelectorAll(".format-toggle-btn")
    );
    formatToggleBtns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var fmt = btn.getAttribute("data-format");
        if (fmt === currentCallFormat) return;
        setCallFormat(fmt);
        saveCache();
      });
    });
  }

  function setCallFormat(fmt) {
    if (fmt !== currentCallFormat) modeSelections[currentCallFormat] = {size: els.size.value, model: els.model.value};
    currentCallFormat = fmt;
    formatToggleBtns.forEach(function (btn) {
      var active = btn.getAttribute("data-format") === fmt;
      btn.classList.toggle("active", active);
    });
    var openai = fmt !== "direct";
    if (openai) {
      show(els.refUploadWrap);
      show(els.refModeWrap);
      show(els.openaiSeedWrap);
      show(els.directorActionWrap);
      show(els.charListWrap);
      if (els.openaiConfigStatus) show(els.openaiConfigStatus);
      els.callFormatHint.textContent =
        `使用插件设置中的 ${upstreamLabel} 地址和密钥。`;
      els.trialBtn.disabled = true;
      els.trialBtn.title = "试用生成仅支持 nai.sta1n.cn格式";
    } else {
      hide(els.refUploadWrap);
      hide(els.refStrengthWrap);
      hide(els.refModeWrap);
      hide(els.refNoiseWrap);
      hide(els.openaiSeedWrap);
      hide(els.directorActionWrap);
      hide(els.charListWrap);
      if (els.openaiConfigStatus) hide(els.openaiConfigStatus);
      els.callFormatHint.textContent =
        "直连 nai.sta1n.cn 原生接口生成。";
      els.trialBtn.disabled = false;
      els.trialBtn.title = "";
      loadTrialStatus();
    }
    applyRefModeUI();
    updateModelOptionsUI();
    updateSizeOptionsUI();
    const saved = modeSelections[fmt];
    const defaults = fmt === "novelai" ? {size: panelConfig.novelai_image_size || "832x1216", model: panelConfig.novelai_api_model || "nai-diffusion-5-full"} : null;
    const selected = saved || defaults;
    if (selected) {
      if (Array.from(els.size.options).some(opt => opt.value === selected.size)) els.size.value = selected.size;
      if (Array.from(els.model.options).some(opt => opt.value === selected.model)) els.model.value = selected.model;
    }
    updateProviderStatus();
    updateCostBadge();
    updateReferenceWarning();
  }

  function handleReferenceFileChange() {
    if (!els.refFile || !els.refFile.files) return;
    addRefImages(els.refFile.files);
    // 允许再次选择同一批文件
    els.refFile.value = "";
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
      await loadUsageStats();
    } catch (err) {
      const msg = err?.message || String(err);
      showError(msg);
      await loadTrialStatus();
    } finally {
      isGenerating = false;
      setLoading(false);
    }
  }

  // ===== 点数用量统计（05 卡片） =====

  // 柱状图 + 趋势折线：rows 为 [{date, cost, count, failed}]，按日期升序。
  function renderUsageChart(rows) {
    if (!els.usageChart) return;
    if (!rows || !rows.length) {
      els.usageChart.innerHTML = "";
      return;
    }
    const W = 720;
    const H = 210;
    const padL = 34;
    const padR = 10;
    const padT = 14;
    const padB = 26;
    const innerW = W - padL - padR;
    const innerH = H - padT - padB;
    const n = rows.length;
    const peak = Math.max(
      1,
      ...rows.map((r) => Math.max(r.cost || 0, r.upstream_cost || 0))
    );
    const slot = innerW / n;
    const barW = Math.max(1.5, Math.min(slot * 0.62, 26));
    const yOf = (value) => padT + innerH - (value / peak) * innerH;

    const parts = [];
    // 网格线与 Y 轴刻度（4 档）
    for (let i = 0; i <= 4; i += 1) {
      const value = Math.round((peak * i) / 4);
      const y = yOf((peak * i) / 4);
      parts.push(
        `<line class="usage-grid-line" x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" />`
      );
      parts.push(
        `<text class="usage-axis-text" x="${padL - 6}" y="${(y + 3).toFixed(1)}" text-anchor="end">${value}</text>`
      );
    }

    const centers = [];
    const upstreamCenters = [];
    rows.forEach((row, index) => {
      const cx = padL + slot * (index + 0.5);
      const cost = row.cost || 0;
      const upstreamCost = row.upstream_cost || 0;
      const y = yOf(cost);
      centers.push([cx, y]);
      upstreamCenters.push([cx, yOf(upstreamCost)]);
      const barH = Math.max(cost > 0 ? 1.5 : 0, padT + innerH - y);
      const title = `${row.date} · 估算 ${cost} 点 / ${row.count || 0} 张${
        row.failed ? ` · 失败 ${row.failed} 次` : ""
      }${upstreamCost ? ` · 上游实际 ${upstreamCost} 点` : ""}`;
      if (barH > 0) {
        parts.push(
          `<rect class="usage-bar" x="${(cx - barW / 2).toFixed(2)}" y="${y.toFixed(
            2
          )}" width="${barW.toFixed(2)}" height="${barH.toFixed(2)}" rx="2"><title>${title}</title></rect>`
        );
      } else {
        // 零消耗日：画一个贴地的占位点，保持日期轴连续
        parts.push(
          `<rect class="usage-bar" x="${(cx - barW / 2).toFixed(2)}" y="${(
            padT +
            innerH -
            1.5
          ).toFixed(2)}" width="${barW.toFixed(2)}" height="1.5" rx="0.75" opacity="0.45"><title>${title}</title></rect>`
        );
      }
    });

    if (centers.length > 1) {
      const points = centers.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
      parts.push(`<polyline class="usage-line" points="${points}" />`);
    }
    // 上游实际扣费（余额同步）：虚线叠加在估算值上，避免两个口径混在一起。
    if (rows.some((row) => (row.upstream_cost || 0) > 0)) {
      const upstreamPoints = upstreamCenters
        .map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`)
        .join(" ");
      parts.push(
        `<polyline class="usage-line usage-line-upstream" points="${upstreamPoints}" />`
      );
    }

    // X 轴标签：最多 10 个，避免拥挤
    const step = Math.max(1, Math.ceil(n / 10));
    rows.forEach((row, index) => {
      if (index % step !== 0 && index !== n - 1) return;
      const cx = padL + slot * (index + 0.5);
      parts.push(
        `<text class="usage-axis-text" x="${cx.toFixed(2)}" y="${H - 8}" text-anchor="middle">${row.date.slice(
          5
        )}</text>`
      );
    });

    els.usageChart.innerHTML =
      `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="每日点数消耗">${parts.join("")}</svg>`;
  }

  function renderUsageSummary(payload) {
    if (!els.usageSummary) return;
    const window_ = (payload.windows || {})[String(usageDays)] || {};
    const overall = payload.overall || {};
    const today = payload.today || {};
    const since = payload.first_day ? `自 ${payload.first_day}` : "暂无记录";
    const cards = [
      { label: "TODAY // 今日", value: today.cost || 0, unit: "点", sub: `${today.count || 0} 张` },
      {
        label: `LAST ${usageDays}D // 近 ${usageDays} 日`,
        value: window_.cost || 0,
        unit: "点",
        sub: `${window_.count || 0} 张 · 日均 ${window_.avg_cost || 0}`,
      },
      {
        label: "PEAK // 峰值",
        value: window_.peak_cost || 0,
        unit: "点",
        sub: window_.peak_date || "—",
      },
      {
        label: "TOTAL // 累计",
        value: overall.cost || 0,
        unit: "点",
        sub: `${overall.count || 0} 张 · ${since}`,
      },
    ];
    const upstream = payload.upstream || {};
    const upstreamWindow = window_.upstream_cost || 0;
    const upstreamOverall = overall.upstream_cost || 0;
    if (
      payload.upstream_enabled ||
      upstreamWindow ||
      upstreamOverall ||
      upstream.balance != null
    ) {
      const syncedAt = upstream.checked_at
        ? new Date(upstream.checked_at * 1000).toLocaleTimeString("zh-CN", {
            hour: "2-digit",
            minute: "2-digit",
          })
        : "—";
      let sub = "等待余额同步";
      if (upstream.balance != null) {
        sub = `余额 ${upstream.balance} 点 · ${syncedAt} 同步`;
      } else if (upstream.last_error) {
        sub = "同步异常";
      }
      cards.push({
        label: "UPSTREAM // 上游实际",
        value: upstreamWindow,
        unit: "点",
        sub,
      });
    }
    els.usageSummary.innerHTML = cards
      .map(
        (card) =>
          `<div class="usage-stat"><span class="usage-stat-label">${card.label}</span>` +
          `<span class="usage-stat-value">${card.value}<span class="usage-unit">${card.unit}</span></span>` +
          `<span class="usage-stat-sub">${card.sub}</span></div>`
      )
      .join("");
  }

  function renderUsageBreakdown(payload) {
    if (!els.usageBreakdown) return;
    const groups = (payload.breakdowns || {})[String(usageDays)] || {};
    const labels = { model: "MODEL", size: "SIZE", transport: "CHANNEL" };
    const html = Object.keys(labels)
      .map((field) => {
        const items = groups[field] || [];
        if (!items.length) return "";
        const chips = items
          .map((item) => `<span class="usage-bd-item">${item.name} · ${item.cost} 点</span>`)
          .join("");
        return `<div class="usage-bd-row"><span class="usage-bd-tag">${labels[field]}</span><span class="usage-bd-items">${chips}</span></div>`;
      })
      .filter(Boolean)
      .join("");
    els.usageBreakdown.innerHTML = html;
  }

  function renderUsage() {
    if (!usageData) return;
    const series = usageData.series || [];
    const rows = series.slice(-usageDays);
    const overall = usageData.overall || {};
    const hasCost =
      (overall.cost || 0) > 0 ||
      (overall.upstream_cost || 0) > 0 ||
      (usageData.upstream && usageData.upstream.balance != null);
    if (els.usageEmpty) els.usageEmpty.classList.toggle("hidden", hasCost);
    renderUsageSummary(usageData);
    renderUsageChart(rows);
    renderUsageBreakdown(usageData);
  }

  async function loadUsageStats() {
    if (!els.usageChart) return;
    try {
      const bridge = await getBridge();
      const resp = await bridge.apiGet("test_panel/usage");
      // 宿主 Bridge 会自动解包 handler 返回 JSON 的 data 字段（response.data?.data ?? response.data），
      // 因此 resp 可能已是数据本体；兼容两种形态，与 loadTokenStatus 的取值方式保持一致。
      usageData = (resp && resp.data) ? resp.data : resp;
      if (!usageData || typeof usageData !== "object") throw new Error("返回数据为空");
      if (els.usageDisabled) {
        els.usageDisabled.classList.toggle("hidden", usageData.enabled !== false);
      }
      if (els.usageError) els.usageError.classList.add("hidden");
      if (usageData.enabled === false) {
        if (els.usageSummary) els.usageSummary.innerHTML = "";
        if (els.usageChart) els.usageChart.innerHTML = "";
        if (els.usageBreakdown) els.usageBreakdown.innerHTML = "";
        if (els.usageEmpty) els.usageEmpty.classList.add("hidden");
        return;
      }
      renderUsage();
    } catch (err) {
      const msg = err?.message || String(err);
      if (els.usageError) {
        els.usageError.textContent = `用量统计加载失败：${msg}`;
        els.usageError.classList.remove("hidden");
      }
    }
  }

  // ===== 事件绑定 =====
  function bindEvents() {
    els.model.addEventListener("change", (event) => {
      if (event.isTrusted && slotState.appliedId) {
        slotState.appliedId = null;
        els.style.value = "custom";
        toggleCustomArtists();
        refreshSlotStatus();
      }
      updateSizeOptionsUI();
      updateCostBadge();
      updateReferenceWarning();
    });
    // 步数 / 画幅 / 张数变化时实时刷新点数预估
    ["steps", "size", "count"].forEach((key) => {
      const el = els[key];
      if (!el) return;
      el.addEventListener("input", updateCostBadge);
      el.addEventListener("change", updateCostBadge);
    });
    els.style.addEventListener("change", () => {
      const slot = slotState.slots.find((item) => `slot:${item.id}` === els.style.value);
      if (slot) applySlotToPanel(slot);
      else {
        slotState.appliedId = null;
        toggleCustomArtists();
        refreshSlotStatus();
      }
    });
    els.customArtists.addEventListener("input", (event) => {
      if (event.isTrusted && slotState.appliedId) {
        slotState.appliedId = null;
        els.style.value = "custom";
        refreshSlotStatus();
      }
    });
    initFormatToggle();
    els.refFile.addEventListener("change", handleReferenceFileChange);
    els.refRemove.addEventListener("click", clearReferenceImage);
    els.directorAction.addEventListener("change", updateReferenceWarning);
    if (els.refMode) {
      els.refMode.addEventListener("change", () => {
        applyRefModeUI();
        renderRefGrid();
        saveCache();
      });
    }
    if (els.charAdd) {
      els.charAdd.addEventListener("click", () => {
        addCharRow();
        saveCache();
      });
    }
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
    if (els.usageRefreshBtn) {
      els.usageRefreshBtn.addEventListener("click", loadUsageStats);
    }
    if (els.usageRangeSwitch) {
      els.usageRangeSwitch.addEventListener("click", (e) => {
        const btn = e.target.closest(".usage-range-btn");
        if (!btn) return;
        usageDays = parseInt(btn.dataset.days, 10) || 7;
        Array.from(els.usageRangeSwitch.querySelectorAll(".usage-range-btn")).forEach((b) => {
          b.classList.toggle("is-active", b === btn);
        });
        renderUsage();
      });
    }

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

  // ===== 画风槽位与法典 =====
  const slotState = { slots: [], activeId: "1", models: [], enabled: false, appliedId: null, autoApplied: false, saving: false };

  function slotEls() {
    return {
      rows: document.getElementById("slotRows"),
      add: document.getElementById("btnSlotAdd"),
      save: document.getElementById("btnSlotSave"),
      reset: document.getElementById("btnSlotReset"),
      status: document.getElementById("slotStatus"),
      grimQuery: document.getElementById("grimQuery"),
      grimSearch: document.getElementById("btnGrimSearch"),
      grimResults: document.getElementById("grimResults"),
    };
  }

  function unwrapData(resp) {
    if (resp && typeof resp === "object" && "data" in resp) return resp.data;
    return resp;
  }

  function renderSlotRows() {
    const e = slotEls();
    if (!e.rows) return;
    e.rows.textContent = "";
    if (!slotState.slots.length) {
      const empty = document.createElement("div");
      empty.className = "grimoire-empty";
      empty.textContent = "还没有槽位，点「新增槽位」开始。";
      e.rows.appendChild(empty);
      return;
    }
    const models = slotState.models.length
      ? slotState.models
      : ["nai-diffusion-4-5-full", "nai-diffusion-5-full"];

    renderSlotOptions();
    slotState.slots.forEach((slot) => {
      const row = document.createElement("div");
      row.className = "slot-row";

      const name = document.createElement("input");
      name.className = "input acrylic-input";
      name.type = "text";
      name.placeholder = "画风名";
      name.value = slot.name || "";
      name.addEventListener("input", () => {
        slot.name = name.value;
        renderSlotOptions();
        refreshSlotStatus();
      });

      const model = document.createElement("select");
      model.className = "select acrylic-input";
      models.forEach((m) => {
        const opt = document.createElement("option");
        opt.value = m;
        opt.textContent = m;
        if (m === slot.model) opt.selected = true;
        model.appendChild(opt);
      });
      if (slot.model && !models.includes(slot.model)) {
        const opt = document.createElement("option");
        opt.value = slot.model;
        opt.textContent = slot.model;
        opt.selected = true;
        model.appendChild(opt);
      }
      model.addEventListener("change", () => {
        slot.model = model.value;
        if (String(slotState.appliedId) === String(slot.id)) applySlotToPanel(slot);
      });

      const artists = document.createElement("input");
      artists.className = "input acrylic-input";
      artists.type = "text";
      artists.placeholder = "对应画师串";
      artists.value = slot.artists || "";
      artists.addEventListener("input", () => {
        slot.artists = artists.value;
        if (String(slotState.appliedId) === String(slot.id)) applySlotToPanel(slot);
      });

      const actions = document.createElement("div");
      actions.className = "slot-row-actions";

      const apply = document.createElement("button");
      apply.type = "button";
      apply.className = "btn btn-primary";
      apply.textContent = String(slot.id) === slotState.activeId ? "全局生效" : "全局启用";
      apply.title = "保存槽位并将该画风设为全局默认";
      apply.disabled = !slotState.enabled || slotState.saving;
      apply.addEventListener("click", () => saveSlots(slot.id));

      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn-secondary";
      del.textContent = "删除";
      del.addEventListener("click", () => {
        slotState.slots = slotState.slots.filter((s) => s.id !== slot.id);
        if (String(slotState.appliedId) === String(slot.id)) {
          slotState.appliedId = null;
          els.style.value = "custom";
        }
        renderSlotRows();
        refreshSlotStatus();
      });

      actions.append(apply, del);
      row.append(name, model, artists, actions);
      e.rows.appendChild(row);
    });
  }

  function renderSlotOptions() {
    const selected = els.style.value;
    els.style.querySelectorAll('[data-slot-option]').forEach((option) => option.remove());
    if (slotState.enabled) {
      slotState.slots.forEach((slot) => {
        const option = document.createElement("option");
        option.dataset.slotOption = "true";
        option.value = `slot:${slot.id}`;
        option.textContent = slot.name || `槽位 ${slot.id}`;
        els.style.appendChild(option);
      });
    }
    els.style.value = selected;
  }

  // Keep the selected slot and the visible generation fields in sync.
  function applySlotToPanel(slot) {
    const e = slotEls();
    if (!slot) return;
    if (!slotState.enabled) {
      if (e.status) {
        e.status.textContent =
          "「启用画风槽位」当前关闭，槽位不参与生图；请到插件设置里打开后再应用。";
      }
      return;
    }
    const modelValue = String(slot.model || "").trim();
    if (els.model && modelValue) {
      if (!Array.from(els.model.options).some((o) => o.value === modelValue)) {
        const opt = document.createElement("option");
        opt.value = modelValue;
        opt.textContent = modelValue;
        els.model.appendChild(opt);
      }
      els.model.value = modelValue;
    }
    if (els.style) els.style.value = `slot:${slot.id}`;
    if (els.customArtists) {
      els.customArtists.value = String(slot.artists || "");
      els.customArtists.dispatchEvent(new Event("input", { bubbles: true }));
    }
    toggleCustomArtists();
    updateSizeOptionsUI();
    updateCostBadge();
    slotState.appliedId = String(slot.id);
    updateReferenceWarning();
    refreshSlotStatus();
    if (e.status) {
      const name = slot.name || `槽位 ${slot.id}`;
      e.status.textContent =
        `已套用画风「${name}」：模型 ${modelValue || "默认"}，画师串 ${
          String(slot.artists || "").trim() ? "已填入" : "为空（不拼画师串）"
        }。改动上方「画风 / 画师串」即脱离该槽位。`;
    }
  }

  function refreshSlotStatus() {
    const e = slotEls();
    if (!e.status) return;
    if (!slotState.enabled) {
      e.status.textContent =
        "「启用画风槽位」当前是关闭的：这里的槽位可以编辑保存，但不会影响聊天生图。" +
        "想让它生效，请到插件设置里打开「启用画风槽位」。";
      return;
    }
    const active = slotState.slots.find(
      (s) => String(s.id) === String(slotState.activeId)
    );
    const activeName = active ? active.name || `槽位 ${active.id}` : slotState.activeId;
    const applied = slotState.slots.find(
      (s) => String(s.id) === String(slotState.appliedId)
    );
    const appliedText = applied
      ? `本面板已套用「${applied.name || `槽位 ${applied.id}`}」。`
      : "";
    e.status.textContent =
      `全局画风：${activeName}（槽位 ${slotState.activeId}），共 ${slotState.slots.length} 个。` +
      appliedText;
  }

  async function loadSlots() {
    const e = slotEls();
    if (!e.rows) return;
    try {
      const bridge = await getBridge();
      const resp = await bridge.apiGet("test_panel/slots");
      const data = unwrapData(resp) || {};
      slotState.slots = Array.isArray(data.slots)
        ? data.slots.map((s) => ({
            id: Number(s.id) || 0,
            name: String(s.name || ""),
            model: String(s.model || ""),
            artists: String(s.artists || ""),
          }))
        : [];
      slotState.activeId = String(data.active_slot_id || "1");
      slotState.models = Array.isArray(data.model_options) ? data.model_options : [];
      slotState.enabled = data.enabled === true;
      renderSlotRows();
      refreshSlotStatus();
      if (slotState.enabled && !slotState.autoApplied) {
        slotState.autoApplied = true;
        const selected = slotState.slots.find((slot) => `slot:${slot.id}` === els.style.value);
        const active = slotState.slots.find(
          (s) => String(s.id) === String(slotState.activeId)
        );
        if (selected || active) applySlotToPanel(selected || active);
      }
    } catch (err) {
      if (e.status) e.status.textContent = `读取槽位失败：${err && err.message ? err.message : err}`;
    }
  }

  async function saveSlots(activeSlotId = null) {
    const e = slotEls();
    if (slotState.saving) return;
    if (!slotState.slots.length) {
      if (e.status) e.status.textContent = "至少保留一个槽位。";
      return;
    }
    slotState.saving = true;
    if (e.save) e.save.disabled = true;
    try {
      const appliedIndex = slotState.slots.findIndex(
        (slot) => String(slot.id) === String(activeSlotId ?? slotState.appliedId)
      );
      const activeIndex = slotState.slots.findIndex(
        (slot) => String(slot.id) === String(activeSlotId ?? slotState.activeId)
      );
      const bridge = await getBridge();
      const resp = await bridge.apiPost("test_panel/slots_save", {
        slots: slotState.slots,
        active_slot_id: String(Math.max(0, activeIndex) + 1),
      });
      const data = unwrapData(resp) || {};
      slotState.activeId = String(data.active_slot_id || slotState.activeId);
      if (Array.isArray(data.slots) && data.slots.length) {
        slotState.slots = data.slots.map((s) => ({
          id: Number(s.id) || 0,
          name: String(s.name || ""),
          model: String(s.model || ""),
          artists: String(s.artists || ""),
        }));
        renderSlotRows();
        if (appliedIndex >= 0 && slotState.slots[appliedIndex]) {
          applySlotToPanel(slotState.slots[appliedIndex]);
        }
      }
      refreshSlotStatus();
      if (e.status) e.status.textContent = `槽位已保存。${e.status.textContent}`;
    } catch (err) {
      if (e.status) e.status.textContent = `保存失败：${err && err.message ? err.message : err}`;
    } finally {
      slotState.saving = false;
      if (e.save) e.save.disabled = false;
      renderSlotRows();
    }
  }

  function appendToNaiPrompt(text) {
    if (!els.naiPrompt || !text) return;
    const cur = els.naiPrompt.value.trim();
    els.naiPrompt.value = cur ? `${cur}, ${text}` : text;
    els.naiPrompt.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function renderGrimoireResults(items) {
    const e = slotEls();
    if (!e.grimResults) return;
    e.grimResults.textContent = "";
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "grimoire-empty";
      empty.textContent = "当前模型可用的词库里没有匹配词条，换个短一点的中文关键词试试。";
      e.grimResults.appendChild(empty);
      return;
    }
    items.forEach((item) => {
      const card = document.createElement("div");
      card.className = "grimoire-item";

      const head = document.createElement("div");
      head.className = "grimoire-item-head";

      const title = document.createElement("span");
      title.className = "grimoire-item-title";
      title.textContent = `【${item.title || ""}】`;

      const insert = document.createElement("button");
      insert.type = "button";
      insert.className = "btn btn-secondary";
      insert.textContent = "插入提示词";
      insert.addEventListener("click", () => appendToNaiPrompt(item.clean_tags || ""));

      head.append(title, insert);

      const meta = document.createElement("div");
      meta.className = "grimoire-item-meta";
      meta.textContent = `${item.volume || ""} · ${item.category || ""} · ${item.model_ver || ""}`;

      const tags = document.createElement("div");
      tags.className = "grimoire-item-tags";
      tags.textContent = item.clean_tags || "";

      card.append(head, meta, tags);
      e.grimResults.appendChild(card);
    });
  }

  async function searchGrimoire() {
    const e = slotEls();
    if (!e.grimQuery) return;
    const query = e.grimQuery.value.trim();
    if (!query) return;
    if (e.grimResults) e.grimResults.textContent = "检索中…";
    try {
      const bridge = await getBridge();
      const resp = await bridge.apiPost("test_panel/grimoire_search", {
        query,
        limit: 12,
      });
      const payload = unwrapData(resp) || {};
      if (payload.message && !(payload.items && payload.items.length)) {
        if (e.grimResults) e.grimResults.textContent = payload.message;
        return;
      }
      renderGrimoireResults(Array.isArray(payload.items) ? payload.items : []);
    } catch (err) {
      if (e.grimResults) {
        e.grimResults.textContent = `检索失败：${err && err.message ? err.message : err}`;
      }
    }
  }

  function bindSlotEvents() {
    const e = slotEls();
    if (!e.rows) return;
    if (e.add) {
      e.add.addEventListener("click", () => {
        const nextId = slotState.slots.reduce((m, s) => Math.max(m, Number(s.id) || 0), 0) + 1;
        slotState.slots.push({
          id: nextId,
          name: `新画风 ${nextId}`,
          model: (slotState.models[0] || "nai-diffusion-4-5-full"),
          artists: "",
        });
        renderSlotRows();
      });
    }
    if (e.save) e.save.addEventListener("click", () => saveSlots());
    if (e.reset) {
      e.reset.addEventListener("click", async () => {
        if (!window.confirm("会用内置画风重建槽位表，当前槽位（含自定义）会被覆盖，确定吗？")) {
          return;
        }
        try {
          const bridge = await getBridge();
          const resp = await bridge.apiPost("test_panel/slots_reset", {});
          const data = unwrapData(resp) || {};
          if (Array.isArray(data.slots)) {
            slotState.activeId = String(data.active_slot_id || "1");
            slotState.slots = data.slots.map((s) => ({
              id: Number(s.id) || 0,
              name: String(s.name || ""),
              model: String(s.model || ""),
              artists: String(s.artists || ""),
            }));
            renderSlotRows();
            slotState.appliedId = null;
            els.style.value = "custom";
            toggleCustomArtists();
            refreshSlotStatus();
          }
          if (e.status) e.status.textContent = "已恢复为内置画风。";
        } catch (err) {
          if (e.status) {
            e.status.textContent = `恢复失败：${err && err.message ? err.message : err}`;
          }
        }
      });
    }
    if (e.grimSearch) e.grimSearch.addEventListener("click", searchGrimoire);
    if (e.grimQuery) {
      e.grimQuery.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") {
          ev.preventDefault();
          searchGrimoire();
        }
      });
    }
  }

  // ===== 初始化 =====
  async function init() {
    console.log(
      "%c ENDFIELD PROTOCOL %c NAI VISUAL SYNTHESIS SYSTEM 04 INITIALIZED ",
      "background: #fff500; color: #101110; font-weight: bold; padding: 2px 4px; border-radius: 4px;",
      "background: #e8e8e2; color: #101110; padding: 2px 4px; border-radius: 4px;"
    );
    // 启动高可见度等高线拓扑地形动画引擎
    initContourEngine();

    bindEvents();
    // 先加载服务配置和完整选项，再恢复用户缓存，避免默认值覆盖选择。
    await loadTokenStatus();
    const cacheRestored = await loadCache();
    slotState.autoApplied = cacheRestored;
    toggleCustomArtists();
    updateCostBadge();
    // 加载试用状态
    await loadTrialStatus();
    // 加载点数用量台账
    await loadUsageStats();
    // 加载画风槽位与法典面板
    bindSlotEvents();
    await loadSlots();
  }

  // DOM 就绪后启动
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
