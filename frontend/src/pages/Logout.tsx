import React, { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { CheckCircleIcon } from '@heroicons/react/24/outline';
import { useI18n } from '../contexts/I18nContext';
import { useUiTitle } from '../hooks/useUiTitle';
import Button from '../components/Button';

const Logout: React.FC = () => {
  const navigate = useNavigate();
  const uiTitle = useUiTitle();
  const { t } = useI18n();

  useEffect(() => {
    document.title = t(uiTitle);
  }, [uiTitle, t]);

  useEffect(() => {
    // Auto-return to the product home after 5 seconds. This page is the IdP's
    // registered post-logout callback, so the IdP must land here; the session
    // cookie is already cleared, so "/" resolves to the anonymous landing page.
    // Sending the user back to the product face (not the login wall) closes the
    // logout loop — landing them on /login after signing out made it look like
    // the console had thrown them out and locked the door behind them.
    const timer = setTimeout(() => {
      window.location.href = '/';
    }, 5000);

    return () => clearTimeout(timer);
  }, [navigate]);

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900 flex flex-col justify-center py-12 sm:px-6 lg:px-8">
      <div className="sm:mx-auto sm:w-full sm:max-w-md">
        <div className="flex justify-center mb-6">
          <CheckCircleIcon className="h-16 w-16 text-green-500" />
        </div>
        <h2 className="text-center text-3xl font-bold text-gray-900 dark:text-white">
          {t('Successfully Logged Out')}
        </h2>
        <p className="mt-2 text-center text-sm text-gray-600 dark:text-gray-400">
          {t('You have been logged out from all sessions')}
        </p>
      </div>

      <div className="mt-8 sm:mx-auto sm:w-full sm:max-w-md">
        <div className="card p-8">
          <div className="text-center space-y-6">
            <p className="text-gray-700 dark:text-gray-300">
              {t("Your session has been terminated and you've been logged out from the identity provider.")}
            </p>

            <div className="pt-4">
              <Button
                variant="primary"
                fullWidth
                onClick={() => {
                  window.location.href = '/';
                }}
                className="py-3 shadow-sm hover:shadow-md focus:ring-2 focus:ring-offset-2 focus:ring-purple-500"
              >
                {t('Back to Home')}
              </Button>
            </div>

            <div className="pt-1">
              <button
                type="button"
                onClick={() => navigate('/login')}
                className="text-sm text-primary hover:underline"
              >
                {t('Sign in again')}
              </button>
            </div>

            <p className="text-xs text-gray-500 dark:text-gray-400">
              {t('Redirecting to home in 5 seconds...')}
            </p>
          </div>
        </div>

        <div className="mt-6 text-center">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {t('{title} - Secure Access Management', { title: t(uiTitle) })}
          </p>
        </div>
      </div>
    </div>
  );
};

export default Logout;
