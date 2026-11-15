import { expect, test } from 'vitest'

import { exactSessionOwnerToWire } from '@/lib/exact-session-owner'

import { deriveSessionOverview, discoverTaskCandidates, parseTaskDirective, validateSessionOverview } from './session-review'

const owner = { instanceId: 'source', profileId: '/source', authorityEpoch: 2, storeInstanceId: 'store' }
const request = { runtimeId: 'runtime', storedId: 'stored', owner }
const message = (role: 'user' | 'assistant', row_id: number, content: string) => ({ role, row_id, content })

const envelope = (over = {}) => ({ version: 1 as const, session_id: 'runtime', stored_session_id: 'stored', resolved_id: 'tip',
  owner: exactSessionOwnerToWire(owner), authority_epoch: 2, snapshot_scope: 'persisted_display' as const, complete: true as const,
  fetched_at: 1234, conversation_revision: [], messages: [], todo_state: null,
  verification: null, verification_cwd: null, verification_error: 'workspace unavailable', ...over })

test('keeps original and current requirements and native checklist separate from acceptance', () => {
  const value = deriveSessionOverview(envelope({ messages: [message('user', 1, 'Deliver the feature'),
    message('assistant', 2, 'Everything is finished'), message('user', 3, 'Also verify remote')],
  todo_state: { revision: 3, todos: [{ id: 'done', content: 'Implementation', status: 'completed' },
    { id: 'wait', content: 'Remote adoption', status: 'pending' }, { id: 'no', content: 'Optional', status: 'cancelled' }] } }))

  expect(value.original?.content).toBe('Deliver the feature')
  expect(value.current?.content).toBe('Also verify remote')
  expect(value.remaining.map(item => item.id)).toEqual(['wait'])
  expect(value.acceptance).toBe('unregistered')
  expect(value.stages.every(stage => stage.status === 'unknown')).toBe(true)
  expect(value.discoveredTasks.map(candidate => candidate.content)).toEqual(['Remote adoption'])
})

test('discovers deduplicated candidates only from unfinished or explicitly recorded work', () => {
  const candidates = discoverTaskCandidates({
    todos: [
      { id: 'pending', content: 'Remote validation', status: 'pending' },
      { id: 'done', content: 'Ship feature', status: 'completed' },
      { id: 'active', content: 'Add metrics', status: 'in_progress' }
    ],
    registeredRemaining: ['Remote validation'],
    deferred: ['Mobile support'],
    decisions: ['Choose server'],
    source: { role: 'assistant', row_id: 7, content: '::task{remaining="Remote validation"}' },
    registrationStale: false
  })

  expect(candidates).toEqual([
    { content: 'Remote validation', evidence: 'native_todo', sourceRow: null },
    { content: 'Add metrics', evidence: 'native_todo', sourceRow: null },
    { content: 'Mobile support', evidence: 'registered_deferred', sourceRow: 7 },
    { content: 'Choose server', evidence: 'registered_decision', sourceRow: 7 }
  ])
})

test('only visible top-level task records count and later execution expires their evidence', () => {
  const record = '::task{title="Feature" remaining="Remote adoption" deferred="Mobile" decision="Choose host" tests="passed" tests_evidence="tests/receipt.json" review="passed"}'
  expect(parseTaskDirective(record)?.title).toBe('Feature')

  for (const content of ['```\n' + record + '\n```', '> ' + record, '- ' + record, '<div>\n' + record + '\n</div>', '$$\n' + record + '\n$$']) {
    expect(deriveSessionOverview(envelope({ messages: [message('assistant', 1, content)] })).registration).toBeNull()
  }

  const value = deriveSessionOverview(envelope({ messages: [message('user', 1, 'Build it'),
    message('assistant', 2, record), { role: 'tool', row_id: 3, name: 'terminal' }] }))

  expect(value.acceptance).toBe('stale')
  expect(value.stages.find(stage => stage.id === 'tests')?.status).toBe('stale')
  expect(value.stages.find(stage => stage.id === 'review')?.status).toBe('unknown')
  expect(value.registeredRemaining).toEqual(['Remote adoption'])
  expect(value.deferred).toEqual(['Mobile'])
  expect(value.decisions).toEqual(['Choose host'])
  expect(value.discoveredTasks).toEqual([])
})

test('ignores hidden summaries, preserves native clears, and never derives todos from tool-shaped JSON', () => {
  const messages = [message('user', 1, 'Original'), { ...message('user', 2, 'Compression summary'), _compressed_summary: true },
    { role: 'tool', row_id: 3, name: 'todo_list', content: '{"todos":[{"id":"fake","content":"Fake","status":"completed"}],"revision":99}' }]

  expect(deriveSessionOverview(envelope({ messages })).todos).toBeNull()
  const value = deriveSessionOverview(envelope({ messages, todo_state: { revision: 4, todos: [] } }))
  expect(value.current?.content).toBe('Original')
  expect(value.todos).toEqual([])
  expect(value.todoRevision).toBe(4)
})

test('validates echoed target and whole source witness before accepting an overview', () => {
  expect(() => validateSessionOverview(envelope(), request)).not.toThrow()

  for (const over of [{ session_id: 'other' }, { stored_session_id: 'other' }, { owner: { ...exactSessionOwnerToWire(owner), instance_id: 'replacement' } },
    { complete: false }, { snapshot_scope: 'live' }, { messages: null }, { todo_state: { revision: 1, todos: [{ id: 'broken' }] } }]) {
    expect(() => validateSessionOverview(envelope(over), request)).toThrow()
  }
})

test('keeps registered outcome separate from a later assistant correction', () => {
  const value = deriveSessionOverview(envelope({ messages: [message('user', 1, 'Build it'),
    message('assistant', 2, '::task{title="Feature" result="Everything succeeded"}'),
    message('assistant', 3, 'Correction: remote verification failed.')] }))

  expect(value.registeredResult).toBe('Everything succeeded')
  expect(value.latest?.content).toBe('Correction: remote verification failed.')
})

test('does not let a registration-only assistant message replace a prior reply', () => {
  const value = deriveSessionOverview(envelope({ messages: [message('user', 1, 'Build it'),
    message('assistant', 2, 'The feature is ready for remote validation.'),
    message('assistant', 3, '::task{title="Feature" tests="pending"}')] }))

  expect(value.latest?.content).toBe('The feature is ready for remote validation.')
})

test('removes only top-level accepted records from reply text and keeps fenced examples', () => {
  const record = '::task{title="Feature" result="Recorded"}'

  const value = deriveSessionOverview(envelope({ messages: [message('assistant', 1,
    'Current reply\n\n' + record + '\n\n```\n' + record + '\n```')] }))

  expect(value.registeredResult).toBe('Recorded')
  expect(value.latest?.content).toBe('Current reply\n\n\n\n```\n' + record + '\n```')
})
