// Page smoke walk: every nav destination must load and render with no console
// errors, no uncaught page errors, and no error when reached via sidebar click.
//
// This is the check behind "the site works and doesn't crash". Run it from the
// REPO ROOT (module resolution needs the root node_modules) with the stack up:
//
//   docker compose up -d && node tests/e2e/smoke-pages.mjs
//
// Frontend host port comes from BASE_URL (default matches the committed
// docker-compose mapping; this machine remaps it via docker-compose.override.yml).

import { chromium } from 'playwright';

const BASE = process.env.BASE_URL || 'http://127.0.0.1:8083';
// Every nav destination. Accounts & Sync used to be a standalone /accounts.html
// page; it is a normal in-app route now.
const ROUTES = ['/', '/txns', '/income', '/expenses', '/refunded', '/excluded', '/mortgage', '/activity', '/compare',
                '/trends', '/budgets', '/context', '/chat', '/networth', '/accounts'];

const browser = await chromium.launch();
const page = await browser.newPage();
const results = [];

for (const route of ROUTES) {
  const errors = [];
  const onConsole = (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 160)); };
  const onPageErr = (e) => errors.push('PAGEERROR: ' + String(e).slice(0, 160));
  page.on('console', onConsole);
  page.on('pageerror', onPageErr);

  let status = 'ok';
  try {
    const resp = await page.goto(BASE + route, { waitUntil: 'networkidle', timeout: 45000 });
    status = 'HTTP ' + (resp ? resp.status() : '?');
    await page.waitForTimeout(2200);
  } catch (e) {
    status = 'NAV FAIL: ' + String(e).slice(0, 100);
  }

  // For SPA routes, click the sidebar item so client-side rendering is exercised.
  const bodyText = (await page.evaluate(() => document.body.innerText || '')).length;
  const name = route.replace(/[^a-z0-9]/gi, '_') || 'root';
  await page.screenshot({ path: `screenshots/walk${name}.png`, fullPage: false });

  page.off('console', onConsole);
  page.off('pageerror', onPageErr);
  results.push({ route, status, textLen: bodyText, errors });
}

// Exercise SPA nav by clicking each sidebar button from the dashboard.
await page.goto(BASE + '/', { waitUntil: 'networkidle' });
const navLabels = await page.evaluate(() =>
  Array.from(document.querySelectorAll('.sb-item')).map((b) => b.textContent.trim()));

const clickErrors = [];
page.on('console', (m) => { if (m.type() === 'error') clickErrors.push(m.text().slice(0, 140)); });
page.on('pageerror', (e) => clickErrors.push('PAGEERROR: ' + String(e).slice(0, 140)));
for (const label of navLabels) {
  try {
    await page.click(`.sb-item:has-text("${label}")`, { timeout: 8000 });
    await page.waitForTimeout(1400);
  } catch (e) { clickErrors.push(`click ${label}: ${String(e).slice(0, 90)}`); }
}
await page.screenshot({ path: 'screenshots/walk-spa-last.png' });

console.log(JSON.stringify({ results, navLabels, clickErrors }, null, 2));
await browser.close();

const broken = results.filter((r) => r.status !== 'HTTP 200' || r.errors.length);
if (broken.length || clickErrors.length) {
  console.error(`FAIL: ${broken.length} page(s) with errors, ${clickErrors.length} nav error(s)`);
  process.exit(1);
}
console.log(`PASS: ${results.length} pages, ${navLabels.length} nav targets, no errors`);
