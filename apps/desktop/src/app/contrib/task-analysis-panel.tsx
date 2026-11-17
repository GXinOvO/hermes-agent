import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { ErrorIcon } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { useI18n } from '@/i18n'
import type { TaskOverviewCopy } from '@/i18n/task-overview'
import { openTaskSource, taskAnalysisSourceReference } from '@/lib/task-overview/analysis'
import type { AnalysisClaim, AnalysisEvidence, AnalysisSource, TaskAnalysis, TaskAnalysisV2 } from '@/lib/task-overview/analysis-model'
import type { Task } from '@/lib/task-overview/model'
import type { TaskRow } from '@/lib/task-overview/store'

import { useTaskAnalysis } from './use-task-analysis'

interface TaskAnalysisPanelProps {
  task: Task
  autoAnalyze?: boolean
  onUpdated: (row: TaskRow) => void
  onAutoAnalyzeStarted: () => void
}

interface SourceError {
  id: string
  message: string
}

interface SourceRowProps {
  copy: TaskOverviewCopy
  error: SourceError | null
  onOpen: (source: AnalysisSource) => void
  openingSource: string | null
  source: AnalysisSource
}

interface ClaimsProps {
  copy: TaskOverviewCopy
  onOpen: (source: AnalysisSource) => void
  openingSource: string | null
  sources?: AnalysisSource[]
  title: string
  values: AnalysisClaim[]
}

function isV2(result: TaskAnalysis): result is TaskAnalysisV2 {
  return result.version === 2
}

function SourceRow({ copy, error, onOpen, openingSource, source }: SourceRowProps) {
  const unavailable = source.status === 'unavailable'
  const sourceError = error?.id === source.id ? error : null

  return <div className="grid gap-1 text-xs">
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <span className="font-medium">{source.id}</span>
      <span>{copy.analysisSourceProfile.replace('{profile}', source.profile)}</span>
      <span>{copy.analysisSourceStored.replace('{id}', source.stored_session_id)}</span>
      <span className="text-muted-foreground">{copy.analysisSourceRelation.replace('{relation}', source.relation)}</span>
    </div>
    {unavailable ? <p className="text-muted-foreground">{copy.analysisSourceUnavailable.replace('{reason}', source.reason ?? '')}</p> : <>
      <Button aria-label={`${copy.openSource} ${source.id}`} disabled={openingSource !== null} loading={openingSource === source.id} onClick={() => onOpen(source)} size="xs" variant="secondary">{copy.openSource}</Button>
      {sourceError ? <div className="grid gap-1" role="alert"><span className="flex items-center gap-1 text-destructive"><ErrorIcon size="0.875rem" />{sourceError.message}</span><Button disabled={openingSource !== null} onClick={() => onOpen(source)} size="xs" variant="secondary">{copy.retryOpen}</Button></div> : null}
    </>}
  </div>
}

function EvidenceRow({ copy, item, onOpen, openingSource, source }: {
  copy: TaskOverviewCopy
  item: AnalysisEvidence
  onOpen: (source: AnalysisSource) => void
  openingSource: string | null
  source?: AnalysisSource
}) {
  return <div className="grid gap-1">
    {source ? <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
      <span>{source.id}</span>
      <span>{copy.analysisSourceStored.replace('{id}', source.stored_session_id)}</span>
      {source.status === 'read' ? <Button aria-label={`${copy.openSource} ${source.id}`} disabled={openingSource !== null} loading={openingSource === source.id} onClick={() => onOpen(source)} size="xs" variant="link">{copy.openSource}</Button> : null}
    </div> : null}
    <span className="text-muted-foreground">{copy.analysisRow.replace('{row}', String(item.row_id))}</span>
    <blockquote className="whitespace-pre-wrap break-words pl-3">{item.quote}</blockquote>
  </div>
}

function Claims({ copy, onOpen, openingSource, sources, title, values }: ClaimsProps) {
  return <div className="grid min-w-0 gap-2">
    <h5 className="text-xs font-medium">{title}</h5>
    {values.length ? <ul className="grid gap-3 text-xs">{values.map((claim, index) => <li className="grid min-w-0 gap-1" key={index}>
      <details>
        <summary className="cursor-pointer whitespace-pre-wrap break-words">{claim.text}<span className="ml-2 text-muted-foreground">{copy.analysisEvidence}</span></summary>
        <div className="mt-2 grid gap-2">{claim.evidence.map((item, evidenceIndex) => <EvidenceRow copy={copy} item={item} key={`${item.source_id ?? 'v1'}-${item.row_id}-${evidenceIndex}`} onOpen={onOpen} openingSource={openingSource} source={item.source_id ? sources?.find(candidate => candidate.id === item.source_id) : undefined} />)}</div>
      </details>
    </li>)}</ul> : <p className="text-xs text-muted-foreground">{copy.analysisNoRecord}</p>}
  </div>
}

function ResultView({ copy, locale, onOpen, openingSource, result, sourceError, task }: {
  copy: TaskOverviewCopy
  locale: string
  onOpen: (source: AnalysisSource) => void
  openingSource: string | null
  result: TaskAnalysis
  sourceError: SourceError | null
  task: Task
}) {
  const linked = isV2(result)
  const sources = linked ? result.sources : undefined
  const statusLabels = {
    todo: copy.todo, planning: copy.analysisPlanning, in_progress: copy.in_progress, waiting: copy.waiting,
    failed: copy.analysisFailedStatus, awaiting_acceptance: copy.analysisAwaitingAcceptance
  }

  return <>
    <div className="grid gap-1 text-xs text-muted-foreground">
      <p>{copy.analysisJudgment}: {statusLabels[result.status]}</p>
      <p>{copy.analysisTime}: <time dateTime={new Date(result.analyzed_at).toISOString()}>{new Date(result.analyzed_at).toLocaleString(locale)}</time></p>
      <p>{copy.analysisCoverage.replace('{analyzed}', String(result.analyzed_messages)).replace('{total}', String(result.message_count))}</p>
      {result.stale ? <p role="status">{copy.analysisStale}</p> : null}
      {linked ? <p role="status">{result.complete ? copy.analysisComplete : copy.analysisIncomplete}</p> : null}
    </div>
    {linked ? <div className="grid gap-2"><h5 className="text-xs font-medium">{copy.analysisSources}</h5>{result.sources.map(source => <SourceRow copy={copy} error={sourceError} key={source.id} onOpen={onOpen} openingSource={openingSource} source={source} />)}</div> : null}
    <Claims copy={copy} onOpen={onOpen} openingSource={openingSource} sources={sources} title={copy.analysisGoal} values={result.goal ? [result.goal] : []} />
    <Claims copy={copy} onOpen={onOpen} openingSource={openingSource} sources={sources} title={copy.analysisCompleted} values={result.completed} />
    <Claims copy={copy} onOpen={onOpen} openingSource={openingSource} sources={sources} title={copy.analysisRemaining} values={result.remaining} />
    <Claims copy={copy} onOpen={onOpen} openingSource={openingSource} sources={sources} title={copy.analysisNeedsUser} values={result.needs_user} />
    {result.limitations.length ? <div className="grid gap-1 text-xs text-muted-foreground"><h5 className="font-medium">{copy.analysisLimitations}</h5><ul className="list-disc pl-4">{result.limitations.map((limitation, index) => <li className="break-words" key={index}>{limitation}</li>)}</ul></div> : null}
    {task.primary ? null : <p className="text-xs text-muted-foreground">{copy.sourceRequired}</p>}
  </>
}

export function TaskAnalysisPanel({ task, autoAnalyze = false, onUpdated, onAutoAnalyzeStarted }: TaskAnalysisPanelProps) {
  const { t, locale } = useI18n()
  const copy = t.taskOverview
  const { result, busy, error, refresh, run } = useTaskAnalysis({ task, autoAnalyze, onUpdated, onAutoAnalyzeStarted })
  const [openingSource, setOpeningSource] = useState<string | null>(null)
  const [sourceError, setSourceError] = useState<SourceError | null>(null)

  const openSource = async (source: AnalysisSource) => {
    if (source.status !== 'read') { return }
    setOpeningSource(source.id)
    setSourceError(null)

    try {
      await openTaskSource(taskAnalysisSourceReference(task, source))
    } catch (cause) {
      setSourceError({ id: source.id, message: cause instanceof Error ? cause.message : String(cause) })
    } finally {
      setOpeningSource(null)
    }
  }

  return <section aria-label={copy.analysisTitle} className="grid min-w-0 gap-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h4 className="text-xs font-medium">{copy.analysisTitle}</h4>
      <Button disabled={busy || !task.primary} onClick={() => void run(true)} size="sm" variant="secondary">{result ? copy.analysisRefresh : copy.analysisStart}</Button>
    </div>
    <p className="text-xs text-muted-foreground">{result && isV2(result) ? copy.analysisScopeLinked : copy.analysisScope}</p>
    {busy ? <div aria-live="polite" className="flex items-center gap-2 text-xs" role="status"><Loader className="size-6" label={refresh ? copy.analysisRunning : copy.analysisReading} type="lemniscate-bloom" /><span>{refresh ? copy.analysisRunning : copy.analysisReading}</span></div> : null}
    {error ? <div className="grid gap-2 text-xs" role="alert"><div className="flex items-start gap-2"><ErrorIcon size="1rem" /><p className="min-w-0 break-words"><strong>{refresh ? copy.analysisError : copy.analysisReadError}</strong><br />{error}</p></div><Button disabled={busy} onClick={() => void run(refresh)} size="sm" variant="secondary">{copy.retry}</Button></div> : null}
    {result ? <ResultView copy={copy} locale={locale} onOpen={openSource} openingSource={openingSource} result={result} sourceError={sourceError} task={task} /> : !busy && !error ? <p className="text-xs text-muted-foreground">{task.primary ? copy.analysisEmpty : copy.sourceRequired}</p> : null}
  </section>
}
