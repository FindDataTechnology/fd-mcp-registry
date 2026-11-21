import React from 'react';

/**
 * CatalogDeprecationBanner — the registry console's catalogue screens are an
 * execution-plane view, not the product's material inventory.
 *
 * unify-supply-spine-lineage 3.4 (2026-11-21): the supply domain's front door
 * owns the one catalogue presentation; this console keeps its browsing screens
 * because operators need to see the execution plane's real rows (including
 * unlisted entries and probe state), but a customer must not arrive here and
 * find a second, differently-shaped inventory. The banner names where the
 * product's inventory lives and nothing more — it is not dismissible, because
 * "this is not the product catalogue" is a standing fact, not a one-time tip.
 *
 * The link target is the supply domain's canonical front door. It is a plain
 * anchor (not the SPA router) because it leaves the registry host entirely.
 */
const CATALOG_URL = 'https://facet.finddatatech.cloud';

export const CatalogDeprecationBanner: React.FC = () => (
  <div
    role="note"
    data-testid="catalog-deprecation-banner"
    className="mx-4 mt-4 rounded-md border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200"
  >
    <span className="font-medium">这是执行面视图，不是素材目录。</span>{' '}
    对外展示的素材清单以供给域正门为准 ——{' '}
    <a className="underline" href={CATALOG_URL} target="_blank" rel="noopener noreferrer">
      facet.finddatatech.cloud
    </a>
    。此处保留服务端 / 技能 / 代理的真实注册条目，供排障与运维核对。
  </div>
);

export default CatalogDeprecationBanner;
