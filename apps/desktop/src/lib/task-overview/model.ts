export type TaskStatus = 'in_progress' | 'todo' | 'unspecified' | 'waiting'
export type TaskPriority = 'high' | 'low' | 'normal' | 'urgent'

export interface TaskReference {
  expectedOwner?: {
    authorityEpoch: number
    instanceId: string
    profileId: string
    storeInstanceId: string
  }
  connectionId?: string
  mode?: 'local' | 'remote'
  profile?: string
  storedId: string
  storeInstanceId?: string
  targetProfile?: string
  unverified?: boolean
}

export interface TaskResolution {
  at?: number
  note?: string
  state: 'accepted' | 'open'
}

export interface Task {
  createdAt: number
  description: string
  id: string
  nextSteps: string[]
  notes: string
  primary: TaskReference | null
  priority: TaskPriority
  related: TaskReference[]
  resolution: TaskResolution
  status: TaskStatus | 'done'
  title: string
  updatedAt: number
  version: 1
}

const STATUSES = new Set<Task['status']>(['in_progress', 'todo', 'unspecified', 'waiting', 'done'])
const PRIORITIES = new Set<TaskPriority>(['high', 'low', 'normal', 'urgent'])

function isIdentity(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value === value.trim()
}

function isTime(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) > 0
}

function referenceOwner(candidate: Record<string, unknown>): TaskReference['expectedOwner'] {
  const owner = candidate.expectedOwner as Record<string, unknown> | undefined

  if (owner !== undefined && (
    !owner || typeof owner !== 'object' || Array.isArray(owner) ||
    !['instanceId', 'profileId', 'storeInstanceId'].every(key => isIdentity(owner[key])) ||
    !Number.isSafeInteger(owner.authorityEpoch) || (owner.authorityEpoch as number) < 1 ||
    (candidate.storeInstanceId !== undefined && candidate.storeInstanceId !== owner.storeInstanceId)
  )) {
    throw new Error('Invalid task session owner')
  }

  return owner === undefined ? undefined : {
    authorityEpoch: owner.authorityEpoch as number,
    instanceId: owner.instanceId as string,
    profileId: owner.profileId as string,
    storeInstanceId: owner.storeInstanceId as string
  }
}

function validateReferenceFields(candidate: Record<string, unknown>, unverified: boolean): void {
  const required = unverified ? ['storedId'] : ['connectionId', 'profile', 'targetProfile', 'storedId']

  if (!required.every(key => isIdentity(candidate[key])) ||
      (candidate.mode !== undefined && candidate.mode !== 'local' && candidate.mode !== 'remote') ||
      (candidate.storeInstanceId !== undefined && !isIdentity(candidate.storeInstanceId)) ||
      (candidate.unverified !== undefined && candidate.unverified !== true) ||
      (unverified && ['connectionId', 'profile', 'targetProfile'].some(key => candidate[key] !== undefined && !isIdentity(candidate[key])))) {
    throw new Error('Invalid task session reference')
  }
}

function reference(value: unknown, optional: boolean): TaskReference | null {
  if (optional && value === null) {
    return null
  }

  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Invalid task session reference')
  }

  const candidate = value as Record<string, unknown>
  const expectedOwner = referenceOwner(candidate)
  const unverified = candidate.unverified === true
  validateReferenceFields(candidate, unverified)

  if (unverified) {
    return {
      storedId: candidate.storedId as string,
      ...(candidate.connectionId === undefined ? {} : { connectionId: candidate.connectionId as string }),
      ...(candidate.profile === undefined ? {} : { profile: candidate.profile as string }),
      ...(candidate.targetProfile === undefined ? {} : { targetProfile: candidate.targetProfile as string }),
      ...(candidate.mode === undefined ? {} : { mode: candidate.mode as 'local' | 'remote' }),
      ...(candidate.storeInstanceId === undefined ? {} : { storeInstanceId: candidate.storeInstanceId as string }),
      ...(expectedOwner ? { expectedOwner } : {}),
      unverified: true
    }
  }

  return {
    connectionId: candidate.connectionId as string,
    profile: candidate.profile as string,
    targetProfile: candidate.targetProfile as string,
    storedId: candidate.storedId as string,
    ...(candidate.mode === undefined ? {} : { mode: candidate.mode as 'local' | 'remote' }),
    ...(candidate.storeInstanceId === undefined ? {} : { storeInstanceId: candidate.storeInstanceId as string }),
    ...(expectedOwner ? { expectedOwner } : {})
  }
}

export function taskReferenceKey(value: TaskReference | null): string | null {
  if (!value) {
    return null
  }

  return JSON.stringify([
    value.unverified === true ? 'unverified' : 'verified',
    value.connectionId,
    value.profile,
    value.targetProfile,
    value.mode,
    value.storedId,
    value.storeInstanceId,
    value.expectedOwner?.instanceId,
    value.expectedOwner?.profileId,
    value.expectedOwner?.authorityEpoch,
    value.expectedOwner?.storeInstanceId
  ])
}

function taskCandidate(task: unknown): Record<string, unknown> {
  if (!task || typeof task !== 'object' || Array.isArray(task)) {
    throw new Error('Invalid task')
  }

  const candidate = task as Record<string, unknown>

  if (candidate.version !== 1 || !isIdentity(candidate.id) || typeof candidate.title !== 'string' || !candidate.title.trim() ||
      !isTime(candidate.createdAt) || !isTime(candidate.updatedAt) || (candidate.updatedAt as number) < (candidate.createdAt as number)) {
    throw new Error('Invalid task')
  }

  validateTaskFields(candidate)

  return candidate
}

function validateTaskFields(candidate: Record<string, unknown>): void {
  if (!Array.isArray(candidate.related) || !Array.isArray(candidate.nextSteps) ||
      !candidate.nextSteps.every(step => typeof step === 'string' && step.trim()) ||
      !candidate.resolution || typeof candidate.resolution !== 'object' || Array.isArray(candidate.resolution) ||
      (candidate.description !== undefined && typeof candidate.description !== 'string') ||
      (candidate.notes !== undefined && typeof candidate.notes !== 'string') ||
      (candidate.status !== undefined && !STATUSES.has(candidate.status as Task['status'])) ||
      (candidate.priority !== undefined && !PRIORITIES.has(candidate.priority as TaskPriority))) {
    throw new Error('Invalid task')
  }
}

function validateTaskResolution(candidate: Record<string, unknown>, resolution: Record<string, unknown>, status: Task['status']): void {
  if (resolution.state === 'accepted') {
    if (status !== 'done' || !isTime(resolution.at) || (resolution.at as number) < (candidate.createdAt as number) ||
        (resolution.at as number) > (candidate.updatedAt as number) || typeof resolution.note !== 'string') {
      throw new Error('Invalid task acceptance')
    }
  } else if (resolution.state !== 'open') {
    throw new Error('Invalid task resolution')
  } else if (status === 'done') {
    throw new Error('Done tasks require explicit acceptance')
  }
}

function validateTask(task: unknown): Task {
  const candidate = taskCandidate(task)
  const primary = reference(candidate.primary, true)
  const related = (candidate.related as unknown[]).map(item => reference(item, false) as TaskReference)
  const keys = [primary, ...related].map(taskReferenceKey).filter((key): key is string => key !== null)

  if (new Set(keys).size !== keys.length) {
    throw new Error('Duplicate task session reference')
  }

  const resolution = candidate.resolution as Record<string, unknown>

  const status = (candidate.status as Task['status'] | undefined) ??
    (resolution.state === 'accepted' ? 'done' : 'unspecified')

  const priority = (candidate.priority as TaskPriority | undefined) ?? 'normal'
  const description = (candidate.description as string | undefined) ?? ''
  const notes = (candidate.notes as string | undefined) ?? ''

  if (!STATUSES.has(status) || !PRIORITIES.has(priority)) {
    throw new Error('Invalid task')
  }

  validateTaskResolution(candidate, resolution, status)

  return {
    createdAt: candidate.createdAt as number,
    description,
    id: candidate.id as string,
    nextSteps: [...(candidate.nextSteps as string[])],
    notes,
    primary,
    priority,
    related,
    resolution: resolution.state === 'accepted'
      ? { at: resolution.at as number, note: resolution.note as string, state: 'accepted' }
      : { state: 'open' },
    status,
    title: candidate.title as string,
    updatedAt: candidate.updatedAt as number,
    version: 1
  }
}

export function validateTasks(tasks: unknown): Task[] {
  if (!Array.isArray(tasks)) {
    throw new Error('Invalid task list')
  }

  const result = tasks.map(validateTask)

  if (new Set(result.map(task => task.id)).size !== result.length) {
    throw new Error('Duplicate task identity')
  }

  return result
}

export function createTask(input: Record<string, unknown>, at: number): Task {
  return validateTask({
    ...input,
    createdAt: at,
    resolution: { state: 'open' },
    updatedAt: at,
    version: 1
  })
}

export type TaskAction =
  | { at: number; note: string; type: 'accept' }
  | { at: number; type: 'reopen' }
  | { at: number; status: Task['status']; type: 'set_status' }
  | { at: number; primary: TaskReference | null; type: 'set_primary' }

export function applyTaskAction(task: Task, action: TaskAction): Task {
  const current = validateTask(task)

  if (!isTime(action.at) || action.at < current.updatedAt) {
    throw new Error('Invalid task action')
  }

  if (action.type === 'set_primary') {
    return validateTask({ ...current, primary: action.primary, updatedAt: action.at })
  }

  if (action.type === 'accept') {
    if (current.resolution.state !== 'open' || !action.note.trim()) {
      throw new Error('Invalid task action')
    }

    return validateTask({
      ...current,
      resolution: { at: action.at, note: action.note, state: 'accepted' },
      status: 'done',
      updatedAt: action.at
    })
  }

  if (action.type === 'reopen') {
    if (current.resolution.state !== 'accepted') {
      throw new Error('Invalid task action')
    }

    return validateTask({ ...current, resolution: { state: 'open' }, status: 'todo', updatedAt: action.at })
  }

  if (current.resolution.state !== 'open' || action.status === 'done') {
    throw new Error('Done tasks require explicit acceptance')
  }

  return validateTask({ ...current, status: action.status, updatedAt: action.at })
}
