import { exactSessionOwnerFromWire, exactSessionOwnerToWire, isExactSessionOwner, requireExactSessionOverview } from '@/lib/exact-session-owner'
import { parseSessionRefValue } from '@/lib/session-refs'
import { activeGatewayConnectionId, activeGatewayProfileKey, dialedGatewayModeFor, gatewayRouteState, openGatewayForAgent } from '@/store/gateway'

import { type TaskAnalysis, validateTaskAnalysis } from './analysis-model'
import { type Task, type TaskReference, taskReferenceKey } from './model'

export function parseTaskSource(value: string): { profile?: string; sessionId: string } {
  let source = value.trim()

  if (source.startsWith('@session:')) { source = source.slice('@session:'.length) }
  else if (source.startsWith('#session/')) { source = decodeURIComponent(source.slice('#session/'.length)) }
  else if (/^https?:\/\//.test(source)) {
    const url = new URL(source)

    if (url.origin !== window.location.origin) { throw new Error('会话链接来自其他连接，请先切换到对应连接。') }
    source = decodeURIComponent((url.hash.startsWith('#/') ? url.hash.slice(2) : url.pathname.slice(1)))
  } else if (source.startsWith('#/')) { source = decodeURIComponent(source.slice(2)) }
  else if (source.startsWith('/')) { source = decodeURIComponent(source.slice(1)) }

  const parsed = parseSessionRefValue(source)

  if (!parsed.sessionId || /[\s/?#]/.test(parsed.sessionId) || (parsed.profile && /[\s/?#]/.test(parsed.profile))) {
    throw new Error('请输入会话编号或 @session:profile/会话编号。')
  }

  return parsed
}

export async function resolveTaskSource(value: string): Promise<TaskReference> {
  const parsed = parseTaskSource(value)
  const connectionId = activeGatewayConnectionId()
  const profile = parsed.profile ?? activeGatewayProfileKey()

  if (!connectionId) { throw new Error('来源连接尚未就绪，请先连接对应的会话来源。') }
  await openGatewayForAgent(connectionId, profile)
  const mode = dialedGatewayModeFor(connectionId, profile)
  const gateway = gatewayRouteState(connectionId, profile).gateway

  if (!gateway || !mode) { throw new Error('来源连接不可用。') }
  const runtime = await gateway.request<any>('runtime.describe')
  const owner = exactSessionOwnerFromWire(runtime)

  if (!owner || !runtime.capabilities?.includes('task-analysis-v1')) {
    throw new Error('此来源尚不支持事项分析，请更新对应后端。')
  }

  const result = await gateway.request<any>('session.task_source', {
    session_id: parsed.sessionId, profile, expected_owner: exactSessionOwnerToWire(owner)
  })

  requireExactSessionOverview(result, parsed.sessionId, parsed.sessionId, owner)

  if (gateway !== gatewayRouteState(connectionId, profile).gateway) { throw new Error('来源连接已变化，请重试。') }

  return { connectionId, profile, targetProfile: profile, mode, storedId: result.stored_session_id, expectedOwner: owner }
}

export function cachedTaskAnalysis(task: Task): TaskAnalysis | null {
  return task.analysis?.title === task.title && task.analysis.referenceKey === taskReferenceKey(task.primary)
    ? task.analysis.result : null
}

export async function loadTaskAnalysis(task: Task, refresh = false): Promise<TaskAnalysis | null> {
  const reference = task.primary

  if (!reference || reference.unverified || !reference.connectionId || !reference.profile || !reference.targetProfile ||
      !isExactSessionOwner(reference.expectedOwner)) { throw new Error('来源尚未核实，请重新关联来源会话。') }

  await openGatewayForAgent(reference.connectionId, reference.profile)
  const gateway = gatewayRouteState(reference.connectionId, reference.profile).gateway

  if (!gateway) { throw new Error('来源连接不可用，保留上次分析结果。') }

  const result = await gateway.request<any>('session.task_analysis', {
    session_id: reference.storedId, stored_id: reference.storedId, profile: reference.targetProfile,
    expected_owner: exactSessionOwnerToWire(reference.expectedOwner), task_id: task.id, title: task.title, refresh
  }, refresh ? 180000 : undefined)

  requireExactSessionOverview(result, reference.storedId, reference.storedId, reference.expectedOwner)

  if (gateway !== gatewayRouteState(reference.connectionId, reference.profile).gateway || result.task_id !== task.id) {
    throw new Error('事项分析来源已变化，请重试。')
  }

  if (result.analysis === null) { return null }
  const analysis = validateTaskAnalysis(result.analysis)

  if (analysis.title !== task.title) { throw new Error('事项分析名称不一致，请重新分析。') }

  return analysis
}
