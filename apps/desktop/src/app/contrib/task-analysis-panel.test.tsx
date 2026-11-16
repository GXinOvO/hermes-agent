import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import type * as Analysis from '@/lib/task-overview/analysis'
import { cachedTaskAnalysis } from '@/lib/task-overview/analysis'
import type { TaskAnalysis } from '@/lib/task-overview/analysis-model'
import { type Task, taskReferenceKey } from '@/lib/task-overview/model'

import { TaskAnalysisPanel } from './task-analysis-panel'

const api = vi.hoisted(() => ({ load: vi.fn(), save: vi.fn(), close: vi.fn() }))

vi.mock('@/lib/task-overview/analysis', async importOriginal => ({
  ...await importOriginal<typeof Analysis>(), loadTaskAnalysis: api.load
}))
vi.mock('@/lib/task-overview/store', () => ({ openTaskStore: async () => ({ saveAnalysis: api.save, close: api.close }) }))

const task: Task = {
  version: 1, id: 'task-1', title: 'Delivery check', description: '', notes: '', nextSteps: [], priority: 'high', status: 'todo',
  createdAt: 1_700_000_000_000, updatedAt: 1_700_000_000_000, related: [], resolution: { state: 'open' },
  primary: {
    storedId: 'session-1', connectionId: 'connection-1', profile: 'default', targetProfile: 'worker', mode: 'remote',
    expectedOwner: { instanceId: 'owner-1', profileId: '/profile/worker', authorityEpoch: 1, storeInstanceId: 'store-1' }
  }
}

const result: TaskAnalysis = {
  version: 1, title: task.title, analyzed_at: 1_700_000_000_000, conversation_revision: 'r1', stale: true,
  scope: 'single_conversation', message_count: 10, analyzed_messages: 7, limitations: ['Only visible messages'],
  status: 'awaiting_acceptance', goal: { text: 'Verify delivered output', evidence: [{ row_id: 42, quote: 'Original request text' }] },
  completed: [], remaining: [], needs_user: []
}

const cached: Task = { ...task, analysis: { title: task.title, referenceKey: taskReferenceKey(task.primary)!, result } }

function show(value: Task, onUpdated = vi.fn(), autoAnalyze = false) {
  return render(<I18nProvider configClient={null} initialLocale="en"><TaskAnalysisPanel autoAnalyze={autoAnalyze} onAutoAnalyzeStarted={vi.fn()} onUpdated={onUpdated} task={value} /></I18nProvider>)
}

beforeEach(() => {
  api.load.mockReset().mockResolvedValue(null)
  api.save.mockReset().mockImplementation(async () => ({ task: cached, revision: 2 }))
  api.close.mockClear()
})
afterEach(cleanup)

describe('task analysis lifecycle', () => {
  it('starts a new task analysis once under the desktop StrictMode root', async () => {
    api.load.mockResolvedValue(result)
    render(<StrictMode><I18nProvider configClient={null} initialLocale="en"><TaskAnalysisPanel autoAnalyze onAutoAnalyzeStarted={vi.fn()} onUpdated={vi.fn()} task={task} /></I18nProvider></StrictMode>)
    await screen.findByText('Verify delivered output')
    expect(api.load).toHaveBeenCalledTimes(1)
    expect(api.load).toHaveBeenCalledWith(task, true)
  })

  it('keeps cached evidence when refresh fails and retries the refresh', async () => {
    api.load.mockResolvedValueOnce(result).mockRejectedValueOnce(new Error('model offline')).mockResolvedValueOnce({ ...result, stale: false })
    show(cached)
    await screen.findByText('Verify delivered output')
    fireEvent.click(screen.getByRole('button', { name: 'Analyze again' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('Verify delivered output')).toBeTruthy()
    expect(screen.getByText('model offline')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(api.load).toHaveBeenLastCalledWith(cached, true))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(api.load.mock.calls.map(call => call[1])).toEqual([false, true, true])
  })

  it('retries a failed read without generating a new analysis', async () => {
    api.load.mockRejectedValueOnce(new Error('read offline')).mockResolvedValueOnce(result)
    show(task)
    expect(await screen.findByText('read offline')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByText('Verify delivered output')
    expect(api.load.mock.calls.map(call => call[1])).toEqual([false, false])
  })

  it('does not save or display an old result after switching to another task', async () => {
    let release!: (value: TaskAnalysis) => void
    api.load.mockImplementationOnce(() => new Promise(resolve => { release = resolve })).mockResolvedValueOnce(null)
    const onUpdated = vi.fn()
    const view = show(task, onUpdated)
    await waitFor(() => expect(api.load).toHaveBeenCalledTimes(1))
    const other = { ...task, id: 'task-2', title: 'Different task' }
    view.rerender(<I18nProvider configClient={null} initialLocale="en"><TaskAnalysisPanel onAutoAnalyzeStarted={vi.fn()} onUpdated={onUpdated} task={other} /></I18nProvider>)
    await waitFor(() => expect(api.load).toHaveBeenCalledTimes(2))
    await act(async () => release(result))
    expect(screen.queryByText('Verify delivered output')).toBeNull()
    expect(api.save).not.toHaveBeenCalled()
    expect(onUpdated).not.toHaveBeenCalled()
  })

  it('does not save a late result after unmounting', async () => {
    let release!: (value: TaskAnalysis) => void
    api.load.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const view = show(task)
    await waitFor(() => expect(api.load).toHaveBeenCalledTimes(1))
    view.unmount()
    await act(async () => release(result))
    expect(api.save).not.toHaveBeenCalled()
  })

  it('saves analysis separately and returns the latest revision without changing acceptance', async () => {
    const accepted: Task = { ...task, status: 'done', resolution: { state: 'accepted', at: task.updatedAt, note: 'Owner verified output' } }
    const saved = { task: { ...accepted, analysis: cached.analysis }, revision: 9 }
    api.load.mockResolvedValueOnce(result)
    api.save.mockResolvedValueOnce(saved)
    const onUpdated = vi.fn()
    show(accepted, onUpdated)
    await waitFor(() => expect(onUpdated).toHaveBeenCalledWith(saved))
    expect(api.save).toHaveBeenCalledWith(task.id, task.title, task.primary, result)
    expect(accepted.status).toBe('done')
    expect(accepted.resolution.state).toBe('accepted')
    expect(api.load.mock.calls.map(call => call[1])).toEqual([false])
  })

  it('reports message coverage, limits and empty sections without inferring no work', async () => {
    api.load.mockResolvedValueOnce(result)
    show(task)
    await screen.findByText('Coverage: 7 / 10 messages')
    expect(screen.getByText('Only visible messages')).toBeTruthy()
    expect(screen.getByText('The conversation has changed since this analysis. Analyze again to update it.')).toBeTruthy()
    expect(screen.getAllByText('No verifiable record in this conversation.')).toHaveLength(3)
    fireEvent.click(screen.getByText('View evidence'))
    expect(screen.getByText('Message row 42')).toBeTruthy()
    expect(screen.getByText('Original request text')).toBeTruthy()
  })

  it('does not reuse cached analysis after a title or source change', () => {
    expect(cachedTaskAnalysis(cached)).toEqual(result)
    expect(cachedTaskAnalysis({ ...cached, title: 'New name' })).toBeNull()
    expect(cachedTaskAnalysis({ ...cached, primary: { ...task.primary!, storedId: 'another-session' } })).toBeNull()
  })

  it('keeps a usable retry after a new task analysis fails', async () => {
    api.load.mockRejectedValueOnce(new Error('analysis offline')).mockResolvedValueOnce(result)
    show(task, vi.fn(), true)
    expect(await screen.findByText('analysis offline')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByText('Verify delivered output')
    expect(api.load.mock.calls.map(call => call[1])).toEqual([true, true])
  })
})
