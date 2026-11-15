import { expect, test } from 'vitest'

import type { ClientSessionState } from '@/app/types'

import { deriveTaskActivity, type TaskActivityReference } from './activity'

const owner = { instanceId: 'instance', profileId: '/profile', authorityEpoch: 2, storeInstanceId: 'store' }
const reference: TaskActivityReference = { connectionId: 'local', profile: 'default', targetProfile: 'default', mode: 'local', storedId: 'stored', expectedOwner: owner }

const state = (overrides: Partial<ClientSessionState> = {}): ClientSessionState => ({
  adoptedRunningTurn: false, awaitingResponse: false, branch: '', busy: false, cwd: '', fast: false, heartbeatSettledStreamId: null,
  interimBoundaryPending: false, interrupted: false, messages: [], model: '', needsInput: false, pendingBranchGroup: null,
  personality: '', provider: '', reasoningEffort: '', runtimeStartedAt: 1, sawAssistantPayload: false, serviceTier: '', streamId: null,
  storedSessionId: 'stored', turnAdmissionId: null, turnAnchorId: null, turnLive: false, turnStartedAt: null, usage: null, yolo: false, ...overrides
})

test('derives running, waiting-input and unread states only from the verified source', () => {
  expect(deriveTaskActivity({ reference, runtimeId: 'runtime', runtimeOwner: owner, state: state({ busy: true, turnLive: true }), unread: false }).status).toBe('running')
  expect(deriveTaskActivity({ reference, runtimeId: 'runtime', runtimeOwner: owner, state: state({ needsInput: true }), unread: false }).status).toBe('waiting_input')
  expect(deriveTaskActivity({ reference, runtimeId: 'runtime', runtimeOwner: owner, state: state(), unread: true }).status).toBe('unread')
})

test('refuses unverified, replaced-owner, missing-runtime and stale-state references', () => {
  expect(deriveTaskActivity({ reference: { ...reference, unverified: true }, runtimeId: 'runtime', runtimeOwner: owner, state: state(), unread: true }).status).toBe('unavailable')
  expect(deriveTaskActivity({ reference: { ...reference, mode: undefined }, runtimeId: 'runtime', runtimeOwner: owner, state: state(), unread: true }).status).toBe('unavailable')
  expect(deriveTaskActivity({ reference: { ...reference, mode: 'other' as never }, runtimeId: 'runtime', runtimeOwner: owner, state: state(), unread: true }).status).toBe('unavailable')
  expect(deriveTaskActivity({ reference, runtimeId: 'runtime', runtimeOwner: { ...owner, instanceId: 'replacement' }, state: state(), unread: true }).reason).toBe('owner_mismatch')
  expect(deriveTaskActivity({ reference, runtimeOwner: owner, state: state(), unread: true }).reason).toBe('runtime_missing')
  expect(deriveTaskActivity({ reference, runtimeId: 'runtime', runtimeOwner: owner, state: state({ storedSessionId: 'other' }), unread: false }).status).toBe('unavailable')
})

test('prefers live waiting/running state over persisted unread and falls back to idle', () => {
  expect(deriveTaskActivity({ reference, runtimeId: 'runtime', runtimeOwner: owner, state: state({ busy: true, turnLive: true }), unread: true }).status).toBe('running')
  expect(deriveTaskActivity({ reference, runtimeId: 'runtime', runtimeOwner: owner, state: state({ needsInput: true }), unread: true }).status).toBe('waiting_input')
  expect(deriveTaskActivity({ reference, runtimeId: 'runtime', runtimeOwner: owner, state: state(), unread: false }).status).toBe('idle')
})

test('uses a known persisted source when its runtime is not mounted', () => {
  expect(deriveTaskActivity({ reference, sourceAvailable: true, unread: true }).status).toBe('unread')
  expect(deriveTaskActivity({ reference, sourceAvailable: true, unread: false }).status).toBe('idle')
  expect(deriveTaskActivity({ reference, sourceAvailable: false, unread: true }).status).toBe('unavailable')
})
