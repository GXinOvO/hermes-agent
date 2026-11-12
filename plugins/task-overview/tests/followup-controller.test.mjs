import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as controller from '../src/followup-controller.mjs'
import { applyFollowupAction } from '../src/followup-tasks.mjs'

const route = { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker', mode: 'remote' }
const makeHost = () => {
  let selected = { storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker', mode: 'remote' } }
  const opened = []
  return {
    state: {
      focusedStoredSessionId: { get: () => selected.storedId },
      focusedSessionOwner: { get: () => selected.owner }
    },
    setSelection: value => { selected = value },
    profileRoutes: async () => [route],
    openExactSession: async (...args) => opened.push(args),
    opened
  }
}
const makeStore = () => {
  const created = []
  const replaced = []
  return {
    created, replaced,
    create: async task => { created.push(task); return { task, revision: 1 } },
    replace: async (task, revision) => { replaced.push([task, revision]); return { task, revision: revision + 1 } },
    changePrimary: async (id, revision, primary, at) => {
      const task = applyFollowupAction({ version: 1, id, title: '待绑定', description: '', primary: null, related: [], nextSteps: [], status: 'todo', priority: 'normal', createdAt: 1000, updatedAt: 1000, resolution: { state: 'open' } }, { type: 'set_primary', primary, at })
      replaced.push([task, revision]); return { task, revision: revision + 1 }
    }
  }
}
const fields = { id: 'matter-1', title: '确认交付', nextSteps: ['核对结果'], related: [] }

test('standalone task creation does not require a focused session', async () => {
  const store = makeStore()
  const result = await controller.createStandaloneTask(store, { id: 'standalone', title: '整理方案', description: '先梳理', nextSteps: [], related: [], status: 'todo', priority: 'high' }, 1000)
  assert.equal(result.task.primary, null)
  assert.equal(result.task.status, 'todo')
  assert.equal(result.task.priority, 'high')
  assert.equal(store.created.length, 1)
})

test('a host with partial focused ownership can record the session without inventing a backend', async () => {
  const host = makeHost(), store = makeStore()
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default' } })
  const captured = await controller.captureSessionForTracking(host)
  assert.deepEqual(captured, { storedId: 'session-1', connectionId: 'ssh-a', profile: 'default', unverified: true })
  const row = await controller.trackCapturedSession(host, store, fields, 1000, captured)
  assert.deepEqual(row.task.primary, captured)
  assert.equal(row.task.status, 'unspecified')
  await assert.rejects(controller.openTrackedReference(host, row.task.primary), error => error.code === 'FOLLOWUP_REFERENCE_IDENTITY_UNAVAILABLE')
  assert.deepEqual(host.opened, [])
})

test('a recorded store identity alone remains an unverified session clue', async () => {
  const host = makeHost(), store = makeStore()
  host.setSelection({ storedId: 'session-1', owner: { ...route, storeInstanceId: 'copied-store' } })
  const captured = await controller.captureSessionForTracking(host)
  assert.equal(captured.unverified, true)
  const row = await controller.trackCapturedSession(host, store, fields, 1000, captured)
  assert.equal(row.task.primary.unverified, true)
  assert.equal(row.task.primary.storeInstanceId, 'copied-store')
  await assert.rejects(controller.openTrackedReference(host, row.task.primary))
  assert.deepEqual(host.opened, [])
})

test('an unverified captured session cannot be saved after focus changes', async () => {
  const host = makeHost(), store = makeStore()
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default' } })
  const captured = await controller.captureSessionForTracking(host)
  host.setSelection({ storedId: 'session-2', owner: { connectionId: 'ssh-a', profile: 'default' } })
  await assert.rejects(controller.trackCapturedSession(host, store, fields, 1000, captured), /focus|session.*changed/i)
  assert.equal(store.created.length, 0)
})

test('an unbound task records the focused session as unverified without changing status', async () => {
  const host = makeHost(), store = makeStore()
  const task = { version: 1, id: 'unbound', title: '待绑定', description: '', primary: null, related: [], nextSteps: [], status: 'todo', priority: 'normal', createdAt: 1000, updatedAt: 1000, resolution: { state: 'open' } }
  const row = { task, revision: 1 }
  const result = await controller.bindPrimarySession(host, store, row, 1100)
  assert.deepEqual(result.task.primary, { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker', storedId: 'session-1', mode: 'remote', unverified: true })
  assert.equal(result.task.status, 'todo')
  assert.equal(store.replaced[0][1], 1)
})

test('explicit tracking stores the observed source and session as unverified', async () => {
  const host = makeHost(), store = makeStore()
  const captured = await controller.captureFocusedSession(host)
  const result = await controller.trackFocusedSession(host, store, fields, 1000, captured)
  assert.deepEqual(result.task.primary, { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker', storedId: 'session-1', mode: 'remote', unverified: true })
  assert.equal(result.task.resolution.state, 'open')
  assert.equal(result.revision, 1)
  assert.equal(store.created.length, 1)
})

test('focused owner without a proven backend target must not create a tracked item', async () => {
  const host = makeHost(), store = makeStore()
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default', mode: 'remote' } })
  await assert.rejects(controller.captureFocusedSession(host), /owner|identity/i)
  assert.equal(store.created.length, 0)
})

test('focused owner target mismatch is not relabelled as the current route', async () => {
  const host = makeHost()
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default', targetProfile: 'previous-backend', mode: 'remote' } })
  await assert.rejects(controller.captureFocusedSession(host), /route|owner|identity/i)
})

test('focused owner target changing while route is being read is refused', async () => {
  const host = makeHost()
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker', mode: 'remote' } })
  let resume
  host.profileRoutes = () => new Promise(resolve => { resume = resolve })
  const pending = controller.captureFocusedSession(host)
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default', targetProfile: 'other', mode: 'remote' } })
  resume([route])
  await assert.rejects(pending, /focus|owner|changed/i)
})

test('losing a previously known focused backend target during capture is refused', async () => {
  const host = makeHost()
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker', mode: 'remote' } })
  let resume
  host.profileRoutes = () => new Promise(resolve => { resume = resolve })
  const pending = controller.captureFocusedSession(host)
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default', mode: 'remote' } })
  resume([route])
  await assert.rejects(pending, /focus|owner|changed/i)
})

test('a session switch between opening and submitting the tracking form is refused', async () => {
  const host = makeHost(), store = makeStore()
  const captured = await controller.captureFocusedSession(host)
  host.setSelection({ storedId: 'session-2', owner: { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker', mode: 'remote' } })
  await assert.rejects(controller.trackFocusedSession(host, store, fields, 1000, captured), /focus|selection|session.*changed/i)
  assert.equal(store.created.length, 0)
})

test('tracking requires the identity captured when the user opened the form', async () => {
  const host = makeHost(), store = makeStore()
  await assert.rejects(controller.trackFocusedSession(host, store, fields, 1000), /captur|selection|identity/i)
  assert.equal(store.created.length, 0)
  const captured = await controller.captureFocusedSession(host)
  const row = await controller.trackFocusedSession(host, store, fields, 1000, captured)
  assert.equal(row.task.primary.storedId, 'session-1')
  assert.equal(store.created.length, 1)
})

test('capture refuses an owner switch during route discovery', async () => {
  const host = makeHost()
  let resume
  host.profileRoutes = () => new Promise(resolve => { resume = resolve })
  const pending = controller.captureFocusedSession(host)
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-b', profile: 'default', targetProfile: 'backend-worker', mode: 'remote' } })
  resume([route])
  await assert.rejects(pending, /focus|selection|session.*changed/i)
})

test('a selection switch while resolving the route refuses to save the old session', async () => {
  const host = makeHost(), store = makeStore()
  const captured = await controller.captureFocusedSession(host)
  let resume
  host.profileRoutes = () => new Promise(resolve => { resume = resolve })
  const pending = controller.trackFocusedSession(host, store, fields, 1000, captured)
  host.setSelection({ storedId: 'session-2', owner: { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker', mode: 'remote' } })
  resume([route])
  await assert.rejects(pending, /focus|selection|session.*changed/i)
  assert.equal(store.created.length, 0)
})

test('missing or ambiguous owner refuses tracking instead of borrowing an ambient source', async () => {
  const store = makeStore(), host = makeHost()
  const captured = await controller.captureFocusedSession(host)
  host.setSelection({ storedId: 'session-1', owner: null })
  await assert.rejects(controller.trackFocusedSession(host, store, fields, 1000, captured), /owner|identity/i)
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker', mode: 'remote' } })
  host.profileRoutes = async () => [route, { ...route }]
  await assert.rejects(controller.trackFocusedSession(host, store, fields, 1000, captured), /route|owner/i)
  assert.equal(store.created.length, 0)
})

test('a mapped backend profile change between focus selection and route refresh refuses to track', async () => {
  const host = makeHost(), store = makeStore()
  const captured = await controller.captureFocusedSession(host)
  let routesCalls = 0
  host.profileRoutes = async () => ++routesCalls === 1 ? [route] : [{ ...route, targetProfile: 'other-backend' }]
  await assert.rejects(controller.trackFocusedSession(host, store, fields, 1000, captured), /route|owner/i)
  assert.equal(store.created.length, 0)
})

test('a storage failure reaches the caller and never reports a saved task', async () => {
  const host = makeHost(), store = { create: async () => { throw Error('storage full') } }
  const captured = await controller.captureFocusedSession(host)
  await assert.rejects(controller.trackFocusedSession(host, store, fields, 1000, captured), /storage full/)
})

test('explicit acceptance uses the stored revision and keeps the session reference', async () => {
  const host = makeHost(), store = makeStore()
  const row = await controller.trackFocusedSession(host, store, fields, 1000, await controller.captureFocusedSession(host))
  const saved = await controller.applyTrackedAction(store, row, { type: 'accept', at: 1200, note: '已核对' })
  assert.equal(saved.revision, 2)
  assert.equal(saved.task.resolution.state, 'accepted')
  assert.deepEqual(saved.task.primary, row.task.primary)
  assert.equal(store.replaced[0][1], 1)
})

test('editing persists only newly linked sessions whose exact route still matches', async () => {
  const host = makeHost(), store = makeStore()
  const row = await controller.trackFocusedSession(host, store, fields, 1000, await controller.captureFocusedSession(host))
  const related = { connectionId: 'ssh-b', profile: 'default', targetProfile: 'backend-b', storedId: 'session-b', mode: 'remote' }
  host.profileRoutes = async () => [route, { ...related, mode: 'remote' }]
  const edit = { title: '新的名称', nextSteps: ['检查'], related: [related] }
  const updated = await controller.reviseTrackedTask(host, store, row, edit, 1200)
  assert.equal(updated.task.title, '新的名称')
  assert.deepEqual(updated.task.primary, row.task.primary)
  assert.deepEqual(updated.task.related, [related])
  assert.equal(store.replaced[0][1], row.revision)
  host.profileRoutes = async () => [route, { ...related, targetProfile: 'other-backend', mode: 'remote' }]
  await assert.rejects(controller.reviseTrackedTask(host, store, row, edit, 1200), /route|owner/i)
  assert.equal(store.replaced.length, 1)
})

test('editing preserves existing offline links while preventing duplicate or invalid references', async () => {
  const host = makeHost(), store = makeStore()
  const original = await controller.trackFocusedSession(host, store, fields, 1000, await controller.captureFocusedSession(host))
  const offline = { connectionId:'offline', profile:'default', targetProfile:'backend-b', storedId:'session-b' }
  const row = { ...original, task: { ...original.task, related: [offline] } }
  host.profileRoutes = async () => { throw Error('offline') }
  const updated = await controller.reviseTrackedTask(host, store, row, { title:'更新', nextSteps:[], related:[offline] }, 1200)
  assert.deepEqual(updated.task.related, [offline])
  await assert.rejects(controller.reviseTrackedTask(host, store, row, { title:'更新', nextSteps:[], related:[row.task.primary] }, 1200), /duplicate/i)
  assert.equal(store.replaced.length, 1)
})

test('editing an existing related session cannot rewrite its stored mode', async () => {
  const host = makeHost(), store = makeStore()
  const original = await controller.trackFocusedSession(host, store, fields, 1000, await controller.captureFocusedSession(host))
  const legacy = { connectionId: 'ssh-b', profile: 'default', targetProfile: 'backend-b', storedId: 'session-b' }
  const remote = { ...legacy, mode: 'remote' }
  host.profileRoutes = async () => [route, { ...legacy, mode: 'remote' }]
  for (const [before, after] of [[legacy, remote], [remote, legacy], [remote, { ...remote, mode: 'local' }]]) {
    const row = { ...original, task: { ...original.task, related: [before] } }
    await assert.rejects(controller.reviseTrackedTask(host, store, row, { title: '更新', nextSteps: [], related: [after] }, 1200), /mode|identity|route/i)
  }
  assert.equal(store.replaced.length, 0)
})

test('editing cannot promote an unverified related session into a verified route', async () => {
  const host = makeHost(), store = makeStore()
  const original = await controller.trackFocusedSession(host, store, fields, 1000, await controller.captureFocusedSession(host))
  const related = { connectionId: 'ssh-b', profile: 'default', targetProfile: 'backend-b', storedId: 'session-b', mode: 'remote' }
  const row = { ...original, task: { ...original.task, related: [{ ...related, unverified: true }] } }
  host.profileRoutes = async () => [route, related]
  await assert.rejects(controller.reviseTrackedTask(host, store, row, { title:'更新', nextSteps:[], related:[related] }, 1200), /identity|unverified|verification/i)
  assert.equal(store.replaced.length, 0)
})

test('opening a saved primary reference is refused even when a store identity is recorded', async () => {
  const host = makeHost(), store = makeStore()
  host.setSelection({ storedId: 'session-1', owner: { ...route, storeInstanceId: 'store-a' } })
  const row = await controller.trackFocusedSession(host, store, fields, 1000, await controller.captureFocusedSession(host))
  assert.equal(row.task.primary.unverified, true)
  await assert.rejects(controller.openTrackedReference(host, row.task.primary), error => error.code === 'FOLLOWUP_REFERENCE_IDENTITY_UNAVAILABLE')
  assert.deepEqual(host.opened, [])
})

test('tracked references retain the focused mode and reject a changed route mode', async () => {
  const host = makeHost(), store = makeStore()
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker', mode: 'remote', storeInstanceId: 'store-a' } })
  const captured = await controller.captureFocusedSession(host)
  assert.equal(captured.mode, 'remote')
  const row = await controller.trackFocusedSession(host, store, fields, 1000, captured)
  assert.equal(row.task.primary.mode, 'remote')
  host.profileRoutes = async () => [{ ...route, mode: 'local' }]
  await assert.rejects(controller.openTrackedReference(host, row.task.primary), /route|owner|mode/i)
  assert.deepEqual(host.opened, [])
})

test('a focused session without a store identity is recorded but cannot be opened', async () => {
  const host = makeHost(), store = makeStore()
  const row = await controller.trackFocusedSession(host, store, fields, 1000, await controller.captureFocusedSession(host))
  assert.equal(Object.hasOwn(row.task.primary, 'storeInstanceId'), false)
  await assert.rejects(controller.openTrackedReference(host, row.task.primary), error => error.code === 'FOLLOWUP_REFERENCE_IDENTITY_UNAVAILABLE')
  assert.deepEqual(host.opened, [])
})

test('legacy tracked references with unknown mode remain readable but cannot navigate', async () => {
  const host = makeHost()
  const legacy = { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker', storedId: 'session-1' }
  await assert.rejects(controller.openTrackedReference(host, legacy), /route|owner|mode/i)
  assert.deepEqual(host.opened, [])
})

test('focused mode must be independently supplied and match the route', async () => {
  const host = makeHost()
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker' } })
  await assert.rejects(controller.captureFocusedSession(host), /mode|owner|identity/i)
  host.setSelection({ storedId: 'session-1', owner: { connectionId: 'ssh-a', profile: 'default', targetProfile: 'backend-worker', mode: 'local' } })
  await assert.rejects(controller.captureFocusedSession(host), /route|owner|mode/i)
})
