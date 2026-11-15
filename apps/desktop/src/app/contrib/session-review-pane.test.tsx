import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { WritableAtom } from 'nanostores'
import { afterEach, expect, test, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { $focusedOverviewKey as key } from '@/lib/task-overview/focused-review'
import { deriveSessionOverview } from '@/lib/task-overview/session-review'
import type * as TaskStorage from '@/lib/task-overview/store'

import { SessionReviewPane } from './session-review-pane'

const fixture = vi.hoisted(() => ({ load: vi.fn(), append: vi.fn(), createIfAbsent: vi.fn(), close: vi.fn() }))
vi.mock('@/lib/task-overview/focused-review', async () => {
  const { atom } = await import('nanostores')

  return { $focusedOverviewKey: atom('source-a'), loadFocusedSessionOverview: fixture.load, appendFocusedOverviewRequest: fixture.append }
})

vi.mock('@/lib/task-overview/store', async importOriginal => ({
  ...await importOriginal<typeof TaskStorage>(),
  openTaskStore: vi.fn(async () => ({ close: fixture.close, createIfAbsent: fixture.createIfAbsent, create: vi.fn(), list: vi.fn(async () => []), replace: vi.fn(), changePrimary: vi.fn() }))
}))

const overview = (content: string) => deriveSessionOverview({ version: 1, session_id: 'runtime', stored_session_id: 'stored', resolved_id: 'tip',
  owner: {}, authority_epoch: 1, snapshot_scope: 'persisted_display', complete: true, fetched_at: 1234, conversation_revision: [],
  messages: [{ role: 'user', row_id: 1, content }, { role: 'assistant', row_id: 2,
    content: '::task{remaining="Deploy" deferred="Mobile" decision="Choose server" tests="passed" tests_evidence="receipt.json"}' }],
  todo_state: { revision: 3, todos: [{ id: 'a', content: 'Remote validation', status: 'pending' }] },
  verification: null, verification_cwd: null, verification_error: 'workspace unavailable' }, {
    connectionId: 'source-a', expectedOwner: { instanceId: 'instance', profileId: 'profile', authorityEpoch: 1, storeInstanceId: 'store' },
    mode: 'local', profile: 'profile', storedId: 'stored', targetProfile: 'profile'
  })

afterEach(() => { cleanup(); vi.useRealTimers(); fixture.load.mockReset(); fixture.append.mockReset(); fixture.createIfAbsent.mockReset(); fixture.close.mockReset(); (key as WritableAtom<string>).set('source-a') })

test('shows real requirements, native remaining work and evidence as a read-only review', async () => {
  fixture.load.mockResolvedValue(overview('Ship feature'))
  render(<I18nProvider configClient={null} initialLocale="en"><SessionReviewPane visible /></I18nProvider>)
  expect(await screen.findByRole('heading', { name: 'Ship feature' })).toBeTruthy()
  expect(screen.getAllByText('Remote validation').length).toBeGreaterThan(0)
  expect(screen.getAllByText('Deploy').length).toBeGreaterThan(0)
  expect(screen.getAllByText('Choose server').length).toBeGreaterThan(0)
  expect(screen.getByText('receipt.json')).toBeTruthy()
  expect(screen.getByText('Message #2')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Mark accepted' })).toBeNull()
  fixture.append.mockResolvedValueOnce(true)
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Update task record' })); await Promise.resolve() })
  expect(fixture.append).toHaveBeenCalledWith('source-a', expect.stringContaining('::task'))
})

test('drops an old response after focus changes and keeps retry available on failure', async () => {
  let finish!: (value: ReturnType<typeof overview>) => void
  fixture.load.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
  fixture.load.mockRejectedValueOnce(new Error('Source unavailable'))
  render(<I18nProvider configClient={null} initialLocale="en"><SessionReviewPane visible /></I18nProvider>)
  act(() => (key as WritableAtom<string>).set('source-b'))
  expect(await screen.findByRole('alert')).toBeTruthy()
  await act(async () => { finish(overview('Old source')); await Promise.resolve() })
  expect(screen.queryByText('Old source')).toBeNull()
  fixture.load.mockResolvedValueOnce(overview('Current source'))
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByRole('heading', { name: 'Current source' })).toBeTruthy()
})

test('shows registered results separately from later assistant corrections', async () => {
  const value = overview('Ship feature')
  fixture.load.mockResolvedValue({ ...value, registeredResult: 'Everything succeeded', latest: {
    role: 'assistant', row_id: 3, content: 'Correction: remote verification failed.'
  } })
  render(<I18nProvider configClient={null} initialLocale="en"><SessionReviewPane visible /></I18nProvider>)
  expect(await screen.findByRole('heading', { name: 'Agent-recorded result' })).toBeTruthy()
  expect(screen.getByText('Everything succeeded')).toBeTruthy()
  expect(screen.getByRole('heading', { name: 'Latest reply' }).parentElement?.textContent).toContain('Correction: remote verification failed.')
  expect(screen.getByRole('heading', { name: 'Latest reply' }).parentElement?.textContent).not.toContain('Everything succeeded')
})

test('loads and polls only while the session overview is visible', async () => {
  vi.useFakeTimers()
  fixture.load.mockResolvedValue(overview('Visible source'))
  const { rerender } = render(<I18nProvider configClient={null} initialLocale="en"><SessionReviewPane visible={false} /></I18nProvider>)
  await act(async () => { await vi.advanceTimersByTimeAsync(30000) })
  expect(fixture.load).not.toHaveBeenCalled()
  rerender(<I18nProvider configClient={null} initialLocale="en"><SessionReviewPane visible /></I18nProvider>)
  await act(async () => { await Promise.resolve() })
  expect(fixture.load).toHaveBeenCalledTimes(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(15000) })
  expect(fixture.load).toHaveBeenCalledTimes(2)
  rerender(<I18nProvider configClient={null} initialLocale="en"><SessionReviewPane visible={false} /></I18nProvider>)
  await act(async () => { await vi.advanceTimersByTimeAsync(30000) })
  expect(fixture.load).toHaveBeenCalledTimes(2)
})

test('shows an empty state without requesting an unbound session', () => {
  ;(key as WritableAtom<string>).set('')
  render(<I18nProvider configClient={null} initialLocale="en"><SessionReviewPane visible /></I18nProvider>)
  expect(screen.getByText('Open a conversation to review its requirements and evidence.')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Refresh overview' }).hasAttribute('disabled')).toBe(true)
  expect(screen.queryByRole('button', { name: 'Update task record' })).toBeNull()
  expect(fixture.load).not.toHaveBeenCalled()
})

test('keeps the overview usable when the draft cannot be updated', async () => {
  fixture.load.mockResolvedValue(overview('Review source'))
  fixture.append.mockResolvedValueOnce(false).mockResolvedValueOnce(true)
  render(<I18nProvider configClient={null} initialLocale="en"><SessionReviewPane visible /></I18nProvider>)
  await screen.findByRole('heading', { name: 'Review source' })
  fireEvent.click(screen.getByRole('button', { name: 'Update task record' }))
  expect((await screen.findByRole('alert')).textContent).toContain('Could not update this conversation’s draft')
  expect(screen.getByRole('heading', { name: 'Review source' })).toBeTruthy()
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Update task record' })); await Promise.resolve() })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(fixture.append).toHaveBeenCalledTimes(2)
})

test('creates a source-bound matter only after confirming a discovered candidate', async () => {
  fixture.load.mockResolvedValue(overview('Review source'))
  fixture.createIfAbsent.mockResolvedValue({ created: true, revision: 1, row: { revision: 1, task: {} } })
  render(<I18nProvider configClient={null} initialLocale="en"><SessionReviewPane visible /></I18nProvider>)
  await screen.findByRole('heading', { name: 'Review source' })

  fireEvent.click(screen.getAllByRole('button', { name: 'Track as matter' })[0])
  await act(async () => { await Promise.resolve() })

  expect(fixture.createIfAbsent).toHaveBeenCalledWith(expect.objectContaining({
    primary: expect.objectContaining({ storedId: 'stored', connectionId: 'source-a', profile: 'profile' }),
    nextSteps: ['Remote validation']
  }), expect.any(Number), expect.objectContaining({ storedId: 'stored' }), 'Remote validation')
  expect(screen.getByRole('button', { name: 'Tracked' })).toBeTruthy()
})
