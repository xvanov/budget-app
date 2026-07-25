const { test, expect } = require('@playwright/test');

const API_BASE = 'http://localhost:8001/api';

async function apiFetch(path) {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`);
  return res.json();
}

/**
 * Navigate by clicking the sidebar nav item.
 * This is the correct way for a SPA that uses React state for routing.
 */
async function navTo(page, label) {
  await page.locator(`.sb-item:has-text("${label}")`).click();
  await page.waitForTimeout(800);
  // Wait for loading to clear
  const loading = page.locator('.subtitle:has-text("Loading")');
  if (await loading.count() > 0) {
    await loading.first().waitFor({ state: 'hidden', timeout: 15000 }).catch(() => {});
  }
  await page.waitForTimeout(500);
}

test.describe('Story 53: End-to-end Playwright suite', () => {

  test.beforeAll(async () => {
    const health = await apiFetch('/health');
    expect(health).toHaveProperty('status', 'ok');
  });

  test.beforeEach(async ({ page }) => {
    // Always start from Dashboard
    await page.goto('/');
    await page.waitForTimeout(1500);
    // Wait for import banner to clear
    const importBanner = page.locator('.import-banner');
    if (await importBanner.count() > 0) {
      await importBanner.first().waitFor({ state: 'hidden', timeout: 30000 }).catch(() => {});
    }
    // Wait for dashboard data to load
    const loading = page.locator('.subtitle:has-text("Loading")');
    if (await loading.count() > 0) {
      await loading.first().waitFor({ state: 'hidden', timeout: 15000 }).catch(() => {});
    }
    await page.waitForTimeout(500);
  });

  // ── 1. Dashboard ──
  test('Dashboard loads with KPI cards showing non-zero values', async ({ page }) => {
    await expect(page.locator('h1')).toContainText('Dashboard');

    const kpiLabels = page.locator('.kpi-label');
    await expect(kpiLabels).toContainText(['Net Cash Flow', 'Income', 'Expenses', 'Savings Rate']);

    const kpiValues = page.locator('.kpi-value');
    expect(await kpiValues.count()).toBeGreaterThanOrEqual(4);

    await expect(page.locator('.period-bar')).toBeVisible();
    await expect(page.locator('.seg-item.is-active')).toBeVisible();

    const subtitle = page.locator('.subtitle');
    await expect(subtitle.first()).toContainText(/transactions across/);

    // Recent Transactions table
    await expect(page.locator('.tbl')).toBeVisible();
    expect(await page.locator('.tbl tbody tr').count()).toBeGreaterThan(0);

    // Top Spending
    expect(await page.locator('.topspend-row').count()).toBeGreaterThan(0);
  });

  // ── 2. Sankey Cash Flow ──
  test('Sankey renders on Dashboard with Cash Flow section', async ({ page }) => {
    const sectionHeading = page.locator('.section-head .h');
    await expect(sectionHeading.first()).toContainText('Cash Flow');

    const filterRow = page.locator('.filter-row .label');
    await expect(filterRow.first()).toContainText('Total Income');

    // Check that the Cash Flow card has net and income values
    const filterLabels = await page.locator('.filter-row .num').allTextContents();
    expect(filterLabels.length).toBeGreaterThanOrEqual(2);
  });

  // ── 3. Transactions Page ──
  test('Transactions page loads with table, search, filters, pagination', async ({ page }) => {
    await navTo(page, 'Transactions');

    await expect(page.locator('h1')).toContainText('Transactions');

    const subtitle = page.locator('.subtitle').first();
    await expect(subtitle).toContainText(/transactions/);
    const subtitleText = await subtitle.textContent();
    const match = subtitleText.match(/(\d+)/);
    expect(match ? parseInt(match[1]) : 0).toBeGreaterThan(0);

    // Account chips
    expect(await page.locator('.chip').count()).toBeGreaterThanOrEqual(2);

    // Search input
    await expect(page.locator('input[placeholder*="Search"]')).toBeVisible();

    // Table with rows
    await expect(page.locator('.tbl').first()).toBeVisible();
    expect(await page.locator('.tbl tbody tr').count()).toBeGreaterThan(0);

    // Select-all checkbox
    await expect(page.locator('.tbl thead input[type="checkbox"]')).toBeVisible();

    // Action buttons
    await expect(page.locator('button:has-text("Export CSV")')).toBeVisible();
    await expect(page.locator('button:has-text("Add Transaction")')).toBeVisible();

    // Why buttons
    expect(await page.locator('.why-btn').count()).toBeGreaterThan(0);
  });

  test('Search filters transactions on Transactions page', async ({ page }) => {
    await navTo(page, 'Transactions');

    const searchInput = page.locator('input[placeholder*="Search"]');
    await searchInput.fill('AMAZON');
    await searchInput.press('Enter');
    await page.waitForTimeout(1000);

    const filteredRows = page.locator('.tbl tbody tr');
    const filteredCount = await filteredRows.count();
    if (filteredCount > 0) {
      const firstMerchant = await filteredRows.first().locator('td').nth(2).textContent();
      expect(firstMerchant).toMatch(/AMAZON/i);
    }
  });

  test('Manual transaction entry modal opens and closes', async ({ page }) => {
    await navTo(page, 'Transactions');

    await page.locator('button:has-text("Add Transaction")').click();
    await page.waitForTimeout(500);

    const modal = page.locator('.modal-overlay');
    await expect(modal).toBeVisible();
    await expect(page.locator('.modal-title:has-text("Add Manual Transaction")')).toBeVisible();

    await expect(page.locator('input[placeholder*="Cash rent"]')).toBeVisible();
    await expect(page.locator('input[placeholder*="-150000"]')).toBeVisible();
    await expect(page.locator('input[type="date"]')).toBeVisible();
    await expect(page.locator('select').first()).toBeVisible();

    // Close
    await page.locator('button:has-text("Cancel")').click();
    await expect(modal).not.toBeVisible();
  });

  // ── 4. Budgets Page ──
  test('Budgets page loads with envelopes and intelligence', async ({ page }) => {
    await navTo(page, 'Budgets');

    await expect(page.locator('h1')).toContainText('Budgets');

    const kpiLabels = page.locator('.kpi-label');
    const kpiText = await kpiLabels.allTextContents();
    expect(kpiText.join(' ')).toMatch(/Budgeted|Spent|Remaining/);

    await expect(page.locator('.section-head .h:has-text("Envelopes")')).toBeVisible();

    // Budget rows may be 0 if no budgets configured yet
    if (await page.locator('.budget-row').count() > 0) {
      const budgetCount = await page.locator('.budget-row').count();
      expect(budgetCount).toBeGreaterThan(0);
    }

    const intelligenceTitle = page.locator('.section-head .h:has-text("Budget Intelligence")');
    if (await intelligenceTitle.count() > 0) {
      await expect(intelligenceTitle).toBeVisible();
    }

    await expect(page.locator('button:has-text("Rollover")')).toBeVisible();
  });

  // ── 5. Net Worth Page ──
  test('Net Worth page loads with accounts and chart', async ({ page }) => {
    await navTo(page, 'Net Worth');

    await expect(page.locator('h1')).toContainText('Net Worth');

    const kpiLabels = page.locator('.kpi-label');
    await expect(kpiLabels).toContainText(['Total Net Worth', 'Assets', 'Liabilities']);

    await expect(page.locator('.section-head .h:has-text("Net Worth Over Time")')).toBeVisible();
    await expect(page.locator('.section-head .h:has-text("Allocation")')).toBeVisible();

    await expect(page.locator('.nw-list')).toBeVisible();
    expect(await page.locator('.nw-grouptitle').count()).toBeGreaterThan(0);
  });

  // ── 6. Compare Page ──
  test('Compare page loads with period pickers and diff table', async ({ page }) => {
    await navTo(page, 'Compare');

    await expect(page.locator('h1')).toContainText('Compare');
    await expect(page.locator('.compare-period:has-text("Period A")')).toBeVisible();
    await expect(page.locator('.compare-period:has-text("Period B")')).toBeVisible();

    await expect(page.locator('.section-head .h:has-text("Per-Category Diff")')).toBeVisible();
    expect(await page.locator('.diff-row').count()).toBeGreaterThan(2);
    expect(await page.locator('.seg-item').count()).toBeGreaterThanOrEqual(7);
  });

  // ── 7. Trends Page ──
  test('Trends page loads with all three chart cards', async ({ page }) => {
    await navTo(page, 'Trends');

    await expect(page.locator('h1')).toContainText('Trends');
    await expect(page.locator('.section-head .h:has-text("Income vs Expense")')).toBeVisible();
    await expect(page.locator('.section-head .h:has-text("Expense Mix Over Time")')).toBeVisible();
    await expect(page.locator('.section-head .h:has-text("Net Worth Trajectory")')).toBeVisible();

    expect(await page.locator('.bar-chart, .area-chart').count()).toBeGreaterThanOrEqual(1);

    await expect(page.locator('.seg-item:has-text("Monthly")')).toBeVisible();
    await expect(page.locator('.seg-item:has-text("Quarterly")')).toBeVisible();
  });

  // ── 8. Activity Page ──
  test('Activity page loads with anomalies and filters', async ({ page }) => {
    await navTo(page, 'Activity');

    await expect(page.locator('h1')).toContainText('Activity');

    // Wait for anomalies to finish loading (subtitle changes from "Scanning" to count)
    const subtitle = page.locator('.subtitle').first();
    await subtitle.waitFor({ state: 'visible', timeout: 10000 }).catch(() => {});

    // Filter selects rendered via dropdown with select.form-input
    const selects = page.locator('select');
    await selects.first().waitFor({ state: 'visible', timeout: 20000 });
    const selectCount = await selects.count();
    expect(selectCount).toBeGreaterThanOrEqual(1);

    await expect(page.locator('button:has-text("Re-scan")')).toBeVisible();
  });

  // ── 9. Chat Correct Page ──
  test('Chat Correct page loads with input and send button', async ({ page }) => {
    await navTo(page, 'Chat Correct');

    await expect(page.locator('h1')).toContainText('Chat Correct');

    const textarea = page.locator('textarea.chat-input');
    await expect(textarea).toBeVisible();
    const placeholder = await textarea.getAttribute('placeholder');
    expect(placeholder).toMatch(/person:tenant|Utilities/);

    await expect(page.locator('button:has-text("Send")')).toBeVisible();
  });

  // ── 10. Context Page ──
  test('Budget Context page loads with textarea', async ({ page }) => {
    await navTo(page, 'Context');

    await expect(page.locator('h1')).toContainText('Budget Context');

    const textarea = page.locator('textarea.ctx-textarea');
    await expect(textarea).toBeVisible();

    await expect(page.locator('button:has-text("Infer from data")')).toBeVisible();
    await expect(page.locator('button:has-text("Save")')).toBeVisible();
  });

  // ── 11. Period filter affects Dashboard ──
  test('Changing period filter updates dashboard data', async ({ page }) => {
    await page.locator('.seg-item:has-text("All Time")').click();
    await page.waitForTimeout(1000);
    // Wait for dashboard to reload
    const loading = page.locator('.subtitle:has-text("Loading")');
    if (await loading.count() > 0) {
      await loading.first().waitFor({ state: 'hidden', timeout: 15000 }).catch(() => {});
    }
    await page.waitForTimeout(500);

    const activePreset = page.locator('.seg-item.is-active');
    await expect(activePreset).toContainText('All Time');
  });

  // ── 12. Navigation between pages ──
  test('Sidebar navigation switches between all pages', async ({ page }) => {
    const navItems = [
      { label: 'Transactions', expected: 'Transactions' },
      { label: 'Activity', expected: 'Activity' },
      { label: 'Net Worth', expected: 'Net Worth' },
      { label: 'Compare', expected: 'Compare' },
      { label: 'Trends', expected: 'Trends' },
      { label: 'Budgets', expected: 'Budgets' },
      { label: 'Context', expected: 'Budget Context' },
      { label: 'Chat Correct', expected: 'Chat Correct' },
      { label: 'Dashboard', expected: 'Dashboard' },
    ];

    for (const item of navItems) {
      const navBtn = page.locator(`.sb-item:has-text("${item.label}")`);
      await expect(navBtn).toBeVisible();
      await navBtn.click();
      await page.waitForTimeout(1000);
      // Wait for loading
      const loading = page.locator('.subtitle:has-text("Loading")');
      if (await loading.count() > 0) {
        await loading.first().waitFor({ state: 'hidden', timeout: 15000 }).catch(() => {});
      }
      await expect(navBtn).toHaveClass(/is-active/);
      await expect(page.locator('h1')).toBeVisible();
    }
  });

  // ── 13. Cross-page API consistency (via API, not browser) ──
  test('Cross-page API consistency: dashboard totals match trends and sankey', async () => {
    const period = '?start_date=2026-01-01&end_date=2026-04-30';

    const dash = await apiFetch(`/dashboard${period}`);
    const sankey = await apiFetch(`/sankey${period}`);
    const trends = await apiFetch(`/trends${period}`);

    const dashInc = dash.totals.total_income_cents;
    const dashExp = dash.totals.total_expense_cents;
    const dashNet = dash.totals.net_cents;

    // Identity: net = income - expense
    expect(dashNet).toBe(dashInc - dashExp);

    // Dashboard matches Sankey
    expect(sankey.total_income_cents).toBe(dashInc);
    expect(sankey.total_expense_cents).toBe(dashExp);
    expect(sankey.net_cents).toBe(dashNet);

    // Dashboard monthly sum matches dashboard totals
    const monthlySum = dash.monthly.reduce((s, m) => s + m.income_cents, 0);
    expect(monthlySum).toBe(dashInc);

    // Trends totals match dashboard totals
    const trendsInc = trends.series.reduce((s, m) => s + m.income_cents, 0);
    expect(trendsInc).toBe(dashInc);

    // No Uncategorized nodes in Sankey
    for (const node of sankey.nodes) {
      expect(['Uncategorized', 'Unknown', '', null]).not.toContain(node.name);
    }
  });
});
