import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nProvider, useI18n } from '../I18nContext';
import { LOCALE_STORAGE_KEY } from '../../i18n/t';

const Probe: React.FC = () => {
  const { locale, setLocale, t } = useI18n();
  return (
    <div>
      <span data-testid="locale">{locale}</span>
      <span data-testid="translated">{t('Sign out')}</span>
      <button type="button" onClick={() => setLocale(locale === 'zh' ? 'en' : 'zh')}>
        flip
      </button>
    </div>
  );
};

describe('I18nProvider', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.history.replaceState({}, '', '/');
    Object.defineProperty(window.navigator, 'language', { value: 'en-US', configurable: true });
  });

  it('starts from the stored locale and applies lang/title', () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh');
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );
    expect(screen.getByTestId('locale')).toHaveTextContent('zh');
    expect(screen.getByTestId('translated')).toHaveTextContent('退出登录');
    expect(document.documentElement.lang).toBe('zh-CN');
    expect(document.title).toBe('AI 网关与注册处');
  });

  it('defaults to en for en browsers', () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );
    expect(screen.getByTestId('locale')).toHaveTextContent('en');
    expect(screen.getByTestId('translated')).toHaveTextContent('Sign out');
    expect(document.documentElement.lang).toBe('en');
    expect(document.title).toBe('AI Gateway & Registry');
  });

  it('switches language, persists the choice and re-translates', async () => {
    const user = userEvent.setup();
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );
    await user.click(screen.getByRole('button', { name: 'flip' }));
    expect(screen.getByTestId('locale')).toHaveTextContent('zh');
    expect(screen.getByTestId('translated')).toHaveTextContent('退出登录');
    expect(document.documentElement.lang).toBe('zh-CN');
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('zh');
  });

  it('falls back to English (no throw) when used outside the provider', () => {
    render(<Probe />);
    expect(screen.getByTestId('locale')).toHaveTextContent('en');
    expect(screen.getByTestId('translated')).toHaveTextContent('Sign out');
  });
});