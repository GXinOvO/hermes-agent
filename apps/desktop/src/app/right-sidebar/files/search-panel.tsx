import type { FileSearchMatch } from '@hermes/shared/file-search'
import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Input } from '@/components/ui/input'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { desktopFsCacheKey } from '@/lib/desktop-fs'
import { searchDesktopFiles } from '@/lib/desktop-search'
import { $fileSearchOwner, type FileSearchScope } from '@/lib/file-search-owner'
import { cn } from '@/lib/utils'
import { $connection } from '@/store/session'

import { $showIgnoredRoots, showsIgnoredFiles } from './prefs'
import { compileQuery, type FileSearchHit, type FileSearchMode, groupHitsByFile } from './search'

/** Typing settles before a walk starts: a search reads the filesystem, so each
 *  keystroke must not spawn its own walk. */
const SEARCH_DEBOUNCE_MS = 220

/** The matched span inside a snippet, so a result row can be scanned by eye. */
function Highlighted({
  caseSensitive,
  query,
  regex,
  ranges,
  text
}: {
  caseSensitive: boolean
  query: string
  regex: boolean
  ranges?: { from: number; to: number }[]
  text: string
}) {
  const parts = useMemo(() => {
    let spans = ranges

    if (!spans) {
      try {
        spans = Array.from(text.matchAll(compileQuery(query, { caseSensitive, regex })), match => ({
          from: match.index,
          to: match.index + match[0].length
        }))
      } catch {
        return [{ hit: false, value: text }]
      }
    }

    const out: Array<{ hit: boolean; value: string }> = []
    let last = 0

    for (const span of spans) {
      if (span.to <= span.from) {
        continue
      }

      if (span.from > last) {
        out.push({ hit: false, value: text.slice(last, span.from) })
      }

      out.push({ hit: true, value: text.slice(span.from, span.to) })
      last = span.to
    }

    if (last < text.length) {
      out.push({ hit: false, value: text.slice(last) })
    }

    return out.length ? out : [{ hit: false, value: text }]
  }, [caseSensitive, query, ranges, regex, text])

  return (
    <>
      {parts.map((part, index) =>
        part.hit ? (
          <mark className="bg-transparent font-semibold text-(--ui-accent)" key={index}>
            {part.value}
          </mark>
        ) : (
          <span key={index}>{part.value}</span>
        )
      )}
    </>
  )
}

interface SearchOutcome {
  scope: FileSearchScope
  hits: FileSearchMatch[]
  truncated: boolean
  warnings: string[]
}

function hasSearchResults(outcome: SearchOutcome | null): outcome is SearchOutcome {
  return outcome !== null && (outcome.hits.length > 0 || outcome.truncated)
}

interface FileSearchPanelProps {
  onClose: () => void
  onOpenFile: (path: string, location?: { line: number; column?: number }, scope?: FileSearchScope) => void
  root: string
}

/**
 * Scoped search over the open project, in the Files pane.
 *
 * `Name` matches the relative path (including folders the tree never expanded) and
 * `Content` greps file text, returning path, line, column and snippet. Results open
 * through the pane's own preview path, so a hit behaves like a tree click.
 *
 * The controls are the app's own `Input` / `SegmentedControl` / `Button`. A
 * hand-rolled <input> here picked up `--ui-bg-input`, which is a near-white
 * value, so typed text was invisible on dark themes.
 */
export function FileSearchPanel({ onClose, onOpenFile, root }: FileSearchPanelProps) {
  const { t } = useI18n()
  const r = t.rightSidebar

  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<FileSearchMode>('name')
  const [scope, setScope] = useState('')
  const [caseSensitive, setCaseSensitive] = useState(false)
  const [regex, setRegex] = useState(false)
  const [outcome, setOutcome] = useState<SearchOutcome | null>(null)
  // Counts and hits from a run still in progress: a content search over a large
  // root takes seconds, and an empty pane during that time reads as "nothing happened".
  const [live, setLive] = useState<SearchOutcome | null>(null)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState(0)
  const [searching, setSearching] = useState(false)

  const inputRef = useRef<HTMLInputElement | null>(null)
  const [searchController, setSearchController] = useState<AbortController | null>(null)
  const connection = useStore($connection)
  const owner = useStore($fileSearchOwner)
  const ownerKey = `${desktopFsCacheKey(connection)}:${owner.key}`
  useStore($showIgnoredRoots)
  const showIgnored = showsIgnoredFiles(root)

  // Final hits once the walk finishes, otherwise whatever the run has found so far.
  const groups = useMemo(() => groupHitsByFile(outcome ? outcome.hits : (live?.hits ?? [])), [outcome, live])
  const flat = useMemo(() => groups.flatMap(group => group.hits), [groups])

  // Row identity → position in the keyboard order, so a render does not scan the
  // whole hit list once per row.
  const positionOf = useMemo(
    () => new Map(flat.map((hit, index) => [`${hit.path}:${hit.line ?? 0}:${hit.col ?? 0}`, index])),
    [flat]
  )

  const placeholder = mode === 'name' ? r.filesSearchPlaceholderName : r.filesSearchPlaceholderContent

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  useEffect(() => {
    if (!query.trim()) {
      setOutcome(null)
      setLive(null)
      setError('')
      setSearching(false)

      return
    }

    const controller = new AbortController()
    setSearchController(controller)
    let cancelled = false
    let lastReport = 0
    const hits: FileSearchMatch[] = []
    let final: SearchOutcome | null = null
    setSearching(true)
    setLive(null)
    setOutcome(null)
    setError('')

    const timer = setTimeout(() => {
      const searchScope = owner.scope ? { ...owner.scope, root } : undefined

      if (!searchScope) {
        setError(owner.error)
        setSearching(false)

        return
      }

      void searchDesktopFiles(
        {
          caseSensitive,
          mode,
          query,
          regex: mode === 'content' && regex,
          root,
          scope,
          showIgnored,
          maxResults: 1000,
          timeoutMs: 15000
        },
        {
          scope: searchScope,
          signal: controller.signal,
          onFrame: frame => {
            if (cancelled || controller.signal.aborted) {
              return
            }

            if (frame.type === 'error') {
              setError(frame.message)
              final = { scope: searchScope, hits: [...hits], truncated: true, warnings: [] }
            } else if (frame.type === 'done') {
              final = { scope: searchScope, hits: [...hits], truncated: frame.truncated, warnings: frame.warnings }
            } else {
              hits.push(...frame.hits)
              const now = Date.now()

              if (lastReport && now - lastReport < 120) {
                return
              }

              lastReport = now
              setLive({ scope: searchScope, hits: [...hits], truncated: false, warnings: [] })
            }
          }
        }
      )
        .then(() => {
          if (cancelled || controller.signal.aborted) {
            return
          }

          setLive(null)

          if (final) {
            setOutcome(final)
          }

          setSelected(0)
          setSearching(false)
        })
        .catch((cause: unknown) => {
          if (cancelled || controller.signal.aborted) {
            return
          }

          setError(cause instanceof Error ? cause.message : String(cause))
          setLive(null)
          setOutcome(hits.length ? { scope: searchScope, hits: [...hits], truncated: true, warnings: [] } : null)
          setSelected(0)
          setSearching(false)
        })
    }, SEARCH_DEBOUNCE_MS)

    return () => {
      cancelled = true
      controller.abort()
      clearTimeout(timer)
    }
  }, [caseSensitive, mode, owner, ownerKey, query, regex, root, scope, showIgnored])

  const openHit = useCallback(
    (hit: FileSearchHit | undefined) => {
      if (hit) {
        const scope = (outcome ?? live)?.scope
        const location = hit.line ? { line: hit.line, column: (hit.col ?? 0) + 1 } : undefined
        onOpenFile(hit.path, location, scope)
      }
    },
    [live, onOpenFile, outcome]
  )

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()

        if (query) {
          setQuery('')
        } else {
          onClose()
        }

        return
      }

      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()

        if (flat.length === 0) {
          return
        }

        setSelected(current => {
          const next = event.key === 'ArrowDown' ? current + 1 : current - 1

          return (next + flat.length) % flat.length
        })

        return
      }

      if (event.key === 'Enter') {
        event.preventDefault()
        openHit(flat[selected])
      }
    },
    [flat, onClose, openHit, query, selected]
  )

  const toggleClass = (active: boolean) =>
    cn(
      'h-6 w-6 shrink-0 rounded-[2.5px] text-(--ui-text-tertiary) hover:text-(--ui-text-primary)',
      active && 'bg-(--ui-row-active-background) text-(--ui-text-primary)'
    )

  const showEmpty =
    Boolean(query.trim()) && !error && outcome !== null && !outcome.truncated && outcome.hits.length === 0 && !searching

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-file-search="" onKeyDown={handleKeyDown}>
      <div className="flex shrink-0 items-center gap-1 px-2.5 pt-1.5">
        <Input
          aria-label={placeholder}
          className="h-6 flex-1 py-0"
          onChange={event => setQuery(event.target.value)}
          placeholder={placeholder}
          ref={inputRef}
          size="xs"
          value={query}
        />
        <Tip label={r.filesSearchClose}>
          <Button
            aria-label={r.filesSearchClose}
            className={toggleClass(false)}
            onClick={onClose}
            size="icon-xs"
            variant="ghost"
          >
            <Codicon name="close" size="0.8125rem" />
          </Button>
        </Tip>
      </div>

      <div className="flex shrink-0 items-center gap-1 px-2.5 py-1.5">
        <div className="shrink-0">
          <SegmentedControl
            onChange={setMode}
            options={[
              { id: 'name' as const, label: r.filesSearchNameMode },
              { id: 'content' as const, label: r.filesSearchContentMode }
            ]}
            sizing="content"
            value={mode}
          />
        </div>
        <Tip label={r.filesSearchCase}>
          <Button
            aria-label={r.filesSearchCase}
            aria-pressed={caseSensitive}
            className={toggleClass(caseSensitive)}
            onClick={() => setCaseSensitive(value => !value)}
            size="icon-xs"
            variant="ghost"
          >
            <Codicon name="case-sensitive" size="0.8125rem" />
          </Button>
        </Tip>
        {mode === 'content' ? (
          <Tip label={r.filesSearchRegex}>
            <Button
              aria-label={r.filesSearchRegex}
              aria-pressed={regex}
              className={toggleClass(regex)}
              onClick={() => setRegex(value => !value)}
              size="icon-xs"
              variant="ghost"
            >
              <Codicon name="regex" size="0.8125rem" />
            </Button>
          </Tip>
        ) : null}
        {searching ? (
          <Button
            aria-label={r.filesSearchCancel}
            className="ms-auto"
            onClick={() => {
              searchController?.abort()
              setSearching(false)
              setOutcome(live ? { ...live, truncated: true } : null)
            }}
            size="icon-xs"
            variant="ghost"
          >
            <Codicon name="debug-stop" size="0.75rem" />
          </Button>
        ) : null}
      </div>

      <div className="shrink-0 px-2.5 pb-1.5">
        <Input
          aria-label={r.filesSearchScope}
          className="h-6"
          onChange={event => setScope(event.target.value)}
          placeholder={r.filesSearchScopePlaceholder}
          size="xs"
          value={scope}
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto" data-file-search-results="">
        {error ? <div className="px-2.5 py-2 text-xs text-(--ui-red)">{r.filesSearchInvalidPattern(error)}</div> : null}

        {!error && !query.trim() ? (
          <div className="px-2.5 py-2 text-xs text-(--ui-text-quaternary)">{r.filesSearchHint}</div>
        ) : null}

        {showEmpty ? (
          <div className="px-2.5 py-2 text-xs text-(--ui-text-quaternary)">{r.filesSearchNoResults}</div>
        ) : null}

        {searching && !outcome ? (
          <div className="px-2.5 pb-1 text-[0.6875rem] text-(--ui-text-tertiary)">
            {r.filesSearchRunning(live?.hits.length ?? 0)}
          </div>
        ) : null}

        {!error && hasSearchResults(outcome) ? (
          <div className="px-2.5 pb-1 text-[0.6875rem] text-(--ui-text-quaternary)">
            {r.filesSearchStatus(outcome.hits.length, groups.length)}
            {outcome.truncated ? ` · ${r.filesSearchTruncated}` : ''}
          </div>
        ) : null}

        {outcome?.warnings.map((warning, index) => (
          <div className="px-2.5 py-1 text-xs text-(--ui-text-tertiary)" key={index} role="status">
            {warning}
          </div>
        ))}

        {groups.map(group => (
          <div key={group.path}>
            <div
              className="flex h-6 min-w-0 items-center gap-1 px-2.5 text-xs text-(--ui-text-secondary)"
              title={group.rel}
            >
              <Codicon className="shrink-0" name="file" size="0.75rem" />
              <span className="shrink-0 pe-1">{group.rel.split('/').pop()}</span>
              <span className="min-w-0 flex-1 truncate text-[0.6875rem] text-(--ui-text-quaternary)">
                {group.rel.includes('/') ? group.rel.slice(0, group.rel.lastIndexOf('/')) : ''}
              </span>
              {group.hits.length > 1 ? (
                <span className="shrink-0 ps-1 text-(--ui-text-quaternary)">{group.hits.length}</span>
              ) : null}
            </div>
            {group.hits.map(hit => {
              const key = `${hit.path}:${hit.line ?? 0}:${hit.col ?? 0}`
              const index = positionOf.get(key) ?? -1

              return (
                <button
                  aria-selected={index === selected}
                  className={cn(
                    'flex h-6 w-full items-center gap-2 px-2.5 ps-7 text-left text-xs text-(--ui-text-tertiary)',
                    'hover:bg-(--ui-row-hover-background) hover:text-(--ui-text-primary)',
                    index === selected && 'bg-(--ui-row-active-background) text-(--ui-text-primary)'
                  )}
                  data-file-search-hit={hit.line ? `${hit.rel}:${hit.line}` : hit.rel}
                  key={key}
                  onClick={() => {
                    setSelected(index)
                    openHit(hit)
                  }}
                  type="button"
                >
                  {hit.line ? <span className="shrink-0 text-(--ui-text-quaternary)">{hit.line}</span> : null}
                  <span className="min-w-0 flex-1 truncate">
                    {hit.text ? (
                      <Highlighted
                        caseSensitive={caseSensitive}
                        query={query}
                        ranges={hit.ranges}
                        regex={regex}
                        text={hit.text}
                      />
                    ) : (
                      hit.rel.split('/').pop()
                    )}
                  </span>
                </button>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}
