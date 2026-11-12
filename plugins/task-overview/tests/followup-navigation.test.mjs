import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as overview from '../src/core.mjs'

const owner = { connectionId: 'host-a', profile: 'default', targetProfile: 'research', mode: 'remote', storeInstanceId: 'store-a' }
const item = { storedId: 'session-1', route: owner }

test('recorded store identity and an exact-open method alone never authorize navigation', async () => {
  const api = host([owner])
  await assert.rejects(overview.openFollowupSession(api, item), error => error.code === 'FOLLOWUP_EXACT_NAVIGATION_UNAVAILABLE')
  assert.deepEqual(api.opens, [])
})

function host(routes) {
  const opens = []
  return { opens, profileRoutes: async () => routes, openExactSession: async (...args) => opens.push(args) }
}

test('an explicit user open cannot bypass the unavailable navigation guard', async () => {
  assert.equal(typeof overview.openFollowupSession, 'function')
  const api = host([owner, { ...owner, connectionId: 'host-b' }])
  await assert.rejects(overview.openFollowupSession(api, item), error => error.code === 'FOLLOWUP_EXACT_NAVIGATION_UNAVAILABLE')
  assert.deepEqual(api.opens, [])
})

test('a removed connection cannot fall back to a same-named profile on another host', async () => {
  const api = host([{ ...owner, connectionId: 'host-b' }])
  await assert.rejects(overview.openFollowupSession(api, item), /route|owner/i)
  assert.deepEqual(api.opens, [])
})

test('a changed backend profile for the same connection and profile refuses a stale route', async () => {
  const api = host([{ ...owner, targetProfile: 'other' }])
  await assert.rejects(overview.openFollowupSession(api, item), /route|owner/i)
  assert.deepEqual(api.opens, [])
})

test('an ambiguous owner route is refused before navigating', async () => {
  const api = host([owner, { ...owner }])
  await assert.rejects(overview.openFollowupSession(api, item), /route|owner/i)
  assert.deepEqual(api.opens, [])
})

test('an owner route with an incomplete duplicate is refused before navigating', async () => {
  for (const targetProfile of [undefined, null, '']) {
    const api = host([owner, { ...owner, targetProfile }])
    await assert.rejects(overview.openFollowupSession(api, item), /route|owner/i)
    assert.deepEqual(api.opens, [])
  }
})

test('a missing durable session id is refused before navigating', async () => {
  const api = host([owner])
  await assert.rejects(overview.openFollowupSession(api, { ...item, storedId: '' }), /session/i)
  assert.deepEqual(api.opens, [])
})

test('the host open method is not called when navigation is unavailable', async () => {
  let called = false
  const api = { profileRoutes: async () => [owner], openExactSession: async () => { called = true; throw Error('source disconnected') } }
  await assert.rejects(overview.openFollowupSession(api, item), error => error.code === 'FOLLOWUP_EXACT_NAVIGATION_UNAVAILABLE')
  assert.equal(called, false)
})

test('an older host without exact-owner navigation refuses instead of using ambiguous openSession', async () => {
  const api = { profileRoutes: async () => [owner], openSession: async (...args) => { api.opens.push(args) }, opens: [] }
  await assert.rejects(overview.openFollowupSession(api, item), error => error.code === 'FOLLOWUP_EXACT_NAVIGATION_UNAVAILABLE')
  assert.deepEqual(api.opens, [])
})

test('a reference without the recorded store identity refuses to open even when its labels match', async () => {
  const api = host([owner])
  for (const storeInstanceId of [undefined, '', ' store-a', 'store-b ']) {
    const route = { connectionId: owner.connectionId, profile: owner.profile, targetProfile: owner.targetProfile, mode: owner.mode,
      ...(storeInstanceId === undefined ? {} : { storeInstanceId }) }
    await assert.rejects(overview.openFollowupSession(api, { storedId: 'session-1', route }),
      error => error.code === 'FOLLOWUP_REFERENCE_IDENTITY_UNAVAILABLE')
  }
  assert.deepEqual(api.opens, [])
})
