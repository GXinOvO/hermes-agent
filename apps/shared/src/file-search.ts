export interface FileSearchRequest {
  root: string
  query: string
  mode: 'name' | 'content'
  scope?: string
  regex?: boolean
  caseSensitive?: boolean
  showIgnored?: boolean
  maxResults?: number
  timeoutMs?: number
}

/** Renderer → host envelope. Native paths never inherit the remote connection. */
export interface FileSearchPayload {
  id: string
  native: boolean
  connectionId?: string
  profile?: string
  /** Selects this owner's session workspace (including its Docker mapping). */
  sessionId?: string
  request: FileSearchRequest
}

export interface FileSearchMatch {
  path: string
  rel: string
  /** One-based source line. */
  line?: number
  /** Zero-based UTF-16 column in the original source line. */
  col?: number
  text?: string
  /** UTF-16 offsets into text (which may be a centered snippet). */
  ranges?: { from: number; to: number }[]
}

export type FileSearchFrame =
  | { type: 'batch'; hits: FileSearchMatch[] }
  | { type: 'done'; truncated: boolean; reason?: string; warnings: string[]; elapsedMs: number; filesSearched?: number }
  | { type: 'error'; code: string; message: string }

export class FileSearchValidationError extends Error {
  readonly code = 'invalid-request'

  constructor(message: string) {
    super(message)
    this.name = 'FileSearchValidationError'
  }
}

function boundedString(value: unknown, field: string, allowEmpty = false): string {
  if (typeof value !== 'string' || (!allowEmpty && !value.length) || value.length > 4096 || value.includes('\0')) {
    throw new FileSearchValidationError(
      `${field} must be a ${allowEmpty ? '' : 'nonempty '}string of at most 4096 characters without NUL`
    )
  }

  return value
}

function boundedInteger(value: unknown, field: string, minimum: number, maximum: number, fallback: number): number {
  if (value === undefined) {
    return fallback
  }

  if (typeof value !== 'number' || !Number.isInteger(value) || value < minimum || value > maximum) {
    throw new FileSearchValidationError(`${field} must be an integer from ${minimum} to ${maximum}`)
  }

  return value
}

/** Pure wire validation: never trim or rewrite a literal query or a host-specific path. */
export function validateFileSearchRequest(input: unknown): FileSearchRequest {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    throw new FileSearchValidationError('Search request must be an object')
  }

  const value = input as Record<string, unknown>
  const root = boundedString(value.root, 'root')
  const query = boundedString(value.query, 'query')

  if (!root.trim()) {
    throw new FileSearchValidationError('root must not be blank')
  }

  if (value.mode !== 'name' && value.mode !== 'content') {
    throw new FileSearchValidationError('mode must be name or content')
  }

  if (value.mode === 'content' && /[\r\n]/.test(query)) {
    throw new FileSearchValidationError('Multiline content queries are not supported')
  }

  for (const field of ['regex', 'caseSensitive', 'showIgnored']) {
    if (value[field] !== undefined && typeof value[field] !== 'boolean') {
      throw new FileSearchValidationError(`${field} must be a boolean`)
    }
  }

  return {
    root,
    query,
    mode: value.mode,
    ...(value.scope === undefined ? {} : { scope: boundedString(value.scope, 'scope', true) }),
    regex: value.regex === true,
    caseSensitive: value.caseSensitive === true,
    showIgnored: value.showIgnored === true,
    maxResults: boundedInteger(value.maxResults, 'maxResults', 1, 2000, 1000),
    timeoutMs: boundedInteger(value.timeoutMs, 'timeoutMs', 50, 30000, 15000)
  }
}
