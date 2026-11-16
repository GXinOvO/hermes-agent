// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import type { TaskAnalysis } from '@/lib/task-overview/analysis-model'
import type { Task, TaskReference } from '@/lib/task-overview/model'
import { taskReferenceKey } from '@/lib/task-overview/model'
import type * as TaskStorage from '@/lib/task-overview/store'
import { $sessions } from '@/store/session'
import { $sessionStates } from '@/store/session-states'
import { $sessionSeenCounts, $unreadFinishedMarkers } from '@/store/session-unread'
import { setTaskOverviewEnabled } from '@/store/task-overview'
import { makeSessionInfo } from '@/test/session-info'

import { TaskOverviewPane } from './task-overview-pane'

const fixture = vi.hoisted(() => {
  const task: Task = {
    version: 1,
    id: 'task-1',
    title: '核对事项',
    description: '',
    notes: '',
      primary: null as TaskReference | null,
    related: [] as TaskReference[],
    nextSteps: ['核对结果'],
    status: 'todo',
    priority: 'normal',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_000_000,
    resolution: { state: 'open' }
  }

  const store = {
    list: vi.fn(async (): Promise<TaskStorage.TaskRow[]> => [{ task, revision: 1 }]),
    create: vi.fn((_input: Record<string, unknown>, _at: number) => new Promise<TaskStorage.TaskRow>(() => {})),
    replace: vi.fn(async (next: Task, _revision: number) => ({ task: next, revision: 2 })),
    changePrimary: vi.fn(async (_id: string, _revision: number, primary: TaskReference, _at: number) => ({ task: { ...task, primary }, revision: 2 })),
    saveAnalysis: vi.fn(),
    close: vi.fn()
  }

  const primary: TaskReference = {
    storedId: 'source-session', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
    expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
  }

  const result: TaskAnalysis = {
    version: 1, title: task.title, analyzed_at: 1_700_000_000_000, conversation_revision: 'revision-1', stale: false,
    scope: 'single_conversation', message_count: 8, analyzed_messages: 6, limitations: ['Earlier tool output omitted'],
    status: 'awaiting_acceptance', goal: { text: '核对交付结果', evidence: [{ row_id: 12, quote: '请核对交付结果原文' }] },
    completed: [{ text: '完成候选实现', evidence: [{ row_id: 13, quote: '候选实现已生成' }] }], remaining: [], needs_user: []
  }

  return { task, store, primary, result, resolve: vi.fn(async (_value: string) => primary), load: vi.fn(async (_task: Task, _refresh?: boolean): Promise<TaskAnalysis | null> => null), open: vi.fn(async () => store) }
})

vi.mock('@/lib/task-overview/analysis', () => ({
  resolveTaskSource: fixture.resolve,
  loadTaskAnalysis: fixture.load,
  cachedTaskAnalysis: (task: { title: string; primary: TaskReference | null; analysis?: { title: string; referenceKey: string; result: unknown } }) =>
    task.analysis?.title === task.title && task.analysis.referenceKey === taskReferenceKey(task.primary) ? task.analysis.result : null
}))

vi.mock('@/lib/task-overview/store', async importOriginal => ({
  ...await importOriginal<typeof TaskStorage>(),
  openTaskStore: fixture.open
}))

beforeEach(() => {
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  setTaskOverviewEnabled(true)
  fixture.open.mockClear()
  fixture.store.list.mockClear()
  fixture.store.create.mockClear()
  fixture.store.replace.mockClear()
  fixture.store.close.mockClear()
  fixture.store.changePrimary.mockClear()
  fixture.store.saveAnalysis.mockReset()
  fixture.resolve.mockReset().mockResolvedValue(fixture.primary)
  fixture.load.mockReset().mockResolvedValue(null)
})

afterEach(() => {
  cleanup()
  setTaskOverviewEnabled(true)
  $sessionStates.set({})
  $sessions.set([])
  $sessionSeenCounts.set({})
  $unreadFinishedMarkers.set({})
})

describe('task overview acceptance', () => {
  it('keeps a newer manual edit when an older analysis save returns late', async () => {
    let release!: (row: TaskStorage.TaskRow) => void
    fixture.store.list.mockResolvedValueOnce([{ task: { ...fixture.task, primary: fixture.primary }, revision: 1 }])
    fixture.load.mockResolvedValueOnce(fixture.result)
    fixture.store.saveAnalysis.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    fixture.store.replace.mockImplementationOnce(async next => ({ task: next, revision: 3 }))
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane /></I18nProvider>)
    await waitFor(() => expect(fixture.store.saveAnalysis).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: '编辑事项' }))
    fireEvent.click(screen.getByText('补充或纠正'))
    fireEvent.change(screen.getByLabelText('说明'), { target: { value: '保留最新人工说明' } })
    fireEvent.click(screen.getByRole('button', { name: '保存事项' }))
    await screen.findByText('保留最新人工说明')
    await act(async () => release({ task: { ...fixture.task, primary: fixture.primary }, revision: 2 }))
    expect(screen.getByText('保留最新人工说明')).toBeTruthy()
  })

  it('locks submission while resolving the source and keeps unresolved tasks unsaved', async () => {
    let reject!: (error: Error) => void
    fixture.resolve.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane /></I18nProvider>)
    await waitFor(() => expect((screen.getByRole('button', { name: '新建事项' }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: '新建事项' }))
    fireEvent.change(screen.getByLabelText('事项名称'), { target: { value: '解析中的事项' } })
    fireEvent.change(screen.getByLabelText('会话链接'), { target: { value: 'bad-session' } })
    const form = screen.getByLabelText('事项名称').closest('form')!
    fireEvent.submit(form)
    fireEvent.submit(form)
    expect(fixture.resolve).toHaveBeenCalledTimes(1)
    await act(async () => reject(new Error('会话不存在')))
    expect((await screen.findByRole('alert')).textContent).toContain('会话不存在')
    expect(fixture.store.create).not.toHaveBeenCalled()
    expect(screen.queryByText('事项存储不可用')).toBeNull()
  })

  it('creates with a captured current source and keeps the task when analysis fails', async () => {
    fixture.store.list.mockResolvedValueOnce([])
    fixture.store.create.mockImplementationOnce(async input => ({ task: { ...fixture.task, ...input }, revision: 1 }))
    fixture.load.mockRejectedValueOnce(new Error('model offline')).mockResolvedValueOnce(null)
    const capture = vi.fn(async () => fixture.primary)
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane captureSession={capture} /></I18nProvider>)
    fireEvent.click(await screen.findByRole('button', { name: '新建事项' }))
    fireEvent.change(screen.getByLabelText('事项名称'), { target: { value: '保留的事项' } })
    fireEvent.click(screen.getByRole('button', { name: '关联当前会话' }))
    await screen.findByText('已关联会话: worker / source-session')
    fireEvent.click(screen.getByRole('button', { name: '保存事项' }))
    expect((await screen.findByRole('alert')).textContent).toContain('model offline')
    expect(screen.getByRole('button', { name: /保留的事项/ })).toBeTruthy()
    expect(screen.queryByText('事项存储不可用')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(fixture.load).toHaveBeenCalledTimes(2))
    expect(fixture.load.mock.calls.map(call => call[1])).toEqual([true, true])
    expect(fixture.resolve).not.toHaveBeenCalled()
    expect(fixture.store.create).toHaveBeenCalledTimes(1)
  })

  it('uses the revision returned by analysis when editing the task', async () => {
    fixture.store.list.mockResolvedValueOnce([{ task: { ...fixture.task, primary: fixture.primary }, revision: 1 }])
    fixture.load.mockResolvedValueOnce(fixture.result)
    fixture.store.saveAnalysis.mockResolvedValueOnce({ task: { ...fixture.task, primary: fixture.primary }, revision: 7 })
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane /></I18nProvider>)
    await waitFor(() => expect(fixture.store.saveAnalysis).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: '编辑事项' }))
    fireEvent.change(screen.getByLabelText('事项名称'), { target: { value: '人工修改名称' } })
    fireEvent.click(screen.getByRole('button', { name: '保存事项' }))
    await waitFor(() => expect(fixture.store.replace).toHaveBeenCalledWith(expect.objectContaining({ title: '人工修改名称' }), 7))
  })

  it('reads saved analysis without refreshing and exposes the original evidence', async () => {
    fixture.store.list.mockResolvedValueOnce([{ task: { ...fixture.task, primary: fixture.primary }, revision: 1 }])
    fixture.load.mockResolvedValueOnce(fixture.result)
    fixture.store.saveAnalysis.mockResolvedValueOnce({ task: { ...fixture.task, primary: fixture.primary }, revision: 2 })
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane /></I18nProvider>)
    expect(await screen.findByText('核对交付结果')).toBeTruthy()
    expect(fixture.load).toHaveBeenCalledWith(expect.objectContaining({ id: 'task-1' }), false)
    expect(fixture.load).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getAllByText('查看依据')[0])
    expect(screen.getByText('请核对交付结果原文')).toBeTruthy()
    expect(screen.getByText('消息行 12')).toBeTruthy()
    expect(screen.getByText('分析判断: 待验收')).toBeTruthy()
    expect(screen.getByText('状态: 待做 · 优先级: 普通')).toBeTruthy()
    expect(fixture.store.replace).not.toHaveBeenCalled()
  })

  it('creates from a name and resolved source then analyzes once', async () => {
    fixture.store.list.mockResolvedValueOnce([])
    fixture.store.create.mockImplementationOnce(async input => ({ task: { ...fixture.task, ...input }, revision: 1 }))
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane /></I18nProvider>)
    fireEvent.click(await screen.findByRole('button', { name: '新建事项' }))
    fireEvent.change(screen.getByLabelText('事项名称'), { target: { value: '新事项' } })
    expect((screen.getByRole('button', { name: '保存事项' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByLabelText('说明')).toBeNull()
    fireEvent.change(screen.getByLabelText('会话链接'), { target: { value: '@session:worker/source-session' } })
    fireEvent.click(screen.getByRole('button', { name: '保存事项' }))
    await waitFor(() => expect(fixture.store.create).toHaveBeenCalledWith(expect.objectContaining({ title: '新事项', primary: fixture.primary }), expect.any(Number)))
    await waitFor(() => expect(fixture.load).toHaveBeenCalledWith(expect.objectContaining({ title: '新事项', primary: fixture.primary }), true))
    expect(fixture.resolve).toHaveBeenCalledWith('@session:worker/source-session')
    expect(screen.queryByText('事项存储不可用')).toBeNull()
  })

  it('does not submit a second task while the first creation is pending', async () => {
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane /></I18nProvider>)
    await screen.findByRole('button', { name: /核对事项/ })
    fireEvent.click(screen.getByRole('button', { name: '新建事项' }))
    fireEvent.change(screen.getByLabelText('事项名称'), { target: { value: '一次提交' } })
    fireEvent.change(screen.getByLabelText('会话链接'), { target: { value: 'source-session' } })
    const save = screen.getByRole('button', { name: '保存事项' })
    fireEvent.click(save)
    fireEvent.click(save)
    await waitFor(() => expect(fixture.store.create).toHaveBeenCalledTimes(1))
  })

  it('requires an explicit acceptance note before recording completion', async () => {
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane /></I18nProvider>)
    await screen.findByRole('button', { name: /核对事项/ })

    fireEvent.click(screen.getByRole('button', { name: '标记已验收' }))

    const note = await screen.findByLabelText('验收说明')
    expect(fixture.store.replace).not.toHaveBeenCalled()
    fireEvent.change(note, { target: { value: '核对了实际结果' } })
    fireEvent.click(screen.getByRole('button', { name: '确认已验收' }))

    await waitFor(() => expect(fixture.store.replace).toHaveBeenCalledOnce())
    expect(fixture.store.replace).toHaveBeenCalledWith(
      expect.objectContaining({
        resolution: expect.objectContaining({ state: 'accepted', note: '核对了实际结果' }),
        status: 'done'
      }),
      1
    )
  })

  it('keeps an accepted task visible and allows tracking it again', async () => {
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane /></I18nProvider>)
    await screen.findByRole('button', { name: /核对事项/ })
    fireEvent.click(screen.getByRole('button', { name: '标记已验收' }))
    fireEvent.change(screen.getByLabelText('验收说明'), { target: { value: '核对了实际结果' } })
    fireEvent.click(screen.getByRole('button', { name: '确认已验收' }))
    await waitFor(() => expect(fixture.store.replace).toHaveBeenCalledTimes(1))

    fireEvent.click(await screen.findByRole('button', { name: /已完成/ }))
    expect(screen.getByRole('button', { name: /核对事项/ })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '重新跟踪' }))

    await waitFor(() => expect(fixture.store.replace).toHaveBeenCalledTimes(2))
    expect(fixture.store.replace).toHaveBeenLastCalledWith(
      expect.objectContaining({ resolution: { state: 'open' }, status: 'todo' }),
      2
    )
  })

  it('opens a witnessed source and preserves the task when the source is unavailable', async () => {
    const primary: TaskReference = {
      storedId: 'session-a', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    fixture.store.list.mockResolvedValueOnce([{ task: { ...fixture.task, primary }, revision: 1 }])
    const openSession = vi.fn().mockRejectedValueOnce(new Error('来源暂时不可用')).mockResolvedValue(undefined)
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane openSession={openSession} /></I18nProvider>)
    fireEvent.click(await screen.findByRole('button', { name: '打开来源会话' }))
    expect((await screen.findByRole('alert')).textContent).toContain('来源暂时不可用')
    expect(screen.getByRole('button', { name: /核对事项/ })).toBeTruthy()
    expect(fixture.store.replace).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '重试打开' }))
    await waitFor(() => expect(openSession).toHaveBeenCalledTimes(2))
    expect(openSession).toHaveBeenLastCalledWith(primary)
  })

  it('keeps legacy references and disables opening without a live owner witness', async () => {
    const primary: TaskReference = { storedId: 'legacy-session', unverified: true }
    fixture.store.list.mockResolvedValueOnce([{ task: { ...fixture.task, primary }, revision: 1 }])
    const openSession = vi.fn()
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane openSession={openSession} /></I18nProvider>)
    expect(await screen.findByText('legacy-session')).toBeTruthy()
    expect((screen.getByRole('button', { name: '打开来源会话' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/来源尚未核实/)).toBeTruthy()
    expect(openSession).not.toHaveBeenCalled()
  })

  it('requires confirmation and rereads the captured owner before linking a source', async () => {
    const primary: TaskReference = {
      storedId: 'session-a', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    const captureSession = vi.fn(async () => primary)
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane captureSession={captureSession} /></I18nProvider>)
    fireEvent.click(await screen.findByRole('button', { name: '关联当前会话' }))
    await screen.findByRole('alertdialog', { name: '确认关联' })
    expect(fixture.store.changePrimary).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认关联' }))
    await waitFor(() => expect(fixture.store.changePrimary).toHaveBeenCalledOnce())
    expect(captureSession).toHaveBeenCalledTimes(2)
    expect(fixture.store.changePrimary).toHaveBeenCalledWith('task-1', 1, primary, expect.any(Number))
    expect(fixture.store.replace).not.toHaveBeenCalled()
    expect(await screen.findByText('session-a')).toBeTruthy()
  })

  it('refuses to relink after the focused source changes during confirmation', async () => {
    const reference: TaskReference = {
      storedId: 'session-a', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    const captureSession = vi.fn().mockResolvedValueOnce(reference).mockResolvedValueOnce({ ...reference, storedId: 'session-b' })
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane captureSession={captureSession} /></I18nProvider>)
    fireEvent.click(await screen.findByRole('button', { name: '关联当前会话' }))
    fireEvent.click(await screen.findByRole('button', { name: '确认关联' }))
    expect((await screen.findByRole('alert')).textContent).toContain('无法关联会话')
    expect(fixture.store.changePrimary).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /核对事项/ })).toBeTruthy()
  })

  it('does not apply a late source capture to a different selected task', async () => {
    const primary: TaskReference = {
      storedId: 'session-a', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    let resolveCapture!: (reference: TaskReference) => void
    const captureSession = vi.fn(() => new Promise<TaskReference>(resolve => { resolveCapture = resolve }))
    fixture.store.list.mockResolvedValueOnce([
      { task: fixture.task, revision: 1 },
      { task: { ...fixture.task, id: 'task-2', title: '另一事项' }, revision: 1 }
    ])
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane captureSession={captureSession} /></I18nProvider>)
    fireEvent.click(await screen.findByRole('button', { name: '关联当前会话' }))
    fireEvent.click(screen.getByRole('button', { name: /另一事项/ }))
    await act(async () => resolveCapture(primary))
    expect(screen.queryByRole('alertdialog', { name: '确认关联' })).toBeNull()
    expect(fixture.store.changePrimary).not.toHaveBeenCalled()
  })

  it('edits manual fields while preserving the source and acceptance state', async () => {
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane /></I18nProvider>)
    await screen.findByRole('button', { name: /核对事项/ })
    fireEvent.click(screen.getByRole('button', { name: '编辑事项' }))
    fireEvent.change(screen.getByLabelText('事项名称'), { target: { value: '更新后的事项' } })
    fireEvent.click(screen.getByText('补充或纠正'))
    fireEvent.change(screen.getByLabelText('说明'), { target: { value: '业务目标' } })
    fireEvent.change(screen.getByLabelText('备注'), { target: { value: '需要核对的证据' } })
    fireEvent.change(screen.getByLabelText('剩余步骤'), { target: { value: '核对输出\n完成审查' } })
    fireEvent.click(screen.getByRole('button', { name: '保存事项' }))
    await waitFor(() => expect(fixture.store.replace).toHaveBeenCalledWith(expect.objectContaining({
      title: '更新后的事项', description: '业务目标', notes: '需要核对的证据', nextSteps: ['核对输出', '完成审查'],
      primary: fixture.task.primary, resolution: { state: 'open' }
    }), 1))
    expect(screen.getByText('业务目标')).toBeTruthy()
    expect(screen.getByText('需要核对的证据')).toBeTruthy()
  })

  it('searches notes without losing stored tasks', async () => {
    fixture.store.list.mockResolvedValueOnce([
      { task: { ...fixture.task, notes: 'needle-only-in-notes' }, revision: 1 },
      { task: { ...fixture.task, id: 'task-2', title: '另一事项' }, revision: 1 }
    ])
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane /></I18nProvider>)
    await screen.findByRole('button', { name: /另一事项/ })
    fireEvent.change(screen.getByLabelText('搜索事项'), { target: { value: 'needle-only-in-notes' } })
    expect(screen.queryByRole('button', { name: /另一事项/ })).toBeNull()
    expect(screen.getByRole('button', { name: /核对事项/ })).toBeTruthy()
    fireEvent.change(screen.getByLabelText('搜索事项'), { target: { value: '' } })
    expect(screen.getByRole('button', { name: /另一事项/ })).toBeTruthy()
    expect(fixture.store.replace).not.toHaveBeenCalled()
  })

  it('confirms adding a related session without replacing the primary source', async () => {
    const reference: TaskReference = {
      storedId: 'related-session', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    const captureSession = vi.fn(async () => reference)
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane captureSession={captureSession} /></I18nProvider>)
    fireEvent.click(await screen.findByRole('button', { name: '添加当前会话为相关来源' }))
    fireEvent.click(await screen.findByRole('button', { name: '确认关联' }))
    await waitFor(() => expect(fixture.store.replace).toHaveBeenCalledWith(expect.objectContaining({
      primary: null, related: [reference], nextSteps: fixture.task.nextSteps, resolution: { state: 'open' }
    }), 1))
    expect(fixture.store.changePrimary).not.toHaveBeenCalled()
  })

  it('disables opening when the navigation callback is absent', async () => {
    const primary: TaskReference = {
      storedId: 'session-a', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    fixture.store.list.mockResolvedValueOnce([{ task: { ...fixture.task, primary }, revision: 1 }])
    render(<I18nProvider configClient={null} initialLocale="zh"><TaskOverviewPane /></I18nProvider>)
    expect((await screen.findByRole('button', { name: '打开来源会话' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('renders source-bound running and waiting activity for a related matter', async () => {
    const primary: TaskReference = {
      storedId: 'session-a', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    fixture.store.list.mockResolvedValueOnce([{ task: { ...fixture.task, primary }, revision: 1 }])
    $sessionStates.set({ runtime: {
      storedSessionId: 'session-a', sourceOwner: primary.expectedOwner, adoptedRunningTurn: false, awaitingResponse: false,
      branch: '', busy: true, cwd: '', fast: false, heartbeatSettledStreamId: null, interimBoundaryPending: false,
      interrupted: false, messages: [], model: '', needsInput: false, pendingBranchGroup: null, personality: '', provider: '',
      reasoningEffort: '', runtimeStartedAt: 1, sawAssistantPayload: false, serviceTier: '', streamId: null,
      turnAdmissionId: null, turnAnchorId: null, turnLive: true, turnStartedAt: 1, usage: null, yolo: false
    } as never })
    render(<I18nProvider configClient={null} initialLocale="en"><TaskOverviewPane /></I18nProvider>)
    expect(await screen.findByText('Source activity: Running')).toBeTruthy()
    await act(async () => { $sessionStates.set({ runtime: { ...$sessionStates.get().runtime, needsInput: true, busy: false, turnLive: false } as never }) })
    expect(await screen.findByText('Source activity: Waiting for input')).toBeTruthy()
  })

  it('renders persisted unread activity when the known source has no mounted runtime', async () => {
    const primary: TaskReference = {
      storedId: 'session-a', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    fixture.store.list.mockResolvedValueOnce([{ task: { ...fixture.task, primary }, revision: 1 }])
    $sessions.set([makeSessionInfo({ connection_id: 'source-a', id: 'session-a', profile: 'worker', message_count: 5, unread: true })])
    $unreadFinishedMarkers.set({ worker: ['session-a'] })
    render(<I18nProvider configClient={null} initialLocale="en"><TaskOverviewPane /></I18nProvider>)

    expect(await screen.findByText('Source activity: Finished — unread')).toBeTruthy()
  })

  it('disables source navigation after the loaded owner is replaced', async () => {
    const primary: TaskReference = {
      storedId: 'session-a', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    fixture.store.list.mockResolvedValueOnce([{ task: { ...fixture.task, primary }, revision: 1 }])
    $sessionStates.set({ runtime: { storedSessionId: primary.storedId, sourceOwner: { ...primary.expectedOwner, instanceId: 'replacement' } } as never })
    const openSession = vi.fn(async () => {})
    render(<I18nProvider configClient={null} initialLocale="en"><TaskOverviewPane attentionOnly openSession={openSession} /></I18nProvider>)
    expect((await screen.findByRole('button', { name: 'Open source session' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('Source activity: Source unavailable')).toBeTruthy()
    expect(openSession).not.toHaveBeenCalled()
    expect(fixture.store.replace).not.toHaveBeenCalled()
  })

  it('filters attention by source activity and reacts when a source disappears', async () => {
    const primary: TaskReference = {
      storedId: 'idle-source', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    const unread = { ...primary, storedId: 'unread-source' }
    fixture.store.list.mockResolvedValueOnce([
      { task: { ...fixture.task, primary, title: 'Idle matter' }, revision: 1 },
      { task: { ...fixture.task, id: 'unread', primary: unread, title: 'Unread matter' }, revision: 1 }
    ])
    $sessions.set([makeSessionInfo({ connection_id: 'source-a', id: primary.storedId, profile: 'worker' }),
      makeSessionInfo({ connection_id: 'source-a', id: unread.storedId, profile: 'worker', unread: true })])
    $unreadFinishedMarkers.set({ worker: [unread.storedId] })
    render(<I18nProvider configClient={null} initialLocale="en"><TaskOverviewPane attentionOnly /></I18nProvider>)
    expect(await screen.findByRole('button', { name: /Unread matter/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Idle matter/ })).toBeNull()

    await act(async () => { $sessions.set([makeSessionInfo({ connection_id: 'source-a', id: unread.storedId, profile: 'worker', unread: true })]) })
    expect(await screen.findByRole('button', { name: /Idle matter/ })).toBeTruthy()
    fireEvent.click(screen.getByRole('combobox', { name: 'Source activity' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Finished — unread' }))
    expect(screen.getByRole('button', { name: /Unread matter/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Idle matter/ })).toBeNull()
    expect(fixture.store.replace).not.toHaveBeenCalled()
  })

  it('includes unavailable sources but excludes idle and unbound matters from attention', async () => {
    const primary: TaskReference = {
      storedId: 'idle-source', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    fixture.store.list.mockResolvedValueOnce([
      { task: { ...fixture.task, primary, title: 'Idle matter' }, revision: 1 },
      { task: { ...fixture.task, id: 'missing', primary: { ...primary, storedId: 'missing-source' }, title: 'Missing source' }, revision: 1 },
      { task: { ...fixture.task, id: 'unbound', primary: null, title: 'Unbound matter' }, revision: 1 }
    ])
    $sessions.set([makeSessionInfo({ connection_id: 'source-a', id: primary.storedId, profile: 'worker' })])
    render(<I18nProvider configClient={null} initialLocale="en"><TaskOverviewPane attentionOnly /></I18nProvider>)

    expect(await screen.findByRole('button', { name: /Missing source/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Idle matter/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Unbound matter/ })).toBeNull()
    expect(screen.getByText('Source activity: Source unavailable')).toBeTruthy()
  })

  it('keeps related unread sources in attention and opens their saved reference', async () => {
    const primary: TaskReference = {
      storedId: 'idle-source', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    const related = { ...primary, storedId: 'unread-source' }
    fixture.store.list.mockResolvedValueOnce([{ task: { ...fixture.task, primary, related: [related] }, revision: 1 }])
    $sessions.set([makeSessionInfo({ connection_id: 'source-a', id: primary.storedId, profile: 'worker' }),
      makeSessionInfo({ connection_id: 'source-a', id: related.storedId, profile: 'worker', unread: true })])
    $unreadFinishedMarkers.set({ worker: [related.storedId] })
    const openSession = vi.fn(async () => {})
    render(<I18nProvider configClient={null} initialLocale="en"><TaskOverviewPane attentionOnly openSession={openSession} /></I18nProvider>)

    expect(await screen.findByRole('button', { name: /核对事项/ })).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: 'Open source session' })[1])
    await waitFor(() => expect(openSession).toHaveBeenCalledWith(related))
    expect(fixture.store.replace).not.toHaveBeenCalled()
  })

  it('reloads newly tracked matters when the review view gives way to attention', async () => {
    const primary: TaskReference = {
      storedId: 'missing-source', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    fixture.store.list.mockResolvedValueOnce([]).mockResolvedValueOnce([{ task: { ...fixture.task, primary }, revision: 1 }])
    const { rerender } = render(<I18nProvider configClient={null} initialLocale="en"><TaskOverviewPane /></I18nProvider>)
    await waitFor(() => expect(fixture.store.list).toHaveBeenCalledOnce())
    rerender(<I18nProvider configClient={null} initialLocale="en"><TaskOverviewPane visible={false} /></I18nProvider>)
    rerender(<I18nProvider configClient={null} initialLocale="en"><TaskOverviewPane attentionOnly visible /></I18nProvider>)
    expect(await screen.findByRole('button', { name: /核对事项/ })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Attention' })).toBeTruthy()
  })

  it('shows only active source matters in the attention view', async () => {
    const primary: TaskReference = {
      storedId: 'session-a', connectionId: 'source-a', profile: 'default', targetProfile: 'worker', mode: 'remote',
      expectedOwner: { instanceId: 'owner-a', profileId: '/profiles/worker', authorityEpoch: 3, storeInstanceId: 'store-a' }
    }

    fixture.store.list.mockResolvedValueOnce([
      { task: { ...fixture.task, primary }, revision: 1 },
      { task: { ...fixture.task, id: 'task-idle', title: '无来源事项', primary: null }, revision: 1 }
    ])
    $sessionStates.set({ runtime: {
      storedSessionId: 'session-a', sourceOwner: primary.expectedOwner, adoptedRunningTurn: false, awaitingResponse: false,
      branch: '', busy: true, cwd: '', fast: false, heartbeatSettledStreamId: null, interimBoundaryPending: false,
      interrupted: false, messages: [], model: '', needsInput: false, pendingBranchGroup: null, personality: '', provider: '',
      reasoningEffort: '', runtimeStartedAt: 1, sawAssistantPayload: false, serviceTier: '', streamId: null,
      turnAdmissionId: null, turnAnchorId: null, turnLive: true, turnStartedAt: 1, usage: null, yolo: false
    } as never })

    render(<I18nProvider configClient={null} initialLocale="en"><TaskOverviewPane attentionOnly /></I18nProvider>)

    expect(await screen.findByRole('button', { name: /核对事项/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: '无来源事项' })).toBeNull()
    expect(screen.getByText('Source activity: Running')).toBeTruthy()
  })
})
