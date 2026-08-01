/**
 * End-to-end: the thinnest test that proves the whole stack works together.
 *
 * It loads a real recording in the real UI, scrubs the timeline to two different instants, and
 * asserts the rendered variables change. That single assertion exercises everything: the FastAPI
 * server, reconstruction at an instant, the DTO wire shape, the generated client, and the "UI is
 * a pure function of currentSeq" wiring. It is the test that catches "demo broken at 2am".
 *
 * The UI opens the recording and lands on the LAST instant, where the program has already exited
 * and no frames are live -- so the test scrubs *into* the recording (to instants that have live
 * frames) rather than probing a specific variable at the empty landing state.
 *
 * DOM contract this test depends on (stable test ids, not CSS the design can change):
 *   [data-testid="timeline"]    the scrubber track -- clicked across its width to scrub
 *   [data-testid="var-row"]     a variable row in the variables panel; its text is the value
 *
 * playwright.config.ts (webServer) starts `chronotrace serve` over a ./recordings fixture; the
 * CI `e2e` job records that fixture before running. See .github/workflows/ci.yml.
 */

import { expect, test } from "@playwright/test";

test("scrubbing the timeline re-renders program state", async ({ page }) => {
  await page.goto("/");

  const timeline = page.locator('[data-testid="timeline"]');
  await expect(timeline).toBeVisible();
  const box = await timeline.boundingBox();
  if (!box) throw new Error("timeline not laid out");

  const rows = page.locator('[data-testid="var-row"]');
  const scrubTo = (fraction: number) =>
    page.mouse.click(box.x + box.width * fraction, box.y + box.height / 2);
  const locals = async () => (await rows.allInnerTexts()).join("|");

  // scrub to an instant well inside the recording, where frames are live, and read the locals
  await scrubTo(0.4);
  await expect(rows.first()).toBeVisible();
  const early = await locals();
  expect(early).not.toEqual("");

  // scrub to a different instant; poll (never a fixed sleep -- that races reconstruction) until
  // the variables panel shows different state. If it never changes, the pure-function wiring is
  // broken and this times out.
  await scrubTo(0.75);
  await expect.poll(locals).not.toEqual(early);
});
