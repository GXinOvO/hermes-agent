import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'

const markdown = unified().use(remarkParse).use(remarkGfm).use(remarkMath)
const STAGE_IDS = ['implementation', 'tests', 'review', 'integration', 'activation']
const STATUS = new Set(['pending', 'in_progress', 'completed', 'cancelled'])
// Conservatively expire a record after execution that could change evidence.
// External edits are not watched; this is not a source fingerprint attestation.
const EDIT_TOOLS = new Set(['patch', 'write_file', 'terminal', 'execute_code', 'browser_exec', 'delegate_task', 'tool_call', 'todo', 'todo_list'])
const object = value => Boolean(value && typeof value === 'object' && !Array.isArray(value))
const decode = value => { try { return typeof value === 'string' ? JSON.parse(value) : value } catch { return null } }
export const textOf = message => typeof message?.text === 'string' ? message.text : typeof message?.content === 'string' ? message.content : Array.isArray(message?.content) ? message.content.filter(p => p?.type === 'text').map(p => p.text || '').join('\n') : ''
const visible = message => !['hidden', 'system', 'compaction_summary'].includes(message.display_kind) && !message._compressed_summary
const lines = value => typeof value === 'string' ? value.split('\n').map(s => s.trim()).filter(Boolean) : []
const evidence = message => ({ rowId: message.row_id ?? message.id ?? null, timestamp: message.timestamp ?? null, text: textOf(message) })

export function parseTaskDirective(line) {
  if (typeof line !== 'string' || line.length > 2056 || !line.startsWith('::task{') || !line.endsWith('}') || /[{}]/.test(line.slice(7, -1))) return null
  const attrs = Object.create(null)
  let rest = line.slice(7, -1)
  const allow = new Set(['title', 'goal', 'result', 'remaining', 'deferred', 'decision', 'scope', ...STAGE_IDS, ...STAGE_IDS.map(s => `${s}_evidence`)])
  while (rest.trim()) {
    const match = /^\s*([a-z_]+)=("(?:[^"\\\r\n]|\\["\\/bfnrt]|\\u[\da-fA-F]{4})*")(?=\s|$)/.exec(rest)
    if (!match || !allow.has(match[1]) || Object.hasOwn(attrs, match[1])) return null
    const value = decode(match[2])
    if (typeof value !== 'string' || value.length > 6000) return null
    attrs[match[1]] = value
    rest = rest.slice(match[0].length)
  }
  return Object.keys(attrs).length ? attrs : null
}

function registrations(message) {
  if (message.role !== 'assistant' || !visible(message)) return []
  const candidates = []
  let offset = 0
  const masked = textOf(message).split('\n').map(line => {
    const parsed = parseTaskDirective(line.trimEnd())
    if (parsed) candidates.push({ start: offset, end: offset + line.trimEnd().length, parsed })
    offset += line.length + 1
    return parsed ? 'x'.repeat(line.length) : line
  }).join('\n')
  if (!candidates.length) return []
  const paragraphs = markdown.parse(masked).children.filter(node => node.type === 'paragraph')
  return candidates.filter(item => paragraphs.some(node => node.children.some(child => child.type === 'text' && child.position.start.offset <= item.start && child.position.end.offset >= item.end))).map(item => item.parsed)
}

function todoSnapshot(value) {
  if (value === null) return null
  if (!object(value) || !Array.isArray(value.todos) || !Number.isSafeInteger(value.revision) || value.revision < 0) throw new Error('Invalid native todo snapshot')
  const todos = value.todos
  if (!todos.every(t => object(t) && typeof t.id === 'string' && t.id && typeof t.content === 'string' && t.content && STATUS.has(t.status)) || new Set(todos.map(t => t.id)).size !== todos.length) throw new Error('Invalid native todo entries')
  return { todos: todos.map(t => ({ ...t })), revision: value.revision }
}

function isEdit(message) {
  if (message.role !== 'tool') return false
  const name = message.name ?? message.tool_name
  if (EDIT_TOOLS.has(name)) return true
  const args = decode(message.args)
  return name === 'tool_call' && Array.isArray(args?.calls) && args.calls.some(c => EDIT_TOOLS.has(c.name))
}

export function identityKey(scope) {
  return JSON.stringify([scope?.owner?.connectionId ?? null, scope?.owner?.profile ?? null, scope?.storedId ?? null, scope?.runtimeId ?? null])
}

export function selectRoute(owner, routes) {
  if (!owner?.connectionId || !owner?.profile) throw new Error('Session owner is unresolved; no cross-source fallback is allowed.')
  const matches = (Array.isArray(routes) ? routes : []).filter(r => r?.connectionId === owner.connectionId && r?.profile === owner.profile)
  if (matches.length !== 1 || typeof matches[0].targetProfile !== 'string' || !matches[0].targetProfile || matches[0].targetProfile !== matches[0].targetProfile.trim()) throw new Error('Exact session owner route is unavailable or ambiguous.')
  return matches[0]
}

export function deriveOverview({ messages, todoState = null, snapshotScope = 'persisted_display', scope = {}, verification = null, verificationError = null, verificationCwd = null, fetchedAt = Date.now() }) {
  if (!Array.isArray(messages)) throw new Error('Invalid history response')
  const users = messages.filter(m => m.role === 'user' && visible(m) && textOf(m).trim())
  const assistants = messages.filter(m => m.role === 'assistant' && visible(m) && textOf(m).trim() && !m.tool_calls?.length)
  const snapshot = todoSnapshot(todoState)
  let registration = null, registrationIndex = -1
  messages.forEach((message, i) => {
    const found = registrations(message)
    if (found.length) { registration = { ...found.at(-1), evidence: evidence(message) }; registrationIndex = i }
  })
  const stale = registration && messages.slice(registrationIndex + 1).some(m => (m.role === 'user' && visible(m) && textOf(m).trim()) || isEdit(m))
  const stages = STAGE_IDS.map(id => {
    const state = registration?.[id]
    const reference = registration?.[`${id}_evidence`]?.trim() || null
    const valid = ['passed', 'failed', 'pending', 'not_applicable'].includes(state)
    const status = !valid || ((state === 'passed' || state === 'not_applicable') && !reference) ? 'unknown' : stale ? 'stale' : state === 'passed' ? 'registered' : state
    return { id, status, evidence: reference, source: reference ? registration?.evidence : null }
  })
  const todos = snapshot?.todos ?? null
  const decisions = lines(registration?.decision)
  const noDecision = /^(?:无|暂无|没有|none|n\/a|暂无需决策|无待决定事项|无需确认)[。.!！\s]*$/i
  const noRemaining = /^(?:无|暂无|没有|none|n\/a|暂无剩余工作|无剩余必做)[。.!！\s]*$/i
  const needsInput = !stale && decisions.some(decision => !noDecision.test(decision))
  const failed = !stale && stages.some(stage => stage.status === 'failed')
  const pending = (!stale && lines(registration?.remaining).some(item => !noRemaining.test(item))) || todos?.some(todo => ['pending', 'in_progress'].includes(todo.status))
  const work = needsInput ? 'needs_input' : failed ? 'failed' : pending ? 'pending' : 'needs_review'
  const execution = ['working', 'idle'].includes(scope.execution) ? scope.execution : 'unknown'
  const followup = {
    work, execution, actionRequired: needsInput || failed, explicitAccepted: false,
    recordFreshness: !registration ? 'unregistered' : stale ? 'stale' : 'registered'
  }
  return {
    scope, key: identityKey(scope), fetchedAt, snapshotScope, followup,
    title: registration?.title || (users[0] ? textOf(users[0]).split('\n')[0].slice(0, 90) : null),
    goal: registration?.goal || null,
    original: users[0] ? evidence(users[0]) : null,
    current: users.at(-1) ? evidence(users.at(-1)) : null,
    latest: assistants.at(-1) ? evidence(assistants.at(-1)) : null,
    result: !stale && registration?.result || null,
    registration, registrationStale: Boolean(stale), acceptance: !registration ? 'unregistered' : stale ? 'stale' : 'registered',
    hasIndependentAcceptance: false,
    stages, todos, todoRevision: snapshot?.revision ?? null,
    remaining: todos?.filter(t => ['pending', 'in_progress'].includes(t.status)) ?? [],
    completed: todos?.filter(t => t.status === 'completed') ?? [],
    cancelled: todos?.filter(t => t.status === 'cancelled') ?? [],
    registeredRemaining: lines(registration?.remaining), deferred: lines(registration?.deferred), decisions: lines(registration?.decision),
    verification: object(verification) ? verification : null, verificationError, verificationCwd,
    messageCount: messages.length
  }
}

export async function loadOverview(host, scope) {
  if (!scope.storedId || !scope.runtimeId) throw new Error('Session identity unavailable')
  const route = selectRoute(scope.owner, await host.profileRoutes())
  const params = { session_id: scope.runtimeId, profile: route.targetProfile }
  const response = await host.requestProfile(route, 'command.dispatch', { ...params, name: 'task-overview-read', arg: JSON.stringify({ stored_id: scope.storedId }) })
  const history = response?.type === 'plugin' ? decode(response.output) : null
  if (!history || history.version !== 2 || history.ok !== true) throw new Error(history?.error || 'Task overview backend needs version 0.1.1 or newer on this session source.')
  if (history.requested_id !== scope.storedId || !history.resolved_id || history.complete !== true || !Array.isArray(history.messages) || history.count !== history.messages.length || history.snapshot_scope !== 'persisted_display' || !Object.hasOwn(history, 'todo_state') || !Number.isFinite(history.fetchedAt) || history.fetchedAt <= 0) throw new Error('Invalid or incomplete session history response')
  const verificationCwd = history.verification_cwd || null
  const verification = verificationCwd && object(history.verification) ? history.verification : null
  const verificationError = history.verification_error || (!verificationCwd ? 'Session workspace unavailable' : null)
  return { ...deriveOverview({ messages: history.messages, todoState: history.todo_state, snapshotScope: history.snapshot_scope, fetchedAt: history.fetchedAt, scope, verification, verificationError, verificationCwd }), sourceRoute: { connectionId: route.connectionId, profile: route.profile, targetProfile: route.targetProfile } }
}

export function buildFollowupQueue(sources) {
  if (!Array.isArray(sources)) throw new Error('Invalid source list')
  const items = [], unreviewed = []
  const seen = new Set()
  let unavailableSources = 0, unreviewedSessions = 0, incomplete = sources.length === 0
  for (const source of sources) {
    const route = source?.route
    if (!route?.connectionId || !route?.profile || !route?.targetProfile || !Array.isArray(source.rows)) throw new Error('Invalid source identity')
    if (source.complete !== true || source.error) { incomplete = true; unavailableSources++ }
    for (const row of source.rows) {
      if (typeof row?.storedId !== 'string' || !row.storedId) throw new Error('Invalid stored session identity')
      const key = JSON.stringify([route.connectionId, route.profile, row.storedId])
      if (seen.has(key)) throw new Error('Duplicate session identity')
      seen.add(key)
      const value = row.snapshot
      const bound = value?.scope?.storedId === row.storedId && value?.scope?.owner?.connectionId === route.connectionId && value?.scope?.owner?.profile === route.profile && value?.sourceRoute?.connectionId === route.connectionId && value?.sourceRoute?.profile === route.profile && value?.sourceRoute?.targetProfile === route.targetProfile && object(value?.followup)
      const candidate = { key, storedId: row.storedId, route, title: row.title || row.storedId }
      const unknown = { work: 'needs_review', execution: 'unknown', actionRequired: false, explicitAccepted: false, recordFreshness: 'unknown' }
      if (!bound) {
        unreviewedSessions++; incomplete = true
        if (row.manuallyTracked) items.push({ ...candidate, followup: unknown, snapshot: null })
        else unreviewed.push({ ...candidate, reason: 'snapshot_unavailable' })
        continue
      }
      const tracked = Boolean(row.manuallyTracked || value.registration || value.remaining.length)
      if (!tracked) { unreviewedSessions++; incomplete = true; unreviewed.push({ ...candidate, reason: 'unregistered' }); continue }
      items.push({ ...candidate, title: value.title || candidate.title, followup: value.followup, snapshot: value })
    }
  }
  const attentionLowerBound = items.filter(item => item.followup.actionRequired).length
  return {
    items, unreviewed,
    counts: { trackedLowerBound: items.length, trackedExact: incomplete ? null : items.length, attentionLowerBound, unreviewedSessions },
    coverage: { complete: !incomplete, unavailableSources }
  }
}

export async function openFollowupSession(host, item) {
  if (typeof item?.storedId !== 'string' || !item.storedId.trim()) throw new Error('Stored session identity unavailable')
  if (item?.route?.unverified === true || !['local', 'remote'].includes(item?.route?.mode)) {
    const error = new Error('Session owner mode cannot be verified')
    error.code = 'FOLLOWUP_REFERENCE_IDENTITY_UNAVAILABLE'
    throw error
  }
  if (typeof item.route.storeInstanceId !== 'string' || !item.route.storeInstanceId.trim() || item.route.storeInstanceId !== item.route.storeInstanceId.trim()) {
    const error = new Error('Session store identity was not recorded, so the same connection labels cannot prove this backend')
    error.code = 'FOLLOWUP_REFERENCE_IDENTITY_UNAVAILABLE'
    throw error
  }
  const route = selectRoute(item?.route, await host.profileRoutes())
  if (route.targetProfile !== item.route.targetProfile || route.mode !== item.route.mode) throw new Error('Session owner route changed')
  const error = new Error('Verified session navigation is unavailable in this plugin version')
  error.code = 'FOLLOWUP_EXACT_NAVIGATION_UNAVAILABLE'
  throw error
}

export { createFollowupTask, validateFollowupTasks, applyFollowupAction } from './followup-tasks.mjs'

export function acceptSnapshot(currentKey, snapshot) {
  return snapshot && snapshot.key === currentKey ? snapshot : null
}
