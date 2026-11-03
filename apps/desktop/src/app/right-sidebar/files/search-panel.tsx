import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { readDesktopFileText } from '@/lib/desktop-fs'
import { cn } from '@/lib/utils'

import { readProjectDir } from './ipc'
import { type FileSearchHit, type FileSearchMode, type FileSearchOutcome, groupHitsByFile, runFileSearch } from './search'

/** Typing settles before a walk starts: a search reads the filesystem, so each
 *  keystroke must not spawn its own walk. */
const SEARCH_DEBOUNCE_MS = 220

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
  const positionOf = useMemo(() => new Map(flat.map((hit, index) => [`${hit.path}:${hit.line ?? 0}:${hit.col ?? 0}`, index])), [flat])
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
      setSearching(false)
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

  const controlClass =
    'h-6 gap-1 rounded-md px-1.5 text-xs text-(--ui-text-tertiary) hover:bg-(--ui-control-hover-background)'

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-file-search="" onKeyDown={handleKeyDown}>
      <div className="flex shrink-0 items-center gap-1 px-2.5 py-1.5">
        <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="search" size="0.8125rem" />
        <input
          aria-label={placeholder}
          className="h-6 min-w-0 flex-1 rounded-md bg-(--ui-bg-input) px-2 text-xs text-(--ui-text-primary) outline-none placeholder:text-(--ui-text-quaternary) focus-visible:ring-1 focus-visible:ring-(--ui-accent)"
          onChange={event => setQuery(event.target.value)}
          placeholder={placeholder}
          ref={inputRef}
          spellCheck={false}
          value={query}
        />
        {searching ? <Codicon name="loading" size="0.75rem" spinning /> : null}
        <Tip label={r.filesSearchClose}>
          <Button aria-label={r.filesSearchClose} className={controlClass} onClick={onClose} size="icon-xs" variant="ghost">
            <Codicon name="close" size="0.8125rem" />
          </Button>
        </Tip>
      </div>

      <div className="flex shrink-0 items-center gap-1 px-2.5 pb-1.5">
        <div className="flex items-center rounded-md bg-(--ui-bg-tertiary) p-0.5">
          {(['name', 'content'] as const).map(value => (
            <button
              aria-pressed={mode === value}
              className={cn(
                'h-5 rounded px-1.5 text-xs text-(--ui-text-tertiary)',
                mode === value && 'bg-(--ui-row-active-background) text-(--ui-text-primary)'
              )}
              key={value}
              onClick={() => setMode(value)}
              type="button"
            >
              {value === 'name' ? r.filesSearchNameMode : r.filesSearchContentMode}
            </button>
          ))}
        </div>
        <Tip label={r.filesSearchCase}>
          <Button
            aria-label={r.filesSearchCase}
            aria-pressed={caseSensitive}
            className={cn(controlClass, caseSensitive && 'bg-(--ui-row-active-background) text-(--ui-text-primary)')}
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
              className={cn(controlClass, regex && 'bg-(--ui-row-active-background) text-(--ui-text-primary)')}
              onClick={() => setRegex(value => !value)}
              size="icon-xs"
              variant="ghost"
            >
              <Codicon name="regex" size="0.8125rem" />
            </Button>
          </Tip>
        ) : null}
      </div>

      <div className="shrink-0 px-2.5 pb-1.5">
        <input
          aria-label={r.filesSearchScope}
          className="h-6 w-full rounded-md bg-(--ui-bg-tertiary) px-2 text-xs text-(--ui-text-secondary) outline-none placeholder:text-(--ui-text-quaternary) focus-visible:ring-1 focus-visible:ring-(--ui-accent)"
          onChange={event => setScope(event.target.value)}
          placeholder={r.filesSearchScopePlaceholder}
          spellCheck={false}
          value={scope}
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {error ? <div className="px-2.5 py-2 text-xs text-(--ui-red)">{r.filesSearchInvalidPattern(error)}</div> : null}

        {!error && !query.trim() ? (
          <div className="px-2.5 py-2 text-xs text-(--ui-text-quaternary)">{r.filesSearchHint}</div>
        ) : null}

        {!error && outcome && outcome.hits.length === 0 && !searching ? (
          <div className="px-2.5 py-2 text-xs text-(--ui-text-quaternary)">{r.filesSearchNoResults}</div>
        ) : null}

        {!error && outcome && outcome.hits.length > 0 ? (
          <div className="px-2.5 pb-1 text-xs text-(--ui-text-quaternary)">
            {r.filesSearchStatus(outcome.hits.length, groups.length)}
            {outcome.truncated ? ` · ${r.filesSearchTruncated}` : ''}
          </div>
        ) : null}

        {groups.map(group => (
          <div key={group.path}>
            <div className="flex h-6 items-center gap-1 px-2.5 text-xs text-(--ui-text-secondary)" title={group.rel}>
              <Codicon name="file" size="0.75rem" />
              <span className="truncate font-medium">{group.rel.split('/').pop()}</span>
              <span className="min-w-0 flex-1 truncate text-(--ui-text-quaternary)">
                {group.rel.includes('/') ? group.rel.slice(0, group.rel.lastIndexOf('/')) : ''}
              </span>
              <span className="text-(--ui-text-quaternary)">{group.hits.length}</span>
            </div>
            {group.hits.map(hit => {
              const key = `${hit.path}:${hit.line ?? 0}:${hit.col ?? 0}`
              const index = positionOf.get(key) ?? -1

              return (
                <button
                  className={cn(
                    'flex h-6 w-full items-center gap-2 px-2.5 pl-7 text-left text-xs text-(--ui-text-tertiary)',
                    index === selected && 'bg-(--ui-row-active-background) text-(--ui-text-primary)'
                  )}
                  key={key}
                  onClick={() => openHit(hit)}
                  title={hit.line ? `${hit.rel}:${hit.line}` : hit.rel}
                  type="button"
                >
                  {hit.line ? <span className="shrink-0 text-(--ui-text-quaternary)">{hit.line}</span> : null}
                  <span className="min-w-0 flex-1 truncate">{hit.text ?? hit.rel.split('/').pop()}</span>
                </button>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}
