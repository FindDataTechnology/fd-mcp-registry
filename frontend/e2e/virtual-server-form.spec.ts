import { test, expect } from '@playwright/test';
import { loginAsAdmin, navigateToVirtualServers } from './helpers/auth';

/**
 * Form validation and wizard navigation tests for the Virtual Server form.
 *
 * Covers: required field validation, wizard step navigation, cancel/escape
 * behavior, and form auto-generation (path from name).
 */
test.describe('Virtual Server Form Validation', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await navigateToVirtualServers(page);
  });

  test('should show validation error when name is empty and Next is clicked', async ({
    page,
  }) => {
    // Open create form
    await page.click('button:has-text("Create Virtual Server")');
    const dialog = page.locator('[role="dialog"]');
    await expect(dialog).toBeVisible({ timeout: 5000 });

    // Name field should be empty by default; click Next
    await dialog.locator('button:has-text("Next")').click();

    // A validation error should appear
    await expect(page.locator('text=Server name is required')).toBeVisible({
      timeout: 3000,
    });
  });

  test('should auto-generate path from name', async ({ page }) => {
    await page.click('button:has-text("Create Virtual Server")');
    const dialog = page.locator('[role="dialog"]');
    await expect(dialog).toBeVisible({ timeout: 5000 });

    // Type a name
    await page.fill('input[placeholder="e.g. Dev Essentials"]', 'My Test Server');

    // The path should be auto-generated
    const pathInput = page.locator('input[placeholder="/virtual/dev-essentials"]');
    await expect(pathInput).toHaveValue('/virtual/my-test-server');
  });

  test('should navigate through all wizard steps', async ({ page }) => {
    await page.click('button:has-text("Create Virtual Server")');
    const dialog = page.locator('[role="dialog"]');
    await expect(dialog).toBeVisible({ timeout: 5000 });

    // Step 1: Basics - fill required fields
    await page.fill('input[placeholder="e.g. Dev Essentials"]', 'Wizard Nav Test');
    await expect(page.locator('text=Basics')).toBeVisible();

    // Go to step 2: Tool Selection
    await dialog.locator('button:has-text("Next")').click();
    await expect(
      page.locator('text=Select tools to include in this virtual server')
    ).toBeVisible({ timeout: 3000 });

    // Go to step 3: Configuration
    await dialog.locator('button:has-text("Next")').click();
    await expect(page.locator('text=Tool Aliases and Version Pins')).toBeVisible({
      timeout: 3000,
    });

    // Go to step 4: Review
    await dialog.locator('button:has-text("Next")').click();
    await expect(page.locator('text=Server Details')).toBeVisible({
      timeout: 3000,
    });

    // Go back to step 3 (footer left button says "Back")
    await dialog.locator('button:has-text("Back")').click();
    await expect(page.locator('text=Tool Aliases and Version Pins')).toBeVisible({
      timeout: 3000,
    });

    // Go back to step 2
    await dialog.locator('button:has-text("Back")').click();
    await expect(
      page.locator('text=Select tools to include in this virtual server')
    ).toBeVisible({ timeout: 3000 });

    // Go back to step 1
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

    // On step 1, the left footer button says "Cancel"
    await dialog.locator('button:has-text("Cancel")').click();

    // Dialog should close
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

    // On step 2+, there is a text "Cancel" button in the footer right area
    const cancelBtn = dialog.locator('button:has-text("Cancel")');
    await cancelBtn.click();

    // Dialog should close
    await expect(dialog).not.toBeVisible({ timeout: 3000 });
  });

  test('should close the form when Escape key is pressed', async ({
    page,
  }) => {
    await page.click('button:has-text("Create Virtual Server")');
    const dialog = page.locator('[role="dialog"]');
    await expect(dialog).toBeVisible({ timeout: 5000 });

    // Press Escape
    await page.keyboard.press('Escape');

    // Dialog should close
    await expect(dialog).not.toBeVisible({ timeout: 3000 });
  });
});
