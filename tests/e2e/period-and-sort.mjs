// Regression test for two controls that looked dead:
//
//   1. The Date column in Transactions could not be re-sorted. handleSort had a
//      third "reset to posted_date desc" state, which ON the Date column was a
//      no-op — so once it was descending it stayed descending forever.
//   2. There was no way to ask for a specific window like "the month of
//      January". The period was preset-only.
//
// Run from the REPO ROOT (module resolution needs the root node_modules) with a
// stack up:
//
//   BASE_URL=http://127.0.0.1:8185 node tests/e2e/period-and-sort.mjs
//
// Read-only: it sorts, picks periods and reloads. It creates and edits nothing,
// so it is safe against real data.
import { chromium } from 'playwright';

const BASE = process.env.BASE_URL || 'http://127.0.0.1:8083';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 160)); });
page.on('pageerror', (e) => errors.push('PAGEERROR ' + String(e).slice(0, 160)));

const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  ${detail}`);
};
const settle = (ms = 2200) => page.waitForTimeout(ms);
const nav = async (label) => { await page.click(`.sb-item:has-text("${label}")`); await settle(2600); };
const bodyText = () => page.evaluate(() => document.body.innerText);
const totalLabel = async () => (await bodyText()).match(/([\d,]+)\s*transactions/i)?.[0] || 'n/a';
// First column of the transactions table is the checkbox; the date cell is next.
const firstDate = () => page.evaluate(() => document.querySelector('.tbl tbody tr td:nth-child(2)')?.innerText.trim() || '');
const dateCells = () => page.evaluate(() =>
  Array.from(document.querySelectorAll('.tbl tbody tr td:nth-child(2)')).map((td) => td.innerText.trim()).filter(Boolean));
// Headers are rendered uppercase by CSS, and innerText reflects that, so match
// case-insensitively: the DOM text is 'DATE ▼'.
const header = (name) => page.evaluate((n) =>
  Array.from(document.querySelectorAll('.tbl thead th'))
    .find((th) => th.innerText.trim().toLowerCase().startsWith(n))?.innerText.trim() || '', name);
const clickHeader = async (name) => {
  await page.evaluate((n) => Array.from(document.querySelectorAll('.tbl thead th'))
    .find((th) => th.innerText.trim().toLowerCase().startsWith(n)).click(), name);
  await settle(2400);
};
const dateHeader = () => header('date');
const clickDateHeader = () => clickHeader('date');
const rangeText = () => page.evaluate(() =>
  document.querySelector('[data-testid="period-control"] .period-range')?.innerText.replace(/\s+/g, ' ').trim() || '');

// Deterministic start: the app remembers route + period in localStorage.
await page.goto(BASE + '/', { waitUntil: 'networkidle' });
await page.evaluate(() => localStorage.clear());
await page.goto(BASE + '/', { waitUntil: 'networkidle' });
await settle(3000);

// ───── Defect B: the Date column must toggle both ways ─────
await nav('Transactions');
const startDate = await firstDate();
const startHeader = await dateHeader();
check('Transactions default to newest first', startHeader.includes('▼'), `header="${startHeader}" first=${startDate}`);

await clickDateHeader();
const ascHeader = await dateHeader();
const ascDate = await firstDate();
check('1st Date click sorts ascending', ascHeader.includes('▲') && ascDate !== startDate,
  `header="${ascHeader}" first=${ascDate}`);
check('Ascending really is oldest first', ascDate < startDate, `${ascDate} < ${startDate}`);

await clickDateHeader();
const descHeader = await dateHeader();
const descDate = await firstDate();
check('2nd Date click sorts descending again', descHeader.includes('▼') && descDate === startDate,
  `header="${descHeader}" first=${descDate}`);

// The bug: this third click used to be a no-op ("reset to posted_date desc").
await clickDateHeader();
const thirdHeader = await dateHeader();
const thirdDate = await firstDate();
check('3rd Date click flips back to ascending (no stuck state)',
  thirdHeader.includes('▲') && thirdDate === ascDate, `header="${thirdHeader}" first=${thirdDate}`);

// A second column must still toggle, and must not be able to stick either.
const firstAmount = () => page.evaluate(() =>
  document.querySelector('.tbl tbody tr td:nth-last-child(2)')?.innerText.trim() || '');
await clickHeader('amount');
const dateCleared = await dateHeader();
const amtAscFirst = await firstAmount();
await clickHeader('amount');
const amtDescFirst = await firstAmount();
await clickHeader('amount');
const amtAscAgain = await firstAmount();
check('Amount column toggles both ways too',
  !/[▲▼]/.test(dateCleared) && amtAscFirst !== amtDescFirst && amtAscAgain === amtAscFirst,
  `date header="${dateCleared}", ${amtAscFirst} -> ${amtDescFirst} -> ${amtAscAgain}`);

// Back to the default order for the period checks below.
await clickHeader('date');
await clickHeader('date');

// ───── Defect A: a custom range, segmented layout (>= 1280px) ─────
const ytdTotal = await totalLabel();
await page.click('[data-testid="period-custom-toggle"]');
await settle(400);
check('Custom… opens the range panel', !!(await page.$('[data-testid="period-custom-panel"]')));

// Absurd / inverted input must be refused, and must not change the period.
await page.fill('[data-testid="period-custom-start"]', '2026-03-31');
await page.fill('[data-testid="period-custom-end"]', '2026-01-01');
await page.click('[data-testid="period-custom-apply"]');
await settle(600);
const errText = await page.evaluate(() =>
  document.querySelector('[data-testid="period-custom-error"]')?.innerText.trim() || '');
check('End-before-start is rejected', /end date must be on or after/i.test(errText), JSON.stringify(errText));
check('Rejected input leaves the period alone', (await totalLabel()) === ytdTotal, `still ${ytdTotal}`);

// The affordance the owner asked for: one click for "the month of January".
await page.click('[data-testid="period-month-2026-01"]');
await settle(2800);
const janRange = await rangeText();
const janTotal = await totalLabel();
check('January 2026 applies as a range', /Jan 1\s*→\s*Jan 31/.test(janRange), `range="${janRange}"`);
check('Custom segment shows the resolved label',
  (await page.evaluate(() => document.querySelector('[data-testid="period-custom-toggle"]')?.innerText.trim())) === 'Jan 2026');
check('Transaction count changes for January', janTotal !== ytdTotal && janTotal !== 'n/a', `${ytdTotal} -> ${janTotal}`);
const cells = await dateCells();
check('Every listed transaction is in January', cells.length > 0 && cells.every((d) => d.startsWith('01-')),
  `${cells.length} rows, e.g. ${cells.slice(0, 3).join(' ')}`);

// It must survive a reload (the period id is what gets persisted).
await page.reload({ waitUntil: 'networkidle' });
await settle(3200);
check('Custom range survives a reload', (await rangeText()) === janRange, `range="${await rangeText()}"`);

// ───── The other period-aware pages must follow ─────
// Assert at the wire: every page must ASK the API for the custom window. A
// screen-scrape can't tell "the page ignored the period" from "January happens
// to look like the year".
// Routed rather than observed via page.on('request'): two pages can fetch the
// same URL, and the second one is served from the memory cache without emitting
// a request event, which reads as "the page ignored the period".
const apiCalls = [];
await page.route('**/api/**', (route) => { apiCalls.push(route.request().url()); route.continue(); });
const money = () => page.evaluate(() => (document.body.innerText.match(/\$[\d.,]+k?/g) || []).slice(0, 6).join(','));

for (const label of ['Dashboard', 'Income', 'Expenses', 'Refunded', 'Excluded', 'Trends', 'Budgets']) {
  apiCalls.length = 0;
  await nav(label);
  await settle(1200);
  const asked = apiCalls.filter((u) => u.includes('2026-01-01') && u.includes('2026-01-31'));
  const janMoney = await money();
  const stillJan = await rangeText();
  await page.click('.topbar-period .seg-item:has-text("Year to Date")');
  await settle(2800);
  const ytdMoney = await money();
  // Two ways to prove it, and a page needs either: it asked the API for the
  // window, or the figures on screen moved with it. Both are needed because
  // Budgets shows $0.00 either way on a DB with no budgets configured, and
  // Income/Expenses fetch the SAME /api/dashboard URL — so whichever loads
  // second is answered from the browser cache and never reaches the wire.
  const showsMoney = /[1-9]/.test(janMoney);
  const movedOnScreen = showsMoney && janMoney !== ytdMoney;
  check(`${label} respects the custom range`,
    (asked.length > 0 || movedOnScreen) && /Jan 1\s*→\s*Jan 31/.test(stillJan),
    `${asked.length} scoped calls, jan=[${janMoney}] ytd=[${ytdMoney}]`);
  // Restore the custom range for the next page.
  await page.click('[data-testid="period-custom-toggle"]');
  await settle(400);
  await page.click('[data-testid="period-month-2026-01"]');
  await settle(2600);
}

// A preset click is the obvious way back, and it clears the custom state.
await page.click('.topbar-period .seg-item:has-text("Last Month")');
await settle(2400);
check('Presets take over again',
  (await page.evaluate(() => document.querySelector('[data-testid="period-custom-toggle"]')?.innerText.trim())) === 'Custom…',
  `range="${await rangeText()}"`);

// ───── Compact layout (< 1280px): same capability, dropdown form ─────
await page.setViewportSize({ width: 1100, height: 900 });
await settle(800);
await nav('Transactions');
await page.click('[data-testid="period-control"] button');
await settle(400);
await page.click('[data-testid="period-custom-toggle"]');
await settle(400);
check('Compact dropdown reaches the custom panel', !!(await page.$('[data-testid="period-custom-panel"]')));
await page.click('[data-testid="period-custom-back"]');
await settle(300);
check('Compact panel has a way back to the presets',
  !(await page.$('[data-testid="period-custom-panel"]')) && !!(await page.$('.popover-item:has-text("Year to Date")')));
await page.click('[data-testid="period-custom-toggle"]');
await settle(300);
await page.click('[data-testid="period-month-2026-01"]');
await settle(2800);
const compactCells = await dateCells();
check('Compact layout applies January too',
  compactCells.length > 0 && compactCells.every((d) => d.startsWith('01-')),
  `${compactCells.length} rows, trigger="${await page.evaluate(() => document.querySelector('[data-testid="period-control"] button')?.innerText.trim())}"`);

// Typed range: the escape hatch for windows no month button covers.
await page.click('[data-testid="period-control"] button');
await settle(300);
await page.click('[data-testid="period-custom-toggle"]');
await settle(300);
await page.fill('[data-testid="period-custom-start"]', '2025-12-15');
await page.fill('[data-testid="period-custom-end"]', '2026-01-14');
await page.click('[data-testid="period-custom-apply"]');
await settle(2800);
const spanCells = await dateCells();
check('A typed cross-year range applies',
  spanCells.length > 0 && spanCells.every((d) => /^(12-1[5-9]|12-2\d|12-3[01]|01-0\d|01-1[0-4])$/.test(d)),
  `${spanCells.length} rows, e.g. ${spanCells.slice(0, 3).join(' ')}`);

await page.screenshot({ path: 'screenshots/period-custom-compact.png' });

console.log('\nCONSOLE ERRORS:', errors.length ? JSON.stringify(errors.slice(0, 6)) : 'none');
const failed = results.filter((r) => !r.ok);
console.log(`SUMMARY: ${results.length - failed.length}/${results.length} passed`);
await browser.close();
if (failed.length || errors.length) process.exit(1);
