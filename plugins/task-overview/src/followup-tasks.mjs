const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const identityPart = value => typeof value === 'string' && value.length > 0 && value === value.trim()
const time = value => Number.isSafeInteger(value) && value > 0
const TASK_STATUSES = new Set(['unspecified', 'todo', 'in_progress', 'waiting', 'done'])
const TASK_PRIORITIES = new Set(['low', 'normal', 'high', 'urgent'])

function reference(value, optional = false) {
  if (optional && value === null) return null
  if (isObject(value) && value.unverified === true) {
    if (!identityPart(value.storedId) || ['connectionId', 'profile', 'targetProfile'].some(key => value[key] !== undefined && !identityPart(value[key])) ||
        (value.mode !== undefined && value.mode !== 'local' && value.mode !== 'remote') ||
        (value.storeInstanceId !== undefined && !identityPart(value.storeInstanceId))) throw new Error('Invalid session reference identity')
    return { storedId: value.storedId, ...(value.connectionId !== undefined ? { connectionId: value.connectionId } : {}),
      ...(value.profile !== undefined ? { profile: value.profile } : {}),
      ...(value.targetProfile !== undefined ? { targetProfile: value.targetProfile } : {}),
      ...(value.mode !== undefined ? { mode: value.mode } : {}),
      ...(value.storeInstanceId !== undefined ? { storeInstanceId: value.storeInstanceId } : {}), unverified: true }
  }
  if (!isObject(value) || !['connectionId', 'profile', 'targetProfile', 'storedId'].every(key => identityPart(value[key])) ||
      (value.mode !== undefined && value.mode !== 'local' && value.mode !== 'remote') ||
      (value.storeInstanceId !== undefined && !identityPart(value.storeInstanceId))) {
    throw new Error('Invalid session reference identity')
  }
  return { connectionId: value.connectionId, profile: value.profile, targetProfile: value.targetProfile, storedId: value.storedId,
    ...(value.mode === undefined ? {} : { mode: value.mode }),
    ...(value.storeInstanceId === undefined ? {} : { storeInstanceId: value.storeInstanceId }) }
}

function referenceKey(value) {
  if (value === null) return null
  return JSON.stringify([value.unverified === true ? 'unverified' : 'verified', value.connectionId, value.profile, value.targetProfile, value.storedId, value.storeInstanceId])
}

export function assertStableRelatedModes(previous, next) {
  const old = new Map(previous.map(ref => [referenceKey(ref), ref.mode]))
  const priorUnverified = new Set(previous.filter(ref => ref.unverified).map(ref => JSON.stringify([ref.connectionId, ref.profile, ref.targetProfile, ref.storedId])))
  for (const ref of next) {
    const key = referenceKey(ref)
    if (old.has(key) && old.get(key) !== ref.mode) throw new Error('Related session owner mode changed')
    if (!ref.unverified && priorUnverified.has(JSON.stringify([ref.connectionId, ref.profile, ref.targetProfile, ref.storedId]))) throw new Error('Unverified related session identity cannot be promoted by editing')
  }
}

function validateTask(task) {
  if (!isObject(task) || task.version !== 1 || !identityPart(task.id) || typeof task.title !== 'string' || !task.title.trim() ||
      !time(task.createdAt) || !time(task.updatedAt) || task.updatedAt < task.createdAt ||
      !Array.isArray(task.related) || !Array.isArray(task.nextSteps) ||
      !task.nextSteps.every(step => typeof step === 'string' && step.trim()) || !isObject(task.resolution) ||
      (task.description !== undefined && typeof task.description !== 'string') ||
      (task.notes !== undefined && typeof task.notes !== 'string') ||
      (task.status !== undefined && !TASK_STATUSES.has(task.status)) ||
      (task.priority !== undefined && !TASK_PRIORITIES.has(task.priority))) {
    throw new Error('Invalid follow-up task')
  }
  const primary = reference(task.primary, true)
  const related = task.related.map(ref => reference(ref))
  const keys = [primary, ...related].map(referenceKey)
  const nonNullKeys = keys.filter(key => key !== null)
  if (new Set(nonNullKeys).size !== nonNullKeys.length) throw new Error('Duplicate session reference')
  const resolution = task.resolution
  const status = task.status ?? (resolution.state === 'accepted' ? 'done' : 'unspecified')
  const priority = task.priority ?? 'normal'
  const description = task.description ?? ''
  const notes = task.notes ?? ''
  if (!TASK_STATUSES.has(status) || !TASK_PRIORITIES.has(priority)) throw new Error('Invalid follow-up task')
  if (resolution.state === 'accepted') {
    if (status !== 'done' || !time(resolution.at) || resolution.at < task.createdAt || resolution.at > task.updatedAt || typeof resolution.note !== 'string') throw new Error('Invalid task acceptance')
  } else if (resolution.state !== 'open') {
    throw new Error('Invalid task resolution')
  } else if (status === 'done') {
    throw new Error('Done tasks require explicit acceptance')
  }
  return { version: 1, id: task.id, title: task.title, description, notes, primary, related, nextSteps: [...task.nextSteps], status, priority,
    createdAt: task.createdAt, updatedAt: task.updatedAt,
    resolution: resolution.state === 'accepted' ? { state: 'accepted', at: resolution.at, note: resolution.note } : { state: 'open' } }
}

export function validateFollowupTasks(tasks) {
  if (!Array.isArray(tasks)) throw new Error('Invalid follow-up task list')
  const result = tasks.map(validateTask)
  if (new Set(result.map(task => task.id)).size !== result.length) throw new Error('Duplicate follow-up task identity')
  return result
}

export function createFollowupTask(input, at) {
  if (!isObject(input)) throw new Error('Invalid follow-up task')
  return validateTask({ version: 1, id: input.id, title: input.title, description: input.description, notes: input.notes, primary: input.primary,
    related: input.related, nextSteps: input.nextSteps, status: input.status, priority: input.priority,
    createdAt: at, updatedAt: at, resolution: { state: 'open' } })
}

export function applyFollowupAction(input, action) {
  const task = validateTask(input)
  if (!isObject(action) || !time(action.at) || action.at < task.updatedAt || !['accept', 'reopen', 'revise', 'set_status', 'bind_primary', 'set_primary'].includes(action.type)) throw new Error('Invalid follow-up action')
  if (action.type === 'accept' && (task.resolution.state !== 'open' || typeof action.note !== 'string' || !action.note.trim())) throw new Error('Invalid follow-up action')
  if (action.type === 'reopen' && task.resolution.state !== 'accepted') throw new Error('Invalid follow-up action')
  if (action.type === 'set_status' && (task.resolution.state !== 'open' || !TASK_STATUSES.has(action.status) || action.status === 'done')) throw new Error('Invalid follow-up action')
  if (action.type === 'set_status') return validateTask({ ...task, status: action.status, updatedAt: action.at })
  if (action.type === 'bind_primary' || action.type === 'set_primary') {
    if (task.resolution.state !== 'open' || (action.type === 'bind_primary' && task.primary !== null)) throw new Error('Invalid primary session action')
    const primary = reference(action.primary, action.type === 'set_primary')
    if (primary && task.related.some(ref => referenceKey(ref) === referenceKey(primary))) throw new Error('Duplicate session reference')
    return validateTask({ ...task, primary, updatedAt: action.at })
  }
  if (action.type === 'revise') {
    if (task.resolution.state !== 'open') throw new Error('Invalid follow-up action')
    const revised = validateTask({ ...task, updatedAt: action.at, title: action.title, description: action.description ?? task.description, notes: action.notes ?? task.notes,
      nextSteps: action.nextSteps, related: action.related, status: action.status ?? task.status, priority: action.priority ?? task.priority })
    assertStableRelatedModes(task.related, revised.related)
    return revised
  }
  return validateTask({ ...task, updatedAt: action.at, status: action.type === 'accept' ? 'done' : 'todo',
    resolution: action.type === 'accept' ? { state: 'accepted', at: action.at, note: action.note } : { state: 'open' } })
}
