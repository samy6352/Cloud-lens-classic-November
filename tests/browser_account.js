async (page) => {
  const context = await page.context().browser().newContext({ viewport: { width: 1440, height: 900 } });
  const test = await context.newPage();
  test.setDefaultTimeout(5000);
  let passed = 0;
  const assert = (value, message) => { if (!value) throw new Error(message); passed++; };
  try {
    const responses = {};
    for (const [key, path] of [["html", "/"], ["css", "/assets/app.css"], ["source", "/assets/app.js"]]) {
      const response = await page.request.get("http://localhost:8100" + path);
      if (!response.ok()) throw new Error(`Local source unavailable: ${path}`);
      responses[key] = await response.text();
    }
    const html = responses.html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
      .replace(/<link\b[^>]*>/gi, "").replace("</head>", `<style>${responses.css}</style></head>`);
    const illustration = await page.request.get("http://localhost:8100/assets/account-illustration.png");
    assert(illustration.ok(), "Account illustration is served locally");
    const image = await illustration.body();
    // The shell stamps asset URLs with a content hash (/assets/v/<hash>/…), so match the name.
    await context.route("**/*", route => /\/account-illustration\.png$/.test(new URL(route.request().url()).pathname)
      ? route.fulfill({ contentType: "image/png", body: image })
      : route.fulfill({ contentType: "text/html", body: html }));
    await test.goto("http://account-fixture.invalid/");
    await test.evaluate(source => {
      const $ = id => document.getElementById(id);
      const load = source.match(/^async function loadIdentity\(\) \{[\s\S]*?^\}/m)?.[0];
      const place = source.match(/^function placeAccount\(onboarding\) \{[\s\S]*?^\}/m)?.[0];
      const wire = source.match(/^function wireAccount\(\) \{[\s\S]*?^\}/m)?.[0];
      const signout = source.match(/^  \$\("signout"\)\.onclick = async \(\) => \{[\s\S]*?^  \};/m)?.[0];
      if (!load || !place || !wire || !signout) throw new Error("Account implementation not found");
      window.fixture = { required: true, authenticated: true,
        user: { name: "Ravi Verma", email: "ravi@example.invalid", admin: true, source: "file" } };
      window.paintAccount = new Function("$", "whoAmI", "toLogin", "describeScope", "isAllSelected", "banner",
        `let identity; ${load}; return loadIdentity;`)($, async () => window.fixture, () => {}, () => {}, () => true, () => {});
      window.placeAccount = new Function("$", `${place}; return placeAccount;`)($);
      new Function("$", `${wire}; wireAccount();`)($);
      window.logoutCalls = []; window.loginTarget = "";
      // cacheWarmer: the shell stops its warmer on sign-out; there is none in this fixture.
      new Function("$", "fetch", "location", "cacheWarmer", signout)($, async (url, options) => {
        window.logoutCalls.push({ url, method: options.method });
      }, { replace: path => { window.loginTarget = path; } }, null);
      $("tabs").innerHTML = ["Overview", "Billing", "Savings", "Monitor", "Governance", "Reports"]
        .map(name => `<button class="rail-item" role="tab">${name}</button>`).join("");
      $("tabBody").innerHTML = "<h2>Fixture data</h2>";
      $("side").remove();
      window.accountNode = $("who");
    }, responses.source);
    await test.evaluate(() => window.paintAccount());
    assert(await test.locator("#whoName").textContent() === "Ravi Verma", "Display name is shown");
    assert(await test.locator("#whoAvatar").textContent() === "RV", "Initials derive from account name");
    assert(await test.locator(".account-status").textContent() === "Signed in", "Status shown in the footer");
    assert(await test.locator("#signout").isHidden(), "Account actions are disclosed on demand");
    await test.locator("#accountTrigger").click();
    assert(await test.locator("#whoEmail").textContent() === "ravi@example.invalid", "Email retained");
    assert(await test.locator("#whoAdmin").isVisible(), "Admin badge retained");
    assert(await test.locator(".bar #who").count() === 0, "Account removed from top header");
    for (const [width, mini] of [[1440, false], [1440, true], [390, true]]) {
      await test.setViewportSize({ width, height: 900 });
      await test.evaluate(mini => {
        document.documentElement.classList.toggle("rail-mini", mini);
        document.body.classList.toggle("rail-mini", mini);
      }, mini);
      const result = await test.evaluate(() => {
        const account = document.getElementById("who").getBoundingClientRect();
        const nav = document.getElementById("tabs").getBoundingClientRect();
        return { bottom: Math.abs(account.bottom - innerHeight), left: account.left,
          right: account.right, navBottom: nav.bottom, accountTop: account.top,
          overflow: document.documentElement.scrollWidth > innerWidth + 1 };
      });
      assert(result.bottom < 3 && result.left < 16, `Account is bottom-left at ${width}, mini=${mini}`);
      assert(!result.overflow && result.navBottom <= result.accountTop + 1, "Footer does not cover navigation");
      assert(await test.getByRole("button", { name: "Sign out", exact: true }).isVisible(), "Sign out remains reachable");
      assert(await test.locator(".account-popover").evaluate(el => {
        const r = el.getBoundingClientRect();
        return r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight;
      }), "Account actions fit the viewport");
      await test.locator("#signout").focus();
      await test.keyboard.press("Escape");
      assert(await test.locator("#signout").isHidden()
        && await test.locator("#accountTrigger").evaluate(el => el === document.activeElement),
      "Escape dismisses account options and restores focus");
      await test.keyboard.press("Enter");
      assert(await test.locator("#signout").isVisible(), "Keyboard opens account options");
    }
    await test.locator("#tabBody").click();
    assert(await test.locator("#signout").isHidden(), "Clicking outside dismisses account options");
    await test.setViewportSize({ width: 1440, height: 900 });
    await test.evaluate(() => {
      document.documentElement.classList.remove("rail-mini");
      document.body.classList.remove("rail-mini");
      document.getElementById("tabs").innerHTML = '<button class="rail-item">Refreshed navigation</button>';
    });
    assert(await test.evaluate(() => window.accountNode === document.getElementById("who")), "Rail rerenders preserve the account node");
    await test.evaluate(() => {
      document.getElementById("shell").hidden = true;
      document.getElementById("firstRun").hidden = false;
      document.getElementById("firstRun").textContent = "First-run setup requires access.";
      window.placeAccount(true);
    });
    await test.locator("#accountTrigger").click();
    assert(await test.locator("#onboardingAccount #signout").isVisible(), "Onboarding still offers sign out");
    await test.evaluate(() => {
      document.getElementById("shell").hidden = false;
      document.getElementById("firstRun").hidden = true;
      window.placeAccount(false);
      window.fixture.user = { name: "<img src=x>", email: "", source: "file", admin: false };
      return window.paintAccount();
    });
    assert(await test.locator("#sidebarAccount #who").isVisible(), "Footer returns to sidebar after onboarding");
    assert(await test.locator("#whoName").textContent() === "<img src=x>"
      && await test.locator("#who img").count() === 1
      && await test.locator(".account-illustration").evaluate(el => el.naturalWidth > 0),
    "Account text is not interpreted as HTML and the decorative illustration loads");
    assert(await test.locator("#whoAdmin").isHidden(), "Non-admin has no admin badge");
    await test.evaluate(() => localStorage.setItem("costScope", '["old-subscription"]'));
    await test.getByRole("button", { name: "Sign out", exact: true }).focus();
    await test.keyboard.press("Enter");
    await test.waitForFunction(() => window.loginTarget === "/login");
    assert(await test.evaluate(() => window.logoutCalls.length === 1
      && window.logoutCalls[0].url === "/api/auth/logout" && window.logoutCalls[0].method === "POST"),
    "Existing logout handler is retained");
    assert(await test.evaluate(() => localStorage.getItem("costScope") === null), "Logout clears remembered scope");
    assert(await test.locator("#signout").count() === 1 && await test.locator("#who").count() === 1,
      "Only one account and sign-out control exist");
    await test.evaluate(() => {
      document.getElementById("who").hidden = true;
      window.fixture = { required: false, authenticated: true };
      return window.paintAccount();
    });
    assert(await test.locator("#sidebarAccount").isHidden(), "Auth-disabled mode has no empty account footer");
    return { passed, logout: "mocked only; no live session signed out" };
  } finally {
    await context.close();
  }
}
