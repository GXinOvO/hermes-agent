export interface TaskOverviewVisibilityState {
  hiddenBySetting: boolean
  initialized: boolean
}

export interface TaskOverviewVisibilityTransition {
  hidePane: boolean | null
  state: TaskOverviewVisibilityState
}

export function taskOverviewVisibilityTransition(
  state: TaskOverviewVisibilityState,
  enabled: boolean,
  manuallyHidden: boolean
): TaskOverviewVisibilityTransition {
  if (!state.initialized) {
    return {
      hidePane: true,
      state: { hiddenBySetting: !enabled, initialized: true }
    }
  }

  if (!enabled) {
    return {
      hidePane: true,
      state: { hiddenBySetting: !manuallyHidden, initialized: true }
    }
  }

  if (state.hiddenBySetting && !manuallyHidden) {
    return {
      hidePane: false,
      state: { hiddenBySetting: false, initialized: true }
    }
  }

  return {
    hidePane: null,
    state: { hiddenBySetting: false, initialized: true }
  }
}
