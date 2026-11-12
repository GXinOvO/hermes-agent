/**
 * Scoped file search for the Files pane.
 *
 * Two modes over the SAME walk: `name` matches the relative path, `content` greps
 * file text. The walk reads directories through an injected IO so the engine stays
 * pure, testable, and identical for a local checkout and a remote backend (the pane
 * passes the same `readProjectDir` bridge the tree uses, so hidden/gitignored rules
 * agree with what the tree shows).
 *
 * Limits are part of the contract: every cap that cuts the result reports itself in
 * `truncated`/`skipped`, because a silent partial answer reads as "no matches".
 */

export type FileSearchMode = 'name' | 'content'

export interface FileSearchEntry {
  isDirectory: boolean
  name: string
  path: string
}

export interface FileSearchReadResult {
  binary?: boolean
  text: string
  truncated?: boolean
}

/** Filesystem door. Directory reads reuse the pane's listing so scope rules match the tree. */
export interface FileSearchIo {
  readDir: (dir: string, root: string) => Promise<{ entries: FileSearchEntry[]; error?: string }>
  readText: (path: string) => Promise<FileSearchReadResult>
}

export interface FileSearchLimits {
  /** Directory listing calls (one per folder). */
  maxDirs: number
  /** Files a walk may discover before it reports truncation. */
  maxFiles: number
  /** Total content matches returned. */
  maxMatches: number
  /** Content matches returned from one file. */
  maxMatchesPerFile: number
  /** File reads in content mode. */
  maxReads: number
  /** Wall-clock budget for the whole walk. A root can be arbitrarily large (a home
   *  directory, a checkout with vendored trees), so the pane answers with what it
   *  has instead of walking forever. */
  maxMs: number
  concurrency: number
}

export const DEFAULT_FILE_SEARCH_LIMITS: FileSearchLimits = {
  maxDirs: 4000,
  maxFiles: 40_000,
  maxMatches: 300,
  maxMatchesPerFile: 20,
  maxReads: 1500,
  maxMs: 15_000,
  concurrency: 6
}

/** Never read these for content: the read would be wasted or refused anyway. */
const BINARY_EXTENSIONS = new Set([
  '7z', 'a', 'avi', 'bin', 'bmp', 'bz2', 'class', 'dll', 'dylib', 'eot', 'exe', 'gif', 'gz', 'ico', 'jar',
  'jpeg', 'jpg', 'lockb', 'm4a', 'mkv', 'mov', 'mp3', 'mp4', 'o', 'otf', 'pdf', 'png', 'pyc', 'so', 'sqlite',
  'sqlite3', 'tar', 'tiff', 'ttf', 'wav', 'webm', 'webp', 'woff', 'woff2', 'xz', 'zip'
])

export interface FileSearchOptions {
  caseSensitive?: boolean
  limits?: Partial<FileSearchLimits>
  mode: FileSearchMode
  query: string
  regex?: boolean
  root: string
  /** Include filter: a folder prefix (`src`, `src/`) or a glob (`src/**\/*.ts`, `*.py`). */
  scope?: string
}

export interface FileSearchHit {
  /** 1-based line, content mode only. */
  line?: number
  /** 0-based character column, content mode only. */
  col?: number
  /** Absolute path. */
  path: string
  /** Path relative to the searched root, always `/` separated. */
  rel: string
  /** Matching line, trimmed to MAX_SNIPPET, content mode only. */
  text?: string
  ranges?: { from: number; to: number }[]
}

export interface FileSearchOutcome {
  dirsScanned: number
  filesScanned: number
  hits: FileSearchHit[]
  /** Files skipped because they were binary, unreadable, or too large to hold. */
  skipped: number
  /** A limit cut the walk or the result set short. */
  truncated: boolean
}

/** Live counters for a run in progress: enough to say what it is doing, without
 *  pretending the answer is final. */
export interface FileSearchProgress {
  dirsScanned: number
  filesScanned: number
  hits: readonly FileSearchHit[]
  reads: number
}

const MAX_SNIPPET = 300

/** Paths arrive in the spelling of the backend that served them: Windows exposed to
 *  WSL, or a POSIX remote. Compare the way that backend does. */
function caselessPaths(value: string) {
  return value.includes('\\') || /^[a-zA-Z]:/.test(value)
}

export function toPosixPath(value: string) {
  return value.replace(/\\/g, '/')
}

/** `target` relative to `root`, or null when it is not underneath it. */
export function relativeToRoot(root: string, target: string): null | string {
  const r = toPosixPath(root).replace(/\/+$/, '')
  const t = toPosixPath(target).replace(/\/+$/, '')
  const caseless = caselessPaths(r) || caselessPaths(t)
  const rKey = caseless ? r.toLowerCase() : r
  const tKey = caseless ? t.toLowerCase() : t

  if (tKey === rKey) {
    return ''
  }

  return tKey.startsWith(`${rKey}/`) ? t.slice(r.length + 1) : null
}

/** Extensions that a content search refuses to read. */
export function hasBinaryExtension(path: string) {
  const name = toPosixPath(path).split('/').pop() ?? ''
  const dot = name.lastIndexOf('.')

  return dot > 0 ? BINARY_EXTENSIONS.has(name.slice(dot + 1).toLowerCase()) : false
}

/**
 * A small glob: `**` crosses folders, `*`/`?` stay inside one, `{a,b}` alternates.
 * A pattern without a slash matches a file name at any depth (`*.ts`), which is what
 * a person typing an extension means.
 */
export function globToRegExp(pattern: string): null | RegExp {
  const raw = toPosixPath(pattern.trim())

  if (!raw) {
    return null
  }

  let out = ''
  let i = 0

  while (i < raw.length) {
    const ch = raw[i]

    if (ch === '*') {
      if (raw[i + 1] === '*') {
        // `**/` also matches zero segments, so `src/**/x.ts` accepts `src/x.ts`.
        if (raw[i + 2] === '/') {
          out += '(?:[^/]+/)*'
          i += 3

          continue
        }

        out += '.*'
        i += 2

        continue
      }

      out += '[^/]*'
      i += 1

      continue
    }

    if (ch === '?') {
      out += '[^/]'
      i += 1

      continue
    }

    if (ch === '{') {
      const close = raw.indexOf('}', i)

      if (close > i) {
        const parts = raw.slice(i + 1, close).split(',').map(part => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))

        out += `(?:${parts.join('|')})`
        i = close + 1

        continue
      }
    }

    out += ch.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    i += 1
  }

  return new RegExp(raw.includes('/') ? `^${out}$` : `(?:^|/)${out}$`, 'i')
}

/**
 * Whether a relative path is inside the requested scope. An empty scope means the
 * whole project; a plain path (`src`, `src/`) is a folder prefix, so it needs no
 * glob and no listing probe.
 */
export function matchesScope(rel: string, scope?: string): boolean {
  const raw = toPosixPath(scope ?? '').trim()

  if (!raw) {
    return true
  }

  if (!/[*?{[]/.test(raw)) {
    const prefix = raw.replace(/\/+$/, '').toLowerCase()

    return rel.toLowerCase() === prefix || rel.toLowerCase().startsWith(`${prefix}/`)
  }

  const re = globToRegExp(raw)

  return re ? re.test(rel) : true
}

/** The user's query as a matcher. An invalid regex throws — the pane shows it as typed input. */
export function compileQuery(query: string, options: { caseSensitive?: boolean; regex?: boolean } = {}) {
  const flags = `g${options.caseSensitive ? '' : 'i'}`
  const source = options.regex ? query : query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

  return new RegExp(source, flags)
}

export interface LineMatch {
  col: number
  line: number
  text: string
}

/**
 * The literal folder prefix of a scope, when it has one.
 *
 * A scope narrows which FILES match, but the walk must not enter folders that cannot
 * contain them: `src/` under a home directory otherwise lists every folder in the home
 * before finding one file. Globs contribute their leading literal segments
 * (`src/**\/*.py` → `src`); a pattern starting with a wildcard has no prefix.
 */
export function scopeFolderPrefix(scope?: string): null | string {
  const raw = toPosixPath(scope ?? '').trim()

  if (!raw) {
    return null
  }

  const segments = raw.split('/')
  const wildcard = segments.findIndex(segment => /[*?{[]/.test(segment))
  const head = (wildcard < 0 ? raw : segments.slice(0, wildcard).join('/')).replace(/\/+$/, '')

  return head || null
}

/** Whether a folder can still contain a path inside `prefix`. */
export function folderCouldHold(rel: string, prefix: string): boolean {
  const folder = rel.toLowerCase()
  const target = prefix.toLowerCase()

  return target === folder || target.startsWith(`${folder}/`) || folder.startsWith(`${target}/`)
}

/** Every match in one file's text, up to `maxPerFile`, each with its 1-based line. */
export function findLineMatches(text: string, re: RegExp, maxPerFile: number): LineMatch[] {
  const out: LineMatch[] = []

  if (!text || maxPerFile <= 0) {
    return out
  }

  const lines = text.split('\n')

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]

    if (!line) {
      continue
    }

    re.lastIndex = 0

    const match = re.exec(line)

    if (!match) {
      continue
    }

    out.push({
      col: match.index,
      line: index + 1,
      text: line.length > MAX_SNIPPET ? `${line.slice(0, MAX_SNIPPET)}…` : line
    })

    if (out.length >= maxPerFile) {
      break
    }
  }

  return out
}

function resolveLimits(limits?: Partial<FileSearchLimits>): FileSearchLimits {
  return { ...DEFAULT_FILE_SEARCH_LIMITS, ...limits }
}

/** Group hits by file for the result list: one header per file, its own hits in line order. */
export function groupHitsByFile(hits: readonly FileSearchHit[]): Array<{ rel: string; path: string; hits: FileSearchHit[] }> {
  const groups = new Map<string, FileSearchHit[]>()

  for (const hit of hits) {
    const bucket = groups.get(hit.path)

    if (bucket) {
      bucket.push(hit)
    } else {
      groups.set(hit.path, [hit])
    }
  }

  return [...groups.entries()].map(([path, fileHits]) => ({
    hits: [...fileHits].sort((a, b) => (a.line ?? 0) - (b.line ?? 0)),
    path,
    rel: fileHits[0].rel
  }))
}

/**
 * Walk the project once and answer the query.
 *
 * The walk is breadth-first over directories in batches, so a deep tree cannot
 * starve the event loop one IPC round-trip at a time, and both modes stop at the
 * same place: caps are checked before a new batch is read.
 */
export async function runFileSearch(
  options: FileSearchOptions,
  io: FileSearchIo,
  signal?: AbortSignal,
  onProgress?: (progress: FileSearchProgress) => void
): Promise<FileSearchOutcome> {
  const limits = resolveLimits(options.limits)
  const scope = options.scope
  const query = options.query
  const hits: FileSearchHit[] = []
  // Reads are the expensive half of a content search: one round trip per file. The
  // budget has to be enforced here, or a big root turns one query into tens of
  // thousands of requests and the pane looks hung.
  let reads = 0

  const outcome: FileSearchOutcome = { dirsScanned: 0, filesScanned: 0, hits, skipped: 0, truncated: false }

  const report = () => onProgress?.({ dirsScanned: outcome.dirsScanned, filesScanned: outcome.filesScanned, hits, reads })

  const startedAt = Date.now()
  const outOfTime = () => Date.now() - startedAt >= limits.maxMs

  if (!query.trim() || !options.root) {
    return outcome
  }

  const nameMode = options.mode === 'name'
  const matcher = nameMode ? compileQuery(query, { caseSensitive: options.caseSensitive, regex: options.regex }) : null
  const contentMatcher = nameMode ? null : compileQuery(query, { caseSensitive: options.caseSensitive, regex: options.regex })

  // Folders outside the scope's literal prefix are never listed: that is what makes a
  // scoped search on a huge root cheap instead of merely filtered.
  const folderPrefix = scopeFolderPrefix(scope)

  const queue: string[] = [options.root]

  while (queue.length > 0) {
    if (signal?.aborted) {
      break
    }

    if (outcome.dirsScanned >= limits.maxDirs || outcome.filesScanned >= limits.maxFiles) {
      outcome.truncated = true

      break
    }

    // One budget for both modes: a name query on a big root can match thousands of
    // paths, and a list that long is neither readable nor cheap to render.
    if (hits.length >= limits.maxMatches) {
      outcome.truncated = true

      break
    }

    if (outOfTime()) {
      outcome.truncated = true

      break
    }

    const batch = queue.splice(0, limits.concurrency)

    const listings = await Promise.all(
      batch.map(async dir => {
        try {
          const listing = await io.readDir(dir, options.root)

          return { dir, listing }
        } catch {
          return { dir, listing: { entries: [] as FileSearchEntry[], error: 'read-error' } }
        }
      })
    )

    const candidates: Array<{ path: string; rel: string }> = []

    for (const { dir, listing } of listings) {
      outcome.dirsScanned += 1

      if (listing.error) {
        outcome.skipped += 1

        continue
      }

      for (const entry of listing.entries) {
        if (signal?.aborted) {
          break
        }

        if (entry.isDirectory) {
          const dirRel = relativeToRoot(options.root, entry.path)

          // `null` means the entry is not under the root (a symlinked listing): keep it
          // out rather than walk somewhere the search was never scoped to.
          if (dirRel === null) {
            continue
          }

          if (!folderPrefix || folderCouldHold(dirRel, folderPrefix)) {
            queue.push(entry.path)
          }

          continue
        }

        const rel = relativeToRoot(options.root, entry.path)

        if (rel === null) {
          continue
        }

        if (outcome.filesScanned >= limits.maxFiles) {
          outcome.truncated = true

          break
        }

        outcome.filesScanned += 1

        if (!matchesScope(rel, scope)) {
          continue
        }

        if (nameMode) {
          matcher!.lastIndex = 0

          if (matcher!.test(rel)) {
            if (hits.length >= limits.maxMatches) {
              outcome.truncated = true

              break
            }

            hits.push({ path: entry.path, rel })
          }

          continue
        }

        candidates.push({ path: entry.path, rel })
      }
    }

    report()

    if (nameMode) {
      continue
    }

    // Content mode: reads are the expensive part, so they run in the same bounded
    // batches and stop as soon as the match budget is spent.
    for (let i = 0; i < candidates.length; i += limits.concurrency) {
      if (signal?.aborted) {
        break
      }

      if (hits.length >= limits.maxMatches) {
        outcome.truncated = true

        break
      }

      if (reads >= limits.maxReads) {
        outcome.truncated = true

        break
      }

      if (outOfTime()) {
        outcome.truncated = true

        break
      }

      const slice = candidates.slice(i, i + limits.concurrency)

      await Promise.all(
        slice.map(async candidate => {
          if (hits.length >= limits.maxMatches) {
            return
          }

          if (reads >= limits.maxReads) {
            outcome.truncated = true

            return
          }

          reads += 1

          if (hasBinaryExtension(candidate.path)) {
            outcome.skipped += 1

            return
          }

          let read: FileSearchReadResult

          try {
            read = await io.readText(candidate.path)
          } catch {
            outcome.skipped += 1

            return
          }

          if (read.binary) {
            outcome.skipped += 1

            return
          }

          // A truncated read means the tail was never searched: say so instead of
          // reporting a complete file with no match.
          if (read.truncated) {
            outcome.skipped += 1
            outcome.truncated = true
          }

          const matches = findLineMatches(read.text, contentMatcher!, limits.maxMatchesPerFile)

          for (const match of matches) {
            if (hits.length >= limits.maxMatches) {
              outcome.truncated = true

              break
            }

            hits.push({ col: match.col, line: match.line, path: candidate.path, rel: candidate.rel, text: match.text })
          }
        })
      )

      report()
    }
  }

  report()

  return outcome
}
