import { computed } from 'nanostores'

import { composerHost } from '@/sdk/composer'
import { gatewayRouteState } from '@/store/gateway'
import { $primaryGatewayRoute } from '@/store/gateway-primary-route'
import { $focusedStoredSessionId } from '@/store/session-focus'
import { $focusedRuntimeId, $focusedSessionState, knownOwnerForSession } from '@/store/session-states'

import { captureFocusedSessionReference, readFocusedSessionReference } from '../exact-session'
import { exactSessionOwnerToWire } from '../exact-session-owner'

import { deriveSessionOverview, validateSessionOverview } from './session-review'

function focusedOverviewKey(): string {
  const storedId = $focusedStoredSessionId.get()
  const runtimeId = $focusedRuntimeId.get()
  const state = $focusedSessionState.get()

  return storedId && runtimeId ? JSON.stringify([
    storedId, runtimeId, state?.sourceOwner, state?.sourceReplayEpoch, knownOwnerForSession(runtimeId), $primaryGatewayRoute.get()
  ]) : ''
}

export const $focusedOverviewKey = computed(
  [$focusedStoredSessionId, $focusedRuntimeId, $focusedSessionState, $primaryGatewayRoute], focusedOverviewKey
)

export async function loadFocusedSessionOverview() {
  const key = $focusedOverviewKey.get()
  const runtimeId = $focusedRuntimeId.get()
  const reference = await captureFocusedSessionReference()

  if (!runtimeId || key !== $focusedOverviewKey.get()) { throw new Error('会话已切换，请重新读取概览。') }
  const gateway = gatewayRouteState(reference.connectionId, reference.profile).gateway

  if (!gateway) { throw new Error('来源连接不可用，请重新打开会话。') }

  const payload = await gateway.request('session.task_overview', {
    session_id: runtimeId, stored_id: reference.storedId, profile: reference.targetProfile,
    expected_owner: exactSessionOwnerToWire(reference.expectedOwner)
  })

  validateSessionOverview(payload, { runtimeId, storedId: reference.storedId, owner: reference.expectedOwner })

  if (key !== $focusedOverviewKey.get() || gateway !== gatewayRouteState(reference.connectionId, reference.profile).gateway) {
    throw new Error('会话来源已变化，请重新读取概览。')
  }

  return deriveSessionOverview(payload, {
    connectionId: reference.connectionId,
    expectedOwner: reference.expectedOwner,
    mode: reference.mode,
    profile: reference.profile,
    storedId: reference.storedId,
    targetProfile: reference.targetProfile
  })
}

export async function appendFocusedOverviewRequest(key: string, text: string): Promise<boolean> {
  const runtimeId = $focusedRuntimeId.get()

  if (!runtimeId || key !== focusedOverviewKey()) { return false }

  try {
    const reference = readFocusedSessionReference()
    const gateway = gatewayRouteState(reference.connectionId, reference.profile).gateway

    return await composerHost.insertText(runtimeId, text, {
      mode: 'block',
      isCurrent: () => {
        if (key !== focusedOverviewKey()) { return false }

        try {
          const current = readFocusedSessionReference()

          return gateway === gatewayRouteState(current.connectionId, current.profile).gateway
        } catch { return false }
      }
    })
  } catch { return false }
}
