import { afterEach, describe, expect, it } from 'vitest'

import { $taskOverviewEnabled, setTaskOverviewEnabled } from './task-overview'

const KEY = 'hermes.desktop.taskOverview.enabled'

afterEach(() => {
  setTaskOverviewEnabled(true)
  window.localStorage.removeItem(KEY)
})

describe('task overview preference', () => {
  it('defaults to enabled so the built-in follow-up surface is available', () => {
    expect($taskOverviewEnabled.get()).toBe(true)
  })

  it('persists the user choice', () => {
    setTaskOverviewEnabled(false)

    expect($taskOverviewEnabled.get()).toBe(false)
    expect(window.localStorage.getItem(KEY)).toBe('false')

    setTaskOverviewEnabled(true)

    expect(window.localStorage.getItem(KEY)).toBe('true')
  })
})