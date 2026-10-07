import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import axios from 'axios';
import ConfigPanel from '../ConfigPanel';

// Mock axios
jest.mock('axios');
const mockedAxios = axios as jest.Mocked<typeof axios>;

// Mock clipboard API
Object.assign(navigator, {
  clipboard: { writeText: jest.fn().mockResolvedValue(undefined) },
});

/** Sample API response used across tests. */
const mockConfigResponse = {
  groups: [
    {
      id: 'deployment',
      title: 'Deployment Mode',
      order: 1,
      fields: [
        { key: 'deployment_mode', label: 'Deployment Mode', value: 'with-gateway', raw_value: 'with-gateway', is_masked: false, unit: null },
        { key: 'registry_mode', label: 'Registry Mode', value: 'full', raw_value: 'full', is_masked: false, unit: null },
      ],
    },
    {
      id: 'storage',
      title: 'Storage Backend',
      order: 2,
      fields: [
        { key: 'storage_backend', label: 'Storage Backend', value: 'mongodb', raw_value: 'mongodb', is_masked: false, unit: null },
      ],
    },
    {
      id: 'auth',
      title: 'Authentication',
      order: 3,
      fields: [
        { key: 'auth_enabled', label: 'Auth Enabled', value: 'true', raw_value: true, is_masked: false, unit: null },
        { key: 'auth_secret_key', label: 'Auth Secret Key', value: 'sk-t********', raw_value: null, is_masked: true, unit: null },
      ],
    },
  ],
  total_groups: 3,
  is_local_dev: false,
};

describe('ConfigPanel', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('renders groups from API response', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: mockConfigResponse });
    render(<ConfigPanel />);

    await waitFor(() => {
      expect(screen.getByText('System Configuration')).toBeInTheDocument();
    });
    // Group titles visible (may appear multiple times due to field labels matching)
    expect(screen.getAllByText(/Deployment Mode/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Storage Backend/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Authentication/)).toBeInTheDocument();
  });

  test('deployment and storage groups are expanded by default', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: mockConfigResponse });
    render(<ConfigPanel />);

    await waitFor(() => {
      expect(screen.getByText('System Configuration')).toBeInTheDocument();
    });

    // Fields from expanded groups should be visible
    expect(screen.getByText('with-gateway')).toBeInTheDocument();
    expect(screen.getByText('mongodb')).toBeInTheDocument();

    // Auth group is collapsed — its field labels should not be rendered
    expect(screen.queryByText('Auth Enabled')).not.toBeInTheDocument();
  });

  test('search filtering hides non-matching fields', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: mockConfigResponse });
    render(<ConfigPanel />);

    await waitFor(() => {
      expect(screen.getByText('System Configuration')).toBeInTheDocument();
    });

    const searchInput = screen.getByPlaceholderText('Search configuration...');
    fireEvent.change(searchInput, { target: { value: 'mongodb' } });

    // Storage group should remain (has matching field value)
    expect(screen.getAllByText(/Storage Backend/).length).toBeGreaterThanOrEqual(1);

    // Deployment group fields should be gone (no match for "mongodb")
    expect(screen.queryByText('Registry Mode')).not.toBeInTheDocument();
  });

  test('shows "no results" message for non-matching search', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: mockConfigResponse });
    render(<ConfigPanel />);

    await waitFor(() => {
      expect(screen.getByText('System Configuration')).toBeInTheDocument();
    });

    const searchInput = screen.getByPlaceholderText('Search configuration...');
    fireEvent.change(searchInput, { target: { value: 'zzz_nonexistent_zzz' } });

    expect(screen.getByTestId('no-results')).toBeInTheDocument();
  });

  test('shows skeleton loading state during fetch', () => {
    mockedAxios.get.mockReturnValue(new Promise(() => {}));
    render(<ConfigPanel />);

    expect(screen.getByTestId('config-skeleton')).toBeInTheDocument();
  });

  test('shows error state on API failure', async () => {
    mockedAxios.get.mockRejectedValueOnce({
      response: { data: { detail: 'Admin access required' } },
    });
    render(<ConfigPanel />);

    await waitFor(() => {
      expect(screen.getByTestId('config-error')).toBeInTheDocument();
    });
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
  });

  test('shows is_local_dev badge when true', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: { ...mockConfigResponse, is_local_dev: true },
    });
    render(<ConfigPanel />);

    await waitFor(() => {
      expect(screen.getByTestId('local-dev-badge')).toBeInTheDocument();
    });
    expect(screen.getByText('Local Development Mode')).toBeInTheDocument();
  });

  test('ARIA attributes present on group headers', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: mockConfigResponse });
    render(<ConfigPanel />);

    await waitFor(() => {
      expect(screen.getByText('System Configuration')).toBeInTheDocument();
    });

    // Find the deployment group header button (expanded by default)
    const buttons = screen.getAllByRole('button');
    const deploymentBtn = buttons.find(
      (btn) => btn.getAttribute('aria-controls') === 'config-group-deployment'
    );
    expect(deploymentBtn).toBeDefined();
    expect(deploymentBtn).toHaveAttribute('aria-expanded', 'true');

    // Auth group header (collapsed by default)
    const authBtn = buttons.find(
      (btn) => btn.getAttribute('aria-controls') === 'config-group-auth'
    );
    expect(authBtn).toBeDefined();
    expect(authBtn).toHaveAttribute('aria-expanded', 'false');
  });
});
