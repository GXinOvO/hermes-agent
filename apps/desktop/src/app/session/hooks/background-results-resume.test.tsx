import type { BackgroundResult, GatewayEvent } from '@hermes/shared'
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, renderHook } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { hydrateStoredSessionTranscript } from '@/app/contrib/hooks/use-background-sync'
import { getLatestSessionMessages } from '@/hermes'
import { chatMessageText, toChatMessages } from '@/lib/chat-messages'
import { $activeGatewayProfile } from '@/store/profile'
import {
  $messages,
  _resetSessionOwnerHintsForTests,
  setActiveSessionId,
  setAwaitingResponse,
  setBusy,
  setConnection,
  setMessages,
  setSelectedStoredSessionId,
  setSessions
} from '@/store/session'
import { clearAllSessionStates } from '@/store/session-states'
import type { SessionMessage, SessionResumeResult } from '@/types/hermes'

import { useMessageStream } from './use-message-stream'
import { useSessionActions } from './use-session-actions'
import { useSessionStateCache } from './use-session-state-cache'

vi.mock('@/hermes', async original => ({
  ...(await original<Record<string, unknown>>()),
  getLatestSessionMessages: vi.fn()
}))
vi.mock('@/store/profile', async original => ({
  ...(await original<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

const noop = async () => undefined
const history: SessionMessage[] = [{ id: 1, role: 'user', content: 'parent question', timestamp: 1 }]

const backgroundResults: BackgroundResult[] = [
  { event_type: 'background.complete', payload: { task_id: 'bg-saved', text: 'sealed background answer' } },
  {
    event_type: 'btw.complete',
    payload: { task_id: 'btw-saved', text: 'sealed side answer', question: 'side question' }
  }
]

function snapshot(id = 'a', results: BackgroundResult[] = backgroundResults): SessionResumeResult {
  return {
    session_id: `runtime-${id}`,
    resumed: id,
    session_key: id,
    running: false,
    message_count: 1,
    messages: [],
    messages_omitted: true,
    background_results: results
  }
}

function mount() {
  const navigate = vi.fn()
  const requestGateway = vi.fn().mockResolvedValue(snapshot())

  const hook = renderHook(() => {
    const busyRef = useRef(false)

    const cache = useSessionStateCache({
      activeSessionId: null,
      selectedStoredSessionId: null,
      busyRef,
      setMessages,
      setBusy,
      setAwaitingResponse
    })

    const actions = useSessionActions({
      ...cache,
      activeSessionId: null,
      selectedStoredSessionId: null,
      busyRef,
      creatingSessionRef: useRef(false),
      getRouteToken: () => 'route',
      getRoutedStoredSessionId: () => null,
      navigate,
      requestGateway,
      routedSessionId: null
    })

    const stream = useMessageStream({
      ...cache,
      queryClient: useRef(new QueryClient()).current,
      hydrateFromStoredSession: noop,
      refreshHermesConfig: noop,
      refreshSessions: noop
    })

    return { cache, actions, stream }
  })

  return Object.assign(hook, { requestGateway, navigate })
}

beforeEach(() => {
  localStorage.clear()
  clearAllSessionStates()
  _resetSessionOwnerHintsForTests()
  $activeGatewayProfile.set('default')
  setConnection(null)
  setMessages([])
  setActiveSessionId(null)
  setSelectedStoredSessionId(null)
  setBusy(false)
  setAwaitingResponse(false)
  setSessions(
    ['a', 'b'].map(id => ({
      id,
      title: id,
      source: 'desktop',
      message_count: 1,
      tool_call_count: 0,
      is_active: true,
      started_at: 1,
      last_active: 1,
      ended_at: null,
      model: null,
      preview: null,
      input_tokens: 0,
      output_tokens: 0
    }))
  )
  vi.mocked(getLatestSessionMessages)
    .mockReset()
    .mockImplementation(async id => ({ session_id: id, messages: history }))
})

afterEach(() => {
  cleanup()
  clearAllSessionStates()
  localStorage.clear()
  setSessions([])
  setMessages([])
  setActiveSessionId(null)
  setSelectedStoredSessionId(null)
  vi.restoreAllMocks()
})

it('a cold resume restores sealed results after REST history and keeps model messages untouched', async () => {
  const h = mount()
  const saved = snapshot()
  h.requestGateway.mockResolvedValue(saved)
  const original = JSON.stringify(saved)
  await act(async () => {
    await h.result.current.actions.resumeSession('a', true)
  })

  expect($messages.get().map(chatMessageText)).toEqual([
    'parent question',
    '[bg bg-saved]\nsealed background answer',
    '[btw "side question" (btw-saved)]\nsealed side answer'
  ])
  expect(
    $messages
      .get()
      .slice(1)
      .map(row => row.role)
  ).toEqual(['system', 'system'])
  expect(h.result.current.cache.sessionStateByRuntimeIdRef.current.get('runtime-a')?.busy).toBe(false)
  expect(JSON.stringify(saved)).toBe(original)
  expect(h.requestGateway.mock.calls.map(([method]) => method)).toEqual(['session.resume'])
  expect(h.navigate).not.toHaveBeenCalled()
})

it('warm activation, repeated resume and live delivery keep one row per task through later hydration', async () => {
  const h = mount()
  await act(async () => {
    await h.result.current.actions.resumeSession('a', true)
  })
  const emit = (event: GatewayEvent) => act(() => h.result.current.stream.handleGatewayEvent(event))

  for (const result of backgroundResults) {
    emit({ type: result.event_type, session_id: 'runtime-a', payload: { ...result.payload } })
    emit({ type: result.event_type, session_id: 'runtime-a', payload: { ...result.payload, text: 'redelivered text' } })
  }

  await act(async () => {
    await h.result.current.actions.resumeSession('a', true)
  })
  expect(h.requestGateway.mock.calls.map(([method]) => method)).toContain('session.activate')
  await act(async () => {
    await hydrateStoredSessionTranscript({
      attempts: 1,
      storedSessionId: 'a',
      runtimeSessionId: 'runtime-a',
      storedProfile: 'default',
      updateSessionState: h.result.current.cache.updateSessionState
    })
  })
  expect(
    $messages
      .get()
      .filter(row => row.role === 'system')
      .map(chatMessageText)
  ).toEqual(['[bg bg-saved]\nsealed background answer', '[btw "side question" (btw-saved)]\nsealed side answer'])
  expect(new Set($messages.get().map(row => row.id)).size).toBe($messages.get().length)
})

it('a live result received while activate waits for REST survives that hydrate without another snapshot copy', async () => {
  const h = mount()
  h.requestGateway.mockResolvedValue(snapshot('a', []))
  await act(async () => {
    await h.result.current.actions.resumeSession('a', true)
  })
  let resolveRest!: (value: { session_id: string; messages: typeof history }) => void
  vi.mocked(getLatestSessionMessages).mockImplementationOnce(
    () =>
      new Promise(resolve => {
        resolveRest = resolve
      })
  )
  let resumed!: Promise<void>
  act(() => {
    resumed = h.result.current.actions.resumeSession('a', true)
  })
  await vi.waitFor(() => expect(resolveRest).toBeTypeOf('function'))
  act(() =>
    h.result.current.stream.handleGatewayEvent({
      type: 'btw.complete',
      session_id: 'runtime-a',
      payload: { task_id: 'late-btw', text: 'arrived during hydrate', question: 'q' }
    })
  )
  await act(async () => {
    resolveRest({ session_id: 'a', messages: history })
    await resumed
  })
  expect($messages.get().map(chatMessageText)).toContain('[btw "q" (late-btw)]\narrived during hydrate')
})

it('switching sessions and a foreign profile event cannot carry sealed answers into the new view', async () => {
  const h = mount()
  await act(async () => {
    await h.result.current.actions.resumeSession('a', true)
  })
  h.requestGateway.mockResolvedValue(snapshot('b', []))
  await act(async () => {
    await h.result.current.actions.resumeSession('b', true)
  })
  act(() =>
    h.result.current.stream.handleGatewayEvent({
      type: 'background.complete',
      session_id: 'runtime-a',
      profile: 'other-profile',
      payload: { task_id: 'foreign', text: 'foreign result' }
    })
  )
  expect($messages.get().map(chatMessageText)).toEqual(['parent question'])
  h.requestGateway.mockResolvedValue(snapshot())
  await act(async () => {
    await h.result.current.actions.resumeSession('a', true)
  })
  expect($messages.get().filter(row => row.role === 'system')).toHaveLength(2)
  expect($messages.get().map(chatMessageText).join('\n')).not.toContain('foreign result')
})

it('a recycled runtime binding cannot carry the previous session sealed results', async () => {
  const h = mount()
  await act(async () => {
    await h.result.current.actions.resumeSession('a', true)
  })
  let rebound: ReturnType<typeof h.result.current.cache.updateSessionState> | undefined
  act(() => {
    rebound = h.result.current.cache.updateSessionState(
      'runtime-a',
      state => ({
        ...state,
        messages: toChatMessages(history)
      }),
      'b'
    )
  })
  expect(rebound?.storedSessionId).toBe('b')
  expect(rebound?.messages.map(chatMessageText)).toEqual(['parent question'])
})

it('fresh cache activation restores snapshot-only results and ignores hidden preview results', async () => {
  const h = mount()
  h.requestGateway.mockResolvedValue(snapshot('a', []))
  await act(async () => {
    await h.result.current.actions.resumeSession('a', true)
  })
  h.requestGateway.mockResolvedValue(
    snapshot('a', [
      ...backgroundResults,
      { event_type: 'preview.restart.complete', payload: { task_id: 'hidden-preview', text: 'hidden restart answer' } },
      { event_type: 'background.complete', payload: { task_id: 'blank', text: '   ' } }
    ])
  )
  await act(async () => {
    await h.result.current.actions.resumeSession('a', true)
  })
  expect(
    $messages
      .get()
      .filter(row => row.role === 'system')
      .map(chatMessageText)
  ).toEqual(['[bg bg-saved]\nsealed background answer', '[btw "side question" (btw-saved)]\nsealed side answer'])
})

it('an older snapshot without the optional result cache keeps live results through hydration', async () => {
  const h = mount()
  const legacy = snapshot('a', [])
  delete legacy.background_results
  h.requestGateway.mockResolvedValue(legacy)
  await act(async () => {
    await h.result.current.actions.resumeSession('a', true)
  })
  act(() =>
    h.result.current.stream.handleGatewayEvent({
      type: 'background.complete',
      session_id: 'runtime-a',
      payload: { task_id: 'legacy-bg', text: 'live answer' }
    })
  )
  await act(async () => {
    await h.result.current.actions.resumeSession('a', true)
  })
  expect(
    $messages
      .get()
      .filter(row => row.role === 'system')
      .map(chatMessageText)
  ).toEqual(['[bg legacy-bg]\nlive answer'])
})
