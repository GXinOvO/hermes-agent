import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { $paneVisible } from '@/components/pane-shell/tree/store'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { useI18n } from '@/i18n'
import { captureFocusedSessionReference } from '@/lib/exact-session'
import { openTaskSource } from '@/lib/task-overview/analysis'
import { $taskOverviewEnabled } from '@/store/task-overview'

import { SessionReviewPane } from './session-review-pane'
import { TaskOverviewPane } from './task-overview-pane'

const $visible = $paneVisible('task-overview')

export function TaskOverviewCorePane() {
  const enabled = useStore($taskOverviewEnabled)
  const visible = useStore($visible)
  const [view, setView] = useState<'tasks' | 'attention' | 'review'>('tasks')
  const { t } = useI18n()

  if (!enabled) { return null }

  return <div className="flex h-full min-h-0 flex-col">
    <div className="px-4 pt-3"><SegmentedControl onChange={setView} options={[
      { id: 'tasks', label: t.taskOverview.title }, { id: 'attention', label: t.taskOverview.attention }, { id: 'review', label: t.sessionReview.tab }
    ]} value={view} /></div>
    <div className="min-h-0 flex-1" hidden={view === 'review'}><TaskOverviewPane attentionOnly={view === 'attention'} captureSession={captureFocusedSessionReference} openSession={openTaskSource} visible={visible && view !== 'review'} /></div>
    <div className="min-h-0 flex-1" hidden={view !== 'review'}><SessionReviewPane visible={visible && view === 'review'} /></div>
  </div>
}
