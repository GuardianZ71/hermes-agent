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