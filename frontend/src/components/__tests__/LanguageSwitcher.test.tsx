import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import LanguageSwitcher from '../LanguageSwitcher';
import { I18nProvider } from '../../contexts/I18nContext';
import { LOCALE_STORAGE_KEY } from '../../i18n/t';

function renderSwitcher() {
  return render(
    <I18nProvider>
      <LanguageSwitcher />
    </I18nProvider>,
  );
}

describe('LanguageSwitcher', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.history.replaceState({}, '', '/');
    Object.defineProperty(window.navigator, 'language', { value: 'en-US', configurable: true });
  });

  it('shows the current language and switches to 简体中文', async () => {
    const user = userEvent.setup();
    renderSwitcher();

    const trigger = screen.getByRole('button', { name: 'Language' });
    expect(trigger).toHaveTextContent('English');

    await user.click(trigger);
    await user.click(screen.getByRole('menuitem', { name: /简体中文/ }));

    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('zh');
    expect(document.documentElement.lang).toBe('zh-CN');
    expect(screen.getByRole('button', { name: '语言' })).toHaveTextContent('简体中文');
  });
});