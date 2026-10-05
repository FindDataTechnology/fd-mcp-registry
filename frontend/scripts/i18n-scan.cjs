#!/usr/bin/env node
/**
 * Translation coverage scanner for the console i18n layer.
 *
 * Walks the customer-facing file set (see COVERAGE below), parses each file,
 * and reports two kinds of gaps:
 *   - declared: `t('...')` keys that have no entry in the zh dictionaries
 *   - unwrapped: user-visible JSX text / placeholder/title/aria-label literals
 *     that are not passed through t() at all (they render English in every locale)
 *
 * Usage:
 *   node scripts/i18n-scan.cjs [--max-missing <pct>] [--json] [--quiet]
 *
 * Exit status: 0 when every critical file is fully covered and the overall
 * missing rate is within the limit; 1 otherwise (or on read errors).
 */
'use strict';

const fs = require('fs');
const path = require('path');
const ts = require('typescript');

const ROOT = path.join(__dirname, '..');
const SRC = path.join(ROOT, 'src');
const LOCALE_DIR = path.join(SRC, 'i18n', 'locales');

/** Files that make up the external-customer experience, grouped by dictionary domain. */
const COVERAGE = [
  {
    domain: 'shell',
    critical: ['Login.tsx', 'Layout.tsx', 'Sidebar.tsx', 'LanguageSwitcher.tsx', 'Logout.tsx'],
    files: [
      'src/pages/Login.tsx',
      'src/pages/Logout.tsx',
      'src/pages/OAuthCallback.tsx',
      'src/components/Layout.tsx',
      'src/components/Sidebar.tsx',
      'src/components/ThemeToggle.tsx',
      'src/components/LanguageSwitcher.tsx',
      'src/components/UptimeDisplay.tsx',
    ],
  },
  {
    domain: 'discover',
    critical: [
      'Dashboard.tsx',
      'ServerCard.tsx',
      'AgentCard.tsx',
      'SkillCard.tsx',
      'DetailsModal.tsx',
      'AgentDetailsModal.tsx',
      'ServerConfigModal.tsx',
      'ProxyConnectButton.tsx',
      'DiscoverTab.tsx',
      'SemanticSearchResults.tsx',
    ],
    files: [
      'src/pages/Dashboard.tsx',
      'src/components/DiscoverTab.tsx',
      'src/components/DiscoverListRow.tsx',
      'src/components/SemanticSearchResults.tsx',
      'src/components/Pagination.tsx',
      'src/components/SearchableSelect.tsx',
      'src/components/StarRatingWidget.tsx',
      'src/components/DeleteConfirmation.tsx',
      'src/components/ServerCard.tsx',
      'src/components/AgentCard.tsx',
      'src/components/SkillCard.tsx',
      'src/components/CustomEntityCard.tsx',
      'src/components/VirtualServerCard.tsx',
      'src/components/VirtualServerList.tsx',
      'src/components/DetailsModal.tsx',
      'src/components/AgentDetailsModal.tsx',
      'src/components/ServerConfigModal.tsx',
      'src/components/ProxyConnectButton.tsx',
      'src/components/ANSBadge.tsx',
      'src/components/VersionSelectorModal.tsx',
      'src/components/ToolSelector.tsx',
      'src/components/entities/sections/ExternalRegistriesSection.tsx',
      'src/components/CustomEntityTab.tsx',
      'src/components/CustomEntityDetail.tsx',
      'src/components/CustomEntityForm.tsx',
      'src/components/cards/CardBody.tsx',
      'src/components/cards/CardFooter.tsx',
      'src/components/cards/CardHeader.tsx',
      'src/components/cards/CardShell.tsx',
      'src/components/cards/CardStatsRow.tsx',
      'src/components/cards/InlineDeleteConfirm.tsx',
      'src/components/cards/StatusDot.tsx',
      'src/components/cards/TagList.tsx',
      'src/components/cards/ToggleSwitch.tsx',
      'src/components/modals/CopyButton.tsx',
      'src/components/modals/CollapsibleSection.tsx',
      'src/components/modals/EntityModal.tsx',
      'src/components/modals/FieldReferenceGrid.tsx',
    ],
  },
  {
    domain: 'forms',
    critical: ['RegisterPage.tsx', 'TokenGeneration.tsx'],
    files: [
      'src/pages/RegisterPage.tsx',
      'src/pages/TokenGeneration.tsx',
      'src/pages/ConnectedAccountsPage.tsx',
      'src/components/AddRegistryEntryModal.tsx',
      'src/components/DuplicateCheckModal.tsx',
      'src/components/PullCardPreviewModal.tsx',
      'src/components/LocalRuntimeFormPanel.tsx',
      'src/components/formFields/AuthSchemeFields.tsx',
      'src/components/formFields/DiscoveryIdentityFields.tsx',
      'src/components/formFields/FormField.tsx',
      'src/components/formFields/MetadataField.tsx',
      'src/components/formFields/OAuthClientCredentialsFields.tsx',
      'src/components/formFields/ProxyField.tsx',
      'src/components/formFields/StatusField.tsx',
      'src/components/formFields/TagsField.tsx',
      'src/components/formFields/UpstreamHeadersField.tsx',
      'src/components/formFields/VisibilityField.tsx',
    ],
  },
];

/** Acronyms, product names, commands and data-shaped strings that are not translation targets. */
const IGNORE_STRINGS = new Set([
  'MCP', 'JSON', 'JWT', 'URL', 'URLs', 'API', 'APIs', 'ID', 'IDs', 'OAuth', 'OIDC', 'SSE', 'HTTP',
  'HTTPS', 'YAML', 'CLI', 'CPU', 'RAM', 'GitHub', 'Logto', 'AWS', 'GCP', 'OK', 'AI', 'UI', 'UX',
  'Admin', 'English', '简体中文', 'npx', 'docker', 'uvx', 'px', 'env', 'ps aux', 'ACME Inc.',
  'Google', 'Atlassian', 'Microsoft', 'Slack', 'curl', 'bash', 'npm', 'SKILL.md',
]);

/** URLs, JSON samples, placeholders, env assignments - code, not copy. */
const CODE_ISH = /(:\/\/|^[{[<]|[=])/;

/** Comma-separated lowercase token lists ("tag1, tag2", "ai, nlp"). */
const TOKEN_LIST = /^[a-z0-9][a-z0-9._-]*(, ?[a-z0-9._-]+)+$/;

/** At least two latin letters and either a space or a lowercase letter. */
function isTranslatableText(raw) {
  const text = raw.replace(/\s+/g, ' ').trim();
  if (!text || IGNORE_STRINGS.has(text)) {
    return false;
  }
  if (/^&[a-z]+;$/.test(text)) {
    return false;
  }
  if ((text.match(/[A-Za-z]/g) || []).length < 2) {
    return false;
  }
  if (CODE_ISH.test(text) || TOKEN_LIST.test(text)) {
    return false;
  }
  // Underscores and glued colons (`read:user`) mark identifiers, not copy.
  if (text.includes('_') || /\S:\S/.test(text)) {
    return false;
  }
  if (!/\s/.test(text)) {
    // Single tokens: paths, filenames, header names, host names, commands.
    if (/[\/\\.:@-]/.test(text)) {
      return false;
    }
    if (!/[a-z]/.test(text)) {
      return false;
    }
  }
  return true;
}

/** JSX elements whose text is code/sample data rather than console copy. */
const CODE_TAGS = new Set(['code', 'pre', 'kbd', 'samp', 'textarea']);

/** Collect t('...') keys and unwrapped user-visible strings from one source file. */
function extractFromSource(fileName, sourceText) {
  const declared = [];
  const unwrapped = [];
  const sourceFile = ts.createSourceFile(fileName, sourceText, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

  const visit = (node, inCode) => {
    let code = inCode;
    if (ts.isJsxElement(node) && CODE_TAGS.has(node.openingElement.tagName.getText())) {
      code = true;
    }
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 't') {
      const first = node.arguments[0];
      if (first && ts.isStringLiteralLike(first)) {
        declared.push(first.text);
      }
    }
    if (ts.isJsxText(node) && !code) {
      if (isTranslatableText(node.text)) {
        unwrapped.push(node.text.replace(/\s+/g, ' ').trim());
      }
    }
    if (
      ts.isJsxAttribute(node) &&
      node.initializer &&
      ts.isStringLiteral(node.initializer) &&
      ['placeholder', 'title', 'aria-label'].includes(node.name.getText())
    ) {
      if (isTranslatableText(node.initializer.text)) {
        unwrapped.push(node.initializer.text);
      }
    }
    ts.forEachChild(node, (child) => visit(child, code));
  };

  visit(sourceFile, false);
  return { declared, unwrapped };
}

function loadDictionaries() {
  const merged = new Set();
  for (const name of ['common.zh.json', 'shell.zh.json', 'discover.zh.json', 'forms.zh.json']) {
    const file = path.join(LOCALE_DIR, name);
    const dict = JSON.parse(fs.readFileSync(file, 'utf8'));
    for (const key of Object.keys(dict)) {
      merged.add(key);
    }
  }
  return merged;
}

function scan() {
  const dict = loadDictionaries();
  const rows = [];
  for (const group of COVERAGE) {
    for (const rel of group.files) {
      const abs = path.join(ROOT, rel);
      if (!fs.existsSync(abs)) {
        rows.push({ rel, domain: group.domain, critical: false, missingDeclared: [], unwrapped: [], skippedMissing: true });
        continue;
      }
      const { declared, unwrapped } = extractFromSource(abs, fs.readFileSync(abs, 'utf8'));
      const missingDeclared = [...new Set(declared.filter((key) => !dict.has(key)))];
      rows.push({
        rel,
        domain: group.domain,
        critical: group.critical.includes(path.basename(rel)),
        declared: declared.length,
        missingDeclared,
        unwrapped,
      });
    }
  }
  return rows;
}

function summarize(rows) {
  const perDomain = {};
  for (const row of rows) {
    const bucket = (perDomain[row.domain] ??= { total: 0, missing: 0, files: 0, missingFiles: 0 });
    const declaredCount = typeof row.declared === 'number' ? row.declared : 0;
    const missing = row.missingDeclared.length + row.unwrapped.length;
    bucket.total += declaredCount + row.unwrapped.length;
    bucket.missing += missing;
    bucket.files += 1;
    if (missing > 0) {
      bucket.missingFiles += 1;
    }
  }
  const totalItems = rows.reduce((acc, row) => acc + (typeof row.declared === 'number' ? row.declared : 0) + row.unwrapped.length, 0);
  const totalMissing = rows.reduce((acc, row) => acc + row.missingDeclared.length + row.unwrapped.length, 0);
  return { perDomain, totalItems, totalMissing };
}

function main() {
  const args = process.argv.slice(2);
  const json = args.includes('--json');
  const quiet = args.includes('--quiet');
  const maxIdx = args.indexOf('--max-missing');
  const maxMissingPct = maxIdx >= 0 ? Number(args[maxIdx + 1]) : 5;

  const rows = scan();
  const { perDomain, totalItems, totalMissing } = summarize(rows);
  const overallPct = totalItems === 0 ? 0 : (totalMissing / totalItems) * 100;
  const criticalGaps = rows.filter((row) => row.critical && (row.missingDeclared.length > 0 || row.unwrapped.length > 0));

  if (json) {
    console.log(JSON.stringify({ rows, perDomain, totalItems, totalMissing, overallPct, criticalGaps: criticalGaps.map((r) => r.rel) }, null, 2));
  } else if (!quiet) {
    console.log('Translation coverage (customer-facing files)\n');
    for (const [domain, stats] of Object.entries(perDomain)) {
      const pct = stats.total === 0 ? 0 : (stats.missing / stats.total) * 100;
      console.log(
        `  ${domain.padEnd(9)} ${String(stats.total - stats.missing).padStart(4)}/${String(stats.total).padEnd(4)} covered` +
          `  (${stats.missing} gaps in ${stats.missingFiles}/${stats.files} files, ${pct.toFixed(1)}% missing)`,
      );
    }
    console.log(`\n  overall: ${totalItems - totalMissing}/${totalItems} covered, ${overallPct.toFixed(1)}% missing (limit ${maxMissingPct}%)`);
    for (const row of rows) {
      if (row.missingDeclared.length === 0 && row.unwrapped.length === 0) {
        continue;
      }
      console.log(`\n  ${row.rel}${row.critical ? ' [critical]' : ''}`);
      for (const key of row.missingDeclared) {
        console.log(`    untranslated key: ${key}`);
      }
      for (const text of row.unwrapped) {
        console.log(`    unwrapped text:   ${text}`);
      }
    }
  }

  const failed = criticalGaps.length > 0 || overallPct > maxMissingPct;
  if (failed && !quiet && !json) {
    console.log('\nRESULT: FAIL');
    if (criticalGaps.length > 0) {
      console.log(`  critical files with gaps: ${criticalGaps.map((r) => r.rel).join(', ')}`);
    }
  } else if (!quiet && !json) {
    console.log('\nRESULT: PASS');
  }
  process.exit(failed ? 1 : 0);
}

if (require.main === module) {
  main();
}

module.exports = { COVERAGE, extractFromSource, isTranslatableText, loadDictionaries, scan, summarize };