// Records one continuous journey through the live Lachesis console for the
// submission video: the cockpit, one live demo run on the public lane with
// every pause approved, the run ledger, the policy table, the measurements
// and the architecture. Nothing on the page is staged: the run is a real
// POST /try against the deployed stack, and every score on screen is the one
// Decider returned. The only additions are a short ring drawn around a button
// just before it is clicked, because the recording shows no mouse pointer,
// and hiding the two links to the repository's code host (header and footer),
// because the video must not show third-party marks.
//
// It writes capture/production.webm, capture/markers.json (seconds from the
// start of the recording for each moment the video cuts on, plus the text of
// every decision card) and one screenshot per marker.
import { chromium } from "@playwright/test";
import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";

const SITE = process.env.LACHESIS_SITE || "https://d2nm49ao7e8z4z.cloudfront.net";
const EXPECT_BUILD = process.env.LACHESIS_EXPECT_BUILD || "";
const out = path.resolve(process.env.CAPTURE_DIR || "capture");
mkdirSync(path.join(out, "shots"), { recursive: true });

const browser = await chromium.launch({ args: ["--force-device-scale-factor=1"] });
const context = await browser.newContext({
  viewport: { width: 1920, height: 1080 },
  deviceScaleFactor: 1,
  reducedMotion: "no-preference",
  recordVideo: { dir: path.join(out, "raw"), size: { width: 1920, height: 1080 } },
});
await context.addInitScript(() => {
  const hide = () => {
    const s = document.createElement("style");
    s.textContent = 'a[href*="github.com"] { visibility: hidden !important; }';
    document.head.appendChild(s);
  };
  if (document.head) hide(); else document.addEventListener("DOMContentLoaded", hide);
});
const page = await context.newPage();
const t0 = Date.now();
const markers = [];
const cards = [];
const errors = [];
page.on("pageerror", (e) => errors.push(`page:${e.name}:${String(e.message).slice(0, 120)}`));
page.on("console", (m) => { if (m.type() === "error") errors.push(`console:${m.text().slice(0, 160)}`); });

const now = () => (Date.now() - t0) / 1000;
async function mark(id, extra = {}) {
  const shot = path.join(out, "shots", `${String(markers.length).padStart(2, "0")}-${id}.png`);
  markers.push({ id, t: Number(now().toFixed(3)), ...extra });
  await page.screenshot({ path: shot });
  console.log(`marker ${id} at ${now().toFixed(2)} s`);
}
const hold = (s) => page.waitForTimeout(s * 1000);
async function glide(y) {
  await page.evaluate((top) => window.scrollTo({ top, behavior: "smooth" }), y);
  await hold(1.2);
}
async function ring(locator) {
  await locator.scrollIntoViewIfNeeded();
  await locator.evaluate((el) => {
    el.dataset.prevOutline = el.style.outline;
    el.style.outline = "4px solid #7ee787";
    el.style.outlineOffset = "4px";
  });
  await hold(0.9);
  await locator.evaluate((el) => { el.style.outline = el.dataset.prevOutline || ""; });
}
async function go(hash) {
  await page.goto(`${SITE}/${hash}`, { waitUntil: "networkidle", timeout: 60_000 });
  await hold(0.8);
}

try {
  // 1. Cockpit
  await go("#/");
  const build = await page.evaluate(() => (window.LACHESIS_CONFIG || window.CONFIG || {}).build || "");
  const footer = (await page.locator("#build").textContent().catch(() => "")) || "";
  if (EXPECT_BUILD && !footer.includes(EXPECT_BUILD) && build !== EXPECT_BUILD) {
    throw new Error(`live console is not build ${EXPECT_BUILD} (footer: ${footer.trim()})`);
  }
  await mark("cockpit", { footer: footer.trim() });
  await hold(6);
  await glide(420);
  await hold(4);
  await glide(0);

  // 2. The live run
  await go("#/try");
  await mark("try");
  await hold(5);
  const start = page.locator("#try-start");
  await ring(start);
  await start.click();
  await mark("start");
  let pauses = 0;
  for (;;) {
    // Only an enabled button on a card that is still waiting: once a card is
    // decided its buttons are disabled, so a stale card never matches.
    const approve = page.locator('.s4 button:has-text("Approve and run"):not([disabled])').first();
    const finished = page.locator("#steps .step-h", { hasText: "Finished" });
    // Only an outcome row means the run ended badly; a decided pause also
    // renders as li.stopped, so match the outcome headings, not the class.
    const stopped = page.locator("#steps li.stopped .step-h", { hasText: /Run failed|Expired|without running|Merge check/u });
    await Promise.race([
      approve.waitFor({ state: "visible", timeout: 180_000 }),
      finished.waitFor({ state: "visible", timeout: 180_000 }),
      stopped.waitFor({ state: "visible", timeout: 180_000 }),
    ]);
    if (await stopped.isVisible().catch(() => false)) {
      throw new Error(`the run stopped: ${(await stopped.textContent()).slice(0, 200)}`);
    }
    if (await finished.isVisible().catch(() => false)) break;
    const card = approve.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' s4 ')][1]");
    const text = ((await card.textContent().catch(() => "")) || "").replace(/\s+/g, " ").trim();
    if (/GPU is asleep|no Decider score|No Decider score/iu.test(text)) {
      throw new Error("the pause carries no Decider score: the GPU is not warm");
    }
    pauses += 1;
    await card.scrollIntoViewIfNeeded();
    await mark(`pause${pauses}`);
    cards.push({ pause: pauses, t: Number(now().toFixed(3)), text: text.slice(0, 1500) });
    await hold(pauses === 2 ? 9 : 7);
    await ring(approve);
    await approve.click();
    const yes = page.locator('button:has-text("Yes, approve and run"):not([disabled])').first();
    // The console asks "Yes, approve and run" only for the destructive classes.
    if (await yes.waitFor({ state: "visible", timeout: 8_000 }).then(() => true).catch(() => false)) {
      await hold(1.2);
      await ring(yes);
      await yes.click();
    }
    await mark(`approved${pauses}`);
    await approve.waitFor({ state: "hidden", timeout: 30_000 }).catch(() => {});
    if (pauses > 6) throw new Error("more pauses than the demo has");
  }
  await page.locator("#steps .step-h", { hasText: "Finished" }).scrollIntoViewIfNeeded();
  await mark("finished", { pauses });
  await hold(7);
  const ledger = page.locator("#run-ledger");
  await ledger.waitFor({ state: "visible", timeout: 30_000 }).catch(() => {});
  if (await ledger.isVisible().catch(() => false)) {
    await ledger.scrollIntoViewIfNeeded();
    await hold(1);
    await mark("ledger");
    await hold(9);
  }

  // 3. The policy table
  await go("#/policy");
  await mark("policy");
  await hold(7);
  await glide(500);
  await hold(6);

  // 4. The measurements
  await go("#/metrics");
  await mark("metrics");
  await hold(8);
  await glide(600);
  await hold(7);
  await glide(1200);
  await hold(6);

  // 5. The architecture
  await go("#/architecture");
  await mark("architecture");
  await hold(8);
  await glide(500);
  await hold(7);

  // 6. Close on the cockpit
  await go("#/");
  await mark("close");
  await hold(12);
} finally {
  const total = now();
  await context.close();
  await browser.close();
  const raw = await page.video().path();
  renameSync(raw, path.join(out, "production.webm"));
  writeFileSync(path.join(out, "markers.json"), JSON.stringify({ site: SITE, total, markers, cards, errors }, null, 2));
  console.log(`capture ${total.toFixed(1)} s, ${markers.length} markers, ${errors.length} page errors`);
}
