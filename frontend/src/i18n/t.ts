/**
 * Minimal translation layer for the registry console.
 *
 * Source strings in the code ARE the keys: the English text stays readable in
 * place, translators get a flat word list, and t() falls back to the source
 * verbatim whenever the target language has no entry (or a dictionary failed
 * to load). That makes any partial translation shippable.
 *
 * Homographs share one English spelling but need different wording per spot;
 * those use an explicit context segment, so `t('Server', undefined, 'nav')`
 * looks up the key `Server::nav` first and falls back to the plain `Server`
 * entry (then to the source string) when no scoped entry exists.
 */
import common from './locales/common.zh.json';
import shell from './locales/shell.zh.json';
import discover from './locales/discover.zh.json';
import forms from './locales/forms.zh.json';

export type Locale = 'zh' | 'en';

/** localStorage key holding the user's explicit choice. */
export const LOCALE_STORAGE_KEY = 'registry.lang';

/** URL query parameter that overrides and then persists the language. */
export const LOCALE_QUERY_PARAM = 'lang';

/** Title the app falls back to before/without a server-provided ui_title. */
export const DEFAULT_APP_TITLE = 'AI Gateway & Registry';

export type TranslateVars = Record<string, string | number>;
export type TranslateFn = (source: string, vars?: TranslateVars, context?: string) => string;

const ZH: Record<string, string> = { ...common, ...shell, ...discover, ...forms };

export function isLocale(value: unknown): value is Locale {
  return value === 'zh' || value === 'en';
}

/** zh* browser languages (zh, zh-CN, zh-Hans, zh-TW...) map to zh; all else en. */
export function detectLocale(navigatorLanguage: string | undefined): Locale {
  return (navigatorLanguage ?? '').toLowerCase().startsWith('zh') ? 'zh' : 'en';
}

export function readStoredLocale(storage: Storage | undefined = _defaultStorage()): Locale | null {
  try {
    const raw = storage?.getItem(LOCALE_STORAGE_KEY);
    return isLocale(raw) ? raw : null;
  } catch {
    return null;
  }
}

export function persistLocale(locale: Locale, storage: Storage | undefined = _defaultStorage()): void {
  try {
    storage?.setItem(LOCALE_STORAGE_KEY, locale);
  } catch {
    // Private-mode / quota failures must not break the console.
  }
}

/** `?lang=` override. Accepts en and any zh variant (zh-CN, zh-Hant...). */
export function readQueryLocale(search: string): Locale | null {
  const raw = new URLSearchParams(search).get(LOCALE_QUERY_PARAM);
  if (!raw) {
    return null;
  }
  const normalized = raw.toLowerCase();
  if (normalized === 'en') {
    return 'en';
  }
  if (normalized === 'zh' || normalized.startsWith('zh-')) {
    return 'zh';
  }
  return null;
}

/** Query override > stored choice > browser language. A query hit persists. */
export function resolveInitialLocale(): Locale {
  const fromQuery = readQueryLocale(window.location.search);
  if (fromQuery) {
    persistLocale(fromQuery);
    return fromQuery;
  }
  return readStoredLocale() ?? detectLocale(typeof navigator === 'undefined' ? undefined : navigator.language);
}

export function translate(locale: Locale, source: string, vars?: TranslateVars, context?: string): string {
  if (locale !== 'zh') {
    return _interpolate(source, vars);
  }
  const scoped = context ? `${source}::${context}` : source;
  return _interpolate(ZH[scoped] ?? ZH[source] ?? source, vars);
}

function _defaultStorage(): Storage | undefined {
  try {
    return typeof window === 'undefined' ? undefined : window.localStorage;
  } catch {
    return undefined;
  }
}

/** Replace `{name}` placeholders; unknown placeholders stay untouched. */
function _interpolate(template: string, vars?: TranslateVars): string {
  if (!vars) {
    return template;
  }
  return template.replace(/\{(\w+)\}/g, (match: string, name: string) =>
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match,
  );
}