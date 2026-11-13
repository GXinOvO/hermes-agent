import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as overview from '../src/core.mjs'

const route = connectionId => ({ connectionId, profile: 'default', targetProfile: 'default', mode: 'remote' })
const scope = (connectionId, storedId) => ({ storedId, runtimeId: null, owner: { connectionId, profile: 'default' }, execution: 'unknown' })
const snapshot = (connectionId, input) => ({ ...overview.deriveOverview(input), sourceRoute: route(connectionId) })
const record = (connectionId, storedId, attrs = 'title="构建" remaining="验证"') => snapshot(connectionId, {
  messages: [{ role: 'user', row_id: 1, text: '交付构建' }, { role: 'assistant', row_id: 2, text: `::task{${attrs}}` }],
  scope: scope(connectionId, storedId)
})
const row = (storedId, snapshot, manuallyTracked = false) => ({ storedId, title: `会话 ${storedId}`, snapshot, manuallyTracked })
const source = (connectionId, rows, complete = true) => ({ route: route(connectionId), rows, complete })

test('fresh registrations and paired pending todos count as unfinished without inferring all conversations are tasks', () => {
  assert.equal(typeof overview.buildFollowupQueue, 'function')
  const pending = snapshot('a', { messages: [{ role: 'user', row_id: 1, text: '修复' }], scope: scope('a', 'todo'), todoState: { revision: 2, todos: [{ id: 't1', content: '测试', status: 'pending' }] } })
  const empty = snapshot('a', { messages: [{ role: 'user', row_id: 1, text: '什么是文件？' }], scope: scope('a', 'question') })
  const result = overview.buildFollowupQueue([source('a', [row('registered', record('a', 'registered')), row('todo', pending), row('question', empty)])])
  assert.deepEqual(result.items.map(x => x.storedId), ['registered', 'todo'])
  assert.equal(result.counts.trackedLowerBound, 2)
  assert.equal(result.counts.unreviewedSessions, 1)
  assert.equal(result.counts.trackedExact, null)
})

test('identical stored ids in different connections are distinct and keep the correct owner route', () => {
  const result = overview.buildFollowupQueue([source('a', [row('same', record('a', 'same'))]), source('b', [row('same', record('b', 'same'))])])
  assert.equal(result.items.length, 2)
  assert.deepEqual(result.items.map(x => x.route.connectionId), ['a', 'b'])
  assert.notEqual(result.items[0].key, result.items[1].key)
  assert.equal(result.counts.trackedExact, 2)
})

test('a missing source and missing snapshots never pretend to be an empty or complete queue', () => {
  const result = overview.buildFollowupQueue([source('a', [row('known', record('a', 'known')), row('unknown', null)]), { route: route('b'), rows: [], complete: false, error: 'source unavailable' }])
  assert.equal(result.items.length, 1)
  assert.equal(result.counts.trackedLowerBound, 1)
  assert.equal(result.counts.trackedExact, null)
  assert.equal(result.coverage.complete, false)
  assert.equal(result.coverage.unavailableSources, 1)
  assert.equal(result.counts.unreviewedSessions, 1)
})

test('a snapshot from another owner or stored session is rejected without borrowing its title or status', () => {
  const result = overview.buildFollowupQueue([source('a', [row('intended', record('b', 'other'))])])
  assert.equal(result.items.length, 0)
  assert.equal(result.counts.unreviewedSessions, 1)
  assert.equal(result.counts.trackedExact, null)
  assert.equal(result.coverage.complete, false)
})

test('a snapshot is not reused when a route maps to a different backend profile', () => {
  const value = record('a', 'mapped')
  value.sourceRoute = { connectionId: 'a', profile: 'default', targetProfile: 'old-backend' }
  const result = overview.buildFollowupQueue([source('a', [row('mapped', value, true)])])
  assert.equal(result.items.length, 1)
  assert.equal(result.items[0].snapshot, null)
  assert.equal(result.items[0].followup.recordFreshness, 'unknown')
  assert.equal(result.coverage.complete, false)
  assert.equal(result.counts.trackedExact, null)
})

test('a snapshot with no captured backend profile is unproven and never authoritative', () => {
  const value = record('a', 'mapped')
  delete value.sourceRoute
  const result = overview.buildFollowupQueue([source('a', [row('mapped', value, true)])])
  assert.equal(result.items[0].snapshot, null)
  assert.equal(result.counts.trackedExact, null)
})

test('a stale tracked record stays in the queue but does not assert its old decision is current', () => {
  const value = snapshot('a', { messages: [{ role: 'user', row_id: 1, text: '完成任务' }, { role: 'assistant', row_id: 2, text: '::task{title="任务" decision="请确认范围"}' }, { role: 'user', row_id: 3, text: '已经改了要求' }], scope: scope('a', 'stale') })
  const result = overview.buildFollowupQueue([source('a', [row('stale', value)])])
  assert.equal(result.items.length, 1)
  assert.equal(result.items[0].followup.work, 'needs_review')
  assert.equal(result.counts.attentionLowerBound, 0)
  assert.equal(result.items[0].followup.recordFreshness, 'stale')
})

test('explicitly tracked unregistered conversation remains visible without claiming acceptance', () => {
  const value = snapshot('a', { messages: [{ role: 'user', row_id: 1, text: '继续做这件事' }], scope: scope('a', 'chosen') })
  const result = overview.buildFollowupQueue([source('a', [row('chosen', value, true)])])
  assert.equal(result.items.length, 1)
  assert.equal(result.items[0].followup.work, 'needs_review')
  assert.equal(result.items[0].followup.explicitAccepted, false)
})
