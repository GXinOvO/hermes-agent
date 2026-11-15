import { useStore } from '@nanostores/react'

import { useI18n } from '@/i18n'
import { $taskOverviewEnabled, setTaskOverviewEnabled } from '@/store/task-overview'

import { ComposerInteractionSettings } from './focus-follows-mouse-setting'
import { ToggleRow } from './primitives'
import { SETTING_IDS, settingElementId } from './settings-manifest'

export function WindowLayoutInteractionSettings() {
  return <><ComposerInteractionSettings /><TaskOverviewSetting /></>
}

export function TaskOverviewSetting() {
  const { t } = useI18n()
  const enabled = useStore($taskOverviewEnabled)

  return (
    <ToggleRow
      checked={enabled}
      description={t.settings.appearance.taskOverviewDesc}
      id={settingElementId(SETTING_IDS.appearance.taskOverview)}
      label={t.settings.appearance.taskOverviewTitle}
      onChange={setTaskOverviewEnabled}
    />
  )
}