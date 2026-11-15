import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import remarkParse from 'remark-parse'
import { unified } from 'unified'

import { type ExactSessionOwner, requireExactSessionOverview } from '@/lib/exact-session-owner'

export interface OverviewMessage {
  role: 'user' | 'assistant' | 'tool'
  row_id?: number | null
  content?: string
  timestamp?: number | null
  display_kind?: string | null
  _compressed_summary?: boolean
  name?: string
  tool_calls?: unknown[]
}

export interface OverviewTodo {
  id: string
  content: string
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled'
  parent?: string
}

export interface SessionOverviewPayload {
  version: 1
  session_id: string
  stored_session_id: string
  resolved_id: string
  authority_epoch: number
  owner: unknown
  snapshot_scope: 'persisted_display'
  complete: true
  fetched_at: number
  conversation_revision: unknown
  messages: OverviewMessage[]
  todo_state: { todos: OverviewTodo[]; revision: number } | null
  verification: Record<string, unknown> | null
  verification_cwd: string | null
  verification_error: string | null
}

export interface OverviewIdentity {
  runtimeId: string
  storedId: string
  owner: ExactSessionOwner
}

export interface DiscoveredTaskCandidate {
  content: string
  evidence: 'native_todo' | 'registered_decision' | 'registered_deferred' | 'registered_remaining'
  sourceRow: number | null
}

export interface OverviewSourceRoute {
  connectionId: string
  expectedOwner: ExactSessionOwner
  mode: 'local' | 'remote'
  profile: string
  storedId: string
  targetProfile: string
}

export const STAGES = ['implementation', 'tests', 'review', 'integration', 'activation'] as const
const EDIT_TOOLS = new Set(['patch', 'write_file', 'terminal', 'execute_code', 'browser_exec', 'delegate_task', 'tool_call', 'todo', 'todo_list'])
const markdown = unified().use(remarkParse).use(remarkGfm).use(remarkMath)
const isObject = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === 'object' && !Array.isArray(value))
const text = (message: OverviewMessage) => message.content ?? ''
const visible = (message: OverviewMessage) => !['hidden', 'system', 'compaction_summary'].includes(message.display_kind ?? '') && !message._compressed_summary
const lines = (value?: string) => value?.split('\n').map(item => item.trim()).filter(Boolean) ?? []

export function parseTaskDirective(line: string): Record<string, string> | null {
  if (line.length > 2056 || !line.startsWith('::task{') || !line.endsWith('}') || /[{}]/.test(line.slice(7, -1))) { return null }
  const attrs: Record<string, string> = Object.create(null)
  const allow = new Set(['title', 'goal', 'result', 'remaining', 'deferred', 'decision', 'scope', ...STAGES, ...STAGES.map(stage => `${stage}_evidence`)])
  let rest = line.slice(7, -1)

  while (rest.trim()) {
    const match = /^\s*([a-z_]+)=("(?:[^"\\\r\n]|\\["\\/bfnrt]|\\u[\da-fA-F]{4})*")(?=\s|$)/.exec(rest)

    if (!match || !allow.has(match[1]) || Object.hasOwn(attrs, match[1])) { return null }
    let value: unknown

    try { value = JSON.parse(match[2]) } catch { return null }

    if (typeof value !== 'string' || value.length > 6000) { return null }
    attrs[match[1]] = value
    rest = rest.slice(match[0].length)
  }

  return Object.keys(attrs).length ? attrs : null
}

function registrationEntries(message: OverviewMessage): { start: number; end: number; lineEnd: number; parsed: Record<string, string> }[] {
  if (message.role !== 'assistant' || !visible(message)) { return [] }
  const candidates: { start: number; end: number; lineEnd: number; parsed: Record<string, string> }[] = []
  let offset = 0

  const masked = text(message).split('\n').map(line => {
    const parsed = parseTaskDirective(line.trimEnd())

    if (parsed) {
      candidates.push({ start: offset, end: offset + line.trimEnd().length, lineEnd: offset + line.length, parsed })
    }

    offset += line.length + 1

    return parsed ? 'x'.repeat(line.length) : line
  }).join('\n')

  const paragraphs = markdown.parse(masked).children.filter(node => node.type === 'paragraph')

  return candidates.filter(item => paragraphs.some(node => node.children.some(child => child.type === 'text' &&
    child.position?.start.offset !== undefined && child.position?.end.offset !== undefined &&
    child.position.start.offset <= item.start && child.position.end.offset >= item.end)))
}

function registrations(message: OverviewMessage): Record<string, string>[] {
  return registrationEntries(message).map(entry => entry.parsed)
}

function messageWithoutTaskDirectives(message: OverviewMessage): string {
  let content = text(message)

  for (const entry of registrationEntries(message).sort((a, b) => b.start - a.start)) {
    content = content.slice(0, entry.start) + content.slice(entry.lineEnd)
  }

  return content.trim()
}

function requireTodo(value: unknown): asserts value is SessionOverviewPayload['todo_state'] {
  if (value === null) { return }

  if (!isObject(value) || !Array.isArray(value.todos) || !Number.isSafeInteger(value.revision) || Number(value.revision) < 0 ||
      !value.todos.every(item => isObject(item) && typeof item.id === 'string' && item.id && typeof item.content === 'string' && item.content &&
        ['pending', 'in_progress', 'completed', 'cancelled'].includes(String(item.status))) ||
      new Set(value.todos.map(item => item.id)).size !== value.todos.length) {
    throw new Error('Invalid native todo snapshot')
  }
}

export function validateSessionOverview(value: unknown, identity: OverviewIdentity): asserts value is SessionOverviewPayload {
  requireExactSessionOverview(value, identity.runtimeId, identity.storedId, identity.owner)
  const payload = value as SessionOverviewPayload

  if (payload.version !== 1 || payload.complete !== true || payload.snapshot_scope !== 'persisted_display' ||
      typeof payload.resolved_id !== 'string' || !payload.resolved_id || !Number.isFinite(payload.fetched_at) || payload.fetched_at <= 0 ||
      !Array.isArray(payload.messages) || !payload.messages.every(message => isObject(message) && ['user', 'assistant', 'tool'].includes(String(message.role)) &&
        (message.content === undefined || typeof message.content === 'string')) ||
      !Object.hasOwn(payload, 'todo_state') || !Object.hasOwn(payload, 'conversation_revision') ||
      (payload.verification !== null && !isObject(payload.verification)) ||
      (payload.verification_cwd !== null && typeof payload.verification_cwd !== 'string') ||
      (payload.verification_error !== null && typeof payload.verification_error !== 'string')) {
    throw new Error('Invalid or incomplete session overview')
  }

  requireTodo(payload.todo_state)
}

export function deriveSessionOverview(payload: SessionOverviewPayload, sourceRoute?: OverviewSourceRoute) {
  requireTodo(payload.todo_state)
  const messages = payload.messages
  const users = messages.filter(message => message.role === 'user' && visible(message) && text(message).trim())

  const assistants = messages.filter(message => message.role === 'assistant' && visible(message) &&
    messageWithoutTaskDirectives(message) && !message.tool_calls?.length)

  let registration: Record<string, string> | null = null
  let source: OverviewMessage | null = null
  let registrationIndex = -1
  messages.forEach((message, index) => {
    const found = registrations(message)

    if (found.length) { registration = found.at(-1)!; source = message; registrationIndex = index }
  })
  const record = registration as Record<string, string> | null
  const latestRecordResult = record?.result

  const stale = Boolean(record && messages.slice(registrationIndex + 1).some(message =>
    (message.role === 'user' && visible(message) && text(message).trim()) || (message.role === 'tool' && EDIT_TOOLS.has(message.name ?? ''))))

  const stages = STAGES.map(id => {
    const state = record?.[id]
    const evidence = record?.[`${id}_evidence`]?.trim() || null
    const valid = ['passed', 'failed', 'pending', 'not_applicable'].includes(state ?? '')

    const status = !valid || ((state === 'passed' || state === 'not_applicable') && !evidence)
      ? 'unknown' : stale ? 'stale' : state === 'passed' ? 'registered' : state!

    return { id, status, evidence, source: evidence ? source : null }
  })

  const todos = payload.todo_state?.todos ?? null
  const registeredRemaining = lines(record?.remaining)
  const deferred = lines(record?.deferred)
  const decisions = lines(record?.decision)

  return {
    title: record?.title || (users[0] ? text(users[0]).split('\n')[0].slice(0, 90) : null),
    goal: record?.goal || null, original: users[0] ?? null, current: users.at(-1) ?? null,
    latest: assistants.at(-1) ? { ...assistants.at(-1)!, content: messageWithoutTaskDirectives(assistants.at(-1)!) } : null,
    registeredResult: latestRecordResult || null,
    registration: record, registrationStale: stale, acceptance: !record ? 'unregistered' : stale ? 'stale' : 'registered', stages,
    todos, todoRevision: payload.todo_state?.revision ?? null,
    remaining: todos?.filter(item => ['pending', 'in_progress'].includes(item.status)) ?? [],
    registeredRemaining, deferred, decisions,
    discoveredTasks: discoverTaskCandidates({ todos, registeredRemaining, deferred, decisions, registrationStale: stale, source }),
    verification: payload.verification, verificationCwd: payload.verification_cwd, verificationError: payload.verification_error,
    fetchedAt: payload.fetched_at, runtimeId: payload.session_id, storedId: payload.stored_session_id,
    resolvedId: payload.resolved_id, messageCount: messages.length, sourceRoute
  }
}

export function discoverTaskCandidates(input: {
  todos: OverviewTodo[] | null
  registeredRemaining: string[]
  deferred: string[]
  decisions: string[]
  registrationStale: boolean
  source: OverviewMessage | null
}): DiscoveredTaskCandidate[] {
  const candidates: DiscoveredTaskCandidate[] = []
  const seen = new Set<string>()

  const add = (content: string, evidence: DiscoveredTaskCandidate['evidence']) => {
    const normalized = content.trim()

    if (!normalized || seen.has(normalized.toLocaleLowerCase())) { return }
    seen.add(normalized.toLocaleLowerCase())
    candidates.push({ content: normalized, evidence, sourceRow: evidence === 'native_todo' ? null : input.source?.row_id ?? null })
  }

  input.todos?.filter(todo => todo.status === 'pending' || todo.status === 'in_progress').forEach(todo => add(todo.content, 'native_todo'))

  if (!input.registrationStale) {
    input.registeredRemaining.forEach(content => add(content, 'registered_remaining'))
    input.deferred.forEach(content => add(content, 'registered_deferred'))
    input.decisions.forEach(content => add(content, 'registered_decision'))
  }

  return candidates
}

export type SessionOverview = ReturnType<typeof deriveSessionOverview>
