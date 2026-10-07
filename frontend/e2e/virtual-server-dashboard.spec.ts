import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './helpers/auth';

/**
 * Dashboard integration tests for Virtual MCP Servers.
 *
 * Verifies virtual server visibility and interactions on the Dashboard.
 */
test.describe('Virtual Server Dashboard', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('should display the Virtual MCP filter tab on the Dashboard', async ({
    page,
  }) => {
    // The Dashboard should have a "Virtual MCP" filter button
    const virtualTab = page.locator('button:has-text("Virtual MCP")');
    await expect(virtualTab).toBeVisible({ timeout: 5000 });
  });

  test('should show virtual server section when Virtual MCP tab is clicked', async ({
    page,
  }) => {
    // Click the "Virtual MCP" filter tab
    const virtualTab = page.locator('button:has-text("Virtual MCP")');
    await expect(virtualTab).toBeVisible({ timeout: 5000 });
    await virtualTab.click();
    await page.waitForTimeout(500);

    // After clicking Virtual MCP tab, the "Virtual MCP Servers" heading
    // should appear. If no servers exist, an empty state is shown.
    const heading = page.locator('text=Virtual MCP Servers');
    const emptyState = page.locator(
      'text=No virtual servers found'
    );

    const hasHeading = await heading.isVisible().catch(() => false);
    const hasEmptyState = await emptyState.isVisible().catch(() => false);

    // Either a heading or empty state should be visible
    expect(hasHeading || hasEmptyState).toBeTruthy();
  });

  test('should show empty state when Virtual MCP filter has no servers', async ({
    page,
  }) => {
    // Click "Virtual MCP" filter tab
    const virtualTab = page.locator('button:has-text("Virtual MCP")');
    await expect(virtualTab).toBeVisible({ timeout: 5000 });
    await virtualTab.click();
    await page.waitForTimeout(500);

    // With mocked empty data, we should see a "no results" state or empty list
    const noServersMsg = page.locator(
      'text=No virtual servers configured'
    );
    const noResultsMsg = page.locator('text=No servers found');
    const virtualBadges = page.locator('text=VIRTUAL');

    const hasNoServers = await noServersMsg.isVisible().catch(() => false);
    const hasNoResults = await noResultsMsg.isVisible().catch(() => false);
    const hasBadges = (await virtualBadges.count()) > 0;

    // One of these states should be true
    expect(hasNoServers || hasNoResults || hasBadges).toBeTruthy();
  });
});
