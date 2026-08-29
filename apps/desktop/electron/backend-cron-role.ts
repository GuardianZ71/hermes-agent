export type DesktopBackendRole = 'primary' | 'pool'

export function normalizeDesktopBackendRole(role: unknown): DesktopBackendRole {
  return role === 'pool' ? 'pool' : 'primary'
}

export function desktopBackendEnv(role: unknown): Record<string, string> {
  return {
    HERMES_DESKTOP: '1',
    HERMES_DESKTOP_BACKEND_ROLE: normalizeDesktopBackendRole(role)
  }
}

export function desktopBackendRoleForRoute(
  profile: unknown,
  primaryProfile: unknown,
  routeSource: unknown
): DesktopBackendRole {
  // App-wide SSH settings produce one shared backend that multiplexes every
  // profile. It remains the cron owner even when a background profile caused
  // the connection to be resolved. Only an explicit per-profile SSH route is
  // a pool backend when it does not serve the Desktop's primary profile.
  return routeSource !== 'profile' || profile === primaryProfile ? 'primary' : 'pool'
}

export function desktopRegistryCronOwnerProfile(profile: unknown): null | string {
  return String(profile ?? '').trim() === 'default' ? null : 'default'
}

export function desktopRegistryBackendRole(profile: unknown): DesktopBackendRole {
  return desktopRegistryCronOwnerProfile(profile) === null ? 'primary' : 'pool'
}

export function desktopRegistryOwnershipProfile(profile: unknown, remoteProfile: unknown): string {
  // An explicit registry remoteProfile names one concrete remote daemon. Local
  // profile aliases must therefore share one ownership scope instead of
  // spawning duplicate serves for that same remote profile.
  return String(remoteProfile ?? '').trim() ? 'default' : String(profile ?? '').trim() || 'default'
}

export function desktopRegistryUsesSharedCronOwner(
  routeSource: unknown,
  routeConnectionId: unknown,
  connectionId: unknown
): boolean {
  // Only app-wide SSH settings describe the shared primary backend. An
  // explicit per-profile route remains independently owned even when it points
  // at the same registry connection.
  return routeSource === 'settings' && routeConnectionId === connectionId
}