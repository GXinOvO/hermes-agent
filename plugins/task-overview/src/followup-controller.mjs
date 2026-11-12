import { openFollowupSession, selectRoute } from './core.mjs'
import { applyFollowupAction, createFollowupTask } from './followup-tasks.mjs'

function focused(host) {
  const storedId = host.state.focusedStoredSessionId.get()
  const owner = host.state.focusedSessionOwner.get()
  if (typeof storedId !== 'string' || !storedId || !owner?.connectionId || !owner?.profile || typeof owner.targetProfile !== 'string' || !owner.targetProfile.trim() || !['local', 'remote'].includes(owner.mode)) {
    const error = new Error('Focused session owner identity unavailable')
    error.code = 'FOLLOWUP_OWNER_IDENTITY_UNAVAILABLE'
    throw error
  }
  const storeInstanceId = typeof owner.storeInstanceId === 'string' && owner.storeInstanceId.trim() === owner.storeInstanceId && owner.storeInstanceId ? owner.storeInstanceId : undefined
  return { storedId, connectionId: owner.connectionId, profile: owner.profile, targetProfile: owner.targetProfile, mode: owner.mode,
    ...(storeInstanceId === undefined ? {} : { storeInstanceId }) }
}

function sameFocus(a, b) {
  return a.storedId === b.storedId && a.connectionId === b.connectionId && a.profile === b.profile && a.targetProfile === b.targetProfile &&
    a.mode === b.mode && a.storeInstanceId === b.storeInstanceId
}


export async function captureFocusedSession(host) {
  const current = focused(host)
  const route = selectRoute(current, await host.profileRoutes())
  if (current.targetProfile !== route.targetProfile || current.mode !== route.mode) throw new Error('Focused session owner route changed')
  if (!sameFocus(current, focused(host))) throw new Error('Focused session changed while selecting')
  return { ...current, targetProfile: route.targetProfile }
}

export function snapshotFocusedSession(host) {
  const storedId = host.state.focusedStoredSessionId.get()
  const owner = host.state.focusedSessionOwner.get()
  if (typeof storedId !== 'string' || !storedId.trim() || storedId !== storedId.trim()) throw new Error('Focused session identity unavailable')
  const result = { storedId }
  for (const key of ['connectionId', 'profile', 'targetProfile', 'mode', 'storeInstanceId']) {
    if (owner?.[key] !== undefined && owner[key] !== null) result[key] = owner[key]
  }
  return { ...result, unverified: true }
}

export async function captureSessionForTracking(host) {
  const current = snapshotFocusedSession(host)
  if (current.connectionId && current.profile && current.targetProfile && ['local', 'remote'].includes(current.mode)) return { ...await captureFocusedSession(host), unverified: true }
  return current
}

export async function trackCapturedSession(host, store, fields, at, captured) {
  if (!captured?.unverified || (captured.connectionId && captured.profile && captured.targetProfile && ['local', 'remote'].includes(captured.mode))) return await trackFocusedSession(host, store, fields, at, captured)
  if (JSON.stringify(captured) !== JSON.stringify(snapshotFocusedSession(host))) throw new Error('Focused session changed before tracking')
  return await store.create(createFollowupTask({ ...fields, primary: captured }, at))
}

export async function trackFocusedSession(host, store, fields, at, captured) {
  if (!captured || typeof captured.targetProfile !== 'string' || !captured.targetProfile.trim() || !['local', 'remote'].includes(captured.mode)) throw new Error('Captured session identity required')
  const current = focused(host)
  if (!sameFocus(current, captured)) throw new Error('Focused session changed before tracking')
  const route = selectRoute(current, await host.profileRoutes())
  if (captured.targetProfile !== route.targetProfile || captured.mode !== route.mode) throw new Error('Focused session owner route changed before tracking')
  if (!sameFocus(current, focused(host))) throw new Error('Focused session changed before tracking')
  const latestRoute = selectRoute(current, await host.profileRoutes())
  if (latestRoute.targetProfile !== route.targetProfile || latestRoute.mode !== route.mode) throw new Error('Focused session owner route changed before tracking')
  if (!sameFocus(current, focused(host))) throw new Error('Focused session changed before tracking')
  const task = createFollowupTask({ ...fields, primary: { ...current, targetProfile: route.targetProfile, unverified: true } }, at)
  return await store.create(task)
}

export async function createStandaloneTask(store, fields, at) {
  return await store.create(createFollowupTask({ ...fields, primary: null }, at))
}

export async function bindPrimarySession(host, store, row, at, expectedFocus) {
  if (expectedFocus !== undefined && JSON.stringify(snapshotFocusedSession(host)) !== JSON.stringify(expectedFocus)) throw new Error('Focused session changed before tracking')
  const captured = await captureSessionForTracking(host)
  if (captured.unverified ? JSON.stringify(captured) !== JSON.stringify(snapshotFocusedSession(host)) : !sameFocus(captured, focused(host))) throw new Error('Focused session changed before tracking')
  return await store.changePrimary(row.task.id, row.revision, captured, at)
}

export async function applyTrackedAction(store, row, action) {
  return await store.replace(applyFollowupAction(row.task, action), row.revision)
}

export async function removeTrackedTask(store, row) {
  return await store.remove(row.task.id, row.revision)
}

export async function reviseTrackedTask(host, store, row, fields, at) {
  const task = applyFollowupAction(row.task, { ...fields, type: 'revise', at })
  const key = ref => JSON.stringify([ref.connectionId, ref.profile, ref.targetProfile, ref.storedId])
  const existing = new Set(row.task.related.map(key))
  const added = task.related.filter(ref => !existing.has(key(ref)))
  const routed = added.filter(item => !item.unverified || (item.connectionId && item.profile && item.targetProfile && ['local', 'remote'].includes(item.mode)))
  if (routed.length) {
    try {
      const routes = await host.profileRoutes()
      for (const ref of routed) {
        const route = selectRoute(ref, routes)
        if (!['local', 'remote'].includes(ref.mode) || route.targetProfile !== ref.targetProfile || route.mode !== ref.mode) throw new Error('Session owner route changed')
      }
    } catch (cause) {
      const error = new Error('Session owner route changed or unavailable', { cause })
      error.code = 'FOLLOWUP_ROUTE_CHANGED'
      throw error
    }
  }
  return await store.replace(task, row.revision)
}

export async function openTrackedReference(host, reference) {
  return await openFollowupSession(host, { storedId: reference?.storedId, route: reference })
}
