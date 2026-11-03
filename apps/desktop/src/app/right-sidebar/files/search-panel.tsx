import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Input } from '@/components/ui/input'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { readDesktopFileText } from '@/lib/desktop-fs'
import { cn } from '@/lib/utils'

import { readProjectDir } from './ipc'
import {
  compileQuery,
  type FileSearchHit,
  type FileSearchMode,
  type FileSearchOutcome,
  groupHitsByFile,
  runFileSearch
} from './search'

/** Typing settles before a walk starts: a search reads the filesystem, so each
 *  keystroke must not spawn its own walk. */
const SEARCH_DEBOUNCE_MS = 220

/** The matched span inside a snippet, so a result row can be scanned by eye. */
function Highlighted({ caseSensitive, query, regex, text }: { caseSensitive: boolean; query: string; regex: boolean; text: string }) {
  const parts = useMemo(() => {
    if (!query.trim()) {
      return [{ hit: false, value: text }]
    }

    let matcher: RegExp

    try {
      matcher = compileQuery(query, { caseSensitive, regex })
    } catch {
      return [{ hit: false, value: text }]
    }

    const out: Array<{ hit: boolean; value: string }> = []
    let last = 0

    for (const match of text.matchAll(matcher)) {
      if (!match[0]) {
        break
      }

      if (match.index > last) {
        out.push({ hit: false, value: text.slice(last, match.index) })
      }

      out.push({ hit: true, value: match[0] })
      last = match.index + match[0].length
    }

    if (last < text.length) {
      out.push({ hit: false, value: text.slice(last) })
    }

    return out.length > 0 ? out : [{ hit: false, value: text }]
  }, [caseSensitive, query, regex, text])

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

interface FileSearchPanelProps {
  onClose: () => void
  onOpenFile: (path: string) => void
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
  const [outcome, setOutcome] = useState<FileSearchOutcome | null>(null)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState(0)
  const [searching, setSearching] = useState(false)

  const inputRef = useRef<HTMLInputElement | null>(null)

  const groups = useMemo(() => (outcome ? groupHitsByFile(outcome.hits) : []), [outcome])
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
      setError('')
      setSearching(false)

      return
    }

    const controller = new AbortController()
    let cancelled = false

    setSearching(true)

    const timer = setTimeout(() => {
      void runFileSearch(
        { caseSensitive, mode, query, regex, root, scope },
        {
          readDir: async dir => {
            const result = await readProjectDir(dir, root)

            return {
              entries: (result.entries ?? []).map(entry => ({
                isDirectory: entry.isDirectory,
                name: entry.name,
                path: entry.path
              })),
              ...(result.error ? { error: result.error } : {})
            }
          },
          readText: async path => {
            const result = await readDesktopFileText(path)

            return { binary: result.binary, text: result.text, truncated: result.truncated }
          }
        },
        controller.signal
      )
        .then(result => {
          if (cancelled) {
            return
          }

          setOutcome(result)
          setError('')
          setSelected(0)
          setSearching(false)
        })
        .catch((cause: unknown) => {
          if (cancelled) {
            return
          }

          // An invalid regex is the person's typo, not a failure: show the pattern
          // error in place of results and keep the typed query.
          setError(cause instanceof Error ? cause.message : String(cause))
          setOutcome(null)
          setSelected(0)
          setSearching(false)
        })
    }, SEARCH_DEBOUNCE_MS)

    return () => {
      cancelled = true
      controller.abort()
      clearTimeout(timer)
    }
  }, [caseSensitive, mode, query, regex, root, scope])

  const openHit = useCallback(
    (hit: FileSearchHit | undefined) => {
      if (hit) {
        onOpenFile(hit.path)
      }
    },
    [onOpenFile]
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

  const showEmpty = Boolean(query.trim()) && !error && outcome !== null && outcome.hits.length === 0 && !searching

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
          <Codicon className="ms-auto shrink-0 text-(--ui-text-quaternary)" name="loading" size="0.75rem" spinning />
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

        {showEmpty ? <div className="px-2.5 py-2 text-xs text-(--ui-text-quaternary)">{r.filesSearchNoResults}</div> : null}

        {!error && outcome && outcome.hits.length > 0 ? (
          <div className="px-2.5 pb-1 text-[0.6875rem] text-(--ui-text-quaternary)">
            {r.filesSearchStatus(outcome.hits.length, groups.length)}
            {outcome.truncated ? ` · ${r.filesSearchTruncated}` : ''}
          </div>
        ) : null}

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
                  className={cn(
                    'flex h-6 w-full items-center gap-2 px-2.5 ps-7 text-left text-xs text-(--ui-text-tertiary)',
                    'hover:bg-(--ui-row-hover-background) hover:text-(--ui-text-primary)',
                    index === selected && 'bg-(--ui-row-active-background) text-(--ui-text-primary)'
                  )}
                  key={key}
                  onClick={() => openHit(hit)}
                  title={hit.line ? `${hit.rel}:${hit.line}` : hit.rel}
                  type="button"
                >
                  {hit.line ? <span className="shrink-0 text-(--ui-text-quaternary)">{hit.line}</span> : null}
                  <span className="min-w-0 flex-1 truncate">
                    {hit.text ? (
                      <Highlighted caseSensitive={caseSensitive} query={query} regex={regex} text={hit.text} />
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
