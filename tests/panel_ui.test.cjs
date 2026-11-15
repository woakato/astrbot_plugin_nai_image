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
      const state = {
        cache: settings.cache || {},
        calls: [],
        slots: [
          { id: 1, name: "Style 1", model: "nai-diffusion-4-5-full", artists: "artist:one" },
          { id: 2, name: "Style 2", model: "nai-diffusion-5-full", artists: "artist:two" },
        ],
        active: "1",
        usage: settings.usage || { enabled: false },
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
            novelai_available: true,
            novelai_service: "official",
            novelai_api_base_url: "http://native.example.invalid",
            novelai_api_model: "nai-diffusion-5-full",
            novelai_image_size: "832x1216",
            novelai_size_options: [
              { value: "832x1216", label: "Standard portrait" },
              { value: "512x768", label: "Small portrait" },
              { value: "1024x1024", label: "Standard square" },
            ],
            novelai_models: [
              "nai-diffusion-4-5-full", "nai-diffusion-5-full", "nai-diffusion-5-curated",
            ],
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

test("native NovelAI cache and provider switches preserve model and size", async () => {
  await withPanel({
    cache: {
      callFormat: "novelai",
      model: "nai-diffusion-5-curated",
      size: "512x768",
      style: "custom",
      customArtists: "artist:native",
    },
  }, async (page) => {
    assert.equal(await page.locator("#model").inputValue(), "nai-diffusion-5-curated");
    assert.equal(await page.locator("#size").inputValue(), "512x768");
    assert.equal(await page.locator("#trialBtn").isDisabled(), true);
    assert.match(await page.locator("#openaiConfigStatusText").textContent(), /native\.example/);
    await page.locator('[data-format="openai"]').click();
    await page.locator('[data-format="novelai"]').click();
    const body = await generate(page);
    assert.equal(body.call_format, "novelai");
    assert.equal(body.model, "nai-diffusion-5-curated");
    assert.equal(body.size, "512x768");
    assert.equal(body.custom_artists, "artist:native");
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
    });
  }
});

test("OpenAI mode keeps the direct-only trial disabled", async () => {
  await withPanel({ cache: { callFormat: "openai" } }, async (page) => {
    assert.equal(await page.locator("#trialBtn").isDisabled(), true);
    assert.equal(await page.locator("#trialStatus").isVisible(), false);
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
