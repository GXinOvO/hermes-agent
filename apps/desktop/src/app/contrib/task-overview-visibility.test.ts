import { describe, expect, it } from 'vitest'

import { taskOverviewVisibilityTransition } from './task-overview-visibility'

describe('task overview visibility', () => {
  it('starts hidden and restores a setting-owned hide', () => {
    const initial = taskOverviewVisibilityTransition(
      { hiddenBySetting: false, initialized: false },
      true,
      false
    )

    expect(initial.hidePane).toBe(true)

    const disabled = taskOverviewVisibilityTransition(initial.state, false, false)
    expect(disabled.hidePane).toBe(true)

    const enabled = taskOverviewVisibilityTransition(disabled.state, true, false)
    expect(enabled.hidePane).toBe(false)
  })

  it('does not override a user-hidden pane when the setting is re-enabled', () => {
    const disabled = taskOverviewVisibilityTransition(
      { hiddenBySetting: false, initialized: true },
      false,
      false
    )

    const enabled = taskOverviewVisibilityTransition(disabled.state, true, true)

    expect(enabled.hidePane).toBe(null)
  })
})
