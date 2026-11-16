import { beforeEach, expect, test, vi } from 'vitest'

import { createTask } from './model'

const fixture = vi.hoisted(() => ({
  request: vi.fn(), open: vi.fn(async () => {}),
  owner: { instance_id: 'runtime', profile_id: '/profiles/work', authority_epoch: 3, store_instance_id: 'store' }
}))

vi.mock('@/store/gateway', () => ({
  activeGatewayConnectionId: () => 'remote-a', activeGatewayProfileKey: () => 'work',
  dialedGatewayModeFor: () => 'remote', openGatewayForAgent: fixture.open,
  gatewayRouteState: () => ({ gateway: fixture })
}))

import { cachedTaskAnalysis, loadTaskAnalysis, parseTaskSource, resolveTaskSource } from './analysis'

const primary = {
  storedId: 'session-a', connectionId: 'remote-a', profile: 'work', targetProfile: 'work', mode: 'remote' as const,
  expectedOwner: { instanceId: 'runtime', profileId: '/profiles/work', authorityEpoch: 3, storeInstanceId: 'store' }
}

const task = () => createTask({ id: 'matter', title: '检查发布', primary, related: [], nextSteps: [] }, 1700000000000)

const result = () => ({
  version: 1, title: '检查发布', analyzed_at: 1700000000100, conversation_revision: [1], stale: false,
  scope: 'single_conversation', message_count: 2, analyzed_messages: 2, limitations: [], status: 'waiting',
  goal: { text: '检查发布', evidence: [{ row_id: 1, quote: '检查发布' }] }, completed: [], remaining: [], needs_user: []
})

const envelope = () => ({ session_id: 'session-a', stored_session_id: 'session-a', authority_epoch: 3, owner: fixture.owner, task_id: 'matter', analysis: result() })

beforeEach(() => { fixture.request.mockReset(); fixture.open.mockClear() })

test('accepts the existing session reference and desktop hash forms', () => {
  expect(parseTaskSource('@session:work/session-a')).toEqual({ profile: 'work', sessionId: 'session-a' })
  expect(parseTaskSource('#session/work%2Fsession-a')).toEqual({ profile: 'work', sessionId: 'session-a' })
  expect(parseTaskSource('#/session-a')).toEqual({ sessionId: 'session-a' })
  expect(() => parseTaskSource('https://other.example/#/session-a')).toThrow('其他连接')
})

test('resolves a source on the explicit connection and profile without restoring a conversation', async () => {
  fixture.request.mockResolvedValueOnce({ ...fixture.owner, capabilities: ['task-analysis-v1'] }).mockResolvedValueOnce(envelope())
  expect(await resolveTaskSource('@session:work/session-a')).toEqual(primary)
  expect(fixture.request.mock.calls.map(call => call[0])).toEqual(['runtime.describe', 'session.task_source'])
  expect(fixture.request.mock.calls[0][1]).toBeUndefined()
  expect(fixture.request).toHaveBeenLastCalledWith('session.task_source', {
    session_id: 'session-a', profile: 'work', expected_owner: fixture.owner
  })
})

test('refuses replaced source witnesses and unsupported backends', async () => {
  fixture.request.mockResolvedValueOnce({ ...fixture.owner, capabilities: ['task-analysis-v1'] })
    .mockResolvedValueOnce({ ...envelope(), owner: { ...fixture.owner, store_instance_id: 'replacement' } })
  await expect(resolveTaskSource('session-a')).rejects.toThrow('来源会话身份')
  fixture.request.mockResolvedValueOnce({ ...fixture.owner, capabilities: [] })
  await expect(resolveTaskSource('session-a')).rejects.toThrow('更新对应后端')
})

test('reads a saved analysis without generating or mutating manual task fields', async () => {
  fixture.request.mockResolvedValueOnce(envelope())
  const original = task()
  expect(await loadTaskAnalysis(original)).toEqual(result())
  expect(fixture.request.mock.calls[0][1]).toMatchObject({ refresh: false, task_id: 'matter', title: '检查发布' })
  expect(original).toMatchObject({ status: 'unspecified', resolution: { state: 'open' } })
  expect(cachedTaskAnalysis(original)).toBeNull()
})

test('refreshes explicitly and rejects wrong task names and unsupported completion', async () => {
  fixture.request.mockResolvedValueOnce(envelope())
  await loadTaskAnalysis(task(), true)
  expect(fixture.request.mock.calls[0][1].refresh).toBe(true)
  fixture.request.mockResolvedValueOnce({ ...envelope(), task_id: 'other' })
  await expect(loadTaskAnalysis(task())).rejects.toThrow('来源已变化')
  fixture.request.mockResolvedValueOnce({ ...envelope(), analysis: { ...result(), title: '其他事项' } })
  await expect(loadTaskAnalysis(task())).rejects.toThrow('名称不一致')
  fixture.request.mockResolvedValueOnce({ ...envelope(), analysis: { ...result(), status: 'done' } })
  await expect(loadTaskAnalysis(task())).rejects.toThrow('Invalid task analysis')
})

test('rejects claims without evidence and inconsistent coverage', async () => {
  fixture.request.mockResolvedValueOnce({ ...envelope(), analysis: { ...result(), goal: { text: '凭空推断', evidence: [] } } })
  await expect(loadTaskAnalysis(task())).rejects.toThrow('Invalid task analysis')
  fixture.request.mockResolvedValueOnce({ ...envelope(), analysis: { ...result(), analyzed_messages: 3 } })
  await expect(loadTaskAnalysis(task())).rejects.toThrow('Invalid task analysis')
})
