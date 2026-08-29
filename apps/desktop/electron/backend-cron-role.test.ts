import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  desktopBackendEnv,
  desktopBackendRoleForRoute,
  normalizeDesktopBackendRole
} from './backend-cron-role'

test('Desktop backend cron ownership is explicit and defaults compatibly to primary', () => {
  assert.equal(normalizeDesktopBackendRole('primary'), 'primary')
  assert.equal(normalizeDesktopBackendRole('pool'), 'pool')
  assert.equal(normalizeDesktopBackendRole(undefined), 'primary')
  assert.equal(normalizeDesktopBackendRole('unexpected'), 'primary')

  assert.deepEqual(desktopBackendEnv('pool'), {
    HERMES_DESKTOP: '1',
    HERMES_DESKTOP_BACKEND_ROLE: 'pool'
  })

  assert.equal(desktopBackendRoleForRoute('work', 'work', 'profile'), 'primary')
  assert.equal(desktopBackendRoleForRoute('other', 'work', 'profile'), 'pool')
  assert.equal(desktopBackendRoleForRoute('other', 'work', 'settings'), 'primary')
  assert.equal(desktopBackendRoleForRoute('other', 'work', 'env'), 'primary')
})