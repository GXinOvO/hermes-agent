import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useI18n } from '@/i18n'
import type { Task, TaskPriority, TaskReference, TaskStatus } from '@/lib/task-overview/model'

export const TASK_STATUSES: TaskStatus[] = ['unspecified', 'todo', 'in_progress', 'waiting']
export const TASK_PRIORITIES: TaskPriority[] = ['urgent', 'high', 'normal', 'low']

export type TaskDraft = Pick<Task, 'title' | 'description' | 'notes' | 'status' | 'priority'> & { steps: string; source: string; primary?: TaskReference }

export function taskDraft(task?: Task): TaskDraft {
  return {
    title: task?.title ?? '', description: task?.description ?? '', notes: task?.notes ?? '', source: '',
    status: task?.status ?? 'todo', priority: task?.priority ?? 'normal', steps: task?.nextSteps.join('\n') ?? ''
  }
}

interface TaskOverviewFormProps {
  draft: TaskDraft
  editing?: boolean
  saving: boolean
  captureSession?: () => Promise<TaskReference>
  onChange: (draft: TaskDraft) => void
  onCancel: () => void
  onSave: () => void
}

export function TaskOverviewForm({ draft, editing = false, saving, captureSession, onChange, onCancel, onSave }: TaskOverviewFormProps) {
  const { t } = useI18n()
  const copy = t.taskOverview
  const [capturing, setCapturing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const request = useRef(0)
  const latest = useRef(draft)
  latest.current = draft
  const disabled = saving || capturing

  useEffect(() => () => { request.current++ }, [])

  const capture = async () => {
    if (!captureSession || disabled) { return }
    const token = ++request.current
    setCapturing(true); setError(null)

    try {
      const primary = await captureSession()

      if (token === request.current) { onChange({ ...latest.current, source: '', primary }) }
    } catch (cause) {
      if (token === request.current) { setError(cause instanceof Error ? cause.message : String(cause)) }
    } finally { if (token === request.current) { setCapturing(false) } }
  }

  return (
    <form className="grid gap-3" onSubmit={event => { event.preventDefault();

 if (!disabled) { onSave() } }}>
      <label className="grid gap-1 text-xs">{copy.taskName}<Input aria-label={copy.taskName} disabled={disabled} onChange={event => onChange({ ...draft, title: event.currentTarget.value })} placeholder={copy.taskName} required value={draft.title} /></label>
      {!editing ? <div className="grid gap-2">
        <label className="grid gap-1 text-xs">{copy.sourceInput}<Input aria-label={copy.sourceInput} disabled={disabled} onChange={event => { request.current++; onChange({ ...draft, source: event.currentTarget.value, primary: undefined }); setError(null) }} placeholder={copy.sourcePlaceholder} value={draft.source} /></label>
        {captureSession ? <Button disabled={disabled} onClick={() => void capture()} size="sm" type="button" variant="secondary">{capturing ? copy.capturingSource : copy.bind}</Button> : null}
        {draft.primary ? <p className="break-all text-xs" role="status">{copy.linkedSource}: {draft.primary.targetProfile || draft.primary.profile} / {draft.primary.storedId}</p> : <p className="text-xs text-muted-foreground">{copy.sourceRequired}</p>}
        {error ? <p className="text-xs" role="alert">{copy.bindingFailed}: {error}</p> : null}
      </div> : <details className="text-xs">
        <summary className="cursor-pointer font-medium">{copy.supplement}</summary>
        <div className="mt-2 grid gap-2">
          <label className="grid gap-1">{copy.description}<Textarea aria-label={copy.description} disabled={disabled} onChange={event => onChange({ ...draft, description: event.currentTarget.value })} value={draft.description} /></label>
          <label className="grid gap-1">{copy.notes}<Textarea aria-label={copy.notes} disabled={disabled} onChange={event => onChange({ ...draft, notes: event.currentTarget.value })} value={draft.notes} /></label>
          <label className="grid gap-1">{copy.remainingSteps}<Textarea aria-label={copy.remainingSteps} disabled={disabled} onChange={event => onChange({ ...draft, steps: event.currentTarget.value })} placeholder={copy.stepsPlaceholder} value={draft.steps} /></label>
        </div>
      </details>}
      <div className="flex flex-wrap gap-2">
        <Select disabled={disabled || draft.status === 'done'} onValueChange={status => onChange({ ...draft, status: status as TaskStatus })} value={draft.status}>
          <SelectTrigger aria-label={copy.status}><SelectValue /></SelectTrigger>
          <SelectContent>{(draft.status === 'done' ? ['done'] as const : TASK_STATUSES).map(status => <SelectItem key={status} value={status}>{copy[status]}</SelectItem>)}</SelectContent>
        </Select>
        <Select disabled={disabled} onValueChange={priority => onChange({ ...draft, priority: priority as TaskPriority })} value={draft.priority}>
          <SelectTrigger aria-label={copy.priority}><SelectValue /></SelectTrigger>
          <SelectContent>{TASK_PRIORITIES.map(priority => <SelectItem key={priority} value={priority}>{copy[priority]}</SelectItem>)}</SelectContent>
        </Select>
      </div>
      <div className="flex gap-2">
        <Button disabled={disabled || !draft.title.trim() || (!editing && !draft.primary && !draft.source.trim())} size="sm" type="submit">{saving ? copy.saving : copy.saveTask}</Button>
        <Button disabled={disabled} onClick={() => { request.current++; onCancel() }} size="sm" type="button" variant="ghost">{copy.cancel}</Button>
      </div>
    </form>
  )
}
