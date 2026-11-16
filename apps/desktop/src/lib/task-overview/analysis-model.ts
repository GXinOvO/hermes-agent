export interface AnalysisClaim {
  text: string
  evidence: { row_id: number; quote: string }[]
}

export interface TaskAnalysis {
  version: 1
  title: string
  analyzed_at: number
  conversation_revision: unknown
  stale: boolean
  scope: 'single_conversation'
  message_count: number
  analyzed_messages: number
  limitations: string[]
  status: 'todo' | 'planning' | 'in_progress' | 'waiting' | 'failed' | 'awaiting_acceptance'
  goal: AnalysisClaim | null
  completed: AnalysisClaim[]
  remaining: AnalysisClaim[]
  needs_user: AnalysisClaim[]
}

const record = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === 'object' && !Array.isArray(value))
const text = (value: unknown): value is string => typeof value === 'string' && Boolean(value.trim())
const count = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0

function claim(value: unknown): value is AnalysisClaim {
  return record(value) && text(value.text) && Array.isArray(value.evidence) && value.evidence.length > 0 &&
    value.evidence.every(item => record(item) && count(item.row_id) && (item.row_id as number) > 0 && text(item.quote))
}

export function validateTaskAnalysis(value: unknown): TaskAnalysis {
  if (!record(value) || value.version !== 1 || !text(value.title) || !count(value.analyzed_at) || value.analyzed_at === 0 ||
      !Object.hasOwn(value, 'conversation_revision') || typeof value.stale !== 'boolean' || value.scope !== 'single_conversation' ||
      !count(value.message_count) || !count(value.analyzed_messages) || value.analyzed_messages > value.message_count ||
      !Array.isArray(value.limitations) || !value.limitations.every(text) ||
      !['todo', 'planning', 'in_progress', 'waiting', 'failed', 'awaiting_acceptance'].includes(String(value.status)) ||
      (value.goal !== null && !claim(value.goal)) ||
      ![value.completed, value.remaining, value.needs_user].every(items => Array.isArray(items) && items.every(claim))) {
    throw new Error('Invalid task analysis')
  }

  return value as unknown as TaskAnalysis
}
