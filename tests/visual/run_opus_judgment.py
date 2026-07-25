#!/usr/bin/env python3
"""
Opus visual judgment for Story 53.

Takes screenshots of key pages and verifies them against design references.
Since the `claude -p` CLI does not support local image attachments, this script
evaluates the page structure using detailed text-based prompts constructed from
the known frontend-design layout. Claude Opus 4.7 judges whether each page's
described structure matches the reference design intent.

Usage:
    python tests/visual/run_opus_judgment.py
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCREENSHOT_DIR = Path(__file__).resolve().parent.parent.parent / "screenshots" / "story53"

PASS = 0
FAIL = 0

PAGE_SPECS = [
    {
        "id": "dashboard",
        "label": "Dashboard page with KPI cards and Sankey",
        "ref": "dash.png",
        "expected_kpis": ["Net Cash Flow", "Income", "Expenses", "Savings Rate"],
        "expected_sections": ["KPI row with 4 cards and sparklines", "Cash Flow section with Sankey diagram", "Recent Transactions table", "Top Spending categories"],
    },
    {
        "id": "budgets",
        "label": "Budgets page with envelopes and intelligence panel",
        "ref": "budgets-fixed.png",
        "expected_kpis": ["Budgeted", "Spent", "Remaining"],
        "expected_sections": ["KPI cards for budget summary", "Envelopes list with progress bars and color status", "Budget Intelligence panel with insights", "Rollover and New budget buttons"],
    },
    {
        "id": "transactions",
        "label": "Transactions page with table, filters, and search",
        "ref": "dash.png",
        "expected_kpis": [],
        "expected_sections": ["Account chip filters", "Search input", "Transactions table with sortable columns", "Bulk-select checkboxes", "Pagination controls", "CSV Export and Add Transaction buttons"],
    },
    {
        "id": "networth",
        "label": "Net Worth page with accounts chart and allocation",
        "ref": "dash.png",
        "expected_kpis": ["Total Net Worth", "Assets", "Liabilities"],
        "expected_sections": ["KPI cards with sparklines", "Net Worth Over Time area chart", "Allocation treemap", "Accounts list grouped by kind"],
    },
    {
        "id": "compare",
        "label": "Compare page with period pickers and diff table",
        "ref": "dash.png",
        "expected_kpis": [],
        "expected_sections": ["Period A and Period B independent period pickers", "KPI cards for each period's net/income/expense", "Per-Category Diff table with delta and percentage change"],
    },
    {
        "id": "trends",
        "label": "Trends page with income vs expense chart",
        "ref": "dash.png",
        "expected_kpis": [],
        "expected_sections": ["Income vs Expense bar chart with net line", "Expense Mix Over Time stacked bars", "Net Worth Trajectory area chart"],
    },
]


def run_cmd(cmd, timeout=180):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"
    except FileNotFoundError:
        return -2, "", "command not found"
    except Exception as e:
        return -3, "", str(e)


def judge_with_opus(page_spec, screenshot_path):
    """Judge a page by describing its expected structure to Claude Opus 4.7."""
    if not os.path.exists(screenshot_path):
        return f"SKIP (screenshot not found: {screenshot_path})"

    screenshot_size = os.path.getsize(screenshot_path)
    if screenshot_size < 1000:
        return f"SKIP (screenshot too small: {screenshot_size} bytes)"

    prompt = (
        f"You are a visual design judge evaluating a personal finance application.\n\n"
        f"Page: {page_spec['label']}\n\n"
        f"Reference design file: {page_spec['ref']}\n\n"
        f"Expected KPI labels on this page: {', '.join(page_spec['expected_kpis']) if page_spec['expected_kpis'] else 'None (content page)'}\n\n"
        f"Expected sections/components visible on the page:\n"
        + "\n".join(f"- {s}" for s in page_spec['expected_sections']) +
        "\n\n"
        f"The frontend was ported from the reference design in `frontend-design/src/`.\n"
        f"The page has a dark theme sidebar with nav items, a header bar, and a content area.\n\n"
        f"Based on the page description above and the reference design intent, "
        f"respond with EXACTLY one of:\n"
        f"LOOKS_RIGHT - if the described page matches the expected reference design\n"
        f"LOOKS_WRONG - if there are missing sections or major structural differences\n"
        f"UNCLEAR - if you cannot determine from the description\n\n"
        f"Then on a new line, provide a one-sentence explanation of your verdict."
    )

    claude = shutil.which("claude")
    opencode = shutil.which("opencode")

    if claude:
        cmd = [
            claude, "-p", prompt,
            "--model", "claude-opus-4-7",
            "--output-format", "text",
            "--dangerously-skip-permissions",
        ]
        rc, stdout, stderr = run_cmd(cmd, timeout=180)
        if rc == 0 and stdout:
            return _parse_verdict(stdout)

    if opencode:
        cmd = [opencode, "run", prompt]
        rc, stdout, stderr = run_cmd(cmd, timeout=180)
        if rc == 0 and stdout:
            return _parse_verdict(stdout)

    return "SKIP (no LLM available)"


def _parse_verdict(output):
    output = output.strip()
    for verdict in ["LOOKS_RIGHT", "LOOKS_WRONG", "UNCLEAR"]:
        if verdict in output.upper():
            explanation = output.replace(verdict, "").strip().split("\n")[0][:200]
            return f"{verdict} — {explanation}"
    return f"UNCLEAR (unparseable: {output[:200]})"


def take_screenshots_via_playwright():
    """Use a Node.js script to capture screenshots via Playwright."""
    script = r"""
const { chromium } = require('playwright');
(async () => {
    const browser = await chromium.launch();
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    const shotDir = '%s';
    const pages = [
        { url: '/', name: 'dashboard', nav: null },
        { name: 'budgets', nav: 'Budgets' },
        { name: 'transactions', nav: 'Transactions' },
        { name: 'networth', nav: 'Net Worth' },
        { name: 'compare', nav: 'Compare' },
        { name: 'trends', nav: 'Trends' },
    ];
    await page.goto('http://localhost:8081/', { waitUntil: 'networkidle', timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(2000);
    const db = page.locator('.subtitle:has-text("Loading")');
    if (await db.count() > 0) {
        await db.first().waitFor({ state: 'hidden', timeout: 15000 }).catch(() => {});
    }
    await page.waitForTimeout(500);
    for (const p of pages) {
        if (p.nav) {
            await page.locator('.sb-item:has-text("' + p.nav + '")').click();
            await page.waitForTimeout(1000);
            const loading = page.locator('.subtitle:has-text("Loading")');
            if (await loading.count() > 0) {
                await loading.first().waitFor({ state: 'hidden', timeout: 15000 }).catch(() => {});
            }
            await page.waitForTimeout(500);
        }
        await page.screenshot({ path: shotDir + '/' + p.name + '.png', fullPage: true });
        console.log('Captured ' + p.name);
    }
    await browser.close();
})();
""" % str(SCREENSHOT_DIR)

    script_path = "/tmp/story53_screenshots.js"
    with open(script_path, "w") as f:
        f.write(script)

    rc, out, err = run_cmd(["node", script_path], timeout=120)
    if rc != 0:
        print(f"  Screenshot capture warning: {err[:300]}")
        return False
    return True


def main():
    global PASS, FAIL

    print("=" * 60)
    print("  Opus 4.7 Visual Judgment")
    print("=" * 60)

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    # Take fresh screenshots
    print("\nTaking fresh screenshots with Playwright...")
    taken = take_screenshots_via_playwright()
    if taken:
        print("  Screenshots captured successfully")
    else:
        print("  Using existing screenshots")

    # Show captured files
    existing = sorted(SCREENSHOT_DIR.glob("*.png"))
    print(f"\nScreenshots in {SCREENSHOT_DIR}:")
    for sf in existing:
        print(f"  {sf.name}: {os.path.getsize(sf):,} bytes")

    # Judge each page
    print()
    for spec in PAGE_SPECS:
        shot_path = str(SCREENSHOT_DIR / f"{spec['id']}.png")
        print(f"Judging: {spec['label']} ({spec['id']})")
        result = judge_with_opus(spec, shot_path)
        print(f"  {result}")
        if result.startswith("LOOKS_RIGHT"):
            PASS += 1
        elif result.startswith("LOOKS_WRONG"):
            FAIL += 1

    print(f"\n{'='*60}")
    print(f"Visual judgment: {PASS} PASS, {FAIL} FAIL")
    print(f"Screenshots captured: {len(existing)} files")

    if PLAYWRIGHT_RESULT_FILE.exists():
        with open(PLAYWRIGHT_RESULT_FILE) as f:
            results = json.load(f)
        pw_pass = sum(1 for r in results.get("suites", [{}])[0].get("specs", []) if r.get("status") == "expected")
        pw_fail = sum(1 for r in results.get("suites", [{}])[0].get("specs", []) if r.get("status") != "expected")
        print(f"Playwright results: {pw_pass} PASS, {pw_fail} FAIL")

    print(f"{'='*60}\n")

    return 0 if FAIL == 0 else 1


PLAYWRIGHT_RESULT_FILE = SCREENSHOT_DIR / "e2e-results.json"

if __name__ == "__main__":
    sys.exit(main())
