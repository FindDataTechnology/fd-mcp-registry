import {
  LOCALE_STORAGE_KEY,
  detectLocale,
  persistLocale,
  readQueryLocale,
  readStoredLocale,
  resolveInitialLocale,
  translate,
} from '../t';

/** In-memory storage double; `throwOnGet`/`throwOnSet` simulate private mode. */
function fakeStorage(initial: Record<string, string> = {}, opts: { throwOnGet?: boolean; throwOnSet?: boolean } = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => {
      if (opts.throwOnGet) {
        throw new Error('denied');
      }
      return map.get(key) ?? null;
    },
    setItem: (key: string, value: string) => {
      if (opts.throwOnSet) {
        throw new Error('denied');
      }
      map.set(key, value);
    },
    removeItem: (key: string) => {
      map.delete(key);
    },
  } as unknown as Storage;
}

describe('detectLocale', () => {
  it.each([
    ['zh', 'zh'],
    ['zh-CN', 'zh'],
    ['zh-Hans', 'zh'],
    ['zh-TW', 'zh'],
    ['ZH-CN', 'zh'],
    ['en-US', 'en'],
    ['fr', 'en'],
    ['', 'en'],
    [undefined, 'en'],
  ])('maps %s to %s', (input, expected) => {
    expect(detectLocale(input as string | undefined)).toBe(expected);
  });
});

describe('readQueryLocale', () => {
  it('accepts zh and zh variants', () => {
    expect(readQueryLocale('?lang=zh')).toBe('zh');
    expect(readQueryLocale('?lang=zh-CN')).toBe('zh');
    expect(readQueryLocale('?foo=1&lang=ZH-hant')).toBe('zh');
  });

  it('accepts en and rejects unknown languages', () => {
    expect(readQueryLocale('?lang=en')).toBe('en');
    expect(readQueryLocale('?lang=fr')).toBeNull();
    expect(readQueryLocale('?other=1')).toBeNull();
    expect(readQueryLocale('')).toBeNull();
  });
});

describe('locale storage', () => {
  it('round-trips a persisted locale', () => {
    const storage = fakeStorage();
    persistLocale('zh', storage);
    expect(readStoredLocale(storage)).toBe('zh');
    expect((storage as unknown as { getItem: (k: string) => string | null }).getItem(LOCALE_STORAGE_KEY)).toBe('zh');
  });

  it('ignores invalid stored values', () => {
    expect(readStoredLocale(fakeStorage({ [LOCALE_STORAGE_KEY]: 'klingon' }))).toBeNull();
  });

  it('degrades gracefully when storage throws', () => {
    expect(readStoredLocale(fakeStorage({}, { throwOnGet: true }))).toBeNull();
    expect(() => persistLocale('zh', fakeStorage({}, { throwOnSet: true }))).not.toThrow();
  });
});

describe('resolveInitialLocale', () => {
  afterEach(() => {
    window.localStorage.clear();
    window.history.replaceState({}, '', '/');
  });

  it('lets ?lang= win and persists it', () => {
    window.history.replaceState({}, '', '/?lang=zh');
    expect(resolveInitialLocale()).toBe('zh');
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('zh');
  });

  it('prefers the stored choice over the browser language', () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh');
    Object.defineProperty(window.navigator, 'language', { value: 'en-US', configurable: true });
    expect(resolveInitialLocale()).toBe('zh');
  });

  it('falls back to the browser language', () => {
    Object.defineProperty(window.navigator, 'language', { value: 'zh-CN', configurable: true });
    expect(resolveInitialLocale()).toBe('zh');
    Object.defineProperty(window.navigator, 'language', { value: 'en-US', configurable: true });
    window.localStorage.clear();
    expect(resolveInitialLocale()).toBe('en');
  });
});

describe('translate', () => {
  it('returns the Chinese entry when present', () => {
    expect(translate('zh', 'Sign out')).toBe('退出登录');
    expect(translate('zh', 'Language')).toBe('语言');
  });

  it('falls back to the source string when no entry exists', () => {
    expect(translate('zh', 'Something never translated')).toBe('Something never translated');
  });

  it('never consults the dictionary for en (source language)', () => {
    expect(translate('en', 'Sign out')).toBe('Sign out');
  });

  it('interpolates vars and leaves unknown placeholders intact', () => {
    expect(translate('en', '{count} widgets', { count: 3 })).toBe('3 widgets');
    expect(translate('en', '{count} widgets')).toBe('{count} widgets');
    expect(translate('en', '{a} of {b}', { a: 1 })).toBe('1 of {b}');
    // Untranslated zh source strings still interpolate the fallback text.
    expect(translate('zh', '{count} widgets', { count: 3 })).toBe('3 widgets');
  });

  it('honours the context segment with a graceful fallback chain', () => {
    // No scoped entry exists for this pair yet: scoped miss -> plain entry.
    expect(translate('zh', 'Language', undefined, 'footer')).toBe('语言');
    // No scoped or plain entry: -> source string.
    expect(translate('zh', 'Untranslated', undefined, 'footer')).toBe('Untranslated');
  });
});