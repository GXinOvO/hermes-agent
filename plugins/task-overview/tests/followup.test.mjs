import { test } from 'node:test'
import assert from 'node:assert/strict'
import { deriveOverview } from '../src/core.mjs'

const user = (id, text) => ({ role: 'user', row_id: id, text })
const answer = (id, text) => ({ role: 'assistant', row_id: id, text })
const record = attrs => `::task{${Object.entries(attrs).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ')}}`
const scope = (execution = 'idle') => ({ storedId: 'stored-a', runtimeId: 'runtime-a', owner: { connectionId: 'remote-a', profile: 'default' }, execution })

test('an idle session with complete todos still needs business acceptance', () => {
  const value = deriveOverview({ messages: [user(1, '修复并部署'), answer(2, '全部完成')], todoState: { revision: 1, todos: [{ id: 't1', content: '实现', status: 'completed' }] }, scope: scope('idle') })
  assert.equal(value.followup.work, 'needs_review')
  assert.equal(value.followup.execution, 'idle')
  assert.equal(value.followup.actionRequired, false)
  assert.equal(value.followup.explicitAccepted, false)
})

test('a fresh recorded decision needs the user while runtime activity stays separate', () => {
  const value = deriveOverview({ messages: [user(1, '完成导出'), answer(2, record({ title: '导出', decision: '请确认导出范围' }))], scope: scope('working') })
  assert.equal(value.followup.work, 'needs_input')
  assert.equal(value.followup.execution, 'working')
  assert.equal(value.followup.actionRequired, true)
  assert.equal(value.followup.explicitAccepted, false)
})

test('a recorded none decision is not a user action', () => {
  for (const decision of ['无', '暂无', '暂无需决策', '无待决定事项', '无需确认', 'none', 'N/A']) {
    const value = deriveOverview({ messages: [user(1, '完成导出'), answer(2, record({ title: '导出', decision }))], scope: scope('idle') })
    assert.equal(value.followup.work, 'needs_review', decision)
    assert.equal(value.followup.actionRequired, false, decision)
  }
})

test('a no-remaining marker never creates a pending task', () => {
  for (const remaining of ['无', '暂无', '暂无剩余工作', '无剩余必做', 'none', 'N/A']) {
    const value = deriveOverview({ messages: [user(1, '完成导出'), answer(2, record({ title: '导出', remaining }))], scope: scope('idle') })
    assert.equal(value.followup.work, 'needs_review', remaining)
    assert.equal(value.followup.explicitAccepted, false, remaining)
  }
})

test('a stale registered decision is shown for review rather than as a current request', () => {
  const value = deriveOverview({ messages: [user(1, '修复'), answer(2, record({ title: '修复', decision: '请选择 A 或 B' })), user(3, '要求已改变')], scope: scope('idle') })
  assert.equal(value.followup.work, 'needs_review')
  assert.equal(value.followup.actionRequired, false)
  assert.equal(value.followup.recordFreshness, 'stale')
  assert.deepEqual(value.decisions, ['请选择 A 或 B'])
})

test('a paired pending todo remains pending even when a separate task registration is stale', () => {
  const value = deriveOverview({
    messages: [user(1, '修复'), answer(2, record({ title: '修复', remaining: '旧步骤' })), user(3, '补充新要求')],
    todoState: { revision: 2, todos: [{ id: 'next', content: '核对新要求', status: 'pending' }] },
    scope: scope('idle')
  })
  assert.equal(value.followup.recordFreshness, 'stale')
  assert.equal(value.followup.work, 'pending')
  assert.deepEqual(value.remaining.map(todo => todo.id), ['next'])
  assert.equal(value.followup.explicitAccepted, false)
})

test('an explicitly failed stage remains attention-worthy when its record is fresh', () => {
  const value = deriveOverview({ messages: [user(1, '修复'), answer(2, record({ title: '修复', tests: 'failed', tests_evidence: 'tests/run.json' }))], scope: scope('idle') })
  assert.equal(value.followup.work, 'failed')
  assert.equal(value.followup.actionRequired, true)
  assert.equal(value.followup.explicitAccepted, false)
})

test('unavailable execution evidence is unknown, not idle or completed', () => {
  const value = deriveOverview({ messages: [user(1, '修复')], scope: scope('unknown') })
  assert.equal(value.followup.execution, 'unknown')
  assert.equal(value.followup.work, 'needs_review')
  assert.equal(value.followup.explicitAccepted, false)
})
