import { exactSessionOwnerFromWire } from '@/lib/exact-session-owner'

export interface AnalysisEvidence {
  row_id: number
  quote: string
  source_id?: string
}

export interface AnalysisClaim {
  text: string
  evidence: AnalysisEvidence[]
}

export type AnalysisSourceRelation = 'primary' | 'reference' | 'read' | 'delegate' | 'continuation'
export type AnalysisSourceStatus = 'read' | 'unavailable'

export interface AnalysisSourceOwner {
  instance_id: string
  profile_id: string
  authority_epoch: number
  store_instance_id: string
}

export interface AnalysisSource {
  id: string
  profile: string
  session_id: string
  stored_session_id: string
  resolved_id: string | null
  owner: AnalysisSourceOwner | null
  relation: AnalysisSourceRelation
  via_source_id: string | null
  via_row_id: number | null
  status: AnalysisSourceStatus
  complete: boolean
  reason: string | null
}

interface TaskAnalysisCommon {
  title: string
  analyzed_at: number
  conversation_revision: unknown
  stale: boolean
  message_count: number
  analyzed_messages: number
  limitations: string[]
  status: 'todo' | 'planning' | 'in_progress' | 'waiting' | 'failed' | 'awaiting_acceptance'
  goal: AnalysisClaim | null
  completed: AnalysisClaim[]
  remaining: AnalysisClaim[]
  needs_user: AnalysisClaim[]
}

export interface TaskAnalysisV1 extends TaskAnalysisCommon {
  version: 1
  scope: 'single_conversation'
}

export interface TaskAnalysisV2 extends TaskAnalysisCommon {
  version: 2
  scope: 'linked_conversations'
  complete: boolean
  sources: AnalysisSource[]
}

export type TaskAnalysis = TaskAnalysisV1 | TaskAnalysisV2

const record = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === 'object' && !Array.isArray(value))
const text = (value: unknown): value is string => typeof value === 'string' && Boolean(value.trim())
const count = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0
const positiveRow = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) > 0
const sourceRelations = new Set<AnalysisSourceRelation>(['primary', 'reference', 'read', 'delegate', 'continuation'])

function commonAnalysis(value: Record<string, unknown>): boolean {
  return text(value.title) && count(value.analyzed_at) && value.analyzed_at !== 0 &&
    Object.hasOwn(value, 'conversation_revision') && typeof value.stale === 'boolean' &&
    count(value.message_count) && count(value.analyzed_messages) && value.analyzed_messages <= value.message_count &&
    Array.isArray(value.limitations) && value.limitations.every(text) &&
    ['todo', 'planning', 'in_progress', 'waiting', 'failed', 'awaiting_acceptance'].includes(String(value.status))
}

function evidence(value: unknown, sourceIds?: Set<string>): value is AnalysisEvidence {
  if (!record(value) || !positiveRow(value.row_id) || !text(value.quote)) { return false }

  if (sourceIds === undefined) { return true }
  return text(value.source_id) && sourceIds.has(value.source_id)
}

function claim(value: unknown, sourceIds?: Set<string>): value is AnalysisClaim {
  return record(value) && text(value.text) && Array.isArray(value.evidence) && value.evidence.length > 0 &&
    value.evidence.every(item => evidence(item, sourceIds))
}

function validSourceIdentity(value: Record<string, unknown>): boolean {
  return text(value.id) && text(value.profile) && text(value.session_id) && text(value.stored_session_id)
}

function validSourceOwner(value: Record<string, unknown>): boolean {
  return value.owner === null || Boolean(exactSessionOwnerFromWire(value.owner))
}

function validSourceRelation(value: Record<string, unknown>): boolean {
  return sourceRelations.has(value.relation as AnalysisSourceRelation)
}

function validSourceLink(value: Record<string, unknown>): boolean {
  const sourceId = value.via_source_id
  const row = value.via_row_id

  return (sourceId === null && row === null) || (text(sourceId) && positiveRow(row))
}

function validSourceMetadata(value: Record<string, unknown>): boolean {
  return (value.resolved_id === null || text(value.resolved_id)) && validSourceOwner(value) &&
    validSourceRelation(value) && validSourceLink(value) && typeof value.complete === 'boolean' &&
    ['read', 'unavailable'].includes(String(value.status)) && (value.reason === null || text(value.reason))
}

function validSourceStatus(value: Record<string, unknown>): boolean {
  if (value.status === 'read') {
    return Boolean(value.resolved_id && exactSessionOwnerFromWire(value.owner))
  }

  return value.resolved_id === null && value.owner === null && value.complete === false && text(value.reason)
}

function source(value: unknown): value is AnalysisSource {
  if (!record(value) || !validSourceIdentity(value) || !validSourceMetadata(value)) { return false }
  return validSourceStatus(value)
}

function analysisClaims(value: Record<string, unknown>, sourceIds?: Set<string>): boolean {
  return (value.goal === null || claim(value.goal, sourceIds)) &&
    [value.completed, value.remaining, value.needs_user].every(items => Array.isArray(items) && items.every(item => claim(item, sourceIds)))
}

function validateV2(value: Record<string, unknown>): boolean {
  if (!commonAnalysis(value) || value.version !== 2 || value.scope !== 'linked_conversations' || typeof value.complete !== 'boolean' ||
      !Array.isArray(value.sources) || value.sources.length === 0 || !value.sources.every(source)) {
    return false
  }

  const sources = value.sources as AnalysisSource[]
  const ids = new Set(sources.map(item => item.id))
  const primary = sources.filter(item => item.relation === 'primary')
  const hasInvalidLink = sources.some(item => item.via_source_id !== null &&
    (!ids.has(item.via_source_id) || item.via_row_id === null))
  const readIds = new Set(sources.filter(item => item.status === 'read').map(item => item.id))

  return ids.size === sources.length && primary.length === 1 && !hasInvalidLink &&
    (!value.complete || sources.every(item => item.status === 'read' && item.complete)) &&
    analysisClaims(value, readIds)
}

function validateV1(value: Record<string, unknown>): boolean {
  return commonAnalysis(value) && value.version === 1 && value.scope === 'single_conversation' && analysisClaims(value)
}

export function validateTaskAnalysis(value: unknown): TaskAnalysis {
  if (!record(value) || (!validateV1(value) && !validateV2(value))) {
    throw new Error('Invalid task analysis')
  }

  return value as unknown as TaskAnalysis
}
