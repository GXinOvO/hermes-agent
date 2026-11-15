import { Codecs, persistentAtom } from '@/lib/persisted'

export const $taskOverviewEnabled = persistentAtom(
  'hermes.desktop.taskOverview.enabled',
  true,
  Codecs.bool
)

export function setTaskOverviewEnabled(enabled: boolean): void {
  $taskOverviewEnabled.set(enabled)
}
