import path from 'path';

// The scanner is a plain CommonJS script (it is also the `npm run i18n:scan`
// CLI), so pull it in through require to exercise the same code path.
const scanner = require(path.join(__dirname, '..', '..', '..', 'scripts', 'i18n-scan.cjs'));

const { extractFromSource, isTranslatableText, loadDictionaries } = scanner as {
  extractFromSource: (file: string, source: string) => { declared: string[]; unwrapped: string[] };
  isTranslatableText: (text: string) => boolean;
  loadDictionaries: () => Set<string>;
};

describe('isTranslatableText', () => {
  it.each([
    ['Sign out', true],
    ['Discover', true],
    ['admin', true],
    ['MCP', false],
    ['JSON', false],
    ['—', false],
    ['1', false],
    ['x', false],
    ['', false],
  ])('classifies %s as %s', (text, expected) => {
    expect(isTranslatableText(text as string)).toBe(expected);
  });
});

describe('extractFromSource', () => {
  const source = `
    export const C: React.FC = () => (
      <div title="Untranslated tooltip">
        <button>{t('Save')}</button>
        <span>No items found</span>
        <input placeholder="Search items" />
        <span>{t('Saved {count} items', { count: 2 })}</span>
      </div>
    );
  `;

  it('collects declared t() keys', () => {
    const { declared } = extractFromSource('fixture.tsx', source);
    expect(declared).toContain('Save');
    expect(declared).toContain('Saved {count} items');
  });

  it('collects unwrapped JSX text and attribute literals', () => {
    const { unwrapped } = extractFromSource('fixture.tsx', source);
    expect(unwrapped).toContain('No items found');
    expect(unwrapped).toContain('Search items');
    expect(unwrapped).toContain('Untranslated tooltip');
  });

  it('does not flag strings that go through t()', () => {
    const { unwrapped } = extractFromSource('fixture.tsx', source);
    expect(unwrapped).not.toContain('Save');
  });
});

describe('loadDictionaries', () => {
  it('merges the zh dictionaries and includes shell entries', () => {
    expect(loadDictionaries().has('Sign out')).toBe(true);
  });
});