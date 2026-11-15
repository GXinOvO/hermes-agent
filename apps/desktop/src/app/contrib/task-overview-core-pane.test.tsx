// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { cleanup } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, expect, it, vi } from 'vitest'

import type * as PaneTreeStore from '@/components/pane-shell/tree/store'
import { I18nProvider } from '@/i18n'
import { setTaskOverviewEnabled } from '@/store/task-overview'

import { TaskOverviewCorePane } from './task-overview-core-pane'

const fixture = vi.hoisted(() => ({
  list: vi.fn(async () => []),
  close: vi.fn()
}))

vi.mock('@/components/pane-shell/tree/store', async importOriginal => ({
  ...await importOriginal<typeof PaneTreeStore>(),
  $paneVisible: () => atom(true)
}))
vi.mock('@/lib/exact-session', () => ({ captureFocusedSessionReference: vi.fn(), openExactSession: vi.fn() }))
vi.mock('@/lib/task-overview/store', () => ({ openTaskStore: async () => fixture }))
vi.mock('./session-review-pane', () => ({ SessionReviewPane: ({ visible }: { visible: boolean }) => visible ? <p>Snapshot review</p> : null }))

afterEach(() => { cleanup(); vi.clearAllMocks() })

it('switches all three views through one matter panel and preserves the current draft', async () => {
  setTaskOverviewEnabled(true)
  render(<I18nProvider configClient={null} initialLocale="en"><TaskOverviewCorePane /></I18nProvider>)
  await waitFor(() => expect(fixture.list).toHaveBeenCalledOnce())
  fireEvent.click(screen.getByRole('button', { name: 'New task' }))
  fireEvent.change(screen.getByLabelText('Task name'), { target: { value: 'Preserved draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Attention' }))
  expect(screen.getByRole('region', { name: 'Attention' })).toBeTruthy()
  expect(screen.getByLabelText('Task name')).toHaveProperty('value', 'Preserved draft')
  expect(fixture.list).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: 'Session overview' }))
  expect(screen.getByText('Snapshot review')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Task follow-up' }))
  expect(screen.getByLabelText('Task name')).toHaveProperty('value', 'Preserved draft')
  await waitFor(() => expect(fixture.list).toHaveBeenCalledTimes(2))
})
