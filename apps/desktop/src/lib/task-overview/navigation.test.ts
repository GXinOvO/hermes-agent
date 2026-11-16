import { beforeEach, expect, test, vi } from 'vitest'

import type { TaskReference } from './model'

const fixture = vi.hoisted(() => ({
  request: vi.fn(), open: vi.fn(async () => {}), navigate: vi.fn(async () => {}),
  owner: { instance_id: 'runtime', profile_id: '/profiles/work', authority_epoch: 3, store_instance_id: 'store' }
}))

vi.mock('@/store/gateway', () => ({
  activeGatewayConnectionId: () => 'remote-a', activeGatewayProfileKey: () => 'work',
  dialedGatewayModeFor: () => 'remote', openGatewayForAgent: fixture.open,
  gatewayRouteState: () => ({ gateway: fixture })
}))
vi.mock('@/lib/exact-session', () => ({ openExactSession: fixture.navigate }))

import { openTaskSource } from './analysis'

const reference: TaskReference = {
  storedId: 'compressed-tip', connectionId: 'remote-a', profile: 'work', targetProfile: 'work', mode: 'remote',
  expectedOwner: { instanceId: 'runtime', profileId: '/profiles/work', authorityEpoch: 3, storeInstanceId: 'store' }
}

beforeEach(() => {
  vi.clearAllMocks()
  fixture.request.mockResolvedValue({ session_id: 'logical-root', stored_session_id: 'compressed-tip',
    resolved_id: 'current-tip', authority_epoch: 3, owner: fixture.owner })
})

test('opens the verified logical root without changing the matter’s original stored tip', async () => {
  const saved = JSON.stringify(reference)
  await openTaskSource(reference)
  expect(fixture.request).toHaveBeenCalledExactlyOnceWith('session.task_source', {
    session_id: 'compressed-tip', profile: 'work', expected_owner: fixture.owner
  })
  expect(fixture.navigate).toHaveBeenCalledExactlyOnceWith({ ...reference, storedId: 'logical-root' })
  expect(JSON.stringify(reference)).toBe(saved)
})

test.each([
  { stored_session_id: 'another-tip' },
  { owner: { ...fixture.owner, instance_id: 'replacement' } },
  { authority_epoch: 4 }
])('does not navigate when the source no longer matches: %j', async mismatch => {
  fixture.request.mockResolvedValueOnce({ session_id: 'logical-root', stored_session_id: 'compressed-tip',
    resolved_id: 'current-tip', authority_epoch: 3, owner: fixture.owner, ...mismatch })
  await expect(openTaskSource(reference)).rejects.toThrow('来源会话身份')
  expect(fixture.navigate).not.toHaveBeenCalled()
})
