import React, { createContext, useCallback, useContext, useEffect, useMemo, useState, ReactNode } from 'react';
import {
  DEFAULT_APP_TITLE,
  type Locale,
  type TranslateFn,
  type TranslateVars,
  persistLocale,
  resolveInitialLocale,
  translate,
} from '../i18n/t';

interface I18nContextType {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: TranslateFn;
}

const I18nContext = createContext<I18nContextType>({
  locale: 'en',
  setLocale: () => undefined,
  t: ((source: string, vars?: TranslateVars, context?: string) =>
    translate('en', source, vars, context)) as TranslateFn,
});

/**
 * Components may render outside the provider (bare unit tests, isolated
 * mounts). They then render English source strings instead of throwing, which
 * mirrors the translation fallback rule and keeps such tests meaningful.
 */
export const useI18n = (): I18nContextType => useContext(I18nContext);

interface I18nProviderProps {
  children: ReactNode;
}

/**
 * Language axis of the console. Initial language comes from `?lang=` (persisted
 * when present), else the stored choice, else the browser language. Switching
 * persists to localStorage and re-renders every consumer of useI18n().
 */
export const I18nProvider: React.FC<I18nProviderProps> = ({ children }) => {
  const [locale, setLocaleState] = useState<Locale>(resolveInitialLocale);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    persistLocale(next);
  }, []);

  useEffect(() => {
    document.documentElement.lang = locale === 'zh' ? 'zh-CN' : 'en';
    // Pages with their own title (Layout/Logout, via ui_title) re-set it after
    // this; the default here covers pre-login pages like /login.
    document.title = translate(locale, DEFAULT_APP_TITLE);
  }, [locale]);

  const value = useMemo<I18nContextType>(
    () => ({
      locale,
      setLocale,
      t: ((source: string, vars?: TranslateVars, context?: string) =>
        translate(locale, source, vars, context)) as TranslateFn,
    }),
    [locale, setLocale],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
};