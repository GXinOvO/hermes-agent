import { activeGatewayConnectionId } from '@/store/gateway'
import { normalizeProfileKey } from '@/store/profile'
import { knownOwnerForSession } from '@/store/session-states'

import { backgroundResultMessage, preserveBackgroundResults } from '../../background-results'

import type { GatewayEventContext } from './types'

/** Live sealed results use the same display identity as resume/activate snapshots. */
export function handleBackgroundResultEvent({
  deps,
  event,
  payload,
  sessionId,
  occurredAt
}: GatewayEventContext): boolean {
  if (event.type !== 'background.complete' && event.type !== 'btw.complete') {
    return false
  }

  if (!sessionId) {
    return true
  }

  const owner = knownOwnerForSession(sessionId)
  const profile = (typeof owner === 'string' ? owner : owner?.profile) ?? deps.activeGatewayProfile
  const connectionId = owner && typeof owner === 'object' ? owner.connectionId : activeGatewayConnectionId()

  if (
    (event.profile && normalizeProfileKey(event.profile) !== normalizeProfileKey(profile)) ||
    (event.connectionId && event.connectionId !== connectionId)
  ) {
    return true
  }

  const message = backgroundResultMessage(event.type, payload ?? {}, occurredAt)

  if (message) {
    deps.flushQueuedDeltas(sessionId)
    deps.updateSessionState(sessionId, state => ({
      ...state,
      messages: preserveBackgroundResults(state.messages, [message])
    }))
  }

  return true
}
