import type { ClientSessionState } from '@/app/types'
import { exactSessionOwnerFromWire } from '@/lib/exact-session-owner'
import type { SessionRuntimeInfo } from '@/types/hermes'

export type SessionRuntimeStatePatch = Partial<Pick<ClientSessionState,
  'sourceOwner' | 'sourceReplayEpoch' | 'branch' | 'cwd' | 'fast' | 'model' | 'personality' | 'provider' |
  'reasoningEffort' | 'reasoningEffortPending' | 'reasoningEffortWire' | 'serviceTier' | 'usage' | 'yolo'
>>

export function runtimeSourceState(info: SessionRuntimeInfo): SessionRuntimeStatePatch {
  return info.session_owner !== undefined ? {
    sourceOwner: exactSessionOwnerFromWire(info.session_owner), sourceReplayEpoch: info.replay_epoch
  } : {}
}
