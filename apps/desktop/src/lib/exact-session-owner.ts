export interface ExactSessionOwner {
  instanceId: string
  profileId: string
  authorityEpoch: number
  storeInstanceId: string
}

const token = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value === value.trim()

export function isExactSessionOwner(value: unknown): value is ExactSessionOwner {
  if (!value || typeof value !== 'object') { return false }
  const owner = value as ExactSessionOwner

  return token(owner.instanceId) && token(owner.profileId) && token(owner.storeInstanceId) &&
    Number.isSafeInteger(owner.authorityEpoch) && owner.authorityEpoch > 0
}

export function exactSessionOwnerFromWire(value: unknown): ExactSessionOwner | undefined {
  if (!value || typeof value !== 'object') { return undefined }
  const wire = value as Record<string, unknown>

  const owner = { instanceId: wire.instance_id, profileId: wire.profile_id,
    authorityEpoch: wire.authority_epoch, storeInstanceId: wire.store_instance_id }

  return isExactSessionOwner(owner) ? owner : undefined
}

export function exactSessionOwnerToWire(owner: ExactSessionOwner) {
  if (!isExactSessionOwner(owner)) { throw new Error('来源身份凭据不完整，请重新绑定来源会话。') }

  return { instance_id: owner.instanceId, profile_id: owner.profileId,
    authority_epoch: owner.authorityEpoch, store_instance_id: owner.storeInstanceId }
}

export function sameExactSessionOwner(a: ExactSessionOwner | undefined, b: ExactSessionOwner | undefined): boolean {
  return Boolean(a && b && a.instanceId === b.instanceId && a.profileId === b.profileId &&
    a.authorityEpoch === b.authorityEpoch && a.storeInstanceId === b.storeInstanceId)
}

export function requireExactSessionOverview(value: any, runtimeId: string, storedId: string, expected: ExactSessionOwner): void {
  if (value?.session_id !== runtimeId || value.stored_session_id !== storedId ||
      !sameExactSessionOwner(exactSessionOwnerFromWire(value.owner), expected) ||
      value.authority_epoch !== expected.authorityEpoch || Object.hasOwn(value, 'open_requests') || Object.hasOwn(value, 'prompts')) {
    throw new Error('来源会话身份已变化或返回目标不符，请重新打开来源会话。')
  }
}

export function requireExactSessionSnapshot(value: any, storedId: string, expected: ExactSessionOwner): void {
  if (value?.session_id !== storedId || value.stored_session_id !== storedId ||
      !sameExactSessionOwner(exactSessionOwnerFromWire(value.owner), expected) ||
      value.authority_epoch !== expected.authorityEpoch ||
      typeof value.replay_epoch !== 'string' || !value.replay_epoch || value.replay_epoch !== value.replay_epoch.trim()) {
    throw new Error('来源会话身份已变化或返回目标不符，请重试；网关重启后请重新绑定来源会话。')
  }
}
