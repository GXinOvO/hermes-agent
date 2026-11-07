import type { BackgroundResult } from '@hermes/shared'

import { type ChatMessage, textPart } from '@/lib/chat-messages'
import { coerceGatewayText } from '@/lib/chat-runtime'

export function backgroundResultMessage(
  type: 'background.complete' | 'btw.complete',
  payload: { task_id?: unknown; text?: unknown; question?: unknown },
  timestamp?: number
): ChatMessage | null {
  const text = coerceGatewayText(payload.text).trim()

  if (!text) {
    return null
  }

  const taskId = String(payload.task_id ?? '').trim()
  const question = coerceGatewayText(payload.question).trim()
  const btw = type === 'btw.complete'

  const header = btw
    ? `[btw${question ? ` "${question}"` : ''}${taskId ? ` (${taskId})` : ''}]`
    : taskId
      ? `[bg ${taskId}]`
      : ''

  return {
    id: `${btw ? 'btw' : 'background'}-complete-${taskId || Date.now()}`,
    role: 'system',
    parts: [textPart(header ? `${header}\n${text}` : text, timestamp)],
    timestamp
  }
}

const isBackgroundResult = (message: ChatMessage) =>
  message.role === 'system' && (message.id.startsWith('background-complete-') || message.id.startsWith('btw-complete-'))

/** Keep display-only results when a stored transcript replaces this runtime's messages. */
export function preserveBackgroundResults(messages: ChatMessage[], previous: ChatMessage[]): ChatMessage[] {
  if (messages === previous) {
    return messages
  }

  const results = previous.filter(isBackgroundResult)

  if (!results.length) {
    return messages
  }

  const ids = new Set(messages.map(message => message.id))

  const missing = results.filter(message => {
    if (ids.has(message.id)) {
      return false
    }

    ids.add(message.id)

    return true
  })

  return missing.length ? [...messages, ...missing] : messages
}

/** The attach snapshot is a separate display cache, never part of its model messages. */
export function restoreBackgroundResults(
  messages: ChatMessage[],
  results: BackgroundResult[] | null | undefined,
  previous: ChatMessage[]
): ChatMessage[] {
  const restored = (results ?? []).flatMap(result => {
    if (result.event_type !== 'background.complete' && result.event_type !== 'btw.complete') {
      return []
    }

    const message = backgroundResultMessage(result.event_type, result.payload)

    return message ? [message] : []
  })

  return preserveBackgroundResults(preserveBackgroundResults(messages, previous), restored)
}
