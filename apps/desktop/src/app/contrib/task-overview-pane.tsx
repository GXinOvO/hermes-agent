import { randomUuid } from '@hermes/shared'
import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { SearchField } from '@/components/ui/search-field'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useI18n } from '@/i18n'
import { deriveTaskActivity, isTaskActivityReferenceVerified, type TaskActivitySnapshot, type TaskActivityStatus } from '@/lib/task-overview/activity'
import type { TaskReference, TaskStatus } from '@/lib/task-overview/model'
import { openTaskStore, taskChanged, type TaskRow, type TaskStore } from '@/lib/task-overview/store'
import { $projectTree } from '@/store/project-tree'
import { $cronSessions, $messagingSessions, $sessions } from '@/store/session'
import { $sessionStates } from '@/store/session-states'
import { $sessionSeenCounts, $unreadFinishedMarkers, isSessionAvailableForProfile, isSessionUnreadForProfile } from '@/store/session-unread'
import { $taskOverviewEnabled } from '@/store/task-overview'

import { TASK_PRIORITIES, TASK_STATUSES, taskDraft, TaskOverviewForm } from './task-overview-form'

interface TaskOverviewPaneProps {
  attentionOnly?: boolean
  captureSession?: () => Promise<TaskReference>
  openSession?: (reference: TaskReference) => Promise<void>
  visible?: boolean
}

function witnessed(reference: TaskReference): boolean {
  return isTaskActivityReferenceVerified(reference)
}

export function TaskOverviewPane({ attentionOnly = false, captureSession, openSession, visible = true }: TaskOverviewPaneProps = {}) {
  const enabled = useStore($taskOverviewEnabled)
  const { t } = useI18n()
  const copy = t.taskOverview
  const [rows, setRows] = useState<TaskRow[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [filter, setFilter] = useState<'open' | 'accepted'>('open')
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState(true)
  const [attempt, setAttempt] = useState(0)
  const [creating, setCreating] = useState(false)
  const [draft, setDraft] = useState(taskDraft)
  const [editing, setEditing] = useState<TaskRow | null>(null)
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<TaskStatus | 'all'>('all')
  const [activityFilter, setActivityFilter] = useState<TaskActivityStatus | 'all'>('all')
  const [sort, setSort] = useState<'updated' | 'priority' | 'title'>('updated')
  const [acceptingId, setAcceptingId] = useState<string | null>(null)
  const [acceptanceNote, setAcceptanceNote] = useState('')
  const [saving, setSaving] = useState(false)
  const writing = useRef(false)
  const bindingRequest = useRef(0)
  const [opening, setOpening] = useState(false)
  const [sourceError, setSourceError] = useState<{ reference: TaskReference; message: string } | null>(null)
  const [bindingError, setBindingError] = useState<string | null>(null)
  const [binding, setBinding] = useState<{ row: TaskRow; reference: TaskReference; related: boolean } | null>(null)
  const sessionStates = useStore($sessionStates)
  const unreadMarkers = useStore($unreadFinishedMarkers)
  const seenCounts = useStore($sessionSeenCounts)
  useStore($sessions)
  useStore($cronSessions)
  useStore($messagingSessions)
  useStore($projectTree)

  useEffect(() => {
    if (!enabled || !visible) { return }
    let active = true
    let store: TaskStore | null = null
    void openTaskStore().then(async opened => {
      if (!active) { opened.close();

 return }

      store = opened
      const next = await opened.list()

      if (active) { setRows(next); setLoading(false) }
    }).catch(cause => {
      if (active) {
        setError(cause instanceof Error ? cause : new Error(String(cause)))
        setLoading(false)
      }
    })

    return () => { active = false; store?.close() }
  }, [enabled, visible, attempt])

  if (!enabled) { return null }

  const openRows = rows.filter(row => row.task.resolution.state === 'open')
  const acceptedRows = rows.filter(row => row.task.resolution.state === 'accepted')

  const filteredRows = (attentionOnly || filter === 'open' ? openRows : acceptedRows).filter(row =>
    (statusFilter === 'all' || row.task.status === statusFilter) &&
    [row.task.title, row.task.description, row.task.notes, ...row.task.nextSteps].join('\n').toLowerCase().includes(query.trim().toLowerCase())
  ).sort((a, b) => sort === 'priority'
    ? TASK_PRIORITIES.indexOf(a.task.priority) - TASK_PRIORITIES.indexOf(b.task.priority) || b.task.updatedAt - a.task.updatedAt
    : sort === 'title' ? a.task.title.localeCompare(b.task.title) : b.task.updatedAt - a.task.updatedAt)

  const activityFor = (reference: TaskReference): TaskActivitySnapshot => {
    const candidates = Object.entries(sessionStates).filter(([, state]) => state.storedSessionId === reference.storedId)

    const profile = reference.targetProfile || reference.profile
    const unread = isSessionUnreadForProfile(reference.storedId, profile, { markers: unreadMarkers, seen: seenCounts }, reference.connectionId)
    const sourceAvailable = isSessionAvailableForProfile(reference.storedId, profile, reference.connectionId)

    let unavailable: TaskActivitySnapshot | undefined

    for (const [runtimeId, state] of candidates) {
      const activity = deriveTaskActivity({
        reference,
        runtimeId,
        runtimeOwner: state.sourceOwner,
        state,
        sourceAvailable,
        unread
      })

      if (activity.status !== 'unavailable') { return activity }
      unavailable ??= activity
    }

    if (unavailable) { return unavailable }

    return deriveTaskActivity({
      reference,
      runtimeId: undefined,
      state: undefined,
      sourceAvailable,
      unread
    })
  }

  const visibleRows = attentionOnly
    ? filteredRows.filter(row => [row.task.primary, ...row.task.related].some(reference => {
        if (!reference) { return false }
        const { status } = activityFor(reference)

        return status !== 'idle' && (activityFilter === 'all' || status === activityFilter)
      }))
    : filteredRows

  const activityLabels = { idle: copy.activityIdle, running: copy.activityRunning, waiting_input: copy.activityWaiting, unread: copy.activityUnread, unavailable: copy.activityUnavailable }

  const canOpenReference = (reference: TaskReference) => witnessed(reference) && activityFor(reference).reason !== 'owner_mismatch'

  const selected = visibleRows.find(row => row.task.id === selectedId) ?? visibleRows[0] ?? null

  const write = async (operation: (store: TaskStore) => Promise<TaskRow>): Promise<TaskRow | undefined> => {
    if (writing.current) { return }
    writing.current = true
    setSaving(true)
    let store: TaskStore | undefined

    try {
      store = await openTaskStore()
      const next = await operation(store)
      setRows(current => current.some(row => row.task.id === next.task.id)
        ? current.map(row => row.task.id === next.task.id ? next : row)
        : [...current, next])

      return next
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    } finally {
      store?.close()
      writing.current = false
      setSaving(false)
    }
  }

  const create = async () => {
    if (!draft.title.trim()) { return }

    const fields = {
      title: draft.title.trim(), description: draft.description, notes: draft.notes, status: draft.status, priority: draft.priority,
      nextSteps: draft.steps.split('\n').map(step => step.trim()).filter(Boolean)
    }

    const next = await write(store => editing
      ? store.replace({ ...editing.task, ...fields, updatedAt: Math.max(Date.now(), editing.task.updatedAt) }, editing.revision)
      : store.create({ ...fields, id: `task-${randomUuid()}`, primary: null, related: [] }, Date.now()))

    if (next) {
      setSelectedId(next.task.id); setDraft(taskDraft()); setCreating(false); setEditing(null); setQuery(''); setStatusFilter('all'); setFilter(next.task.resolution.state)
    }
  }

  const accept = async (row: TaskRow) => {
    if (!acceptanceNote.trim()) { return }

    const next = await write(store => store.replace(taskChanged(row.task, {
      at: Math.max(Date.now(), row.task.updatedAt), note: acceptanceNote.trim(), type: 'accept'
    }), row.revision))

    if (next) { setAcceptingId(null); setAcceptanceNote('') }
  }

  const reopen = async (row: TaskRow) => {
    const next = await write(store => store.replace(taskChanged(row.task, {
      at: Math.max(Date.now(), row.task.updatedAt), type: 'reopen'
    }), row.revision))

    if (next) { setFilter('open'); setSelectedId(next.task.id) }
  }

  const openReference = async (reference: TaskReference) => {
    if (!openSession || !canOpenReference(reference) || opening) { return }
    setOpening(true); setSourceError(null)

    try { await openSession(reference) }
    catch (cause) { setSourceError({ reference, message: cause instanceof Error ? cause.message : String(cause) }) }
    finally { setOpening(false) }
  }

  const prepareBinding = async (row: TaskRow, related = false) => {
    if (!captureSession || saving) { return }
    setSaving(true); setBindingError(null)
    const request = ++bindingRequest.current

    try {
      const reference = await captureSession()

      if (request === bindingRequest.current) { setBinding({ row, reference, related }) }
    } catch (cause) {
      if (request === bindingRequest.current) { setBindingError(cause instanceof Error ? cause.message : String(cause)) }
    } finally { setSaving(false) }
  }

  const confirmBinding = async () => {
    if (!binding || !captureSession) { return }
    const { row, reference, related } = binding

    const next = await write(async store => {
      const current = await captureSession()

      if (JSON.stringify(current) !== JSON.stringify(reference)) { throw new Error(copy.bindingFailed) }

      const at = Math.max(Date.now(), row.task.updatedAt)

      return related
        ? store.replace({ ...row.task, related: [...row.task.related, reference], updatedAt: at }, row.revision)
        : store.changePrimary(row.task.id, row.revision, reference, at)
    })

    if (next) { setBinding(null); setSourceError(null) }
  }

  const renderReference = (reference: TaskReference, key: string) => {
    const activity = activityFor(reference)
    const activityLabel = activityLabels[activity.status]

    return <div className="grid gap-1 text-xs" key={key}>
      <span className="break-all">{reference.storedId}</span>
      {reference.connectionId ? <span className="break-all text-muted-foreground">{reference.connectionId} / {reference.targetProfile || reference.profile}</span> : null}
      <span className="text-muted-foreground">{copy.activity}: {activityLabel}</span>
      <Button disabled={opening || !openSession || !canOpenReference(reference)} onClick={() => void openReference(reference)} size="sm" variant="secondary">
        {opening ? copy.opening : copy.openSource}
      </Button>
      {!witnessed(reference) ? <p className="text-muted-foreground">{copy.sourceUnverified}</p> : activity.reason === 'owner_mismatch' ? <p className="text-muted-foreground">{copy.sourceUnavailable}</p> : !openSession ? <p className="text-muted-foreground">{copy.navigationUnavailable}</p> : null}
    </div>
  }

  const renderHeader = () => (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="font-medium">{attentionOnly ? copy.attention : copy.title}</h2>
      <div className="flex items-center gap-2">
        <Badge variant="muted">{loading || error ? '—' : attentionOnly ? visibleRows.length : openRows.length} {attentionOnly ? copy.attention : copy.open}</Badge>
        {!attentionOnly ? <Button disabled={loading || Boolean(error) || saving} onClick={() => { setDraft(taskDraft()); setEditing(null); setCreating(value => !value) }} size="sm" variant="secondary">{copy.newTask}</Button> : null}
      </div>
    </div>
  )

  const renderFilters = () => (
    <>
      <SearchField onChange={setQuery} placeholder={copy.search} value={query} />
      <div className="flex flex-wrap gap-2">
        {attentionOnly ? <Select onValueChange={value => setActivityFilter(value as typeof activityFilter)} value={activityFilter}>
          <SelectTrigger aria-label={copy.activity}><SelectValue /></SelectTrigger>
          <SelectContent><SelectItem value="all">{copy.allStatuses}</SelectItem>{(['running', 'waiting_input', 'unread', 'unavailable'] as const).map(status => <SelectItem key={status} value={status}>{activityLabels[status]}</SelectItem>)}</SelectContent>
        </Select> : null}
        <Select onValueChange={value => setStatusFilter(value as typeof statusFilter)} value={statusFilter}>
          <SelectTrigger aria-label={copy.statusFilter}><SelectValue /></SelectTrigger>
          <SelectContent><SelectItem value="all">{copy.allStatuses}</SelectItem>{TASK_STATUSES.map(status => <SelectItem key={status} value={status}>{copy[status]}</SelectItem>)}</SelectContent>
        </Select>
        <Select onValueChange={value => setSort(value as typeof sort)} value={sort}>
          <SelectTrigger aria-label={copy.sort}><SelectValue /></SelectTrigger>
          <SelectContent><SelectItem value="updated">{copy.sortUpdated}</SelectItem><SelectItem value="priority">{copy.priority}</SelectItem><SelectItem value="title">{copy.taskName}</SelectItem></SelectContent>
        </Select>
      </div>
      {!attentionOnly ? <div aria-label={copy.filter} className="flex gap-2">
        <Button aria-pressed={filter === 'open'} onClick={() => setFilter('open')} size="sm" variant={filter === 'open' ? 'secondary' : 'ghost'}>{copy.open} · {loading || error ? '—' : openRows.length}</Button>
        <Button aria-pressed={filter === 'accepted'} onClick={() => setFilter('accepted')} size="sm" variant={filter === 'accepted' ? 'secondary' : 'ghost'}>{copy.completed} · {loading || error ? '—' : acceptedRows.length}</Button>
      </div> : null}
    </>
  )

  const renderAcceptance = (selected: TaskRow) => selected.task.resolution.state === 'accepted' ? (
    <Button disabled={saving || Boolean(error)} onClick={() => void reopen(selected)} size="sm" variant="secondary">{copy.reTrack}</Button>
  ) : acceptingId === selected.task.id ? (
    <form className="grid gap-2" onSubmit={event => { event.preventDefault(); void accept(selected) }}>
      <label>{copy.acceptanceNote}<Textarea aria-label={copy.acceptanceNote} disabled={saving} onChange={event => setAcceptanceNote(event.currentTarget.value)} required value={acceptanceNote} /></label>
      <div className="flex gap-2"><Button disabled={saving || !acceptanceNote.trim()} size="sm" type="submit">{copy.confirmAccepted}</Button><Button disabled={saving} onClick={() => { setAcceptingId(null); setAcceptanceNote('') }} size="sm" type="button" variant="ghost">{copy.cancel}</Button></div>
    </form>
  ) : (
    <Button disabled={saving || Boolean(error)} onClick={() => { setAcceptanceNote(''); setAcceptingId(selected.task.id) }} size="sm" variant="secondary">{copy.markAccepted}</Button>
  )

  const renderDetails = (selected: TaskRow) => (
    <div className="grid gap-3 border-t border-border/60 pt-3">
      <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-medium">{selected.task.title}</h3><Button disabled={saving || Boolean(error)} onClick={() => { setDraft(taskDraft(selected.task)); setEditing(selected); setCreating(false) }} size="sm" variant="ghost">{copy.editTask}</Button></div>
      {selected.task.description ? <p className="whitespace-pre-wrap break-words text-xs">{selected.task.description}</p> : null}
      {selected.task.notes ? <p className="whitespace-pre-wrap break-words text-xs text-muted-foreground">{selected.task.notes}</p> : null}
      <p className="text-xs text-muted-foreground">{copy.status}: {copy[selected.task.status]} · {copy.priority}: {copy[selected.task.priority]}</p>
      {selected.task.resolution.state === 'accepted' && selected.task.resolution.note ? <p className="text-xs">{selected.task.resolution.note}</p> : null}
      <ul className="list-disc pl-5 text-xs">{selected.task.nextSteps.map((step, index) => <li key={index}>{step}</li>)}</ul>
      <h4 className="text-xs font-medium">{copy.source}</h4>
      {selected.task.primary ? renderReference(selected.task.primary, 'primary') : <p className="text-xs text-muted-foreground">{copy.noSource}</p>}
      {selected.task.related.length ? <h4 className="text-xs font-medium">{copy.related}</h4> : null}
      {selected.task.related.map((reference, index) => renderReference(reference, `related-${index}`))}
      {captureSession ? <Button disabled={saving || Boolean(error)} onClick={() => void prepareBinding(selected)} size="sm" variant="secondary">{selected.task.primary ? copy.rebind : copy.bind}</Button> : null}
      {captureSession ? <Button disabled={saving || Boolean(error)} onClick={() => void prepareBinding(selected, true)} size="sm" variant="ghost">{copy.addRelated}</Button> : null}
      {sourceError ? <div className="grid gap-2 text-xs" role="alert"><strong>{copy.sourceUnavailable}</strong><span>{sourceError.message}</span><Button disabled={opening} onClick={() => void openReference(sourceError.reference)} size="sm" variant="secondary">{copy.retryOpen}</Button></div> : null}
      {bindingError ? <p className="text-xs" role="alert">{copy.bindingFailed}: {bindingError}</p> : null}
      {binding && binding.row.task.id === selected.task.id ? <div aria-label={copy.confirmBinding} className="grid gap-2 text-xs" role="alertdialog"><p>{copy.bindingHint}</p><p className="break-all">{binding.reference.connectionId} / {binding.reference.targetProfile} / {binding.reference.storedId}</p><div className="flex gap-2"><Button disabled={saving} onClick={() => void confirmBinding()} size="sm">{copy.confirmBinding}</Button><Button disabled={saving} onClick={() => setBinding(null)} size="sm" variant="ghost">{copy.cancel}</Button></div></div> : null}
      {renderAcceptance(selected)}
    </div>
  )

  return (
    <section aria-label={attentionOnly ? copy.attention : copy.title} className="flex h-full min-h-0 flex-col gap-3 overflow-auto p-4 text-sm">
      {renderHeader()}
      <p className="text-xs text-muted-foreground">{copy.scope}</p>
      {creating || editing ? <TaskOverviewForm draft={draft} onCancel={() => { setCreating(false); setEditing(null) }} onChange={setDraft} onSave={() => void create()} saving={saving} /> : null}
      {renderFilters()}
      {error ? <div className="grid gap-2 text-xs" role="alert"><strong>{copy.storageError}</strong><span>{error.message}</span><Button onClick={() => { setError(null); setLoading(true); setAttempt(value => value + 1) }} size="sm" variant="secondary">{copy.retry}</Button></div> : null}
      {loading ? <p className="text-muted-foreground">{copy.loading}</p> : null}
      {!loading && !error && visibleRows.length === 0 ? <p className="text-muted-foreground">{attentionOnly ? copy.attentionEmpty : filter === 'open' ? copy.empty : copy.noAccepted}</p> : null}
      <div className="grid gap-2">
        {visibleRows.map(row => (
          <div className="grid gap-1" key={row.task.id}>
            <Button aria-pressed={selected?.task.id === row.task.id} className="justify-between whitespace-normal text-left" onClick={() => { bindingRequest.current++; setSelectedId(row.task.id); setSourceError(null); setBindingError(null); setBinding(null) }} size="sm" variant={selected?.task.id === row.task.id ? 'secondary' : 'ghost'}>
              <span className="min-w-0 flex-1 break-words">{row.task.title}</span><Badge variant="muted">{copy[row.task.priority]}</Badge>
            </Button>
            <span className="text-xs text-muted-foreground">{row.task.nextSteps[0] || copy.noSteps}</span>
            {attentionOnly ? <span className="text-xs text-(--ui-text-secondary)">{[...new Set([row.task.primary, ...row.task.related].filter((reference): reference is TaskReference => Boolean(reference)).map(reference => activityFor(reference).status).filter(status => status !== 'idle'))].map(status => activityLabels[status]).join(' · ')}</span> : null}
          </div>
        ))}
      </div>
      {selected ? renderDetails(selected) : null}
    </section>
  )
}
