import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildFollowupQueue } from '../src/core.mjs'

const route = { connectionId: 'remote-a', profile: 'default', targetProfile: 'default', mode: 'remote' }

test('without any available sources the count is unknown, not zero completed or zero unfinished', () => {
  const result = buildFollowupQueue([])
  assert.equal(result.items.length, 0)
  assert.equal(result.coverage.complete, false)
  assert.equal(result.counts.trackedExact, null)
})

test('an invalid route cannot become an authoritative source', () => {
  assert.throws(() => buildFollowupQueue([{ route: { ...route, targetProfile: '' }, rows: [], complete: true }]), /source identity/)
})

test('the same session cannot be counted twice within a source', () => {
  const rows = [{ storedId: 's1', snapshot: null }, { storedId: 's1', snapshot: null }]
  assert.throws(() => buildFollowupQueue([{ route, rows, complete: true }]), /duplicate/i)
})
