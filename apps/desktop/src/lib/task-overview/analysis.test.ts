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

const source = () => ({ session_id: 'session-a', stored_session_id: 'session-a', resolved_id: 'session-a', authority_epoch: 3, owner: fixture.owner })
const envelope = () => ({ ...source(), task_id: 'matter', analysis: result() })
const respond = (analysis: unknown) => fixture.request.mockImplementation(async method => {
  if (method === 'runtime.describe') { return { capabilities: [] } }
  return method === 'session.task_source' ? source() : analysis
})

beforeEach(() => { fixture.request.mockReset(); fixture.open.mockClear(); respond(envelope()) })

test('accepts the existing session reference and desktop hash forms', () => {
  expect(parseTaskSource('@session:work/session-a')).toEqual({ profile: 'work', sessionId: 'session-a' })
  expect(parseTaskSource('#session/work%2Fsession-a')).toEqual({ profile: 'work', sessionId: 'session-a' })
  expect(parseTaskSource('#/session-a')).toEqual({ sessionId: 'session-a' })
  expect(() => parseTaskSource('https://other.example/#/session-a')).toThrow('其他连接')
})

test('resolves on the explicit profile using its own source witness without restoring a conversation', async () => {
  expect(await resolveTaskSource('@session:work/session-a')).toEqual(primary)
  expect(fixture.request).toHaveBeenCalledExactlyOnceWith('session.task_source', { session_id: 'session-a', profile: 'work' })
})

test('resolves and analyzes a compressed tip on its returned logical source', async () => {
  const resolved = { ...source(), session_id: 'root-a', resolved_id: 'tip-b', stored_session_id: 'tip-a' }
  fixture.request.mockImplementation(async method => {
    if (method === 'runtime.describe') { return { capabilities: [] } }
    return method === 'session.task_source' ? resolved : { ...resolved, task_id: 'matter', analysis: result() }
  })
  const reference = await resolveTaskSource('@session:work/tip-a')
  expect(reference).toEqual({ ...primary, storedId: 'tip-a' })
  const compressed = createTask({ ...task(), primary: reference }, 1700000000000)
  expect(await loadTaskAnalysis(compressed, true)).toEqual(result())
  expect(fixture.request).toHaveBeenLastCalledWith('session.task_analysis', expect.objectContaining({
    session_id: 'root-a', stored_id: 'tip-a', expected_owner: fixture.owner, refresh: true
  }), 180000)
})

test('refuses a wrong echoed id, incomplete witness and an unsupported source method', async () => {
  fixture.request.mockResolvedValueOnce({ ...source(), stored_session_id: 'other' })
  await expect(resolveTaskSource('session-a')).rejects.toThrow('来源会话身份')
  fixture.request.mockResolvedValueOnce({ ...source(), owner: { instance_id: 'incomplete' } })
  await expect(resolveTaskSource('session-a')).rejects.toThrow('来源会话身份')
  fixture.request.mockRejectedValueOnce(new Error('unknown_method'))
  await expect(resolveTaskSource('session-a')).rejects.toThrow('unknown_method')
})

test('refuses a replaced source witness before analysis and does not silently rebind', async () => {
  fixture.request.mockResolvedValueOnce({ capabilities: [] })
  fixture.request.mockResolvedValueOnce({ ...source(), owner: { ...fixture.owner, store_instance_id: 'replacement' } })
  await expect(loadTaskAnalysis(task(), true)).rejects.toThrow('来源会话身份')
  expect(fixture.request.mock.calls.map(call => call[0])).toEqual(['runtime.describe', 'session.task_source'])
})

test('reads saved analysis without generation or manual task mutation', async () => {
  const original = task()
  expect(await loadTaskAnalysis(original)).toEqual(result())
  const analysisCall = fixture.request.mock.calls.find(call => call[0] === 'session.task_analysis')
  expect(analysisCall?.[1]).toMatchObject({ refresh: false, task_id: 'matter', title: '检查发布' })
  expect(analysisCall?.[1]).not.toHaveProperty('follow_sources')
  expect(original).toMatchObject({ status: 'unspecified', resolution: { state: 'open' } })
  expect(cachedTaskAnalysis(original)).toBeNull()
})

test('refreshes explicitly and rejects wrong tasks, names and unsupported completion', async () => {
  await loadTaskAnalysis(task(), true)
  expect(fixture.request.mock.calls.find(call => call[0] === 'session.task_analysis')?.[1].refresh).toBe(true)
  respond({ ...envelope(), task_id: 'other' })
  await expect(loadTaskAnalysis(task())).rejects.toThrow('来源已变化')
  respond({ ...envelope(), analysis: { ...result(), title: '其他事项' } })
  await expect(loadTaskAnalysis(task())).rejects.toThrow('名称不一致')
  respond({ ...envelope(), analysis: { ...result(), status: 'done' } })
  await expect(loadTaskAnalysis(task())).rejects.toThrow('Invalid task analysis')
})

test('rejects claims without evidence and inconsistent coverage', async () => {
  respond({ ...envelope(), analysis: { ...result(), goal: { text: '凭空推断', evidence: [] } } })
  await expect(loadTaskAnalysis(task())).rejects.toThrow('Invalid task analysis')
  respond({ ...envelope(), analysis: { ...result(), analyzed_messages: 3 } })
  await expect(loadTaskAnalysis(task())).rejects.toThrow('Invalid task analysis')
})

test('negotiates task-analysis-v2 before requesting linked sources and keeps v1 parameters on old runtimes', async () => {
  const v2 = {
    ...result(), version: 2 as const, scope: 'linked_conversations' as const, complete: true,
    sources: [
      { id: 's0', profile: 'work', session_id: 'root-a', stored_session_id: 'session-a', resolved_id: 'root-a',
        owner: fixture.owner, relation: 'primary' as const, via_source_id: null, via_row_id: null, status: 'read' as const, complete: true, reason: null },
      { id: 's1', profile: 'other', session_id: 'root-b', stored_session_id: 'session-b', resolved_id: 'root-b',
        owner: { ...fixture.owner, profile_id: '/profiles/other' }, relation: 'reference' as const, via_source_id: 's0', via_row_id: 7,
        status: 'read' as const, complete: true, reason: null }
    ],
    goal: { text: '检查发布', evidence: [
      { source_id: 's0', row_id: 7, quote: '主来源记录' },
      { source_id: 's1', row_id: 7, quote: '关联来源记录' }
    ] }
  }
  fixture.request.mockImplementation(async method => {
    if (method === 'runtime.describe') { return { capabilities: ['task-analysis-v2'] } }
    if (method === 'session.task_source') { return source() }
    return { ...source(), task_id: 'matter', analysis: v2 }
  })

  await expect(loadTaskAnalysis(task(), true)).resolves.toEqual(v2)
  expect(fixture.request.mock.calls.map(call => call[0])).toEqual(['runtime.describe', 'session.task_source', 'session.task_analysis'])
  expect(fixture.request).toHaveBeenLastCalledWith('session.task_analysis', expect.objectContaining({ follow_sources: true }), 180000)
})
