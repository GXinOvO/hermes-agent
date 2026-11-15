import type { ClientSessionState } from '@/app/types'
import { type ExactSessionOwner, isExactSessionOwner } from '@/lib/exact-session-owner'

export type TaskActivityStatus = 'idle' | 'running' | 'waiting_input' | 'unread' | 'unavailable'

export interface TaskActivityReference {
  expectedOwner?: ExactSessionOwner
  connectionId?: string
  profile?: string
  targetProfile?: string
  mode?: 'local' | 'remote'
  storedId: string
  unverified?: boolean
}

export interface TaskActivitySnapshot {
  profile: string | null
  status: TaskActivityStatus
  storedId: string
  reason: 'owner_mismatch' | 'owner_unverified' | 'runtime_missing' | 'waiting_input' | 'running' | 'unread' | 'idle'
}

export interface TaskActivityInputs {
  reference: TaskActivityReference
  state?: ClientSessionState
  sourceAvailable?: boolean
  unread: boolean
  runtimeOwner?: ExactSessionOwner
  runtimeId?: string
}

function sameOwner(left: ExactSessionOwner | undefined, right: ExactSessionOwner | undefined): boolean {
  return Boolean(left && right && left.instanceId === right.instanceId && left.profileId === right.profileId &&
    left.authorityEpoch === right.authorityEpoch && left.storeInstanceId === right.storeInstanceId)
}

export function isTaskActivityReferenceVerified(reference: TaskActivityReference): boolean {
  return reference.unverified !== true && Boolean(reference.connectionId && reference.profile && reference.targetProfile &&
    (reference.mode === 'local' || reference.mode === 'remote') && isExactSessionOwner(reference.expectedOwner))
}

export function deriveTaskActivity({ reference, state, sourceAvailable, unread, runtimeOwner, runtimeId }: TaskActivityInputs): TaskActivitySnapshot {
  const profile = reference.targetProfile || reference.profile || null
  const hasRuntime = Boolean(runtimeId || state)
  const sourceIsAvailable = sourceAvailable ?? Boolean(runtimeId && state?.storedSessionId === reference.storedId)

  if (!isTaskActivityReferenceVerified(reference)) {
    return { profile, status: 'unavailable', storedId: reference.storedId, reason: reference.unverified ? 'owner_unverified' : 'owner_mismatch' }
  }

  if (hasRuntime && (!runtimeId || !state || state.storedSessionId !== reference.storedId)) {
    return { profile, status: 'unavailable', storedId: reference.storedId, reason: 'runtime_missing' }
  }

  if (hasRuntime && !sameOwner(reference.expectedOwner, runtimeOwner)) {
    return { profile, status: 'unavailable', storedId: reference.storedId, reason: 'owner_mismatch' }
  }

  if (!hasRuntime && !sourceIsAvailable) {
    return { profile, status: 'unavailable', storedId: reference.storedId, reason: 'runtime_missing' }
  }

  if (state?.needsInput) {
    return { profile, status: 'waiting_input', storedId: reference.storedId, reason: 'waiting_input' }
  }

  if (state?.busy && state.turnLive) {
    return { profile, status: 'running', storedId: reference.storedId, reason: 'running' }
  }

  if (unread) {
    return { profile, status: 'unread', storedId: reference.storedId, reason: 'unread' }
  }

  return { profile, status: 'idle', storedId: reference.storedId, reason: 'idle' }
}
