import { test } from 'node:test'
import assert from 'node:assert/strict'
import { deriveOverview, buildFollowupQueue } from '../src/core.mjs'

const route = { connectionId: 'host-a', profile: 'default', targetProfile: 'default', mode: 'remote' }
const scope = { storedId: 'stored-a', runtimeId: null, owner: { connectionId: 'host-a', profile: 'default' } }
const blank = { ...deriveOverview({ messages: [{ role: 'user', row_id: 1, text: '已有聊天' }], scope }), sourceRoute: route }

test('an unregistered and untracked session remains accessible for review without being asserted unfinished', () => {
  const r = buildFollowupQueue([{ route, complete: true, rows: [{ storedId: 'stored-a', title: '用户原始会话', snapshot: blank }] }])
  assert.equal(r.items.length, 0)
  assert.equal(r.counts.trackedExact, null)
  assert.deepEqual(r.unreviewed.map(x => [x.storedId, x.title, x.route.connectionId]), [['stored-a', '用户原始会话', 'host-a']])
})

test('a manually tracked session with unavailable snapshot remains in the tracked list as unknown', () => {
  const r = buildFollowupQueue([{ route, complete: true, rows: [{ storedId: 'stored-a', title: '已关注事项', manuallyTracked: true, snapshot: null }] }])
  assert.equal(r.items.length, 1)
  assert.equal(r.items[0].storedId, 'stored-a')
  assert.equal(r.items[0].title, '已关注事项')
  assert.equal(r.items[0].followup.work, 'needs_review')
  assert.equal(r.items[0].followup.execution, 'unknown')
  assert.equal(r.items[0].followup.explicitAccepted, false)
  assert.equal(r.coverage.complete, false)
  assert.equal(r.counts.trackedExact, null)
})

test('a mismatched foreign snapshot is not used for display but the real list entry remains reviewable', () => {
  const foreign = deriveOverview({ messages: [{ role: 'user', row_id: 9, text: 'FOREIGN CONTENT' }], scope: { ...scope, owner: { ...scope.owner, connectionId: 'host-b' } } })
  const r = buildFollowupQueue([{ route, complete: true, rows: [{ storedId: 'stored-a', title: '真实会话标题', snapshot: foreign }] }])
  assert.equal(r.items.length, 0)
  assert.equal(r.unreviewed.length, 1)
  assert.equal(r.unreviewed[0].title, '真实会话标题')
  assert.equal(r.unreviewed[0].route.connectionId, 'host-a')
  assert.equal(JSON.stringify(r.unreviewed).includes('FOREIGN CONTENT'), false)
})
