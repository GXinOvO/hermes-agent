import { gatewayRouteState, openGatewayForAgent } from '@/store/gateway'
import { $primaryGatewayRoute } from '@/store/gateway-primary-route'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'
import { $focusedStoredSessionId } from '@/store/session-focus'
import {
  $focusedRuntimeId, $focusedSessionState, $sessionStates, $sessionTiles,
  focusOpenSession, knownOwnerForSession, ownerConnectionMode, sessionTileDelegate
} from '@/store/session-states'

import { type ExactSessionOwner, isExactSessionOwner, sameExactSessionOwner } from './exact-session-owner'

export type { ExactSessionOwner } from './exact-session-owner'

export interface ExactSessionReference {
  storedId: string
  connectionId: string
  profile: string
  targetProfile: string
  mode: 'local' | 'remote'
  expectedOwner: ExactSessionOwner
}

const token = (value: unknown): value is string => typeof value === 'string' && Boolean(value) && value === value.trim()

export function readFocusedSessionReference(): ExactSessionReference {
  const storedId = $focusedStoredSessionId.get()
  const runtimeId = $focusedRuntimeId.get()
  const state = $focusedSessionState.get()
  const owner = knownOwnerForSession(runtimeId)
  const profile = typeof owner === 'string' ? owner : owner?.profile
  const connectionId = typeof owner === 'object' && owner ? owner.connectionId : $primaryGatewayRoute.get().connectionId
  const targetProfile = typeof owner === 'object' && owner ? owner.targetProfile || profile : profile
  const mode = ownerConnectionMode(owner)

  if (!token(storedId) || !runtimeId || state?.storedSessionId !== storedId ||
      !token(connectionId) || !token(profile) || !token(targetProfile) || !mode ||
      !isExactSessionOwner(state.sourceOwner) || !token(state.sourceReplayEpoch)) {
    throw new Error('当前会话缺少已绑定的来源身份证据，请重新打开来源会话后再绑定。')
  }

  const gateway = gatewayRouteState(connectionId, profile).gateway
  const witness = gateway?.sessionOwnerWitness(runtimeId, targetProfile, state.sourceReplayEpoch)

  if (!sameExactSessionOwner(witness, state.sourceOwner)) {
    throw new Error('当前前台会话与已连接来源不一致，请重新打开来源会话后再绑定。')
  }

  return { storedId, connectionId, profile, targetProfile, mode, expectedOwner: { ...state.sourceOwner } }
}

export async function captureFocusedSessionReference(): Promise<ExactSessionReference> {
  return readFocusedSessionReference()
}

export async function openExactSession(reference: ExactSessionReference): Promise<void> {
  if (!reference || ![reference.storedId, reference.connectionId, reference.profile, reference.targetProfile].every(token) ||
      !['local', 'remote'].includes(reference.mode) || !isExactSessionOwner(reference.expectedOwner)) {
    throw new Error('来源引用缺少完整身份证据，请重新绑定来源会话。')
  }

  const tile = $sessionTiles.get().find(item => item.storedSessionId === reference.storedId)
  const heldRuntime = tile?.runtimeId ?? ($selectedStoredSessionId.get() === reference.storedId ? $activeSessionId.get() : null)
  const held = heldRuntime ? $sessionStates.get()[heldRuntime] : undefined

  if (held && !sameExactSessionOwner(held.sourceOwner, reference.expectedOwner)) {
    throw new Error('相同会话编号已由另一来源打开，请关闭该标签后重试或重新绑定来源。')
  }

  const delegate = sessionTileDelegate()

  if (!delegate) { throw new Error('会话导航尚未就绪，请稍后重试。') }

  try {
    await openGatewayForAgent(reference.connectionId, reference.profile, { spawnPriority: 'foreground' })
    const runtimeId = await delegate.resumeTile(reference.storedId, { exactReference: reference })
    const state = $sessionStates.get()[runtimeId]

    if (state?.storedSessionId !== reference.storedId || !sameExactSessionOwner(state.sourceOwner, reference.expectedOwner)) {
      throw new Error('恢复结果未绑定到来源会话')
    }

    const deadline = Date.now() + 5000

    while (!focusOpenSession(reference.storedId)) {
      if (Date.now() >= deadline) { throw new Error('来源已恢复但标签尚未显示，请重试。') }
      await new Promise(resolve => setTimeout(resolve, 25))
    }
  } catch (error) {
    throw new Error(`无法打开精确来源会话，请重试；来源或网关已变化时请重新绑定。${error instanceof Error ? error.message : ''}`)
  }
}
