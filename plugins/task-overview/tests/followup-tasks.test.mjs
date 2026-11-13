import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as overview from '../src/core.mjs'

const ref = (storedId = 'session-a', connectionId = 'host-a', profile = 'default', targetProfile = 'default') => ({ connectionId, profile, targetProfile, storedId })
const input = (id = 'task-1', primary = ref()) => ({ id, title: '交付事项', primary, related: [], nextSteps: ['核对结果'] })

const create = (value = input()) => overview.createFollowupTask(value, 1000)

test('two separately identified matters can belong to the same conversation', () => {
  assert.equal(typeof overview.createFollowupTask, 'function')
  const tasks = overview.validateFollowupTasks([create(input('task-1')), create(input('task-2'))])
  assert.equal(tasks.length, 2)
  assert.equal(tasks[0].primary.storedId, tasks[1].primary.storedId)
  assert.notEqual(tasks[0].id, tasks[1].id)
})

test('a first-version task can be created without a session and keeps its status, priority and description', () => {
  const task = create({
    id: 'standalone',
    title: '整理方案',
    description: '先梳理现有会话',
    primary: null,
    related: [],
    nextSteps: ['列出待核对内容'],
    status: 'todo',
    priority: 'high'
  })
  assert.equal(task.primary, null)
  assert.equal(task.description, '先梳理现有会话')
  assert.equal(task.status, 'todo')
  assert.equal(task.priority, 'high')
})

test('task status can move between todo, in-progress and waiting without implying acceptance', () => {
  const task = create({ ...input(), status: 'todo', priority: 'normal' })
  const working = overview.applyFollowupAction(task, { type: 'set_status', status: 'in_progress', at: 1100 })
  assert.equal(working.status, 'in_progress')
  assert.equal(working.resolution.state, 'open')
  const waiting = overview.applyFollowupAction(working, { type: 'set_status', status: 'waiting', at: 1200 })
  assert.equal(waiting.status, 'waiting')
  assert.equal(waiting.resolution.state, 'open')
})

test('acceptance is the only transition to done and reopening returns to todo', () => {
  const task = create({ ...input(), status: 'in_progress', priority: 'normal' })
  const accepted = overview.applyFollowupAction(task, { type: 'accept', at: 1200, note: '本人已核对' })
  assert.equal(accepted.status, 'done')
  const reopened = overview.applyFollowupAction(accepted, { type: 'reopen', at: 1300 })
  assert.equal(reopened.status, 'todo')
  assert.equal(reopened.resolution.state, 'open')
})

test('a task without a primary session can bind it exactly once', () => {
  const task = create({ id: 'unbound', title: '待绑定事项', primary: null, related: [], nextSteps: [], status: 'todo', priority: 'normal' })
  const primary = ref('session-bound', 'host-b', 'default', 'worker')
  const bound = overview.applyFollowupAction(task, { type: 'bind_primary', primary, at: 1100 })
  assert.deepEqual(bound.primary, primary)
  assert.equal(bound.status, 'todo')
  assert.throws(() => overview.applyFollowupAction(bound, { type: 'bind_primary', primary: ref('other'), at: 1200 }), /primary|bound|action/i)
})

test('an older open task without a manual status stays unspecified', () => {
  const legacy = { version: 1, ...input(), createdAt: 1000, updatedAt: 1000, resolution: { state: 'open' } }
  const [restored] = overview.validateFollowupTasks([legacy])
  assert.equal(restored.status, 'unspecified')
  assert.equal(restored.resolution.state, 'open')
})

test('explicitly unverified session references survive storage without guessing an owner', () => {
  const primary = { storedId: 'cold-session', connectionId: 'host-a', profile: 'default', unverified: true }
  const related = { storedId: 'other-session', unverified: true }
  const task = create({ ...input(), primary, related: [related] })
  const restored = overview.validateFollowupTasks(JSON.parse(JSON.stringify([task])))[0]
  assert.deepEqual(restored.primary, primary)
  assert.deepEqual(restored.related, [related])
  assert.equal(Object.hasOwn(restored.primary, 'targetProfile'), false)
})

test('task notes survive editing and JSON restoration', () => {
  const task = create({ ...input(), notes: '等待负责人反馈' })
  assert.equal(task.notes, '等待负责人反馈')
  const edited = overview.applyFollowupAction(task, { type:'revise', at:1100, title:task.title, nextSteps:task.nextSteps, related:task.related, notes:'反馈已收到' })
  assert.equal(overview.validateFollowupTasks(JSON.parse(JSON.stringify([edited])))[0].notes, '反馈已收到')
})

test('a task persists exact source identity and related conversations through JSON', () => {
  const item = create({ ...input(), related: [ref('session-b', 'host-b', 'default', 'worker')] })
  const restored = overview.validateFollowupTasks(JSON.parse(JSON.stringify([item])))
  assert.deepEqual(restored[0].primary, ref())
  assert.deepEqual(restored[0].related, [ref('session-b', 'host-b', 'default', 'worker')])
  assert.equal(restored[0].resolution.state, 'open')
})

test('known connection modes survive storage without inventing modes for legacy references', () => {
  const known = { ...ref(), mode: 'remote' }
  const unknown = ref('legacy-session')
  const item = create({ ...input('mode-aware', known), related: [unknown] })
  const restored = overview.validateFollowupTasks(JSON.parse(JSON.stringify([item])))[0]
  assert.equal(restored.primary.mode, 'remote')
  assert.equal(Object.hasOwn(restored.related[0], 'mode'), false)
  assert.throws(() => create(input('invalid-mode', { ...known, mode: 'sideways' })), /identity|reference|mode/i)
})

test('a remapped backend with the same connection, profile and ID remains a distinct reference', () => {
  const old = ref('shared-id', 'host-a', 'default', 'backend-old')
  const current = ref('shared-id', 'host-a', 'default', 'backend-new')
  const task = create({ ...input('mapped', old), related: [current] })
  assert.deepEqual(task.primary, old)
  assert.deepEqual(overview.validateFollowupTasks(JSON.parse(JSON.stringify([task])))[0].related, [current])
  assert.throws(() => create({ ...input('duplicate', old), related: [old] }), /duplicate/i)
})

test('the owner can explicitly accept and reopen a task without deleting its record', () => {
  const item = create()
  const accepted = overview.applyFollowupAction(item, { type: 'accept', at: 1200, note: '本人已核对' })
  assert.equal(accepted.resolution.state, 'accepted')
  assert.equal(accepted.resolution.note, '本人已核对')
  assert.equal(item.resolution.state, 'open')
  const reopened = overview.applyFollowupAction(accepted, { type: 'reopen', at: 1300 })
  assert.equal(reopened.resolution.state, 'open')
  assert.equal(reopened.id, item.id)
  assert.deepEqual(reopened.primary, item.primary)
})

test('the owner can update the task title, remaining steps and related sessions without changing its primary session', () => {
  const original = create()
  const revised = overview.applyFollowupAction(original, { type: 'revise', at: 1100, title: '更新事项', nextSteps: ['重新验证', '核对上线'], related: [ref('review-session', 'host-b')] })
  assert.equal(revised.title, '更新事项')
  assert.deepEqual(revised.nextSteps, ['重新验证', '核对上线'])
  assert.deepEqual(revised.related, [ref('review-session', 'host-b')])
  assert.deepEqual(revised.primary, original.primary)
  assert.equal(revised.createdAt, original.createdAt)
  assert.equal(revised.updatedAt, 1100)
  assert.equal(original.title, '交付事项')
})

test('a completed task must be reopened before revising and invalid edits leave it unchanged', () => {
  const original = create()
  const accepted = overview.applyFollowupAction(original, { type: 'accept', at: 1100, note: '已核对' })
  assert.throws(() => overview.applyFollowupAction(accepted, { type: 'revise', at: 1200, title: 'changed', nextSteps: [], related: [] }), /action/i)
  assert.throws(() => overview.applyFollowupAction(original, { type: 'revise', at: 1200, title: 'changed', nextSteps: [], related: [original.primary] }), /duplicate/i)
  assert.equal(original.resolution.state, 'open')
  assert.deepEqual(original.related, [])
})

test('acceptance rejects an empty or whitespace-only note and does not change the task', () => {
  const item = create()
  for (const note of ['', '  ', '\n\t']) {
    assert.throws(() => overview.applyFollowupAction(item, { type: 'accept', at: 1200, note }), /action|acceptance/i)
    assert.equal(item.resolution.state, 'open')
  }
  const accepted = overview.applyFollowupAction(item, { type: 'accept', at: 1200, note: '已核验结果' })
  assert.equal(accepted.resolution.state, 'accepted')
  assert.equal(accepted.resolution.note, '已核验结果')
})

test('runtime events cannot impersonate explicit owner acceptance', () => {
  const item = create()
  for (const type of ['turn.complete', 'idle', 'todo.completed', 'openSession']) {
    assert.throws(() => overview.applyFollowupAction(item, { type, at: 1200 }), /action/i)
  }
  assert.equal(item.resolution.state, 'open')
})

test('malformed or ambiguous owners, duplicate links, and duplicate task IDs are refused', () => {
  for (const primary of [ref('', 'host-a'), ref('x', '', 'default'), ref('x', 'host-a', '', 'default'), ref('x', 'host-a', 'default', '')]) {
    assert.throws(() => create(input('x', primary)), /identity|reference/i)
  }
  assert.throws(() => create({ ...input(), related: [ref()] }), /duplicate/i)
  assert.throws(() => create({ ...input(), related: [ref('b'), ref('b')] }), /duplicate/i)
  assert.throws(() => overview.validateFollowupTasks([create(), create()]), /duplicate/i)
})

test('owner route target profile survives, including mapped profiles, without being normalized', () => {
  const mapped = ref('id-1', 'host-a', 'default', 'backend-research')
  const item = create(input('mapped', mapped))
  assert.deepEqual(item.primary, mapped)
  assert.throws(() => create(input('bad', { ...mapped, targetProfile: ' backend-research ' })), /identity|reference/i)
})

test('invalid persisted records cannot silently turn into a clean empty list', () => {
  for (const value of [null, {}, [null], [{ ...create(), resolution: { state: 'accepted' } }]]) {
    assert.throws(() => overview.validateFollowupTasks(value), /invalid/i)
  }
})
