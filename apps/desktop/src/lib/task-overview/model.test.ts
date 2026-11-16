import { describe, expect, it } from 'vitest'

import { applyTaskAction, createTask, taskReferenceKey, validateTasks } from '@/lib/task-overview/model'

const at = 1_700_000_000_000

function input(overrides: Record<string, unknown> = {}) {
  return {
    id: 'task-1',
    title: '核对事项',
    description: '',
    notes: '',
    primary: null,
    related: [],
    nextSteps: ['完成核对'],
    status: 'todo',
    priority: 'normal',
    ...overrides
  }
}

describe('task overview domain model', () => {
  it('preserves source-bound analysis separately from manual state and acceptance', () => {
    const primary = { storedId: 'source', connectionId: 'local', profile: 'default', targetProfile: 'default', mode: 'local' as const }

    const result = {
      version: 1, title: '核对事项', analyzed_at: at, conversation_revision: [1], stale: false, scope: 'single_conversation',
      message_count: 2, analyzed_messages: 2, limitations: [], status: 'awaiting_acceptance',
      goal: { text: '核对结果', evidence: [{ row_id: 1, quote: '核对结果' }] }, completed: [], remaining: [], needs_user: []
    }

    const analysis = { title: '核对事项', referenceKey: taskReferenceKey(primary), result }
    const task = createTask(input({ primary, analysis }), at)

    expect(validateTasks([task])[0]).toMatchObject({ analysis, status: 'todo', resolution: { state: 'open' } })
    expect(applyTaskAction(task, { type: 'accept', note: '人工确认', at: at + 1 })).toMatchObject({ analysis, status: 'done' })
    const relinked = applyTaskAction(task, { type: 'set_primary', primary: { ...primary, storedId: 'other' }, at: at + 1 })
    expect(relinked.analysis).toBeUndefined()
    expect(validateTasks([{ ...task, title: '另一事项' }])[0].analysis).toBeUndefined()
  })

  it('does not allow completion without explicit acceptance', () => {
    const task = createTask(input(), at)

    expect(() => applyTaskAction(task, { type: 'set_status', status: 'done', at: at + 1 })).toThrow(
      /done/i
    )
    expect(task.resolution.state).toBe('open')
  })

  it('records acceptance separately from ordinary status changes', () => {
    const task = createTask(input(), at)
    const accepted = applyTaskAction(task, { type: 'accept', note: '已核对结果', at: at + 1 })

    expect(accepted.status).toBe('done')
    expect(accepted.resolution).toEqual({ state: 'accepted', at: at + 1, note: '已核对结果' })
    expect(validateTasks([accepted])).toEqual([accepted])
  })

  it('reads the legacy plugin reference shape without inventing an owner', () => {
    const task = createTask(input({
      primary: { storedId: 'legacy-session', unverified: true }
    }), at)

    expect(task.primary).toStrictEqual({ storedId: 'legacy-session', unverified: true })
  })

  it('preserves a captured live owner witness when saving and rereading a reference', () => {
    const primary = {
      storedId: 'session-a', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      storeInstanceId: 'store-a',
      expectedOwner: { instanceId: 'runtime-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    const task = createTask(input({ primary }), at)

    expect(task.primary).toStrictEqual(primary)
    expect(validateTasks([task])[0].primary).toStrictEqual(primary)
  })

  it('rejects an incomplete owner witness instead of silently dropping it', () => {
    const primary = {
      storedId: 'session-a', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'runtime-a', profileId: '/profiles/worker', authorityEpoch: 3 }
    }

    expect(() => createTask(input({ primary }), at)).toThrow(/owner/i)
  })

  it('relinks a task without changing its acceptance or remaining work', () => {
    const task = createTask(input({ primary: { storedId: 'legacy-session', unverified: true } }), at)
    const primary = { storedId: 'source-session', connectionId: 'local', profile: 'default', targetProfile: 'default', mode: 'local' as const }
    const linked = applyTaskAction(task, { type: 'set_primary', primary, at: at + 1 })

    expect(linked.primary).toStrictEqual(primary)
    expect(linked.nextSteps).toEqual(task.nextSteps)
    expect(linked.resolution).toEqual({ state: 'open' })
    expect(task.primary).toEqual({ storedId: 'legacy-session', unverified: true })
  })

  it('preserves the old complete reference shape and field order', () => {
    const primary = {
      storedId: 'legacy-session',
      connectionId: 'host',
      profile: 'default',
      targetProfile: 'default',
      mode: 'remote',
      unverified: true
    }

    const task = createTask(input({ primary }), at)

    expect(JSON.stringify(task.primary)).toBe(JSON.stringify(primary))
    expect(task.primary).toStrictEqual(primary)
  })

  it('canonicalizes a verified source reference independently of object field order', () => {
    const first = {
      connectionId: 'source-a', profile: 'default', targetProfile: 'worker', storedId: 'session-a', mode: 'remote' as const,
      expectedOwner: { instanceId: 'runtime-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    const second = {
      storedId: 'session-a', targetProfile: 'worker', mode: 'remote' as const, profile: 'default', connectionId: 'source-a',
      expectedOwner: { storeInstanceId: 'store-a', authorityEpoch: 3, profileId: '/profiles/worker', instanceId: 'runtime-a' }
    }

    expect(taskReferenceKey(first)).toBe(taskReferenceKey(second))
  })

  it('keeps same stored ids distinct when their source owner changes', () => {
    const base = { connectionId: 'source-a', profile: 'default', targetProfile: 'worker', storedId: 'session-a', mode: 'remote' as const }
    expect(taskReferenceKey({ ...base, expectedOwner: { instanceId: 'one', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' } }))
      .not.toBe(taskReferenceKey({ ...base, expectedOwner: { instanceId: 'two', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' } }))
  })
})
