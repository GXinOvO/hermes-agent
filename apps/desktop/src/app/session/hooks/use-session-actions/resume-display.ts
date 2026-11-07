import { type ChatMessage, restorePendingClarifyToolCall, settlePendingClarifyToolCall } from '@/lib/chat-messages'
import type { ConnectionRequest } from '@/store/connection-request'
import type { SessionResumeResult } from '@/types/hermes'

import { restoreBackgroundResults } from '../background-results'

import { type PendingClarifyResumeState, pendingClarifyToolPayload } from './restore-pending-clarify'
import { projectPendingConnection } from './restore-pending-connection'

/** Apply view-only prompt cards and sealed result lines after authoritative transcript hydration. */
export function projectResumedDisplay(
  messages: ChatMessage[],
  resumed: SessionResumeResult,
  previous: ChatMessage[],
  clarify: PendingClarifyResumeState,
  clarifyAbsent: boolean,
  connection: ConnectionRequest | null,
  running: boolean
) {
  const pendingClarifyProjection = clarify.request
    ? restorePendingClarifyToolCall(messages, pendingClarifyToolPayload(clarify.request))
    : null

  const clearedClarifyProjection = clarifyAbsent
    ? settlePendingClarifyToolCall(messages, clarify.cleared ? pendingClarifyToolPayload(clarify.cleared) : {}, running)
    : null

  const pendingConnectionProjection = projectPendingConnection(
    pendingClarifyProjection?.messages ?? clearedClarifyProjection?.messages ?? messages,
    connection
  )

  const visibleMessagesForView = restoreBackgroundResults(
    pendingConnectionProjection?.messages ??
      pendingClarifyProjection?.messages ??
      clearedClarifyProjection?.messages ??
      messages,
    resumed.background_results,
    previous
  )

  return { pendingClarifyProjection, clearedClarifyProjection, pendingConnectionProjection, visibleMessagesForView }
}
