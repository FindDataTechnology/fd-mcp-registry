import { test, expect, type Page } from '@playwright/test';

/**
 * Chinese smoke coverage for the console i18n layer.
 *
 * Self-contained: every /api call the console makes is fulfilled from page
 * routes (auth shim + deterministic fixtures), so the three specs run against
 * ANY target without credentials or a backend —
 *   PLAYWRIGHT_BASE_URL=https://<deploy> npx playwright test e2e/i18n.spec.ts
 * also works against `npm run start` or the compose stack. Production is
 * never touched: the routes intercept at the browser level.
 *
 * Covers the paths the change gates on: dashboard first paint, the login page
 * with the switcher and persistence, and the connect modal.
 */

const ADMIN_PROFILE = {
  username: 'i18n-probe',
  email: 'i18n-probe@local',
  auth_method: 'oauth2',
  provider: 'logto',
  scopes: ['mcp-registry-admin'],
  groups: ['mcp-registry-admin'],
  can_modify_servers: true,
  is_admin: true,
  ui_permissions: {
    list_service: ['all'],
    register_service: ['all'],
    health_check_service: ['all'],
    toggle_service: ['all'],
    modify_service: ['all'],
    list_agents: ['all'],
    get_agent: ['all'],
    publish_agent: ['all'],
    modify_agent: ['all'],
    delete_agent: ['all'],
  },
  accessible_servers: ['*'],
  accessible_services: ['all'],
  accessible_agents: ['all'],
};

// Backend row shape: the stats context maps display_name/is_enabled/
// health_status into the frontend Server model, so the fixture uses those.
const SMOKE_SERVER = {
  display_name: 'Smoke Server',
  path: '/smoke-server/',
  is_enabled: true,
  health_status: 'healthy',
  tags: [],
  rating_details: [],
  num_tools: 9,
  last_checked_iso: new Date().toISOString(),
};

const json = (body: unknown) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(body),
});

/** Fulfil every console API from fixtures; register this one FIRST (lowest precedence). */
async function shimApi(page: Page): Promise<void> {
  await page.route('**/api/**', async (route) => {
    if (route.request().method() !== 'GET') {
      return route.fulfill(json({}));
    }
    const path = new URL(route.request().url()).pathname;
    const plural = path.match(/\/([a-z-]+)s\/?$/) ? '[]' : '{}';
    return route.fulfill(json(plural === '[]' ? [] : {}));
  });
  await page.route('**/api/auth/me', (route) => route.fulfill(json(ADMIN_PROFILE)));
  await page.route('**/api/version', (route) =>
    route.fulfill(json({ version: 'fd-e2e', ui_title: 'AI Gateway & Registry' })),
  );
  await page.route('**/api/config', (route) =>
    route.fulfill(
      json({
        features: { mcp_servers: true, agents: true, skills: true, virtual_servers: true, federation: true },
      }),
    ),
  );
  await page.route('**/api/servers**', (route) => route.fulfill(json({ servers: [SMOKE_SERVER] })));
  await page.route('**/api/agents**', (route) => route.fulfill(json({ agents: [] })));
  await page.route('**/api/skills**', (route) => route.fulfill(json({ skills: [] })));
  await page.route('**/api/virtual-servers**', (route) => route.fulfill(json({ servers: [] })));
  await page.route('**/api/stats', (route) =>
    route.fulfill(
      json({
        uptime_seconds: 3600,
        started_at: new Date().toISOString(),
        version: 'fd-e2e',
        database_status: { status: 'healthy', backend: 'mongodb', host: 'e2e' },
        auth_status: { status: 'healthy', provider: 'logto', url: 'https://e2e' },
        deployment_type: 'compose',
        deployment_mode: 'local',
        registry_stats: { servers: 1, agents: 0, skills: 0 },
      }),
    ),
  );
}

test.describe('console i18n (zh)', () => {
  test('dashboard first paint is Chinese and html lang follows', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('registry.lang', 'zh'));
    await shimApi(page);
    await page.goto('/?lang=zh');
    await page.waitForLoadState('domcontentloaded');

    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
    // Sidebar (shell) copy is Chinese, the switcher shows the native name, and
    // the header brand renders the translated stock ui_title (fd-1.1.1).
    await expect(page.getByText('统计')).toBeVisible();
    await expect(page.getByText('全部')).toBeVisible();
    await expect(page.getByRole('button', { name: '语言' })).toBeVisible();
    await expect(page.getByRole('link', { name: /AI 网关与注册处/ })).toBeVisible();
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
    await page.addInitScript(() => localStorage.setItem('registry.lang', 'zh'));
    await shimApi(page);
    await page.goto('/?lang=zh');
    await page.waitForLoadState('domcontentloaded');

    // The admin profile sees the servers tab; open it, then the connect modal.
    await page.getByRole('button', { name: 'MCP Server', exact: true }).click();
    const connect = page.getByRole('button', { name: '连接到Smoke Server' });
    await expect(connect).toBeVisible({ timeout: 15000 });
    await connect.click();
    await expect(page.getByRole('heading', { name: 'Smoke Server 的 MCP 配置' })).toBeVisible();
  });
});