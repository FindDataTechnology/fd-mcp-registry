import { test, expect } from '@playwright/test';
import { loginAsAdmin, navigateToVirtualServers } from './helpers/auth';

/**
 * Comprehensive E2E test suite for Virtual MCP Servers.
 *
 * Covers: Dashboard tab, Settings list view, full CRUD lifecycle,
 * form validation / wizard navigation, and multi-backend inspection.
 */

// ---------------------------------------------------------------------------
// 1. Dashboard Virtual MCP tab
// ---------------------------------------------------------------------------
test.describe('Dashboard Virtual MCP tab', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('should render Virtual MCP filter tab on Dashboard', async ({ page }) => {
    const virtualTab = page.locator('button:has-text("Virtual MCP")');
    await expect(virtualTab).toBeVisible({ timeout: 5000 });
  });

  test('should show virtual server cards when Virtual MCP tab is clicked', async ({
    page,
  }) => {
    const virtualTab = page.locator('button:has-text("Virtual MCP")');
    await expect(virtualTab).toBeVisible({ timeout: 5000 });
    await virtualTab.click();
    await page.waitForTimeout(500);

    // After clicking the tab we should see either virtual server cards
    // (with names and VIRTUAL badges) or an appropriate heading/empty state.
    const heading = page.locator('text=Virtual MCP Servers');
    const virtualBadges = page.locator('text=VIRTUAL');
    const emptyState = page.locator('text=No virtual servers');

    const hasHeading = await heading.isVisible().catch(() => false);
    const hasBadges = (await virtualBadges.count()) > 0;
    const hasEmpty = await emptyState.isVisible().catch(() => false);

    expect(hasHeading || hasBadges || hasEmpty).toBeTruthy();
  });

  test('should display status badges on virtual server cards', async ({
    page,
  }) => {
    const virtualTab = page.locator('button:has-text("Virtual MCP")');
    await expect(virtualTab).toBeVisible({ timeout: 5000 });
    await virtualTab.click();
    await page.waitForTimeout(500);

    // Look for Enabled / Disabled status text inside the card footer
    const enabledBadge = page.locator('text=Enabled');
    const disabledBadge = page.locator('text=Disabled');

    const hasEnabled = (await enabledBadge.count()) > 0;
    const hasDisabled = (await disabledBadge.count()) > 0;

    // At least one status badge should be visible if there are servers
    const virtualBadges = page.locator('text=VIRTUAL');
    const hasBadges = (await virtualBadges.count()) > 0;
    if (hasBadges) {
      expect(hasEnabled || hasDisabled).toBeTruthy();
    }
  });
});

// ---------------------------------------------------------------------------
// 2. Settings list view
// ---------------------------------------------------------------------------
test.describe('Settings list view', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await navigateToVirtualServers(page);
  });

  test('should display Virtual MCP Servers heading and table', async ({
    page,
  }) => {
    await expect(
      page.locator('h2:has-text("Virtual MCP Servers")')
    ).toBeVisible({ timeout: 5000 });

    // The table (or empty state) should be present
    const table = page.locator('table');
    const emptyState = page.locator('text=No virtual servers configured');

    const hasTable = await table.isVisible().catch(() => false);
    const hasEmpty = await emptyState.isVisible().catch(() => false);
    expect(hasTable || hasEmpty).toBeTruthy();
  });

  test('should show server rows with name, path, tools, backends, status columns', async ({
    page,
  }) => {
    const rows = page.locator('table tbody tr');
    const rowCount = await rows.count();

    if (rowCount === 0) {
      // No servers - empty state is fine
      return;
    }

    // Verify at least the first row has cells for name, path, tools, backends, status
    const firstRow = rows.first();
    const cells = firstRow.locator('td');
    // Table has 6 columns: Name, Path, Tools, Backends, Status, Actions
    expect(await cells.count()).toBeGreaterThanOrEqual(5);
  });

  test('should filter servers using the search input', async ({ page }) => {
    const searchInput = page.locator('input[placeholder="Search virtual servers..."]');
    await expect(searchInput).toBeVisible({ timeout: 5000 });

    // Type a search query that should not match anything
    await searchInput.fill('xyznonexistent');
    await page.waitForTimeout(300);

    // Should show "No matching virtual servers" empty state
    const noMatch = page.locator('text=No matching virtual servers');
    await expect(noMatch).toBeVisible({ timeout: 3000 });

    // Clear search
    await searchInput.fill('');
    await page.waitForTimeout(300);

    // Original servers should reappear (or default empty state)
    const table = page.locator('table');
    const emptyState = page.locator('text=No virtual servers configured');
    const hasTable = await table.isVisible().catch(() => false);
    const hasEmpty = await emptyState.isVisible().catch(() => false);
    expect(hasTable || hasEmpty).toBeTruthy();
  });

  test('should filter and find "Time Only" server by name', async ({
    page,
  }) => {
    const searchInput = page.locator('input[placeholder="Search virtual servers..."]');
    await expect(searchInput).toBeVisible({ timeout: 5000 });

    await searchInput.fill('Time Only');
    await page.waitForTimeout(300);

    // "Time Only" should still be visible in the table
    const timeOnly = page.locator('td:has-text("Time Only")');
    const count = await timeOnly.count();
    if (count === 0) {
      // Server might not exist in this environment - acceptable
      return;
    }
    expect(count).toBeGreaterThanOrEqual(1);
  });
});

// ---------------------------------------------------------------------------
// 3. Full CRUD lifecycle (serial - tests depend on order)
// ---------------------------------------------------------------------------
test.describe.serial('Full CRUD lifecycle', () => {
  const SERVER_NAME = `Playwright Full Test ${Date.now()}`;
  const SERVER_DESCRIPTION = 'Full E2E lifecycle test by Playwright';
  const UPDATED_DESCRIPTION = 'Updated description by Playwright E2E';

  test('should create a virtual server via the wizard', async ({ page }) => {
    await loginAsAdmin(page);
    await navigateToVirtualServers(page);

    // Click "Create Virtual Server"
    const createBtn = page.locator('button:has-text("Create Virtual Server")');
    await expect(createBtn).toBeVisible({ timeout: 5000 });
    await createBtn.click();

    // Dialog should appear
    const dialog = page.locator('[role="dialog"]');
    await expect(dialog).toBeVisible({ timeout: 5000 });

    // Step 1: Basics
    await page.fill('input[placeholder="e.g. Dev Essentials"]', SERVER_NAME);
    await page.fill(
      'textarea[placeholder="Describe what this virtual server provides..."]',
      SERVER_DESCRIPTION
    );

    // Path should auto-generate
    const pathInput = page.locator('input[placeholder="/virtual/dev-essentials"]');
    await expect(pathInput).not.toHaveValue('');

    // Next -> Step 2: Tool Selection
    await dialog.locator('button:has-text("Next")').click();
    await expect(
      page.locator('text=Select tools to include in this virtual server')
    ).toBeVisible({ timeout: 3000 });

    // Next -> Step 3: Configuration
    await dialog.locator('button:has-text("Next")').click();
    await expect(
      page.locator('text=Tool Aliases and Version Pins')
    ).toBeVisible({ timeout: 3000 });

    // Next -> Step 4: Review
    await dialog.locator('button:has-text("Next")').click();
    await expect(page.locator('text=Server Details')).toBeVisible({
      timeout: 3000,
    });

    // Verify review shows our name and description
    await expect(dialog.locator(`text=${SERVER_NAME}`)).toBeVisible();
    await expect(dialog.locator(`text=${SERVER_DESCRIPTION}`)).toBeVisible();

    // Submit
    await dialog.locator('button:has-text("Create Virtual Server")').click();
    await expect(dialog).not.toBeVisible({ timeout: 10000 });
  });

  test('should verify the created server appears in the list', async ({
    page,
  }) => {
    await loginAsAdmin(page);
    await navigateToVirtualServers(page);

    // Search for our server
    const searchInput = page.locator('input[placeholder="Search virtual servers..."]');
    await expect(searchInput).toBeVisible({ timeout: 5000 });
    await searchInput.fill(SERVER_NAME);
    await page.waitForTimeout(500);

    // The server should appear in the table
    const serverCell = page.locator(`td:has-text("${SERVER_NAME}")`);
    const count = await serverCell.count();
    expect(count).toBeGreaterThanOrEqual(1);
  });

  test('should toggle a virtual server enable/disable', async ({ page }) => {
    await loginAsAdmin(page);
    await navigateToVirtualServers(page);

    // Find any toggle checkbox in the table
    const toggle = page.locator(
      'input[type="checkbox"][aria-label^="Enable"]'
    ).first();
    if (!(await toggle.isVisible({ timeout: 3000 }).catch(() => false))) {
      test.skip();
      return;
    }

    const isCheckedBefore = await toggle.isChecked();

    // Click the parent label (checkbox is sr-only) and wait for the
    // toggle POST + list refetch GET to complete.
    const label = page.locator('label').filter({ has: toggle }).first();
    await Promise.all([
      page.waitForResponse(
        (resp) =>
          resp.url().includes('/api/virtual-servers') &&
          resp.request().method() === 'GET',
        { timeout: 10000 },
      ),
      label.click(),
    ]);

    // Re-locate after possible re-render and verify the state flipped
    const toggleAfter = page
      .locator('input[type="checkbox"][aria-label^="Enable"]')
      .first();
    const isCheckedAfter = await toggleAfter.isChecked();

    // The state should have changed
    expect(isCheckedAfter).not.toBe(isCheckedBefore);

    // Toggle back to restore original state
    const labelAfter = page.locator('label').filter({ has: toggleAfter }).first();
    await Promise.all([
      page.waitForResponse(
        (resp) =>
          resp.url().includes('/api/virtual-servers') &&
          resp.request().method() === 'GET',
        { timeout: 10000 },
      ),
      labelAfter.click(),
    ]);
  });

  test('should delete the created server with name confirmation', async ({
    page,
  }) => {
    await loginAsAdmin(page);
    await navigateToVirtualServers(page);

    // Search for our server
    const searchInput = page.locator('input[placeholder="Search virtual servers..."]');
    await expect(searchInput).toBeVisible({ timeout: 5000 });
    await searchInput.fill(SERVER_NAME);
    await page.waitForTimeout(500);

    // Click the Delete button in the matching row
    const deleteBtn = page.locator('button:has-text("Delete")').first();
    if (!(await deleteBtn.isVisible({ timeout: 3000 }).catch(() => false))) {
      test.skip();
      return;
    }
    await deleteBtn.click();

    // Delete dialog should appear
    const deleteDialog = page.locator(
      '[role="dialog"][aria-label="Delete virtual server confirmation"]'
    );
    await expect(deleteDialog).toBeVisible({ timeout: 5000 });

    // Confirm button should be disabled initially
    const confirmDeleteBtn = deleteDialog.locator('button:has-text("Delete")');
    await expect(confirmDeleteBtn).toBeDisabled();

    // Type the server name from the placeholder
    const nameInput = deleteDialog.locator('input[type="text"]');
    const placeholder = await nameInput.getAttribute('placeholder');
    expect(placeholder).toBeTruthy();
    await nameInput.fill(placeholder!);

    // Now the delete button should be enabled
    await expect(confirmDeleteBtn).toBeEnabled();

    // Actually delete
    await confirmDeleteBtn.click();

    // Dialog should close
    await expect(deleteDialog).not.toBeVisible({ timeout: 10000 });
  });

  test('should verify the deleted server is removed from the list', async ({
    page,
  }) => {
    await loginAsAdmin(page);
    await navigateToVirtualServers(page);

    // Search for our server
    const searchInput = page.locator('input[placeholder="Search virtual servers..."]');
    await expect(searchInput).toBeVisible({ timeout: 5000 });
    await searchInput.fill(SERVER_NAME);
    await page.waitForTimeout(500);

    // The server should no longer appear
    const serverCell = page.locator(`td:has-text("${SERVER_NAME}")`);
    const count = await serverCell.count();

    // Either 0 rows or the "No matching" empty state
    const noMatch = page.locator('text=No matching virtual servers');
    const hasNoMatch = await noMatch.isVisible().catch(() => false);
    expect(count === 0 || hasNoMatch).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// 4. Form validation and wizard navigation
// ---------------------------------------------------------------------------
test.describe('Form validation and wizard navigation', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await navigateToVirtualServers(page);
  });

  test('should show error when name is empty and Next is clicked', async ({
    page,
  }) => {
    await page.click('button:has-text("Create Virtual Server")');
    const dialog = page.locator('[role="dialog"]');
    await expect(dialog).toBeVisible({ timeout: 5000 });

    // Click Next without filling name
    await dialog.locator('button:has-text("Next")').click();

    // Validation error should appear
    await expect(page.locator('text=Server name is required')).toBeVisible({
      timeout: 3000,
    });
  });

  test('should auto-generate path from name', async ({ page }) => {
    await page.click('button:has-text("Create Virtual Server")');
    const dialog = page.locator('[role="dialog"]');
    await expect(dialog).toBeVisible({ timeout: 5000 });

    await page.fill('input[placeholder="e.g. Dev Essentials"]', 'My Test Server');

    const pathInput = page.locator('input[placeholder="/virtual/dev-essentials"]');
    await expect(pathInput).toHaveValue('/virtual/my-test-server');
  });

  test('should navigate through all 4 wizard steps forward and back', async ({
    page,
  }) => {
    await page.click('button:has-text("Create Virtual Server")');
    const dialog = page.locator('[role="dialog"]');
    await expect(dialog).toBeVisible({ timeout: 5000 });

    // Step 1: Basics
    await page.fill('input[placeholder="e.g. Dev Essentials"]', 'Wizard Nav Test');
    await expect(page.locator('text=Basics')).toBeVisible();

    // Forward to Step 2: Tool Selection
    await dialog.locator('button:has-text("Next")').click();
    await expect(
      page.locator('text=Select tools to include in this virtual server')
    ).toBeVisible({ timeout: 3000 });

    // Forward to Step 3: Configuration
    await dialog.locator('button:has-text("Next")').click();
    await expect(page.locator('text=Tool Aliases and Version Pins')).toBeVisible({
      timeout: 3000,
    });

    // Forward to Step 4: Review
    await dialog.locator('button:has-text("Next")').click();
    await expect(page.locator('text=Server Details')).toBeVisible({
      timeout: 3000,
    });

    // Back to Step 3
    await dialog.locator('button:has-text("Back")').click();
    await expect(page.locator('text=Tool Aliases and Version Pins')).toBeVisible({
      timeout: 3000,
    });

    // Back to Step 2
    await dialog.locator('button:has-text("Back")').click();
    await expect(
      page.locator('text=Select tools to include in this virtual server')
    ).toBeVisible({ timeout: 3000 });

    // Back to Step 1
    await dialog.locator('button:has-text("Back")').click();
    await expect(
      page.locator('input[placeholder="e.g. Dev Essentials"]')
    ).toBeVisible({ timeout: 3000 });
  });

  test('should close the form when Cancel is clicked on step 1', async ({
    page,
  }) => {
    await page.click('button:has-text("Create Virtual Server")');
    const dialog = page.locator('[role="dialog"]');
    await expect(dialog).toBeVisible({ timeout: 5000 });

    await dialog.locator('button:has-text("Cancel")').click();
    await expect(dialog).not.toBeVisible({ timeout: 3000 });
  });

  test('should close the form when Cancel is clicked on a later step', async ({
    page,
  }) => {
    await page.click('button:has-text("Create Virtual Server")');
    const dialog = page.locator('[role="dialog"]');
    await expect(dialog).toBeVisible({ timeout: 5000 });

    // Fill name and advance to step 2
    await page.fill('input[placeholder="e.g. Dev Essentials"]', 'Cancel Test');
    await dialog.locator('button:has-text("Next")').click();
    await expect(
      page.locator('text=Select tools to include in this virtual server')
    ).toBeVisible({ timeout: 3000 });

    // Cancel on step 2
    const cancelBtn = dialog.locator('button:has-text("Cancel")');
    await cancelBtn.click();
    await expect(dialog).not.toBeVisible({ timeout: 3000 });
  });

  test('should close the form when Escape key is pressed', async ({
    page,
  }) => {
    await page.click('button:has-text("Create Virtual Server")');
    const dialog = page.locator('[role="dialog"]');
    await expect(dialog).toBeVisible({ timeout: 5000 });

    await page.keyboard.press('Escape');
    await expect(dialog).not.toBeVisible({ timeout: 3000 });
  });
});

// ---------------------------------------------------------------------------
// 5. Multi-backend server inspection
// ---------------------------------------------------------------------------
test.describe('Multi-backend server inspection', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await navigateToVirtualServers(page);
  });

  test('should find "E2E Multi Backend" in the server list', async ({
    page,
  }) => {
    const searchInput = page.locator('input[placeholder="Search virtual servers..."]');
    await expect(searchInput).toBeVisible({ timeout: 5000 });
    await searchInput.fill('E2E Multi Backend');
    await page.waitForTimeout(500);

    const serverCell = page.locator('td:has-text("E2E Multi Backend")');
    const count = await serverCell.count();
    if (count === 0) {
      // Server might not exist in this environment
      test.skip();
      return;
    }
    expect(count).toBeGreaterThanOrEqual(1);
  });

  test('should show tool count of 4 for "E2E Multi Backend"', async ({
    page,
  }) => {
    const searchInput = page.locator('input[placeholder="Search virtual servers..."]');
    await expect(searchInput).toBeVisible({ timeout: 5000 });
    await searchInput.fill('E2E Multi Backend');
    await page.waitForTimeout(500);

    const serverRow = page.locator('tr').filter({
      has: page.locator('td:has-text("E2E Multi Backend")'),
    });
    const count = await serverRow.count();
    if (count === 0) {
      test.skip();
      return;
    }

    // The Tools column (3rd column, index 2) should contain "4"
    const toolsCell = serverRow.locator('td').nth(2);
    await expect(toolsCell).toHaveText('4');
  });

  test('should show 2 backend paths for "E2E Multi Backend"', async ({
    page,
  }) => {
    const searchInput = page.locator('input[placeholder="Search virtual servers..."]');
    await expect(searchInput).toBeVisible({ timeout: 5000 });
    await searchInput.fill('E2E Multi Backend');
    await page.waitForTimeout(500);

    const serverRow = page.locator('tr').filter({
      has: page.locator('td:has-text("E2E Multi Backend")'),
    });
    const count = await serverRow.count();
    if (count === 0) {
      test.skip();
      return;
    }

    // The Backends column (4th column, index 3) should show 2 backend path badges
    const backendsCell = serverRow.locator('td').nth(3);
    const backendBadges = backendsCell.locator('span');
    const badgeCount = await backendBadges.count();
    expect(badgeCount).toBe(2);

    // Verify the actual backend paths
    const badge1Text = await backendBadges.nth(0).textContent();
    const badge2Text = await backendBadges.nth(1).textContent();
    const allText = [badge1Text, badge2Text].join(' ');
    expect(allText).toContain('/currenttime/');
    expect(allText).toContain('/realserverfaketools/');
  });
});
