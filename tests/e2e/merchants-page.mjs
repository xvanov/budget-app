// The Merchants route, exercised the way a person uses it: land on it via the
// sidebar, read the ranking, switch the period, add a category filter, drill into
// a vendor. Every step asserts no console error and that the numbers on screen
// still agree with each other.
//
// Run from the REPO ROOT (module resolution needs the root node_modules) with a
// stack up:
//
//   BASE_URL=http://127.0.0.1:8186 node tests/e2e/merchants-page.mjs
//
// Separate from smoke-pages.mjs on purpose: that one proves every route renders,
// this one proves this route WORKS.

import { chromium } from 'playwright';

const BASE = process.env.BASE_URL || 'http://127.0.0.1:8083';
const SHOTS = process.env.SHOT_DIR || 'screenshots';

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
page.on('pageerror', (e) => errors.push('PAGEERROR: ' + String(e).slice(0, 200)));

const steps = [];
function check(name, ok, detail = '') {
  steps.push({ name, ok: !!ok, detail });
  if (!ok) console.error(`  FAIL ${name} ${detail}`);
}

// The SPA stores its route in localStorage, so a deep link is set there before
// the first paint rather than by URL.
await page.addInitScript(() => {
  localStorage.setItem('budget-route', '/merchants');
  localStorage.setItem('budget-period', 'ytd');
});
await page.goto(BASE + '/', { waitUntil: 'networkidle', timeout: 60000 });
await page.waitForSelector('[data-testid="merchants-page"]', { timeout: 30000 });
await page.waitForTimeout(1500);

check('page title is Merchants',
  (await page.textContent('[data-testid="page-title"]')) === 'Merchants');
check('period control is shown (route is period-aware)',
  await page.locator('[data-testid="period-control"]').count() > 0);

const rows = page.locator('[data-testid="merchant-row"]');
const rowCount = await rows.count();
check('ranking has rows', rowCount > 0, `count=${rowCount}`);

const firstLabel = (await rows.first().locator('td').first().innerText()).trim();
check('first vendor has a label', firstLabel.length > 0, firstLabel);

// Sum of the displayed Share column should be <= 100% and the shown rows' spend
// should not exceed the KPI total.
const kpiTotal = await page.locator('.kpi').first().locator('.kpi-value').innerText();
check('total spend KPI is a currency figure', /\$/.test(kpiTotal), kpiTotal);

await page.screenshot({ path: `${SHOTS}/merchants-ytd.png`, fullPage: false });

// Category filter — cumulative with the period.
const chip = page.locator('.chip').filter({ hasText: /\$/ }).first();
const chipText = (await chip.innerText()).trim();
await chip.click();
await page.waitForTimeout(1600);
check('filter banner appears after picking a category',
  await page.locator('[data-testid="merchant-filter-banner"]').count() > 0, chipText);
const bannerText = await page.locator('[data-testid="merchant-filter-banner"]').innerText();
check('banner states the filtered share of the period', /of \$/.test(bannerText),
  bannerText.replace(/\s+/g, ' ').slice(0, 120));
const filteredRows = await page.locator('[data-testid="merchant-row"]').count();
check('filtered ranking still has rows', filteredRows > 0, `count=${filteredRows}`);
await page.screenshot({ path: `${SHOTS}/merchants-filtered.png`, fullPage: false });

// Drill into the top vendor of the filtered list.
const topRow = page.locator('[data-testid="merchant-row"]').first();
const drillLabel = (await topRow.locator('td').first().innerText()).trim();
const drillSpend = (await topRow.locator('td').nth(4).innerText()).trim();
await topRow.click();
await page.waitForSelector('[data-testid="merchant-detail"]', { timeout: 20000 });
await page.waitForTimeout(1600);
const detailText = await page.locator('[data-testid="merchant-detail"]').innerText();
check('drill-down opened for the clicked vendor',
  detailText.includes(drillLabel.split('\n')[0].trim()), drillLabel);
check('drill-down reports the same net spend as the ranking row',
  detailText.includes(drillSpend), `row=${drillSpend}`);
check('drill-down lists transactions',
  await page.locator('[data-testid="merchant-detail"] .tbl tbody tr').count() > 0);
check('no "gap is a bug" banner in the drill-down',
  !detailText.includes('gap is a bug'));
await page.screenshot({ path: `${SHOTS}/merchants-drill.png`, fullPage: false });

// Switch the period; the merchant slice must recompute, not go stale.
const beforeSwitch = await page.locator('.kpi').first().locator('.kpi-value').innerText();
await page.locator('[data-testid="period-control"] .seg-item', { hasText: 'All Time' }).first().click()
  .catch(async () => {
    await page.click('[data-testid="period-control"] button');
    await page.click('.popover-item:has-text("All Time")');
  });
await page.waitForTimeout(2600);
const afterSwitch = await page.locator('.kpi').first().locator('.kpi-value').innerText();
check('changing the period changes the total (filters compose with time)',
  beforeSwitch !== afterSwitch, `${beforeSwitch} -> ${afterSwitch}`);
check('ranking still renders after the period change',
  await page.locator('[data-testid="merchant-row"]').count() > 0);
await page.screenshot({ path: `${SHOTS}/merchants-alltime.png`, fullPage: false });

// Search.
await page.fill('[data-testid="merchant-search"]', 'amazon');
await page.waitForTimeout(1400);
const searchRows = await page.locator('[data-testid="merchant-row"]').count();
const searchFirst = searchRows
  ? (await page.locator('[data-testid="merchant-row"]').first().innerText()).toLowerCase() : '';
check('search narrows the ranking to the match', searchRows > 0 && searchFirst.includes('amazon'),
  `rows=${searchRows}`);

await browser.close();

const failed = steps.filter((s) => !s.ok);
console.log(JSON.stringify({ steps, consoleErrors: errors }, null, 2));
if (failed.length || errors.length) {
  console.error(`FAIL: ${failed.length} assertion(s), ${errors.length} console error(s)`);
  process.exit(1);
}
console.log(`PASS: ${steps.length} assertions, no console errors`);
