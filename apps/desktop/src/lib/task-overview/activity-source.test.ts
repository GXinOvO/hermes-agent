import { beforeEach, describe, expect, it } from 'vitest'

import { $sessions } from '@/store/session'
import { $sessionSeenCounts, $unreadFinishedMarkers, isSessionAvailableForProfile, isSessionUnreadForProfile } from '@/store/session-unread'
import { makeSessionInfo } from '@/test/session-info'

import { deriveTaskActivity, type TaskActivityReference } from './activity'

const reference: TaskActivityReference = {
  connectionId: 'connection-a',
  expectedOwner: { authorityEpoch: 1, instanceId: 'owner-a', profileId: '/profiles/worker', storeInstanceId: 'store-a' },
  mode: 'remote',
  profile: 'default',
  storedId: 'shared-id',
  targetProfile: 'worker'
}

describe('task activity source scope', () => {
  beforeEach(() => {
    $sessions.set([])
    $sessionSeenCounts.set({})
    $unreadFinishedMarkers.set({})
  })

  it('does not use another connection row or its profile marker as the task source', () => {
    $sessions.set([makeSessionInfo({ connection_id: 'connection-b', id: 'shared-id', message_count: 8, profile: 'worker' })])
    $unreadFinishedMarkers.set({ worker: ['shared-id'] })

    const sourceAvailable = isSessionAvailableForProfile(reference.storedId, reference.targetProfile, reference.connectionId)
    const unread = isSessionUnreadForProfile(reference.storedId, reference.targetProfile, undefined, reference.connectionId)

    expect(sourceAvailable).toBe(false)
    expect(unread).toBe(false)
    expect(deriveTaskActivity({ reference, sourceAvailable, unread }).status).toBe('unavailable')
  })

  it('uses a row and unread state proven to belong to the referenced connection', () => {
    $sessions.set([
      makeSessionInfo({ connection_id: 'connection-b', id: 'shared-id', message_count: 4, profile: 'worker' }),
      makeSessionInfo({ connection_id: 'connection-a', id: 'shared-id', message_count: 8, profile: 'worker', unread: true })
    ])
    $unreadFinishedMarkers.set({ worker: ['shared-id'] })

    const sourceAvailable = isSessionAvailableForProfile(reference.storedId, reference.targetProfile, reference.connectionId)
    const unread = isSessionUnreadForProfile(reference.storedId, reference.targetProfile, undefined, reference.connectionId)

    expect(sourceAvailable).toBe(true)
    expect(unread).toBe(true)
    expect(deriveTaskActivity({ reference, sourceAvailable, unread }).status).toBe('unread')
  })

  it('does not attribute a profile-only marker to a connection-matched row', () => {
    $sessions.set([makeSessionInfo({ connection_id: 'connection-a', id: 'shared-id', message_count: 8, profile: 'worker' })])
    $unreadFinishedMarkers.set({ worker: ['shared-id'] })

    const unread = isSessionUnreadForProfile(reference.storedId, reference.targetProfile, undefined, reference.connectionId)

    expect(unread).toBe(false)
    expect(deriveTaskActivity({ reference, sourceAvailable: true, unread }).status).toBe('idle')
  })
})
