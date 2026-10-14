import React from 'react';
import { render, screen, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Logout from '../Logout';

/**
 * Logout is the IdP's registered post-logout callback, so the IdP must land
 * here — the page then sends the user somewhere sensible. It used to auto-
 * redirect to /login after 5s, which made a completed sign-out look like the
 * console had locked the door behind you: the product face (/) was never
 * shown again. These tests pin the return-home behavior.
 */

jest.mock('../../contexts/I18nContext', () => ({
  useI18n: () => ({ t: (s: string) => s, locale: 'zh' }),
}));
jest.mock('../../hooks/useUiTitle', () => ({
  useUiTitle: () => 'Yizuo Packs',
}));
jest.mock('../../components/Button', () => {
  const M = ({ children, onClick }: any) => <button onClick={onClick}>{children}</button>;
  M.displayName = 'Button';
  return M;
});

// window.location is read-only in jsdom; replace it with a writable stub and
// assert on the href the page assigns.
const assignMock = jest.fn();

function renderLogout() {
  return render(
    <MemoryRouter>
      <Logout />
    </MemoryRouter>,
  );
}

describe('Logout page', () => {
  let originalLocation: PropertyDescriptor | undefined;

  beforeEach(() => {
    assignMock.mockClear();
    originalLocation = Object.getOwnPropertyDescriptor(window, 'location');
    // @ts-expect-error - test stub for the read-only jsdom location
    delete window.location;
    // @ts-expect-error - test stub
    window.location = { href: '', assign: assignMock } as any;
  });

  afterEach(() => {
    if (originalLocation) Object.defineProperty(window, 'location', originalLocation);
  });

  it('shows the confirmation screen', () => {
    renderLogout();
    expect(screen.getByText('Successfully Logged Out')).toBeTruthy();
  });

  it('offers "Back to Home" as the primary action and takes the user to /', () => {
    renderLogout();
    const primary = screen.getByText('Back to Home');
    primary.click();
    expect(window.location.href).toBe('/');
  });

  it('keeps a secondary "Sign in again" path for users who want back in', () => {
    renderLogout();
    expect(screen.getByText('Sign in again')).toBeTruthy();
  });

  it('auto-returns to the product home after the 5s countdown', () => {
    jest.useFakeTimers();
    try {
      renderLogout();
      act(() => {
        jest.advanceTimersByTime(5000);
      });
      expect(window.location.href).toBe('/');
    } finally {
      jest.useRealTimers();
    }
  });

  it('does not redirect before the countdown elapses', () => {
    jest.useFakeTimers();
    try {
      renderLogout();
      act(() => {
        jest.advanceTimersByTime(4999);
      });
      expect(window.location.href).toBe('');
    } finally {
      jest.useRealTimers();
    }
  });
});
