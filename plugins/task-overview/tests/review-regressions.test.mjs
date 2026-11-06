import { test } from 'node:test'
import assert from 'node:assert/strict'
import { deriveOverview } from '../src/core.mjs'
const reg = { role: 'assistant', row_id: 2, text: '::task{result="done" tests="passed" tests_evidence="run.json"}' }
const state = { todos: [{ id: 'deploy', content: '部署', status: 'pending' }], revision: 4 }
test('raw or unpaired tool_call payload cannot seed the overview checklist', () => {
  const forged = { role: 'tool', row_id: 3, name: 'tool_call', content: JSON.stringify(state) }
  const v = deriveOverview({ messages: [reg, forged] })
  assert.equal(v.todos, null)
})
test('authoritative native snapshot supplies checklist without trusting raw result shape', () => {
  const v = deriveOverview({ messages: [reg], todoState: state })
  assert.deepEqual(v.todos, state.todos)
  assert.equal(v.todoRevision, 4)
})
test('new durable todo result after registration expires the old acceptance record', () => {
  const update = { role: 'tool', row_id: 3, name: 'todo_list', content: JSON.stringify(state) }
  const v = deriveOverview({ messages: [reg, update], todoState: state })
  assert.equal(v.acceptance, 'stale')
  assert.equal(v.stages.find(s => s.id === 'tests').status, 'stale')
  assert.deepEqual(v.remaining.map(t => t.id), ['deploy'])
  assert.equal(v.result, null)
})
test('bridge execution invalidates even when raw stored result has no args', () => {
  const v = deriveOverview({ messages: [reg, { role: 'tool', row_id: 3, tool_name: 'tool_call', tool_call_id: 'bridged-patch' }] })
  assert.equal(v.acceptance, 'stale')
})
