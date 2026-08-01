/**
 * End-to-end: the thinnest test that proves the whole stack works together.
 *
 * It loads a real recording in the real UI, scrubs the timeline, and asserts a variable's
 * *rendered* value changes. That single assertion exercises everything: the FastAPI server,
 * reconstruction at an instant, the DTO wire shape, the generated client, and the "UI is a
 * pure function of currentSeq" wiring. It is the test that catches "demo broken at 2am".
 *
 * ------------------------------------------------------------------------------------------
 * STATUS: scaffolding (day 39). The frontend is built in Antigravity and is not in this repo
 * yet, so this spec does not run until it lands. It is written against a small DOM contract
 * the frontend agrees to expose (below); wiring it up in the Antigravity project means adding
 * @playwright/test, a playwright.config.ts whose `webServer` starts `chronotrace serve` over a
 * buggy_pipeline recording, and these data-testids. The CI job (.github/workflows/ci.yml, the
 * `e2e` job) is guarded on the frontend existing, exactly like the `frontend` job.
 * ------------------------------------------------------------------------------------------
 *
 * The DOM contract this test depends on (stable test ids, not CSS the design can change):
 *   [data-testid="timeline"]                      the scrubber track — draggable across its width
 *   [data-testid="var-row"][data-name="<name>"]   a variable row; its text is the rendered value
 *
 * The recording under test is examples/buggy_pipeline.py: three regional totals that are all
 * identical because a dict is aliased. `total` changes as you move through time, which is
 * exactly what makes it a good probe for "did scrubbing re-render state?".
 */

import { expect, test } from "@playwright/test";

test("scrubbing the timeline changes a variable's rendered value", async ({ page }) => {
  await page.goto("/"); // baseURL comes from playwright.config.ts (the serve origin)

  // The variable panel resolves the current instant's locals.
  const total = page.locator('[data-testid="var-row"][data-name="total"]');
  await expect(total).toBeVisible();
  const atEnd = (await total.textContent())?.trim() ?? "";
  expect(atEnd).not.toEqual(""); // it rendered a real value, not a blank

  // Scrub to the very start by dragging the playhead to the left edge of the timeline.
  const timeline = page.locator('[data-testid="timeline"]');
  const box = await timeline.boundingBox();
  if (!box) throw new Error("timeline not laid out");
  await timeline.hover({ position: { x: box.width / 2, y: box.height / 2 } });
  await page.mouse.down();
  await page.mouse.move(box.x + 2, box.y + box.height / 2, { steps: 20 }); // drag to seq ~0
  await page.mouse.up();

  // The whole point: the same variable now shows a *different* value, because every panel is a
  // pure function of currentSeq. Assert on the rendered value (auto-retried), never a timeout —
  // waiting on a fixed sleep is how this test would flake by racing reconstruction.
  await expect(total).not.toHaveText(atEnd);
});
