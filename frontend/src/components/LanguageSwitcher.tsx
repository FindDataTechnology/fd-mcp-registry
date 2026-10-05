import React, { Fragment } from 'react';
import { Menu, Transition } from '@headlessui/react';
import { CheckIcon, LanguageIcon } from '@heroicons/react/24/outline';
import { useI18n } from '../contexts/I18nContext';
import type { Locale } from '../i18n/t';

/** Native language names are intentionally not translated. */
const OPTIONS: Array<{ value: Locale; label: string }> = [
  { value: 'zh', label: '简体中文' },
  { value: 'en', label: 'English' },
];

interface LanguageSwitcherProps {
  /** Extra classes for the trigger; lets callers tune placement (e.g. login page). */
  className?: string;
}

/**
 * Header/login control that switches the console language. Mirrors the
 * ThemeToggle trigger styling; the menu mirrors the user dropdown in Layout.
 */
const LanguageSwitcher: React.FC<LanguageSwitcherProps> = ({ className = '' }) => {
  const { locale, setLocale, t } = useI18n();
  const current = OPTIONS.find((option) => option.value === locale) ?? OPTIONS[0];

  return (
    <Menu as="div" className="relative">
      <Menu.Button
        className={`flex items-center space-x-1.5 p-2 text-gray-400 hover:text-gray-500 dark:text-gray-300 dark:hover:text-gray-100 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 focus:outline-none focus:ring-2 focus:ring-purple-500 ${className}`}
        title={t('Language')}
        aria-label={t('Language')}
      >
        <LanguageIcon className="h-5 w-5" />
        <span className="hidden md:block text-sm font-medium">{current.label}</span>
      </Menu.Button>

      <Transition
        as={Fragment}
        enter="transition ease-out duration-100"
        enterFrom="transform opacity-0 scale-95"
        enterTo="transform opacity-100 scale-100"
        leave="transition ease-in duration-75"
        leaveFrom="transform opacity-100 scale-100"
        leaveTo="transform opacity-0 scale-95"
      >
        <Menu.Items className="absolute right-0 z-20 mt-2 w-40 origin-top-right rounded-md bg-white dark:bg-gray-800 py-1 shadow-lg ring-1 ring-black ring-opacity-5 focus:outline-none">
          {OPTIONS.map((option) => (
            <Menu.Item key={option.value}>
              {({ active }) => (
                <button
                  type="button"
                  onClick={() => setLocale(option.value)}
                  className={`${
                    active ? 'bg-gray-100 dark:bg-gray-700' : ''
                  } flex items-center w-full px-4 py-2 text-sm text-gray-700 dark:text-gray-100`}
                >
                  {option.value === locale ? (
                    <CheckIcon className="mr-3 h-4 w-4" />
                  ) : (
                    <span className="mr-3 h-4 w-4" aria-hidden="true" />
                  )}
                  {option.label}
                </button>
              )}
            </Menu.Item>
          ))}
        </Menu.Items>
      </Transition>
    </Menu>
  );
};

export default LanguageSwitcher;