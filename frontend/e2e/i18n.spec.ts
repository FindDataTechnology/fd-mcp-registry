import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './helpers/auth';

/**
 * Chinese smoke coverage for the console i18n layer.
 *
 * The app follows the stored choice first, so `localStorage.registry.lang` (or
 * `?lang=`) drives the language deterministically. These three specs cover the
 * paths the change gates on: first paint, the login page with the switcher and
 * persistence, and the connect modal.
 *
 * Requires the compose stack (nginx on :80); run with `npx playwright test e2e/i18n.spec.ts`.
 */
const ZH_STORAGE = () => localStorage.setItem('registry.lang', 'zh');

test.describe('console i18n (zh)', () => {
  test('dashboard first paint is Chinese and html lang follows', async ({ page }) => {
    await page.addInitScript(ZH_STORAGE);
    await loginAsAdmin(page);

    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
    // Sidebar (shell) copy is Chinese and the switcher shows the native name.
    await expect(page.getByText('统计')).toBeVisible();
    await expect(page.getByText('全部')).toBeVisible();
    await expect(page.getByRole('button', { name: '语言' })).toBeVisible();
  });

  test('login page: ?lang=zh renders Chinese, the switcher flips to English and persists', async ({ page }) => {
    await page.goto('/?lang=zh');

    // Unauthenticated visits land on the login page (client-side redirect).
    await expect(page.getByText('进入你的 AI 管理控制台')).toBeVisible();
    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
    expect(await page.evaluate(() => localStorage.getItem('registry.lang'))).toBe('zh');

    // Switch to English through the header switcher.
    await page.getByRole('button', { name: '语言' }).click();
    await page.getByRole('menuitem', { name: 'English' }).click();
    await expect(page.getByText('Access your AI management dashboard')).toBeVisible();
    await expect(page.locator('html')).toHaveAttribute('lang', 'en');

    // The choice survives a reload.
    await page.reload();
    await expect(page.getByText('Access your AI management dashboard')).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('registry.lang'))).toBe('en');
  });

  test('connect modal shows Chinese copy', async ({ page }) => {
    await page.addInitScript(ZH_STORAGE);
    await loginAsAdmin(page);

    // Serve one server for the dashboard list (registered after the auth helper
    // so this handler wins on the reload below).
    await page.route('**/api/servers**', async (route) => {
      if (route.request().method() !== 'GET') {
        return route.fallback();
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          servers: [
            {
              name: 'Smoke Server',
              path: '/smoke-server/',
              enabled: true,
              status: 'healthy',
              tags: [],
              rating_details: [],
            },
          ],
        }),
      });
    });
    await page.reload();

    const connect = page.getByRole('button', { name: '连接到Smoke Server' });
    await expect(connect).toBeVisible({ timeout: 15000 });
    await connect.click();
    await expect(page.getByRole('heading', { name: 'Smoke Server 的 MCP 配置' })).toBeVisible();
  });
});