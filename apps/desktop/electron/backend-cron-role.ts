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