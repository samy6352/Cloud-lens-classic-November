/** Stop on the application's terminal error state, not just on a successful boot. */
export async function waitForDashboard(page, timeout = 120000) {
  await page.waitForFunction(() => ["true", "error"].includes(document.documentElement.dataset.dashboardReady),
    null, { timeout });
  const state = await page.evaluate(() => ({
    ready: document.documentElement.dataset.dashboardReady,
    message: document.querySelector("#tabBody .banner.err")?.textContent.trim(),
  }));
  if (state.ready !== "true") {
    throw new Error(`Dashboard startup failed: ${state.message || "The view entered an error state."}`);
  }
}
