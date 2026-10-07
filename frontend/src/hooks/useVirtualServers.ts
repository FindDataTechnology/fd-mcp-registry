import { useState, useEffect, useCallback } from 'react';
import { useI18n } from '../contexts/I18nContext';
import { localizeError } from '../utils/apiError';
import axios from 'axios';
import {
  VirtualServerInfo,
  VirtualServerConfig,
  CreateVirtualServerRequest,
  UpdateVirtualServerRequest,
  ResolvedTool,
  ToolCatalogEntry,
} from '../types/virtualServer';


/**
 * Return type for the useVirtualServers hook.
 */
interface UseVirtualServersReturn {
  virtualServers: VirtualServerInfo[];
  loading: boolean;
  error: string | null;
  refreshData: () => Promise<void>;
  createVirtualServer: (data: CreateVirtualServerRequest) => Promise<VirtualServerConfig>;
  updateVirtualServer: (path: string, data: UpdateVirtualServerRequest) => Promise<VirtualServerConfig>;
  deleteVirtualServer: (path: string) => Promise<void>;
  toggleVirtualServer: (path: string, enabled: boolean) => Promise<VirtualServerConfig>;
}


/**
 * Encode a virtual server path for use in URL segments.
 *
 * Virtual server paths contain slashes (e.g., "/virtual/dev-essentials"),
 * so they must be encoded for safe use in API URLs.
 */
function _encodeServerPath(path: string): string {
  return encodeURIComponent(path);
}


/**
 * Hook for listing and managing virtual servers.
 *
 * Provides the list of virtual servers with create, update, delete,
 * and toggle operations. The list is automatically refreshed after
 * any mutation operation.
 */
export const useVirtualServers = (): UseVirtualServersReturn => {
  const { t } = useI18n();
  const [virtualServers, setVirtualServers] = useState<VirtualServerInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);

      const response = await axios.get('/api/virtual-servers');
      const responseData = response.data || {};
      const serversList: VirtualServerInfo[] = responseData.virtual_servers || [];

      setVirtualServers(serversList);
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string } }; message?: string };
      console.error('Failed to fetch virtual servers:', err);
      setError(localizeError(err, t, 'Failed to fetch virtual servers'));
      setVirtualServers([]);
    } finally {
      setLoading(false);
    }
  }, [t]);

  const createVirtualServer = useCallback(async (
    data: CreateVirtualServerRequest,
  ): Promise<VirtualServerConfig> => {
    const response = await axios.post('/api/virtual-servers', data);
    await fetchData();
    return response.data;
  }, [fetchData]);

  const updateVirtualServer = useCallback(async (
    path: string,
    data: UpdateVirtualServerRequest,
  ): Promise<VirtualServerConfig> => {
    const response = await axios.put(
      `/api/virtual-servers/${_encodeServerPath(path)}`,
      data,
    );
    await fetchData();
    return response.data;
  }, [fetchData]);

  const deleteVirtualServer = useCallback(async (
    path: string,
  ): Promise<void> => {
    await axios.delete(`/api/virtual-servers/${_encodeServerPath(path)}`);
    await fetchData();
  }, [fetchData]);

  const toggleVirtualServer = useCallback(async (
    path: string,
    enabled: boolean,
  ): Promise<VirtualServerConfig> => {
    const response = await axios.post(
      `/api/virtual-servers/${_encodeServerPath(path)}/toggle`,
      { enabled },
    );
    await fetchData();
    return response.data;
  }, [fetchData]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return {
    virtualServers,
    loading,
    error,
    refreshData: fetchData,
    createVirtualServer,
    updateVirtualServer,
    deleteVirtualServer,
    toggleVirtualServer,
  };
};


/**
 * Return type for the useVirtualServer hook.
 */
interface UseVirtualServerReturn {
  virtualServer: VirtualServerConfig | null;
  loading: boolean;
  /** @deprecated Use `loading` instead */
  isLoading: boolean;
  error: string | null;
  refetch: () => Promise<void>;
}


/**
 * Hook for fetching a single virtual server by path.
 *
 * @param path - The virtual server path (e.g., '/virtual/dev-essentials'), or undefined to skip fetching
 */
export const useVirtualServer = (path: string | undefined): UseVirtualServerReturn => {
  const { t } = useI18n();
  const [virtualServer, setVirtualServer] = useState<VirtualServerConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    if (!path) {
      setVirtualServer(null);
      return;
    }

    try {
      setLoading(true);
      setError(null);

      const response = await axios.get(
        `/api/virtual-servers/${_encodeServerPath(path)}`,
      );
      setVirtualServer(response.data);
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string } }; message?: string };
      console.error(`Failed to fetch virtual server ${path}:`, err);
      setError(localizeError(err, t, 'Failed to fetch virtual server'));
      setVirtualServer(null);
    } finally {
      setLoading(false);
    }
  }, [path, t]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return {
    virtualServer,
    loading,
    isLoading: loading,
    error,
    refetch: fetchData,
  };
};


/**
 * Return type for the useVirtualServerTools hook.
 */
interface UseVirtualServerToolsReturn {
  tools: ResolvedTool[];
  loading: boolean;
  /** @deprecated Use `loading` instead */
  isLoading: boolean;
  error: string | null;
  refetch: () => Promise<void>;
}


/**
 * Hook for fetching resolved tools for a virtual server.
 *
 * @param path - The virtual server path, or undefined to skip fetching
 */
export const useVirtualServerTools = (path: string | undefined): UseVirtualServerToolsReturn => {
  const { t } = useI18n();
  const [tools, setTools] = useState<ResolvedTool[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    if (!path) {
      setTools([]);
      return;
    }

    try {
      setLoading(true);
      setError(null);

      const response = await axios.get(
        `/api/virtual-servers/${_encodeServerPath(path)}/tools`,
      );
      const responseData = response.data || {};
      const toolsList: ResolvedTool[] = responseData.tools || [];

      setTools(toolsList);
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string } }; message?: string };
      console.error(`Failed to fetch tools for virtual server ${path}:`, err);
      setError(localizeError(err, t, 'Failed to fetch virtual server tools'));
      setTools([]);
    } finally {
      setLoading(false);
    }
  }, [path, t]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return {
    tools,
    loading,
    isLoading: loading,
    error,
    refetch: fetchData,
  };
};


/**
 * Return type for the useToolCatalog hook.
 */
interface UseToolCatalogReturn {
  catalog: ToolCatalogEntry[];
  loading: boolean;
  error: string | null;
  refreshData: () => Promise<void>;
}


/**
 * Hook for fetching the global tool catalog across all enabled backend servers.
 */
export const useToolCatalog = (): UseToolCatalogReturn => {
  const { t } = useI18n();
  const [catalog, setCatalog] = useState<ToolCatalogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);

      const response = await axios.get('/api/tool-catalog');
      const responseData = response.data || {};
      const toolsList: ToolCatalogEntry[] = responseData.tools || [];

      setCatalog(toolsList);
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string } }; message?: string };
      console.error('Failed to fetch tool catalog:', err);
      setError(localizeError(err, t, 'Failed to fetch tool catalog'));
      setCatalog([]);
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return {
    catalog,
    loading,
    error,
    refreshData: fetchData,
  };
};


/**
 * Standalone API functions for virtual server management operations.
 * These can be used outside of hooks for one-off operations.
 */
export async function createVirtualServer(
  data: CreateVirtualServerRequest,
): Promise<VirtualServerConfig> {
  const response = await axios.post('/api/virtual-servers', data);
  return response.data;
}


export async function updateVirtualServer(
  path: string,
  updates: UpdateVirtualServerRequest,
): Promise<VirtualServerConfig> {
  const response = await axios.put(
    `/api/virtual-servers/${_encodeServerPath(path)}`,
    updates,
  );
  return response.data;
}


export async function deleteVirtualServer(path: string): Promise<void> {
  await axios.delete(`/api/virtual-servers/${_encodeServerPath(path)}`);
}


export async function toggleVirtualServer(
  path: string,
  enabled: boolean,
): Promise<VirtualServerConfig> {
  const response = await axios.post(
    `/api/virtual-servers/${_encodeServerPath(path)}/toggle`,
    { enabled },
  );
  return response.data;
}
