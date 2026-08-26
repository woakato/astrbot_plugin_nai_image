/**
 * NAI 生图测试面板 - 前端逻辑 v4.6 (Endfield Protocol)
 *
 * 包含：
 *   - 双提示词框：NAI 风格 + 自然语言
 *   - OpenAI 兼容格式：参考图（vibe / img2img / director 精准参考）、噪声、
 *     种子、director-tools 图片处理动作、多角色坐标控制与完整模型列表
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
    refPreviewWrap: $("refPreviewWrap"),
    refPreview: $("refPreview"),
    refRemove: $("refRemove"),
    refStrengthWrap: $("refStrengthWrap"),
    refStrength: $("refStrength"),
    refModeWrap: $("refModeWrap"),
    refMode: $("refMode"),
    refNoiseWrap: $("refNoiseWrap"),
    refNoise: $("refNoise"),
    openaiSeedWrap: $("openaiSeedWrap"),
    openaiSeed: $("openaiSeed"),
    directorActionWrap: $("directorActionWrap"),
    directorAction: $("directorAction"),
    directorCaptionWrap: $("directorCaptionWrap"),
    directorCaption: $("directorCaption"),
    charListWrap: $("charListWrap"),
    charList: $("charList"),
    charAdd: $("charAdd"),
  };

  // ===== 状态 =====
  let isGenerating = false;
  let lastRequestBody = null;
  // 本地上传参考图的裸 base64（不含 data: 前缀），仅 OpenAI 兼容格式使用
  let referenceImageB64 = "";
  // 当前调用格式："direct" | "openai"
  let currentCallFormat = "direct";
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
  // NAI 直连模式可用的模型下拉项
  const DIRECT_MODEL_OPTIONS = [
    { value: "nai-diffusion-4-5-full", label: "V4.5 完整版 [4.5_FULL]" },
    { value: "nai-diffusion-5-full", label: "V5 完整版 [5.0_FULL]" },
  ];

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
      "refStrength", "refMode", "refNoise", "openaiSeed", "directorAction",
      "directorCaption"
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
      getCachedFields().forEach((key) => {
        const el = els[key];
        if (el && data[key] != null) {
          el.value = data[key];
          restored = true;
        }
      });
      if (data.callFormat === "openai" || data.callFormat === "direct") {
        currentCallFormat = data.callFormat;
        setCallFormat(currentCallFormat);
        restored = true;
      }
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
      if (config.image_gen_key === "已配置") {
        setBadge(els.tokenBadge, "Token: 已配置", "badge-success");
      } else {
        setBadge(els.tokenBadge, "Token: 未配置", "badge-error");
      }
      setBadge(els.baseUrlBadge, config.base_url || "--", "badge-info");
      // 后端下发的 OpenAI 兼容模型列表（对齐接口文档 §9）
      if (Array.isArray(config.openai_models) && config.openai_models.length) {
        openaiModels = config.openai_models;
        if (currentCallFormat === "openai") updateModelOptionsUI();
      }
      // 精准参考描述：后端配置下发默认值（不覆盖已缓存的输入）
      if (els.directorCaption && !els.directorCaption.value.trim() && config.openai_director_caption) {
        els.directorCaption.value = config.openai_director_caption;
      }
      // OpenAI 兼容格式配置状态（仅在 OpenAI 模式下展示）
      if (els.openaiConfigStatus && currentCallFormat === "openai") {
        show(els.openaiConfigStatus);
        if (config.openai_available) {
          els.openaiConfigStatusText.textContent =
            "OpenAI 接口已配置：" + (config.openai_api_base_url || "");
          els.openaiConfigStatus.querySelector(".status-dot").className =
            "status-dot ok";
        } else {
          els.openaiConfigStatusText.textContent =
            "OpenAI 接口未配置：请在插件设置填写 openai_api_base_url 与 openai_api_key";
          els.openaiConfigStatus.querySelector(".status-dot").className =
            "status-dot error";
        }
      } else if (els.openaiConfigStatus) {
        hide(els.openaiConfigStatus);
      }
    } catch (err) {
      setBadge(els.tokenBadge, "配置加载失败", "badge-error");
    }
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

  function getModelCostFloor(modelName) {
    return modelName === "nai-diffusion-5-full" ? 5 : 1;
  }

  function updateSizeOptionsUI() {
    const curSize = els.size.value;
    const curModel = els.model.value;
    els.size.innerHTML = "";
    SIZE_BASE_COSTS.forEach((opt) => {
      const cost = Math.max(opt.baseCost, getModelCostFloor(curModel));
      const elOpt = document.createElement("option");
      elOpt.value = opt.value;
      elOpt.textContent = `${opt.value}(-${cost})`;
      if (opt.value === curSize) {
        elOpt.selected = true;
      }
      els.size.appendChild(elOpt);
    });
    if (!SIZE_BASE_COSTS.some((opt) => opt.value === curSize)) {
      els.size.value = "竖图";
    }
  }

  // 按当前调用格式替换模型下拉项：直连只有 V4.5/V5 完整版，
  // OpenAI 兼容格式展示接口文档 §9 的完整模型列表。
  function updateModelOptionsUI() {
    const curModel = els.model.value;
    els.model.innerHTML = "";
    if (currentCallFormat === "openai") {
      openaiModels.forEach((name) => {
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
    if (els.style.value === "custom") {
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
      style: els.style.value,
      size: els.size.value,
      sampler: els.sampler.value,
      steps: safeInt(els.steps.value, 24),
      scale: safeFloat(els.scale.value, 6),
      cfg: safeFloat(els.cfg.value, 7),
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

    // OpenAI 兼容格式：附带本地上传参考图、重绘强度、噪声、种子、
    // 参考图使用模式与 director-tools 动作（对齐接口文档 §4-§8）
    if (body.call_format === "openai") {
      if (referenceImageB64) {
        body.reference_image_b64 = referenceImageB64;
      }
      // director 模式下这两项是 img2img 参数、不适用，跳过以免默认值
      // 覆盖精准参考的主强度（后端按文档 §8.5 默认 1.0）
      const isDirector = els.refMode && els.refMode.value === "director";
      if (!isDirector && els.refStrength) {
        const strength = parseFloat(els.refStrength.value);
        if (!Number.isNaN(strength) && strength > 0 && strength <= 1) {
          body.strength = strength;
        }
      }
      if (!isDirector && els.refNoise) {
        const noise = parseFloat(els.refNoise.value);
        if (!Number.isNaN(noise) && noise >= 0 && noise <= 1) {
          body.noise = noise;
        }
      }
      if (els.openaiSeed && els.openaiSeed.value !== "") {
        const seed = parseInt(els.openaiSeed.value, 10);
        if (!Number.isNaN(seed) && seed >= -1) {
          body.seed = seed;
        }
      }
      if (els.refMode) {
        body.reference_mode = els.refMode.value;
      }
      if (els.directorAction && els.directorAction.value) {
        body.director_action = els.directorAction.value;
      }
      // 精准参考（§6）：director 模式下附带 base_caption
      if (els.refMode && els.refMode.value === "director" && els.directorCaption) {
        const caption = els.directorCaption.value.trim();
        if (caption) {
          body.director_caption = caption;
        }
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

  // 精准参考描述输入框仅在 director 模式下可见
  function toggleDirectorCaption() {
    const openai = currentCallFormat === "openai";
    const director = els.refMode && els.refMode.value === "director";
    if (openai && director) {
      show(els.directorCaptionWrap);
      // 精准参考不需要重绘强度/附加噪声（那是 img2img 参数），隐藏避免误导
      hide(els.refStrengthWrap);
      hide(els.refNoiseWrap);
    } else {
      hide(els.directorCaptionWrap);
      if (openai) {
        show(els.refStrengthWrap);
        show(els.refNoiseWrap);
      }
    }
  }

  // ===== 生成图片 =====
  async function generate() {
    if (isGenerating) return;

    const body = buildRequestBody();
    if (!body.nai_prompt && !body.nl_prompt && !body.director_action) {
      showError("请至少填写一个提示词框（NAI 风格或自然语言）。");
      return;
    }
    if (body.director_action && !body.reference_image_b64) {
      showError("图片处理动作需要先在「调用格式」卡片上传一张源图片。");
      return;
    }
    if (body.reference_mode === "director" && !body.reference_image_b64) {
      showError("精准参考（director）需要先在「调用格式」卡片上传一张参考图。");
      return;
    }

    lastRequestBody = body;
    isGenerating = true;
    els.loadingText.textContent = body.director_action
      ? "正在以 director-tools 处理图片..."
      : body.call_format === "openai"
        ? "正在以 OpenAI 兼容格式生成（参考图已随请求提交）..."
        : "正在转译 + 生成图片...";
    setLoading(true);
    hideError();
    hideResults();
    hideMergeInfo();

    try {
      const bridge = await getBridge();
      const endpoint =
        body.call_format === "openai"
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
    const sizeNames = {
      "竖图": "竖图", "横图": "横图", "方图": "方图",
      "2K竖图": "2K竖图", "2K横图": "2K横图", "2K方图": "2K方图",
      "4K竖图": "4K竖图", "4K横图": "4K横图", "4K方图": "4K方图"
    };
    const formatLabel = requestBody.call_format === "openai" ? "OpenAI" : "NAI直连";
    const metaText = `${styleNames[requestBody.style] || requestBody.style} · ${sizeNames[requestBody.size] || requestBody.size} · ${images.length} UNIT · ${formatLabel}`;
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
    els.cfg.value = "7";
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
    els.directorCaption.value = "character&style";
    clearCharRows();
    toggleCustomArtists();
    setCallFormat("direct");
    saveCache();
  }

  // ===== 调用格式切换（NAI 直连 / OpenAI 兼容） =====
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
    currentCallFormat = fmt;
    formatToggleBtns.forEach(function (btn) {
      var active = btn.getAttribute("data-format") === fmt;
      btn.classList.toggle("active", active);
    });
    var openai = fmt === "openai";
    if (openai) {
      show(els.refUploadWrap);
      show(els.refStrengthWrap);
      show(els.refModeWrap);
      show(els.refNoiseWrap);
      show(els.openaiSeedWrap);
      show(els.directorActionWrap);
      show(els.charListWrap);
      if (els.openaiConfigStatus) show(els.openaiConfigStatus);
      els.callFormatHint.textContent =
        "请求按 OpenAI 兼容格式发往配置的 /v1/images 端点，支持参考图、种子与图片处理动作。";
      els.trialBtn.disabled = true;
      els.trialBtn.title = "试用生成仅支持 NAI 直连格式";
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
    toggleDirectorCaption();
    updateModelOptionsUI();
  }

  function clearReferenceImage() {
    referenceImageB64 = "";
    if (els.refFile) els.refFile.value = "";
    hide(els.refPreviewWrap);
    if (els.refPreview) els.refPreview.removeAttribute("src");
  }

  function handleReferenceFileChange() {
    const file = els.refFile && els.refFile.files && els.refFile.files[0];
    if (!file) {
      clearReferenceImage();
      return;
    }
    const reader = new FileReader();
    reader.onload = (e) => {
      const dataUrl = String(e.target.result || "");
      const comma = dataUrl.indexOf(",");
      referenceImageB64 = comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
      els.refPreview.src = dataUrl;
      show(els.refPreviewWrap);
    };
    reader.onerror = () => {
      clearReferenceImage();
      showError("参考图读取失败，请重新选择图片文件。");
    };
    reader.readAsDataURL(file);
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
    els.model.addEventListener("change", () => {
      updateSizeOptionsUI();
    });
    els.style.addEventListener("change", toggleCustomArtists);
    initFormatToggle();
    els.refFile.addEventListener("change", handleReferenceFileChange);
    els.refRemove.addEventListener("click", clearReferenceImage);
    if (els.refMode) {
      els.refMode.addEventListener("change", toggleDirectorCaption);
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
    console.log(
      "%c ENDFIELD PROTOCOL %c NAI VISUAL SYNTHESIS SYSTEM 04 INITIALIZED ",
      "background: #fff500; color: #101110; font-weight: bold; padding: 2px 4px; border-radius: 4px;",
      "background: #e8e8e2; color: #101110; padding: 2px 4px; border-radius: 4px;"
    );
    // 启动高可见度等高线拓扑地形动画引擎
    initContourEngine();

    bindEvents();
    // 从后端恢复面板状态
    await loadCache();
    updateSizeOptionsUI();
    toggleCustomArtists();
    setCallFormat(currentCallFormat);
    // 仅加载 token 状态 badge（不填表单）
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
