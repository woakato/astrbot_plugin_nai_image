// Run with: node --test tests/panel_ui.test.cjs (requires Playwright and Chromium).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { after, before, test } = require("node:test");
const { chromium } = require("playwright");

const panelDir = path.join(__dirname, "../pages/test-panel");
const png = Buffer.from(
  "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489" +
  "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082",
  "hex"
);
let server;
let browser;
let origin;

before(async () => {
  server = http.createServer((request, response) => {
    const filename = path.basename(new URL(request.url, "http://localhost").pathname);
    const types = {
      "index.html": "text/html; charset=utf-8",
      "app.js": "application/javascript; charset=utf-8",
      "app.css": "text/css; charset=utf-8",
    };
    if (!types[filename]) {
      response.writeHead(404).end();
      return;
    }
    response.setHeader("Content-Type", types[filename]);
    response.end(fs.readFileSync(path.join(panelDir, filename)));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({
    headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {}),
  });
});

after(async () => {
  if (browser) await browser.close();
  if (server) await new Promise((resolve) => server.close(resolve));
});

async function withPanel(settings, callback) {
  const context = await browser.newContext({
    viewport: { width: settings.width || 1440, height: 1000 },
  });
  context.setDefaultTimeout(5000);
  try {
    await context.addInitScript(({ settings, png }) => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: async (text) => { window.__copiedPrompt = text; } },
      });
      const galleryImages = Array.from({ length: settings.galleryCount || 0 }, (_, index) => {
        const canvas = document.createElement("canvas");
        canvas.width = 480;
        canvas.height = 600;
        const context = canvas.getContext("2d");
        const hue = [172, 34, 208, 344, 202][index % 5];
        const sky = context.createLinearGradient(0, 0, 0, canvas.height);
        sky.addColorStop(0, `hsl(${hue} 48% 72%)`);
        sky.addColorStop(0.58, `hsl(${(hue + 28) % 360} 42% 48%)`);
        sky.addColorStop(1, `hsl(${(hue + 55) % 360} 38% 24%)`);
        context.fillStyle = sky;
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.fillStyle = `hsl(${(hue + 42) % 360} 85% 82%)`;
        context.beginPath();
        context.arc(350 - (index % 3) * 35, 150 + (index % 2) * 22, 52, 0, Math.PI * 2);
        context.fill();
        context.fillStyle = `hsl(${(hue + 16) % 360} 34% 30%)`;
        context.beginPath();
        context.moveTo(0, 420);
        context.lineTo(130, 270 + (index % 3) * 24);
        context.lineTo(235, 415);
        context.lineTo(340, 295);
        context.lineTo(480, 430);
        context.lineTo(480, 600);
        context.lineTo(0, 600);
        context.fill();
        context.fillStyle = `hsl(${(hue + 185) % 360} 28% 14%)`;
        context.beginPath();
        context.ellipse(230, 385, 52, 82, 0, 0, Math.PI * 2);
        context.fill();
        context.beginPath();
        context.arc(230, 286, 34, 0, Math.PI * 2);
        context.fill();
        context.fillStyle = "rgba(255,255,255,.72)";
        for (let dot = 0; dot < 18; dot++) {
          const x = (dot * 83 + index * 47) % canvas.width;
          const y = (dot * 131 + 38) % 260;
          context.fillRect(x, y, 3, 3);
        }
        return canvas.toDataURL("image/png").split(",", 2)[1];
      });
      const state = {
        cache: settings.cache || {},
        calls: [],
        slots: [
          { id: 1, name: "Style 1", model: "nai-diffusion-4-5-full", artists: "artist:one" },
          { id: 2, name: "Style 2", model: "nai-diffusion-5-full", artists: "artist:two" },
        ],
        active: "1",
        usage: settings.usage || { enabled: false },
        gallery: galleryImages.map((thumbnail_b64, index) => ({
          name: `nai_${index + 1}.png`,
          created_at: 1794787200000 + index * 1000,
          width: 832,
          height: 1216,
          thumbnail_b64,
          prompt_preview: `1girl, gallery sample ${index + 1}`,
        })),
        galleryFailures: settings.galleryFailures || 0,
      };
      window.__panelTest = state;
      const wrap = (data) => settings.wrapped ? { status: "ok", data } : data;
      window.AstrBotPluginPage = {
        ready: async () => {},
        apiGet: async (endpoint) => {
          if (endpoint.endsWith("/load_cache")) return wrap(structuredClone(state.cache));
          if (endpoint.endsWith("/config")) return wrap({
            image_gen_key: "",
            base_url: "http://example.invalid",
            show_cost_estimate: false,
            openai_available: true,
            openai_api_base_url: "http://images.example.invalid",
            openai_models: [
              "nai-diffusion-4-5-full", "nai-diffusion-5-full", "nai-diffusion-5-curated",
            ],
          });
          if (endpoint.endsWith("/trial_status")) return wrap({
            available: true, key_loaded: true, used: 0, remaining: 3, max_uses: 3,
          });
          if (endpoint.endsWith("/usage")) return wrap(state.usage);
          if (endpoint.endsWith("/slots")) return wrap({
            slots: state.slots,
            active_slot_id: state.active,
            enabled: settings.enabled !== false,
            model_options: ["nai-diffusion-4-5-full", "nai-diffusion-5-full"],
          });
          throw new Error(`Unexpected GET ${endpoint}`);
        },
        apiPost: async (endpoint, body) => {
          state.calls.push({ endpoint, body: structuredClone(body) });
          if (endpoint.endsWith("/save_cache")) {
            state.cache = structuredClone(body);
            return { status: "ok" };
          }
          if (endpoint.endsWith("/slots_save")) {
            state.slots = body.slots.map((slot, index) => ({ ...slot, id: index + 1 }));
            state.active = body.active_slot_id;
            return wrap({ slots: state.slots, active_slot_id: state.active });
          }
          if (endpoint.endsWith("/gallery")) {
            if (state.galleryFailures > 0) {
              state.galleryFailures--;
              throw new Error("Gallery temporarily unavailable");
            }
            const query = String(body.query || "").trim().toLowerCase();
            const filtered = query
              ? state.gallery.filter((item) =>
                  `${item.name} ${item.prompt_preview}`.toLowerCase().includes(query)
                )
              : state.gallery;
            return wrap({
              items: filtered.slice(body.offset, body.offset + body.limit),
              total: filtered.length,
              all_total: state.gallery.length,
              offset: body.offset,
              limit: body.limit,
              saving_enabled: settings.historyEnabled !== false,
            });
          }
          if (endpoint.endsWith("/gallery_image")) {
            if (settings.missingGalleryImage) throw new Error("图片已不存在");
            const index = Number(body.name.match(/\d+/)?.[0] || 1);
            return wrap({
              name: body.name,
              mime_type: "image/png",
              b64_json: galleryImages[index - 1] || png,
              prompt: `1girl, full gallery prompt ${index}`,
              metadata: {
                model: "nai-diffusion-5-full",
                size: "832x1216",
                steps: 24,
                scale: 6,
                cfg: 1,
                sampler: "k_dpmpp_2m_sde",
                seed: 12345,
                mode: "t2i",
              },
            });
          }
          if (endpoint.endsWith("/gallery_delete")) {
            state.gallery = state.gallery.filter((item) => item.name !== body.name);
            return wrap({ deleted: body.name });
          }
          if (endpoint.endsWith("/grimoire_search")) return wrap({ items: [] });
          if (endpoint.endsWith("/trial_generate") && settings.failTrial) {
            throw new Error("Trial failed");
          }
          if (/\/(generate|generate_openai|trial_generate)$/.test(endpoint)) {
            if (settings.holdGeneration) {
              await new Promise((resolve) => { state.releaseGeneration = resolve; });
            }
            return [{ b64_json: png }];
          }
          throw new Error(`Unexpected POST ${endpoint}`);
        },
      };
    }, { settings, png: png.toString("base64") });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${origin}/index.html`);
    await page.locator("#slotRows .slot-row").first().waitFor();
    await page.waitForFunction(() => !document.querySelector("#generateBtn").disabled);
    await callback(page);
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
}

async function generate(page) {
  await page.locator("#naiPrompt").fill("1girl");
  await page.locator("#generateBtn").click();
  await page.waitForFunction(() => !document.querySelector("#generateBtn").disabled);
  return page.evaluate(() => window.__panelTest.calls.find(
    (call) => /\/generate(_openai)?$/.test(call.endpoint)
  ).body);
}

test("OpenAI model, resolution and zero coordinates survive cache restore", async () => {
  await withPanel({
    enabled: false,
    wrapped: true,
    cache: {
      callFormat: "openai",
      model: "nai-diffusion-5-curated",
      size: "1920x1088",
      characters: [{ prompt: "1girl", x: 0, y: 0 }],
    },
  }, async (page) => {
    assert.equal(await page.locator("#model").inputValue(), "nai-diffusion-5-curated");
    assert.equal(await page.locator("#size").inputValue(), "1920x1088");
    const body = await generate(page);
    assert.equal(body.model, "nai-diffusion-5-curated");
    assert.equal(body.size, "1920x1088");
    assert.deepEqual(body.characters, [{ prompt: "1girl", x: 0, y: 0 }]);
  });
});

test("manual custom artists survive reload with global slots enabled", async () => {
  await withPanel({
    cache: { style: "custom", customArtists: "artist:manual", model: "nai-diffusion-5-full" },
  }, async (page) => {
    assert.equal(await page.locator("#style").inputValue(), "custom");
    assert.equal(await page.locator("#customArtists").inputValue(), "artist:manual");
    const body = await generate(page);
    assert.equal(body.custom_artists, "artist:manual");
    assert.equal(body.model, "nai-diffusion-5-full");
  });
});

test("zero coordinates survive independently of the cached channel", async () => {
  await withPanel({
    cache: { characters: [{ prompt: "1girl", x: 0, y: 0 }] },
  }, async (page) => {
    await page.locator('[data-format="openai"]').click();
    const body = await generate(page);
    assert.deepEqual(body.characters, [{ prompt: "1girl", x: 0, y: 0 }]);
  });
});

test("enabled slots and legacy presets remain mutually exclusive", async () => {
  for (const enabled of [true, false]) {
    await withPanel({ enabled }, async (page) => {
      const values = await page.locator("#style option").evaluateAll(
        (options) => options.map((option) => option.value)
      );
      assert.deepEqual(values, enabled
        ? ["slot:1", "slot:2", "custom"]
        : ["vertical", "comicDoujin", "r18", "lolita25d", "anime", "galgame", "custom"]);
      assert.equal(await page.locator("#style").isVisible(), !enabled);
      assert.equal(await page.locator("#customArtists").isVisible(), false);
    });
  }
});

test("global style controls stay in the slot section and follow the saved selection", async () => {
  for (const width of [1440, 320]) {
    await withPanel({ width, wrapped: true }, async (page) => {
      const slotsCard = page.locator(".card").filter({ has: page.locator("#slotRows") });
      assert.equal(await slotsCard.locator("#styleFields").count(), 1);
      assert.equal(await slotsCard.locator("#negative").count(), 1);
      assert.equal(await page.locator(".config-section #negative").count(), 0);
      assert.equal(await page.locator(".app-main > section:last-child.usage-section").count(), 1);
      const negativeField = page.locator(".field").filter({ has: page.locator("#negative") });
      assert.equal(await page.evaluate(() => {
        const style = document.querySelector("#styleFields");
        const negative = document.querySelector("#negative");
        const grimoire = document.querySelector("#grimQuery");
        return Boolean(style.compareDocumentPosition(negative) & Node.DOCUMENT_POSITION_FOLLOWING) &&
          Boolean(negative.compareDocumentPosition(grimoire) & Node.DOCUMENT_POSITION_FOLLOWING);
      }), true);
      const active = page.getByRole("button", { name: "全局生效", exact: true });
      const inactive = page.getByRole("button", { name: "全局启用", exact: true });
      assert.equal(await active.count(), 1);
      assert.equal(await active.getAttribute("aria-pressed"), "true");
      assert.equal(
        await active.evaluate((button) => getComputedStyle(button).backgroundColor),
        "rgb(20, 208, 208)"
      );
      await inactive.click();
      await page.waitForFunction(() => document.querySelector(
        "#slotRows .slot-row:nth-child(2) button[aria-pressed='true']"
      ));
      assert.equal(await active.count(), 1);
      assert.equal(await page.locator("#styleFields").isVisible(), false);
      await page.locator("#slotRows .slot-row:nth-child(2) input").last().fill("artist:edited");
      await page.locator("#negative").fill("bad anatomy, lowres");
      const body = await generate(page);
      assert.equal(body.style, "custom");
      assert.equal(body.custom_artists, "artist:edited");
      assert.equal(body.model, "nai-diffusion-5-full");
      assert.equal(body.negative, "bad anatomy, lowres");
      const slotsBounds = await page.locator(".style-section").boundingBox();
      const usageBounds = await page.locator(".usage-section").boundingBox();
      assert(usageBounds.y >= slotsBounds.y + slotsBounds.height);
      for (const button of await page.locator(".btn-slot-apply").all()) {
        const bounds = await button.boundingBox();
        assert(bounds.x >= 0 && bounds.x + bounds.width <= width, JSON.stringify(bounds));
      }
      if (process.env.PANEL_SCREENSHOT_DIR) {
        await page.locator(".app-header").evaluate((header) => { header.style.visibility = "hidden"; });
        await negativeField.screenshot({
          path: path.join(process.env.PANEL_SCREENSHOT_DIR, `negative-${width}.png`),
        });
        await slotsCard.screenshot({
          path: path.join(process.env.PANEL_SCREENSHOT_DIR, `slots-${width}.png`),
        });
      }
    });
  }
});

test("OpenAI mode keeps the direct-only trial disabled", async () => {
  await withPanel({ cache: { callFormat: "openai" } }, async (page) => {
    assert.equal(await page.locator("#trialBtn").isDisabled(), true);
    assert.equal(await page.locator("#trialStatus").isVisible(), false);
  });
});

test("gallery supports pagination, originals and downloads without changing the form", async () => {
  for (const width of [1440, 320]) {
    await withPanel({ width, galleryCount: 13, wrapped: true }, async (page) => {
      await page.locator("#naiPrompt").fill("keep this prompt");
      if (width === 1440 && process.env.PANEL_SCREENSHOT_DIR) {
        await page.screenshot({
          path: path.join(process.env.PANEL_SCREENSHOT_DIR, "panel.png"),
          fullPage: true,
        });
      }
      await page.locator("#galleryBtn").click();
      await page.waitForFunction(() => document.querySelectorAll(".gallery-item").length === 12);
      assert.equal(
        await page.locator(".gallery-item-prompt").first().textContent(),
        "1girl, gallery sample 1"
      );
      assert.equal(await page.locator("#galleryDialog").isVisible(), true);
      assert.equal(await page.locator("#galleryPageInfo").textContent(), "1 / 2");
      assert.equal(await page.locator("#galleryPrev").isDisabled(), true);
      await page.waitForFunction(() => document.querySelector(".gallery-image-btn img").naturalWidth > 0);
      for (const selector of ["#galleryBtn", "#galleryDialog", "#galleryClose", "#galleryNext"]) {
        const bounds = await page.locator(selector).boundingBox();
        assert(bounds.x >= 0 && bounds.x + bounds.width <= width, `${selector}: ${JSON.stringify(bounds)}`);
      }
      if (process.env.PANEL_SCREENSHOT_DIR) {
        await page.locator("#galleryDialog").screenshot({
          path: path.join(process.env.PANEL_SCREENSHOT_DIR, `gallery-${width}.png`),
        });
      }
      await page.locator(".gallery-image-btn").first().click();
      await page.locator("#galleryViewer:not(.hidden)").waitFor();
      for (const selector of [
        "#galleryBack", "#galleryDownload", "#galleryRefresh", "#galleryClose",
        "#galleryImagePrev", "#galleryImageNext",
      ]) {
        const bounds = await page.locator(selector).boundingBox();
        assert(bounds.x >= 0 && bounds.x + bounds.width <= width,
          `${selector}: ${JSON.stringify(bounds)}`);
      }
      if (process.env.PANEL_SCREENSHOT_DIR) {
        await page.locator("#galleryDialog").screenshot({
          path: path.join(process.env.PANEL_SCREENSHOT_DIR, `gallery-detail-${width}.png`),
        });
      }
      assert.equal(
        await page.locator("#galleryPrompt").textContent(),
        "1girl, full gallery prompt 1"
      );
      assert.match(await page.locator("#galleryMetaList").textContent(), /nai-diffusion-5-full/);
      assert.equal(await page.locator("#galleryImageInfo").textContent(), "1 / 13");
      assert.equal(await page.locator("#galleryImagePrev").isDisabled(), true);
      assert.equal(await page.locator("#galleryImageNext").isDisabled(), false);
      assert.match(await page.locator("#galleryHint").textContent(), /832 × 1216/);
      await page.locator("#galleryCopyPrompt").click();
      await page.waitForFunction(
        () => window.__copiedPrompt === "1girl, full gallery prompt 1"
      );
      assert.equal(await page.locator("#galleryDownload").getAttribute("download"), "nai_1.png");
      const downloadPromise = page.waitForEvent("download");
      await page.locator("#galleryDownload").click();
      const download = await downloadPromise;
      assert.equal(download.suggestedFilename(), "nai_1.png");
      await download.delete();
      await page.keyboard.press("ArrowRight");
      await page.waitForFunction(
        () => document.querySelector("#galleryImageInfo").textContent === "2 / 13"
      );
      assert.equal(await page.locator("#galleryOriginal").getAttribute("alt"), "nai_2.png");
      await page.keyboard.press("ArrowLeft");
      await page.waitForFunction(
        () => document.querySelector("#galleryImageInfo").textContent === "1 / 13"
      );
      await page.locator("#galleryBack").click();
      await page.waitForFunction(() => document.querySelectorAll(".gallery-item").length === 12);
      assert.equal(
        await page.locator('[data-gallery-name="nai_1.png"]').evaluate((button) =>
          button === document.activeElement
        ),
        true
      );
      await page.locator("#galleryNext").click();
      await page.waitForFunction(() => document.querySelectorAll(".gallery-item").length === 1);
      assert.equal(await page.locator("#galleryPageInfo").textContent(), "2 / 2");
      assert.equal(await page.locator("#galleryNext").isDisabled(), true);
      await page.locator(".gallery-image-btn").click();
      await page.waitForFunction(
        () => document.querySelector("#galleryImageInfo").textContent === "13 / 13"
      );
      await page.keyboard.press("ArrowLeft");
      await page.waitForFunction(
        () => document.querySelector("#galleryImageInfo").textContent === "12 / 13"
      );
      assert.equal(await page.locator("#galleryOriginal").getAttribute("alt"), "nai_12.png");
      await page.locator("#galleryImageNext").click();
      await page.waitForFunction(
        () => document.querySelector("#galleryImageInfo").textContent === "13 / 13"
      );
      await page.locator("#galleryRefresh").click();
      await page.waitForFunction(() => document.querySelectorAll(".gallery-item").length === 1);
      await page.keyboard.press("Escape");
      assert.equal(await page.locator("#galleryDialog").isVisible(), false);
      assert.equal(await page.locator("#naiPrompt").inputValue(), "keep this prompt");
      assert.equal(await page.locator("#galleryBtn").evaluate((button) => button === document.activeElement), true);
    });
  }
});

test("gallery supports search, applying prompt/parameters and deleting images", async () => {
  await withPanel({ galleryCount: 3, wrapped: true }, async (page) => {
    await page.locator("#galleryBtn").click();
    await page.waitForFunction(() => document.querySelectorAll(".gallery-item").length === 3);
    await page.locator("#gallerySearch").fill("sample 2");
    await page.waitForFunction(() => document.querySelectorAll(".gallery-item").length === 1);
    assert.equal(
      await page.locator(".gallery-item-prompt").first().textContent(),
      "1girl, gallery sample 2"
    );
    await page.locator("#gallerySearchClear").click();
    await page.waitForFunction(() => document.querySelectorAll(".gallery-item").length === 3);

    await page.locator(".gallery-image-btn").first().click();
    await page.locator("#galleryViewer:not(.hidden)").waitFor();
    page.once("dialog", (dialog) => dialog.accept());
    await page.locator("#galleryDelete").click();
    await page.waitForFunction(() => document.querySelectorAll(".gallery-item").length === 2);

    await page.locator(".gallery-image-btn").first().click();
    await page.locator("#galleryViewer:not(.hidden)").waitFor();
    await page.locator("#galleryApplyPrompt").click();
    assert.equal(await page.locator("#galleryDialog").isVisible(), false);
    assert.equal(await page.locator("#naiPrompt").inputValue(), "1girl, full gallery prompt 2");
    assert.equal(await page.locator("#model").inputValue(), "nai-diffusion-5-full");
  });
});

test("empty gallery reports history state and retries failed loads", async () => {
  await withPanel({ historyEnabled: false, galleryFailures: 1 }, async (page) => {
    await page.locator("#galleryBtn").click();
    await page.waitForFunction(() => document.querySelector("#galleryStatus").textContent.includes("读取画廊失败"));
    await page.locator("#galleryRefresh").click();
    await page.waitForFunction(() => document.querySelector("#galleryStatus").textContent === "还没有已保存的图片");
    assert.match(await page.locator("#galleryHint").textContent(), /图片历史保存已关闭/);
    assert.equal(await page.locator("#galleryPrev").isDisabled(), true);
    assert.equal(await page.locator("#galleryNext").isDisabled(), true);
    await page.locator("#galleryClose").click();
    assert.equal(await page.locator("#galleryDialog").isVisible(), false);
  });
});

test("missing original keeps the gallery usable", async () => {
  await withPanel({ galleryCount: 1, missingGalleryImage: true }, async (page) => {
    await page.locator("#galleryBtn").click();
    await page.locator(".gallery-image-btn").click();
    await page.waitForFunction(() => document.querySelector("#galleryStatus").textContent.includes("图片已不存在"));
    assert.equal(await page.locator("#galleryDownload").isVisible(), false);
    await page.locator("#galleryBack").click();
    await page.locator(".gallery-image-btn").waitFor();
    await page.locator("#galleryClose").click();
  });
});

test("normal generation restores trial availability", async () => {
  await withPanel({}, async (page) => {
    assert.equal(await page.locator("#trialBtn").isDisabled(), false);
    await generate(page);
    assert.equal(await page.locator("#trialBtn").isDisabled(), false);
  });
});

test("generation shows only the loading state and keeps trial locked", async () => {
  await withPanel({ holdGeneration: true }, async (page) => {
    await page.locator("#naiPrompt").fill("1girl");
    await page.locator("#generateBtn").click();
    await page.waitForFunction(() => typeof window.__panelTest.releaseGeneration === "function");
    assert.equal(await page.locator("#loadingState").isVisible(), true);
    assert.equal(await page.locator("#emptyState").isVisible(), false);
    await page.locator('[data-format="openai"]').click();
    await page.locator('[data-format="direct"]').click();
    assert.equal(await page.locator("#trialBtn").isDisabled(), true);
    await page.evaluate(() => window.__panelTest.releaseGeneration());
    await page.waitForFunction(() => !document.querySelector("#generateBtn").disabled);
  });
});

test("retrying a failed trial never calls paid generation", async () => {
  await withPanel({ failTrial: true }, async (page) => {
    await page.locator("#naiPrompt").fill("1girl");
    await page.locator("#trialBtn").click();
    await page.locator("#errorState:not(.hidden)").waitFor();
    await page.waitForFunction(() => !document.querySelector("#generateBtn").disabled);
    await page.locator("#retryBtn").click();
    await page.waitForFunction(() => window.__panelTest.calls.filter(
      (call) => /\/(generate|trial_generate)$/.test(call.endpoint)
    ).length === 2);
    const endpoints = await page.evaluate(() => window.__panelTest.calls.filter(
      (call) => /\/(generate|trial_generate)$/.test(call.endpoint)
    ).map((call) => call.endpoint));
    assert.deepEqual(endpoints, ["test_panel/trial_generate", "test_panel/trial_generate"]);
  });
});

test("changing channel detaches an incompatible slot model", async () => {
  await withPanel({}, async (page) => {
    await page.locator('[data-format="openai"]').click();
    await page.locator("#slotRows .slot-row select").first().selectOption({
      label: "nai-diffusion-5-full",
    });
    await page.locator("#model").selectOption("nai-diffusion-5-curated");
    await page.locator('[data-format="direct"]').click();
    assert.equal(await page.locator("#style").inputValue(), "custom");
  });
});

test("switching to OpenAI shows the loaded service configuration", async () => {
  await withPanel({}, async (page) => {
    await page.locator('[data-format="openai"]').click();
    assert.match(await page.locator("#openaiConfigStatusText").textContent(), /http:\/\/images\.example\.invalid/);
  });
});

test("grimoire search follows the panel model", async () => {
  await withPanel({}, async (page) => {
    await page.locator("#model").selectOption("nai-diffusion-5-full");
    await page.locator("#grimQuery").fill("style");
    await page.locator("#btnGrimSearch").click();
    await page.waitForFunction(() => window.__panelTest.calls.some(
      (call) => call.endpoint.endsWith("/grimoire_search")
    ));
    const body = await page.evaluate(() => window.__panelTest.calls.find(
      (call) => call.endpoint.endsWith("/grimoire_search")
    ).body);
    assert.equal(body.model, "nai-diffusion-5-full");
  });
});

test("usage breakdown names render as plain text", async () => {
  const name = '<img src="invalid" onerror="window.__unsafeName = true">';
  await withPanel({
    usage: {
      enabled: true,
      series: [],
      overall: { cost: 1 },
      breakdowns: { "7": { model: [{ name, cost: 1 }] } },
    },
  }, async (page) => {
    await page.locator("#usageBreakdown .usage-bd-item").waitFor();
    assert.equal(await page.locator("#usageBreakdown img").count(), 0);
    assert.match(await page.locator("#usageBreakdown").textContent(), /<img/);
    assert.equal(await page.evaluate(() => window.__unsafeName), undefined);
  });
});

test("character and reference controls remain inside a 320px panel", async () => {
  await withPanel({ width: 320 }, async (page) => {
    await page.locator('[data-format="openai"]').click();
    await page.locator("#charAdd").click();
    await page.locator("#refMode").selectOption("director");
    await page.locator("#refFile").setInputFiles({
      name: "reference.png", mimeType: "image/png", buffer: png,
    });
    await page.locator("#refGrid .ref-item").waitFor();
    for (const selector of [".char-prompt", ".char-coord", ".char-remove", ".ref-caption-select", ".ref-item-remove"]) {
      for (const element of await page.locator(selector).all()) {
        const bounds = await element.boundingBox();
        const minWidth = selector === ".ref-caption-select" ? 160
          : selector === ".char-prompt" ? 120 : 20;
        assert(bounds.width >= minWidth && bounds.x >= 0 && bounds.x + bounds.width <= 320, `${selector}: ${JSON.stringify(bounds)}`);
      }
    }
    if (process.env.PANEL_SCREENSHOT_DIR) {
      await page.locator(".app-header").evaluate((header) => { header.style.visibility = "hidden"; });
      await page.locator("#charListWrap").screenshot({
        path: path.join(process.env.PANEL_SCREENSHOT_DIR, "characters-mobile.png"),
      });
      await page.locator("#refUploadWrap").screenshot({
        path: path.join(process.env.PANEL_SCREENSHOT_DIR, "references-mobile.png"),
      });
    }
  });
});
