import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useI18n } from '@/i18n'
import type { Task, TaskPriority, TaskStatus } from '@/lib/task-overview/model'

export const TASK_STATUSES: TaskStatus[] = ['unspecified', 'todo', 'in_progress', 'waiting']
export const TASK_PRIORITIES: TaskPriority[] = ['urgent', 'high', 'normal', 'low']

export type TaskDraft = Pick<Task, 'title' | 'description' | 'notes' | 'status' | 'priority'> & { steps: string }

export function taskDraft(task?: Task): TaskDraft {
  return {
    title: task?.title ?? '', description: task?.description ?? '', notes: task?.notes ?? '',
    status: task?.status ?? 'todo', priority: task?.priority ?? 'normal', steps: task?.nextSteps.join('\n') ?? ''
  }
}

interface TaskOverviewFormProps {
  draft: TaskDraft
  saving: boolean
  onChange: (draft: TaskDraft) => void
  onCancel: () => void
  onSave: () => void
}

export function TaskOverviewForm({ draft, saving, onChange, onCancel, onSave }: TaskOverviewFormProps) {
  const { t } = useI18n()
  const copy = t.taskOverview

  return (
    <form className="grid gap-2" onSubmit={event => { event.preventDefault(); onSave() }}>
      <Input aria-label={copy.taskName} disabled={saving} onChange={event => onChange({ ...draft, title: event.currentTarget.value })} placeholder={copy.taskName} value={draft.title} />
      <Textarea aria-label={copy.description} disabled={saving} onChange={event => onChange({ ...draft, description: event.currentTarget.value })} placeholder={copy.description} value={draft.description} />
      <Textarea aria-label={copy.notes} disabled={saving} onChange={event => onChange({ ...draft, notes: event.currentTarget.value })} placeholder={copy.notes} value={draft.notes} />
      <Textarea aria-label={copy.remainingSteps} disabled={saving} onChange={event => onChange({ ...draft, steps: event.currentTarget.value })} placeholder={copy.stepsPlaceholder} value={draft.steps} />
      <div className="flex flex-wrap gap-2">
        <Select disabled={saving || draft.status === 'done'} onValueChange={status => onChange({ ...draft, status: status as TaskStatus })} value={draft.status}>
          <SelectTrigger aria-label={copy.status}><SelectValue /></SelectTrigger>
          <SelectContent>{(draft.status === 'done' ? ['done'] as const : TASK_STATUSES).map(status => <SelectItem key={status} value={status}>{copy[status]}</SelectItem>)}</SelectContent>
        </Select>
        <Select disabled={saving} onValueChange={priority => onChange({ ...draft, priority: priority as TaskPriority })} value={draft.priority}>
          <SelectTrigger aria-label={copy.priority}><SelectValue /></SelectTrigger>
          <SelectContent>{TASK_PRIORITIES.map(priority => <SelectItem key={priority} value={priority}>{copy[priority]}</SelectItem>)}</SelectContent>
        </Select>
      </div>
      <div className="flex gap-2">
        <Button disabled={saving || !draft.title.trim()} size="sm" type="submit">{saving ? copy.saving : copy.saveTask}</Button>
        <Button disabled={saving} onClick={onCancel} size="sm" type="button" variant="ghost">{copy.cancel}</Button>
      </div>
    </form>
  )
}
