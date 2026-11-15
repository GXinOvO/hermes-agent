import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import type { TitlebarTool } from '@/app/shell/titlebar-controls'
import { isStripTabHidden, revealTreePane, setTreePaneHidden } from '@/components/pane-shell/tree/store'
import { useI18n } from '@/i18n'
import { Clipboard } from '@/lib/icons'
import { $taskOverviewEnabled } from '@/store/task-overview'

import { taskOverviewVisibilityTransition } from '../task-overview-visibility'

export function useTaskOverviewTool(tools: TitlebarTool[]): TitlebarTool[] {
  const { t } = useI18n()
  const enabled = useStore($taskOverviewEnabled)
  const [visibility, setVisibility] = useState({ hiddenBySetting: false, initialized: false })

  useEffect(() => {
    const transition = taskOverviewVisibilityTransition(visibility, enabled, isStripTabHidden('task-overview'))

    if (transition.state.hiddenBySetting !== visibility.hiddenBySetting || transition.state.initialized !== visibility.initialized) {
      setVisibility(transition.state)
    }

    if (transition.hidePane !== null) {
      setTreePaneHidden('task-overview', transition.hidePane)
    }
  }, [enabled, visibility])

  const followup = enabled ? [{ id: 'task-overview', icon: <Clipboard />, label: t.taskOverview.title,
    onSelect: () => revealTreePane('task-overview'), badge: undefined }] : []

  return [...followup, ...tools]
}
