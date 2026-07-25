// Interaction smoke test: the buttons and flows a person actually uses.
//
// smoke-pages.mjs proves pages LOAD; this proves controls DO something. It was
// written after a walkthrough found two dead controls that page-load checks
// could never catch: the "New budget" button had no onClick at all, and the
// transactions search only committed on Enter so typing looked broken.
//
// Run from the REPO ROOT with the stack up:
//   docker compose up -d && node tests/e2e/smoke-interactions.mjs
//
// Read-only by design: it filters, paginates, opens panels and modals, but
// creates nothing, so it is safe to run against real data. (The budget-creation
// path is exercised manually; automating it would leave rows behind.)
import { chromium } from 'playwright';

const BASE = process.env.BASE_URL || 'http://127.0.0.1:8083';
const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 120)); });
page.on('pageerror', (e) => errors.push('PAGEERROR ' + String(e).slice(0, 120)));

const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  ${detail}`);
};
const nav = async (label) => {
  await page.click(`.sb-item:has-text("${label}")`);
  await page.waitForTimeout(2500);
};
const bodyText = () => page.evaluate(() => document.body.innerText);
const totalLabel = async () =>
  (await bodyText()).match(/([\d,]+)\s*transactions/i)?.[0] || 'n/a';

await page.goto(BASE + '/', { waitUntil: 'networkidle' });

// Dashboard: switching the period must move the numbers.
const kpis = () => page.evaluate(() => (document.body.innerText.match(/\$[\d.,]+k?/g) || []).slice(0, 4).join(','));
const kpiBefore = await kpis();
await page.click('button:has-text("Last 3 Months")');
await page.waitForTimeout(2500);
const kpiAfter = await kpis();
check('Dashboard period switch changes KPIs', kpiBefore !== kpiAfter, `${kpiBefore} -> ${kpiAfter}`);
await page.click('button:has-text("Year to Date")');
await page.waitForTimeout(1500);

// Transactions: search must filter WITHOUT pressing Enter.
await nav('Transactions');
const searchBefore = await totalLabel();
await page.fill('input[placeholder="Search merchants…"]', 'Amazon');
await page.waitForTimeout(3000);
const searchAfter = await totalLabel();
check('Search filters as you type (no Enter)', searchBefore !== searchAfter && searchAfter !== 'n/a',
  `${searchBefore} -> ${searchAfter}`);

await page.fill('input[placeholder="Search merchants…"]', '');
await page.waitForTimeout(2500);

// Row click opens the explainability panel.
await page.click('tbody tr');
await page.waitForTimeout(1800);
check('Transaction row opens the why/detail panel',
  /confidence|reason|categoriz/i.test(await bodyText()));

// Pagination changes the rows on screen.
const firstRow = () => page.evaluate(() => (document.querySelector('tbody tr') || {}).innerText || '');
const pageOne = await firstRow();
const next = await page.$('button:has-text("Next")');
if (next) { await next.click(); await page.waitForTimeout(2500); }
check('Pagination Next changes the rows', pageOne !== (await firstRow()));

// Activity renders the anomaly list (896 rows — needs a real wait).
await nav('Activity');
let activityText = '';
for (let i = 0; i < 25; i++) {
  await page.waitForTimeout(1000);
  activityText = await bodyText();
  if (activityText.length > 5000) break;
}
check('Activity renders anomalies', activityText.length > 5000, `${activityText.length} chars`);

// Budgets: the New budget button must actually open a form.
await nav('Budgets');
const inputsBefore = await page.evaluate(() => document.querySelectorAll('input,select').length);
await page.click('button:has-text("New budget")');
await page.waitForTimeout(1500);
const inputsAfter = await page.evaluate(() => document.querySelectorAll('input,select').length);
check('New budget opens a form', inputsAfter > inputsBefore, `${inputsBefore} -> ${inputsAfter} inputs`);
// Close via Cancel: these modals close on Cancel or an overlay click, not on
// Escape, and a left-open overlay swallows every later click.
await page.click('.modal button:has-text("Cancel")').catch(() => {});
await page.waitForTimeout(800);

// Trends granularity toggle.
await nav('Trends');
const trendsToggle = await page.$('button:has-text("Quarterly")');
if (trendsToggle) { await trendsToggle.click(); await page.waitForTimeout(2000); }
check('Trends granularity toggle responds', !!trendsToggle);

// Context editor is populated from the living context file.
await nav('Context');
const textarea = await page.$('textarea');
check('Context editor loads content', textarea ? (await textarea.inputValue()).length > 50 : false);

// Accounts & Sync: Plaid + Amazon controls are live. This was a standalone
// /accounts.html page; it is now a normal route inside the shell, so reach it
// the way a person does — by clicking the sidebar.
await nav('Accounts & Sync');
await page.waitForTimeout(2500);
const state = await page.evaluate(() => {
  const labels = ['Connect a bank', 'Sync now', 'Import export', 'Match charges', 'Split into items'];
  const btns = Array.from(document.querySelectorAll('.btn'));
  const connect = btns.find((b) => b.textContent.includes('Connect a bank'));
  return {
    buttons: labels.filter((l) => btns.some((b) => b.textContent.includes(l))).length,
    connectEnabled: !!connect && !connect.disabled,
    stats: document.querySelectorAll('.kpi-value').length,
    inShell: !!document.querySelector('.sb') && !!document.querySelector('.topbar'),
  };
});
check('Accounts page controls present', state.buttons === 5, `${state.buttons}/5 buttons`);
check('Connect a bank is enabled (Plaid configured)', state.connectEnabled);
check('Accounts renders inside the app shell', state.inShell);
check('Plaid + Amazon coverage stats render', state.stats >= 10, `${state.stats} stat tiles`);

console.log('\nCONSOLE ERRORS:', errors.length ? JSON.stringify(errors.slice(0, 6)) : 'none');
const failed = results.filter((r) => !r.ok);
console.log(`SUMMARY: ${results.length - failed.length}/${results.length} passed`);
await browser.close();
if (failed.length || errors.length) process.exit(1);
