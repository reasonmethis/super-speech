import assert from "node:assert/strict";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";

export async function testComposerFeatures({ page, runEngine, status, waitFor, inbox }) {
  runEngine("pause");
  for (let number = 0; number < 55; number++) {
    const result = await page.evaluate((text) => window.superSpeech.mutateTimeline({
      type: "enqueue", text, voice: "af_heart", source: "History test",
    }), `Older speech ${number}`);
    assert.equal(result.outcome, "committed");
  }
  runEngine("speak", "A reply-enabled Speechicle", "--voice", "af_heart", "--source", "Test agent", "--inbox", inbox);
  runEngine("speak", "Another from the same inbox", "--voice", "af_heart", "--source", "Test agent", "--inbox", inbox);
  const blockedInbox = `${inbox}-blocked.jsonl`;
  mkdirSync(blockedInbox);
  runEngine("speak", "A reply target with a broken destination", "--voice", "af_heart", "--source", "Unavailable agent", "--inbox", blockedInbox);
  await page.evaluate(() => window.superSpeech.mutateTimeline({ type: "clear" }));
  await waitFor(async () => await page.locator("body").getAttribute("data-state") === "idle", "Composer fixture did not clear");

  assert.equal(await page.locator(".speechicle-item.is-history").count(), 50);
  const firstPageIds = status().history.map(({ id }) => id);
  const more = page.locator(".load-history-button");
  await more.click();
  await waitFor(async () => await page.locator(".speechicle-item.is-history").count() === status().history_count, "Older History did not load");
  assert.deepEqual(status().history.slice(0, 50).map(({ id }) => id), firstPageIds);
  assert(!await more.isVisible(), "Load more must disappear at the end");
  await page.waitForTimeout(900);
  assert.equal(await page.locator(".speechicle-item.is-history").count(), status().history_count, "Polling dropped loaded History");

  const reply = page.locator(".speechicle-reply").first();
  await reply.click();
  assert(await page.locator("#inbox-reply-dialog").isVisible());
  await page.locator("#inbox-reply-cancel").click();
  assert.equal(await page.locator('.speechicle-item[data-inbox=""] .speechicle-reply').count(), 0);

  await page.locator("#settings-button").click();
  await page.locator("#default-voice").click();
  const menu = page.locator("#voice-menu");
  await page.keyboard.press("Escape");
  assert(await page.locator("#settings-panel").isVisible(), "Escape from a picker must preserve Settings");
  await page.locator("#default-voice").click();
  assert(await menu.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return rect.left >= 0 && rect.top >= 0 && rect.bottom <= innerHeight &&
      element.contains(document.elementFromPoint(rect.left + 20, rect.top + 20));
  }), "Settings voice picker must be visible above its popover");
  await menu.locator('[data-voice="piper_alba"]').click();
  await page.reload();
  await waitFor(async () => await page.locator("body").getAttribute("data-state") === "idle", "Reload did not settle");
  await page.locator("#current-text").click();
  assert(await page.locator("#composer-actions").isVisible(), "An empty composer needs controls");
  assert.equal(await page.locator("#composer-voice").textContent(), "Alba");
  assert(!await page.locator("#composer-submit").isEnabled());
  await page.locator("#composer-voice").click();
  await page.keyboard.press("Escape");
  assert(await page.locator("#speech-composer").isVisible(), "Escape from a picker must preserve the composer");
  await page.locator("#composer-inbox").click();
  assert.equal(await menu.getByRole("option", { name: "Test agent", exact: true }).count(), 1, "One inbox must have one destination");
  await menu.getByRole("option", { name: "Unavailable agent", exact: true }).click();
  await page.locator("#composer-text").fill("Keep this message if sending fails.");
  await page.locator("#composer-submit").click();
  await waitFor(async () => (await page.locator("#composer-status").textContent()).startsWith("Could not send"), "Failed replies need a visible error");
  assert.equal(await page.locator("#composer-text").inputValue(), "Keep this message if sending fails.");
  await page.locator("#composer-inbox").click();
  await menu.getByRole("option", { name: "Test agent", exact: true }).click();
  assert.equal(await page.locator("#composer-submit").textContent(), "Send reply");
  assert(!await page.locator("#composer-voice").isVisible(), "An inbox message has no speech voice");
  const text = "A reply sent from the main composer.";
  await page.locator("#composer-text").fill(text);
  const revision = status().timeline_revision;
  await page.locator("#composer-submit").click();
  await waitFor(async () => await page.locator("#composer-status").textContent() === "Reply sent", "Composer reply did not finish");
  const messages = readFileSync(inbox, "utf8").trim().split("\n").map((line) => JSON.parse(line));
  assert.equal(messages.at(-1).text, text);
  assert.equal(status().timeline_revision, revision, "Sending an inbox message must not enqueue speech");
  assert.equal(await page.locator("#composer-text").inputValue(), "");

  if (process.env.SUPER_SPEECH_SCREENSHOT) {
    const image = path.parse(process.env.SUPER_SPEECH_SCREENSHOT);
    for (const theme of ["dark", "light"]) {
      await page.evaluate((theme) => document.body.dataset.theme = theme, theme);
      await page.screenshot({ path: path.join(image.dir, `${image.name}-reply-composer-${theme}${image.ext}`) });
    }
  }
  await page.locator("#composer-inbox").click();
  await menu.getByRole("option", { name: "Speak aloud", exact: true }).click();
  await page.locator("#composer-text").fill("Alba uses the same pause controls as every other voice.");
  await page.locator("#composer-submit").click();
  await waitFor(() => status().current?.voice === "piper_alba" && status().current?.piece > 0, "The bundled Alba voice did not generate audio", 60_000);
  const pause = await page.evaluate(() => window.superSpeech.setPaused(true));
  assert.equal(pause.state, "paused");
  const clear = await page.evaluate(() => window.superSpeech.mutateTimeline({ type: "clear" }));
  assert.equal(clear.outcome, "committed");
  assert.equal(clear.snapshot.state, "idle");
  assert.equal(clear.snapshot.history.length, clear.snapshot.history_count, "A mutation dropped loaded History");
}
