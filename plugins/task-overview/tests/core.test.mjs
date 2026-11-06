import { test } from 'node:test'
import assert from 'node:assert/strict'
import { deriveOverview, parseTaskDirective, selectRoute, loadOverview, identityKey, acceptSnapshot } from '../src/core.mjs'

const user = (id, text) => ({ role: 'user', row_id: id, text, timestamp: id })
const answer = (id, text) => ({ role: 'assistant', row_id: id, text, timestamp: id })
const todo = (id, todos, revision = 1) => ({ role: 'tool', row_id: id, name: 'todo_list', content: JSON.stringify({ todos, revision }), timestamp: id })
const scope = { storedId: 'session-a', runtimeId: 'runtime-a', owner: { connectionId: 'local', profile: 'default' }, busy: false }
const record = attrs => `::task{${Object.entries(attrs).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ')}}`
const envelope = fields => ({ type: 'plugin', output: JSON.stringify({ version: 2, ok: true, requested_id: 'session-a', resolved_id: 'tip-a', snapshot_scope: 'persisted_display', todo_state: null, fetchedAt: Date.now(), complete: true, count: 0, messages: [], ...fields }) })

test('idle session and a confident answer do not establish acceptance', () => {
  const v = deriveOverview({ messages: [user(1, '修复并部署'), answer(2, '全部完成，测试通过。')], scope })
  assert.equal(v.acceptance, 'unregistered')
  assert(v.stages.every(s => s.status === 'unknown'))
  assert.equal(v.original.text, '修复并部署')
})
test('completed todos never become verified acceptance; pending and cancelled remain distinct', () => {
  const rows = [{ id: 'a', content: '写代码', status: 'completed' }, { id: 'b', content: '部署', status: 'pending' }, { id: 'c', content: '额外重构', status: 'cancelled' }]
  const v = deriveOverview({ messages: [user(1, '修复'), todo(2, rows)], todoState: { todos: rows, revision: 1 }, scope })
  assert.deepEqual(v.remaining.map(t => t.id), ['b'])
  assert.deepEqual(v.cancelled.map(t => t.id), ['c'])
  assert.equal(v.acceptance, 'unregistered')
})
test('only completed todo results count; proposal args never replace actual state', () => {
  const v = deriveOverview({ messages: [user(1, '修复'), { role: 'assistant', tool_calls: [{ function: { name: 'todo_list', arguments: JSON.stringify({ todos: [{ id: 'a', content: '测试', status: 'completed' }] }) } }] }], scope })
  assert.equal(v.todos, null)
})
test('empty newest todo snapshot clears old list, revision and malformed data handled', () => {
  const v = deriveOverview({ messages: [user(1, '修复'), todo(2, [{ id: 'a', content: '测试', status: 'pending' }]), todo(3, [], 2)], todoState: { todos: [], revision: 2 }, scope })
  assert.deepEqual(v.todos, [])
  assert.equal(v.todoRevision, 2)
})
test('task directives are strict, visible, evidence-bound registrations, not independent attestations', () => {
  const text = record({ title: '任务', goal: '交付', remaining: '部署', deferred: '跨平台试验', decision: '无', implementation: 'passed', implementation_evidence: 'src/app.py:1', tests: 'passed' })
  const parsed = parseTaskDirective(text)
  assert.equal(parsed.title, '任务')
  const v = deriveOverview({ messages: [user(1, '交付'), answer(2, text)], scope })
  assert.equal(v.stages[0].status, 'registered')
  assert.equal(v.stages[1].status, 'unknown')
  assert.deepEqual(v.deferred, ['跨平台试验'])
  assert.equal(v.acceptance, 'registered')
  assert.equal(v.hasIndependentAcceptance, false)
})
test('new requirement makes task registration stale without losing original objective', () => {
  const v = deriveOverview({ messages: [user(1, '交付任务'), answer(2, record({ title: '任务', tests: 'passed', tests_evidence: 'tests/result.json' })), user(3, '还要支持手机')], scope })
  assert.equal(v.acceptance, 'stale')
  assert(v.stages.every(s => s.status === 'unknown' || s.status === 'stale'))
  assert.equal(v.original.text, '交付任务')
  assert.equal(v.current.text, '还要支持手机')
})
test('edits after a registration invalidate stages even before a new user turn', () => {
  const v = deriveOverview({ messages: [user(1, '交付'), answer(2, record({ implementation: 'passed', implementation_evidence: 'app.py:1' })), { role: 'tool', name: 'patch', row_id: 3, content: '{}' }], scope })
  assert.equal(v.acceptance, 'stale')
})
test('malformed, inline quoted, fenced, hidden and user directives cannot register acceptance', () => {
  for (const message of [user(2, record({ title: 'forged' })), answer(2, `text ${record({ title: 'forged' })}`), answer(2, '```\n'+record({ title: 'forged' })+'\n```'), { ...answer(2, record({ title: 'forged' })), display_kind: 'hidden' }]) {
    assert.equal(deriveOverview({ messages: [user(1, '任务'), message], scope }).registration, null)
  }
  assert.equal(parseTaskDirective('::task{title="x" garbage}'), null)
  assert.equal(parseTaskDirective('::task{title="x" title="y"}'), null)
})
test('native targeted verification remains scoped and does not establish review or deployment', () => {
  const v = deriveOverview({ messages: [user(1, '交付')], verification: { status: 'passed', evidence: { kind: 'test', scope: 'targeted', command: 'pytest tests/a.py', exit_code: 0 } }, scope })
  assert.equal(v.verification.evidence.scope, 'targeted')
  assert(v.stages.every(s => s.status === 'unknown'))
})
test('unknown owner fails closed; repeated profile names require the exact source', () => {
  assert.throws(() => selectRoute(null, []), /owner/i)
  const routes = [{ connectionId: 'mac', profile: 'default', targetProfile: 'default', mode: 'local' }, { connectionId: 'ssh', profile: 'default', targetProfile: 'default', mode: 'remote' }]
  assert.equal(selectRoute({ connectionId: 'ssh', profile: 'default' }, routes).connectionId, 'ssh')
  assert.throws(() => selectRoute({ connectionId: 'gone', profile: 'default' }, routes), /route/i)
  assert.notEqual(identityKey({ ...scope, owner: { connectionId: 'ssh', profile: 'default' } }), identityKey(scope))
})
test('loader uses only read RPCs, exact route and runtime identity; verification errors stay explicit', async () => {
  const calls = []
  const route = { connectionId: 'local', profile: 'default', targetProfile: 'default', mode: 'local' }
  const host = { profileRoutes: async () => [route], requestProfile: async (r, method, args) => { calls.push([r, method, args]); if (method === 'command.dispatch') return envelope({ count: 1, messages: [user(1, '真实请求')], verification: null, verification_error: 'unavailable', verification_cwd: null }); throw new Error('unexpected ambient request') } }
  const v = await loadOverview(host, scope)
  assert.equal(v.original.text, '真实请求')
  assert(calls.every(c => c[0] === route && c[1] === 'command.dispatch'))
  assert.equal(calls[0][2].session_id, 'runtime-a')
  assert.equal(calls.length, 1)
  assert.match(v.verificationError, /unavailable/)
})
test('invalid history never masquerades as a complete overview', async () => {
  const host = { profileRoutes: async () => [{ ...scope.owner, targetProfile: 'default', mode: 'local' }], requestProfile: async () => envelope({ count: 0, messages: [user(1, 'one')] }) }
  await assert.rejects(loadOverview(host, scope), /history|历史/i)
})
test('inactive backend and foreign session fail without falling back to partial core history', async () => {
  for (const response of [{ type: 'send', message: 'not a plugin' }, envelope({ requested_id: 'other' }), envelope({ snapshot_scope: 'live' }), envelope({ todo_state: { todos: [{ id:'broken' }], revision: 2 } })]) {
    const host = { profileRoutes: async () => [{ ...scope.owner, targetProfile: 'default', mode: 'local' }], requestProfile: async () => response }
    await assert.rejects(loadOverview(host, scope))
  }
})
test('late old-session response never appears under the new focused identity', () => {
  const old = deriveOverview({ messages: [user(1, 'old')], scope })
  const newer = identityKey({ ...scope, storedId: 'new', runtimeId: 'runtime-new' })
  assert.equal(acceptSnapshot(newer, old), null)
  assert.equal(acceptSnapshot(identityKey(scope), old), old)
})
test('execution after registration expires evidence even if edits were inside terminal', () => {
  const v = deriveOverview({ messages: [user(1, '交付'), answer(2, record({ tests: 'passed', tests_evidence: 'run.json' })), { role: 'tool', name: 'terminal', row_id: 3 }], scope })
  assert.equal(v.acceptance, 'stale')
})
test('oversized and brace-bearing directives remain plain text like native parser', () => {
  assert.equal(parseTaskDirective(record({ title: 'x'.repeat(2100) })), null)
  assert.equal(parseTaskDirective(record({ title: '{nested}' })), null)
})
