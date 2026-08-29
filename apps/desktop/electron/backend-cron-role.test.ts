import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  desktopBackendEnv,
  desktopBackendRoleForRoute,
  desktopRegistryBackendRole,
  desktopRegistryCronOwnerProfile,
  desktopRegistryOwnershipProfile,
  desktopRegistryUsesSharedCronOwner,
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

  assert.equal(desktopRegistryCronOwnerProfile('default'), null)
  assert.equal(desktopRegistryCronOwnerProfile('work'), 'default')
  assert.equal(desktopRegistryBackendRole('default'), 'primary')
  assert.equal(desktopRegistryBackendRole('work'), 'pool')
  assert.equal(desktopRegistryUsesSharedCronOwner('settings', 'homelab', 'homelab'), true)
  assert.equal(desktopRegistryUsesSharedCronOwner('profile', 'homelab', 'homelab'), false)
  assert.equal(desktopRegistryUsesSharedCronOwner('settings', 'other', 'homelab'), false)

  assert.equal(desktopRegistryOwnershipProfile('default', ''), 'default')
  assert.equal(desktopRegistryOwnershipProfile('work', ''), 'work')
  assert.equal(desktopRegistryOwnershipProfile('default', 'remote-owner'), 'default')
  assert.equal(desktopRegistryOwnershipProfile('work', 'remote-owner'), 'default')
})