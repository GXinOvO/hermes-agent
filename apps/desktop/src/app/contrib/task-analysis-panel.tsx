import { Button } from '@/components/ui/button'
import { ErrorIcon } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { useI18n } from '@/i18n'
import type { AnalysisClaim } from '@/lib/task-overview/analysis-model'
import type { Task } from '@/lib/task-overview/model'
import type { TaskRow } from '@/lib/task-overview/store'

import { useTaskAnalysis } from './use-task-analysis'

interface TaskAnalysisPanelProps {
  task: Task
  autoAnalyze?: boolean
  onUpdated: (row: TaskRow) => void
  onAutoAnalyzeStarted: () => void
}

export function TaskAnalysisPanel({ task, autoAnalyze = false, onUpdated, onAutoAnalyzeStarted }: TaskAnalysisPanelProps) {
  const { t, locale } = useI18n()
  const copy = t.taskOverview
  const { result, busy, error, refresh, run } = useTaskAnalysis({ task, autoAnalyze, onUpdated, onAutoAnalyzeStarted })

  const statusLabels = {
    todo: copy.todo, planning: copy.analysisPlanning, in_progress: copy.in_progress, waiting: copy.waiting,
    failed: copy.analysisFailedStatus, awaiting_acceptance: copy.analysisAwaitingAcceptance
  }

  const claims = (title: string, values: AnalysisClaim[]) => <div className="grid min-w-0 gap-2">
    <h5 className="text-xs font-medium">{title}</h5>
    {values.length ? <ul className="grid gap-3 text-xs">{values.map((claim, index) => <li className="grid min-w-0 gap-1" key={index}>
      <details>
        <summary className="cursor-pointer whitespace-pre-wrap break-words">{claim.text}<span className="ml-2 text-muted-foreground">{copy.analysisEvidence}</span></summary>
        <div className="mt-2 grid gap-2">{claim.evidence.map((evidence, evidenceIndex) => <div className="grid gap-1" key={`${evidence.row_id}-${evidenceIndex}`}>
          <span className="text-muted-foreground">{copy.analysisRow.replace('{row}', String(evidence.row_id))}</span>
          <blockquote className="whitespace-pre-wrap break-words pl-3">{evidence.quote}</blockquote>
        </div>)}</div>
      </details>
    </li>)}</ul> : <p className="text-xs text-muted-foreground">{copy.analysisNoRecord}</p>}
  </div>

  return <section aria-label={copy.analysisTitle} className="grid min-w-0 gap-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h4 className="text-xs font-medium">{copy.analysisTitle}</h4>
      <Button disabled={busy || !task.primary} onClick={() => void run(true)} size="sm" variant="secondary">{result ? copy.analysisRefresh : copy.analysisStart}</Button>
    </div>
    <p className="text-xs text-muted-foreground">{copy.analysisScope}</p>
    {busy ? <div aria-live="polite" className="flex items-center gap-2 text-xs" role="status"><Loader className="size-6" label={refresh ? copy.analysisRunning : copy.analysisReading} type="lemniscate-bloom" /><span>{refresh ? copy.analysisRunning : copy.analysisReading}</span></div> : null}
    {error ? <div className="grid gap-2 text-xs" role="alert"><div className="flex items-start gap-2"><ErrorIcon size="1rem" /><p className="min-w-0 break-words"><strong>{refresh ? copy.analysisError : copy.analysisReadError}</strong><br />{error}</p></div><Button disabled={busy} onClick={() => void run(refresh)} size="sm" variant="secondary">{copy.retry}</Button></div> : null}
    {result ? <>
      <div className="grid gap-1 text-xs text-muted-foreground">
        <p>{copy.analysisJudgment}: {statusLabels[result.status]}</p>
        <p>{copy.analysisTime}: <time dateTime={new Date(result.analyzed_at).toISOString()}>{new Date(result.analyzed_at).toLocaleString(locale)}</time></p>
        <p>{copy.analysisCoverage.replace('{analyzed}', String(result.analyzed_messages)).replace('{total}', String(result.message_count))}</p>
        {result.stale ? <p role="status">{copy.analysisStale}</p> : null}
      </div>
      {claims(copy.analysisGoal, result.goal ? [result.goal] : [])}
      {claims(copy.analysisCompleted, result.completed)}
      {claims(copy.analysisRemaining, result.remaining)}
      {claims(copy.analysisNeedsUser, result.needs_user)}
      {result.limitations.length ? <div className="grid gap-1 text-xs text-muted-foreground"><h5 className="font-medium">{copy.analysisLimitations}</h5><ul className="list-disc pl-4">{result.limitations.map((limitation, index) => <li className="break-words" key={index}>{limitation}</li>)}</ul></div> : null}
    </> : !busy && !error ? <p className="text-xs text-muted-foreground">{task.primary ? copy.analysisEmpty : copy.sourceRequired}</p> : null}
  </section>
}
