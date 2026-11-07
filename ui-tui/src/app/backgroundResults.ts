import type { AnyGatewayEvent } from '@hermes/shared/gateway-types'

import type { GatewayEventHandlerContext } from './interfaces.js'
import { getUiState, patchUiState } from './uiStore.js'

/** Side results are system lines with stable task identity in the current history.
 * Resetting that history permits snapshot replay; live/snapshot overlap stays idempotent. */
export function handleBackgroundResultEvent(
  ev: AnyGatewayEvent,
  { setHistoryItems }: GatewayEventHandlerContext['transcript']
): boolean {
  if (ev.type !== 'background.complete' && ev.type !== 'btw.complete') {
    return false
  }

  if (!ev.payload || (ev.profile && ev.profile !== (getUiState().info?.profile_name || 'default'))) {
    return true
  }

  const { task_id: taskId, text, question } = ev.payload

  if (ev.type === 'background.complete') {
    patchUiState(state => {
      const bgTasks = new Set(state.bgTasks)
      bgTasks.delete(taskId)

      return { ...state, bgTasks }
    })
  }

  if (!text.trim()) {
    return true
  }

  const id = `${ev.session_id || ''}:${taskId}`

  const line =
    ev.type === 'background.complete' ? `[bg ${taskId}] ${text}` : `[btw${question ? ` "${question}"` : ''}] ${text}`

  setHistoryItems(previous =>
    previous.some(row => row.backgroundResultId === id)
      ? previous
      : [...previous, { backgroundResultId: id, role: 'system', text: line }]
  )

  return true
}
