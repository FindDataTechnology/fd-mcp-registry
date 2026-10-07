/**
 * Settings page access control utility.
 *
 * All Settings categories (Audit, Federation, IAM) require admin access.
 * The backend enforces this on every endpoint. The frontend mirrors it
 * as a UX convenience layer.
 *
 * The ui_permissions from scopes.yml control server/agent access
 * (e.g., list_service, toggle_service, list_agents, publish_agent)
 */

interface SettingsUser {
  is_admin?: boolean;
}

/**
 * Check if a user can access the Settings page.
 * Returns true only when is_admin === true.
 */
export function canAccessSettings(user: SettingsUser | null): boolean {
  if (!user) return false;
  return user.is_admin === true;
}
