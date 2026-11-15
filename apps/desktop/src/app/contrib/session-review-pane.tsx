import { randomUuid } from '@hermes/shared'
import { useStore } from '@nanostores/react'
import { type ReactNode, useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { LogView } from '@/components/ui/log-view'
import { useI18n } from '@/i18n'
import type { SessionReviewCopy } from '@/i18n/session-review'
import { $focusedOverviewKey, appendFocusedOverviewRequest, loadFocusedSessionOverview } from '@/lib/task-overview/focused-review'
import type { TaskReference } from '@/lib/task-overview/model'
import type { DiscoveredTaskCandidate, OverviewMessage, SessionOverview } from '@/lib/task-overview/session-review'
import { openTaskStore } from '@/lib/task-overview/store'

function SourceText({ label, message, copy }: { label: string; message: OverviewMessage | null; copy: SessionReviewCopy }) {
  if (!message) { return null }

  return <details open><summary className="cursor-pointer text-xs font-medium">{label}</summary><p className="mt-2 whitespace-pre-wrap break-words text-xs leading-relaxed">{message.content}</p>{message.row_id ? <p className="mt-1 text-xs text-(--ui-text-secondary)">{copy.row} #{message.row_id}</p> : null}</details>
}

function ReviewSection({ title, children }: { title: string; children: ReactNode }) {
  return <section className="grid min-w-0 gap-2"><h3 className="text-xs font-medium">{title}</h3>{children}</section>
}

function TextItems({ items, empty }: { items: string[]; empty: string }) {
  return items.length ? <ul className="list-disc space-y-1 pl-4 text-xs">{items.map((item, index) => <li className="whitespace-pre-wrap break-words" key={index}>{item}</li>)}</ul>
    : <p className="text-xs text-(--ui-text-secondary)">{empty}</p>
}

function candidateKey(candidate: DiscoveredTaskCandidate): string {
  return `${candidate.evidence}:${candidate.content.toLocaleLowerCase()}`
}

function sourceReference(value: SessionOverview): TaskReference | null {
  const route = value.sourceRoute

  return route ? {
    connectionId: route.connectionId,
    expectedOwner: route.expectedOwner,
    mode: route.mode,
    profile: route.profile,
    storedId: route.storedId,
    targetProfile: route.targetProfile
  } : null
}

async function createDiscoveredTask(value: SessionOverview, candidate: DiscoveredTaskCandidate): Promise<boolean> {
  const reference = sourceReference(value)

  if (!reference) { throw new Error('The source reference is unavailable') }

  const store = await openTaskStore()

  try {
    const result = await store.createIfAbsent({
      description: `Discovered from ${candidate.evidence}.`,
      id: `task-${randomUuid()}`,
      nextSteps: [candidate.content],
      notes: candidate.sourceRow ? `Snapshot message #${candidate.sourceRow}.` : 'Persisted session snapshot.',
      priority: 'normal',
      primary: reference,
      related: [],
      status: 'todo',
      title: candidate.content.slice(0, 120)
    }, Date.now(), reference, candidate.content)

    return result.created
  } finally { store.close() }
}

interface ReviewDetailProps {
  value: SessionOverview
  copy: SessionReviewCopy
}

function ReviewChecklist({ value, copy }: ReviewDetailProps) {
  return <details><summary className="cursor-pointer text-xs font-medium">{copy.checklist}</summary><div className="mt-2 grid gap-2">{value.todos?.length ? <ul className="grid gap-2 text-xs">{value.todos.map(item => <li className="flex flex-wrap justify-between gap-2" key={item.id}><span className="min-w-0 flex-1 break-words">{item.content}</span><span className="text-(--ui-text-secondary)">{copy[item.status]}</span></li>)}</ul> : <p className="text-xs text-(--ui-text-secondary)">{value.todos === null ? copy.noTodos : copy.noRemaining}</p>}<p className="text-xs text-(--ui-text-secondary)">{copy.revision} · {value.todoRevision ?? '—'}</p></div></details>
}

function ReviewVerification({ value, copy }: ReviewDetailProps) {
  const ledger = value.verification
  const evidence = ledger?.evidence && typeof ledger.evidence === 'object' ? ledger.evidence as Record<string, unknown> : null

  return <ReviewSection title={copy.verification}>{value.verificationCwd ? <p className="break-all text-xs text-(--ui-text-secondary)">{value.verificationCwd}{typeof ledger?.root === 'string' ? ` → ${ledger.root}` : ''}</p> : null}{value.verificationError ? <p className="break-words text-xs text-(--ui-text-secondary)">{copy.verificationUnavailable} · {value.verificationError}</p> : evidence ? <><p className="break-words text-xs">{String(ledger?.status ?? '')} · {String(evidence.kind ?? '')} · {String(evidence.scope ?? '')}</p><LogView>{String(evidence.command ?? '')}{'\n'}{String(evidence.output_summary ?? '')}</LogView><p className="text-xs text-(--ui-text-secondary)">{copy.verificationScope}</p></> : <p className="text-xs text-(--ui-text-secondary)">{copy.noVerification}</p>}</ReviewSection>
}

export function SessionReviewView({ value, onTracked }: { onTracked?: (key: string) => void; value: SessionOverview }) {
  const { t } = useI18n()
  const copy = t.sessionReview
  const [tracking, setTracking] = useState<string | null>(null)
  const [tracked, setTracked] = useState<Set<string>>(new Set())
  const [trackingError, setTrackingError] = useState<string | null>(null)

  const track = async (candidate: DiscoveredTaskCandidate) => {
    const key = candidateKey(candidate)
    setTracking(key); setTrackingError(null)

    try {
      await createDiscoveredTask(value, candidate)
      setTracked(current => new Set(current).add(key))
      onTracked?.(key)
    } catch (error) {
      setTrackingError(error instanceof Error ? error.message : String(error))
    } finally { setTracking(null) }
  }

  return <div className="grid min-w-0 gap-5">
    <div className="grid gap-2"><h2 className="break-words text-base font-medium">{value.title || copy.noRecord}</h2><span className="text-xs font-medium">{copy[value.acceptance as 'registered' | 'unregistered' | 'stale']}</span><p className="text-xs text-(--ui-text-secondary)">{value.registrationStale ? copy.staleNote : copy.evidenceNote}</p></div>
    {value.goal ? <ReviewSection title={copy.goal}><p className="whitespace-pre-wrap break-words text-xs">{value.goal}</p></ReviewSection> : null}
    <SourceText copy={copy} label={copy.original} message={value.original} />
    {value.current?.row_id !== value.original?.row_id ? <SourceText copy={copy} label={copy.current} message={value.current} /> : null}
    <ReviewSection title={copy.remaining}><TextItems empty={value.todos === null ? copy.noTodos : copy.noRemaining} items={value.remaining.map(item => item.content)} /></ReviewSection>
    {value.registeredRemaining.length ? <ReviewSection title={copy.registeredRemaining}><TextItems empty={copy.noItems} items={value.registeredRemaining} /></ReviewSection> : null}
    <ReviewSection title={copy.decisions}><TextItems empty={copy.noItems} items={value.decisions} /></ReviewSection>
    {value.registeredResult ? <ReviewSection title={copy.registeredResult}><p className="whitespace-pre-wrap break-words text-xs leading-relaxed">{value.registeredResult}</p></ReviewSection> : null}
    <ReviewSection title={copy.latest}><p className="whitespace-pre-wrap break-words text-xs leading-relaxed">{value.latest?.content || copy.noReply}</p></ReviewSection>
    <ReviewSection title={copy.deferred}><TextItems empty={copy.noItems} items={value.deferred} /></ReviewSection>
    {value.discoveredTasks.length ? <ReviewSection title={copy.discovered}><p className="text-xs text-(--ui-text-secondary)">{copy.discoveryHint}</p><ul className="grid gap-2">{value.discoveredTasks.map(candidate => { const key = candidateKey(candidate); const isTracked = tracked.has(key);

 return <li className="flex flex-wrap items-center gap-2 text-xs" key={key}><span className="min-w-0 flex-1 break-words">{candidate.content}</span><span className="text-(--ui-text-secondary)">{copy.candidateEvidence}: {candidate.evidence}</span><Button disabled={isTracked || tracking === key || !value.sourceRoute} onClick={() => void track(candidate)} size="sm" variant="secondary">{isTracked ? copy.trackedCandidate : tracking === key ? copy.loading : copy.trackCandidate}</Button></li> })}</ul>{trackingError ? <p className="text-xs text-destructive" role="alert">{trackingError}</p> : null}</ReviewSection> : null}
    <ReviewSection title={copy.stages}><dl className="grid gap-3">{value.stages.map(stage => <div className="grid gap-1" key={stage.id}><div className="flex flex-wrap items-baseline justify-between gap-2"><dt className="text-xs">{copy[stage.id]}</dt><dd className="text-xs text-(--ui-text-secondary)">{copy[stage.status as 'unknown' | 'stale' | 'registered' | 'failed' | 'pending' | 'not_applicable']}</dd></div>{stage.evidence ? <dd className="grid gap-1 whitespace-pre-wrap break-words text-xs text-(--ui-text-secondary)"><p>{stage.evidence}</p>{stage.source?.row_id ? <p>{copy.row} #{stage.source.row_id}</p> : null}</dd> : null}</div>)}</dl></ReviewSection>
    <ReviewChecklist copy={copy} value={value} />
    <ReviewVerification copy={copy} value={value} />
    <footer className="grid gap-1 break-all text-xs text-(--ui-text-secondary)"><p>{copy.updated} · {new Date(value.fetchedAt).toLocaleString()}</p><p>{copy.source} · {value.sourceRoute ? `${value.sourceRoute.connectionId} / ${value.sourceRoute.targetProfile} / ` : ''}{value.storedId}{value.resolvedId !== value.storedId ? ` → ${value.resolvedId}` : ''}</p><p>{copy.details} · {value.messageCount} {copy.messages}</p></footer>
  </div>
}

export function SessionReviewPane({ visible }: { visible: boolean }) {
  const key = useStore($focusedOverviewKey)
  const { t } = useI18n()
  const copy = t.sessionReview
  const [snapshot, setSnapshot] = useState<{ key: string; value: SessionOverview } | null>(null)
  const [failure, setFailure] = useState<{ key: string; message: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [registering, setRegistering] = useState(false)
  const [registerFailure, setRegisterFailure] = useState<{ key: string; message: string } | null>(null)

  useEffect(() => {
    if (!visible || !key) { return }
    let active = true
    let pending = false

    const load = async () => {
      if (pending) { return }
      pending = true
      setLoading(true)

      try {
        const value = await loadFocusedSessionOverview()

        if (active) { setSnapshot({ key, value }); setFailure(null) }
      } catch (error) {
        if (active) { setFailure({ key, message: error instanceof Error ? error.message : String(error) }) }
      } finally {
        pending = false

        if (active) { setLoading(false) }
      }
    }

    void load()
    const timer = setInterval(() => void load(), 15000)

    return () => { active = false; clearInterval(timer) }
  }, [visible, key, attempt])

  const value = snapshot?.key === key ? snapshot.value : null
  const error = failure?.key === key ? failure.message : null

  const register = async () => {
    setRegistering(true)
    setRegisterFailure(null)

    try {
      if (!await appendFocusedOverviewRequest(key, copy.registerPrompt)) { setRegisterFailure({ key, message: copy.registerFailed }) }
    } catch {
      setRegisterFailure({ key, message: copy.registerFailed })
    } finally { setRegistering(false) }
  }

  return <section aria-label={copy.title} className="flex h-full min-h-0 flex-col gap-4 overflow-auto p-4 text-sm">
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-medium">{copy.title}</h2><Button disabled={!key || loading} onClick={() => setAttempt(value => value + 1)} size="sm" variant="ghost">{copy.refresh}</Button></div>
    <p className="text-xs text-(--ui-text-secondary)">{copy.scope}</p>
    {!key ? <EmptyState description={copy.noSession} title={copy.title} /> : error ? <div role="alert"><ErrorState description={error} title={copy.error}><Button onClick={() => { setFailure(null); setAttempt(value => value + 1) }} size="sm" variant="secondary">{copy.retry}</Button></ErrorState></div> : null}
    {key && !error && !value ? <Loader label={copy.loading} /> : null}
    {value && !error ? <SessionReviewView value={value} /> : null}
    {value && !error ? <div className="grid gap-2"><Button disabled={registering} onClick={() => void register()} size="sm" variant="secondary">{copy.register}</Button><p className="text-xs text-(--ui-text-secondary)">{copy.registerHint}</p>{registerFailure?.key === key ? <p className="text-xs text-destructive" role="alert">{registerFailure.message}</p> : null}</div> : null}
  </section>
}
