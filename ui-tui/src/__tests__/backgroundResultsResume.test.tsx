import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import type { AnyGatewayEvent } from '@hermes/shared/gateway-types'
import React, { useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { turnController } from '../app/turnController.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { useSessionLifecycle } from '../app/useSessionLifecycle.js'
import { GatewayClient } from '../gatewayClient.js'
import type { Msg } from '../types.js'

const noop = () => {}

const backgroundResults = [
  { event_type: 'background.complete', payload: { task_id: 'saved-bg', text: 'sealed background answer' } },
  {
    event_type: 'btw.complete',
    payload: { task_id: 'saved-btw', text: 'sealed side answer', question: 'side question' }
  }
] as const

const saved = (id = 'a', results: readonly unknown[] = backgroundResults) => ({
  session_id: id,
  stored_session_id: id,
  subscription_id: `sub-${id}`,
  authority_epoch: 1,
  execution_generation: 2,
  status: 'idle',
  running: false,
  info: { model: 'test', tools: {}, skills: {}, stored_session_id: id, execution_epoch: '1', execution_generation: 2 },
  messages: [{ role: 'user', text: 'parent question' }],
  background_results: results,
  prompts: []
})

function mount(canonical = true) {
  const gw = new GatewayClient()
  gw.isCanonical = canonical

  const request = vi
    .spyOn(gw, 'request')
    .mockImplementation(async (method, params) =>
      method === 'session.resume' || method === 'session.activate'
        ? saved(String(params?.session_id), params?.session_id === 'b' ? [] : backgroundResults)
        : { detached: true }
    )

  const rpc = (method: string, params: Record<string, unknown>) => gw.request(method, params)
  let lifecycle!: ReturnType<typeof useSessionLifecycle>
  let rows: Msg[] = []
  let handler: ReturnType<typeof createGatewayEventHandler> | undefined

  function Harness() {
    const [history, setHistoryItems] = useState<Msg[]>([])
    rows = history
    const sys = (text: string) => setHistoryItems(previous => [...previous, { role: 'system', text }])

    if (!handler) {
      handler = createGatewayEventHandler({
        composer: { dequeue: () => undefined, queueEditRef: { current: null }, sendQueued: noop, setInput: noop },
        gateway: { gw, rpc },
        session: {
          STARTUP_RESUME_ID: '',
          colsRef: { current: 80 },
          newSession: noop,
          resetSession: noop,
          resumeById: noop,
          setCatalog: noop
        },
        submission: { submitRef: { current: noop } },
        system: { bellOnComplete: false, sys },
        transcript: { appendMessage: (msg: Msg) => setHistoryItems(p => [...p, msg]), panel: noop, setHistoryItems },
        voice: { setProcessing: noop, setRecording: noop, setVoiceEnabled: noop }
      } as any)
      gw.on('event', handler)
      gw.drain()
    }

    lifecycle = useSessionLifecycle({
      colsRef: { current: 80 },
      composerActions: { setComposerTokens: noop },
      gw,
      rpc,
      scrollRef: { current: null },
      panel: noop,
      sys,
      setHistoryItems,
      setLastUserMsg: noop,
      setSessionStartedAt: noop,
      setStickyPrompt: noop,
      setVoiceProcessing: noop,
      setVoiceRecording: noop
    } as any)

    return <Text>{history.map(row => row.text).join('\n')}</Text>
  }

  const instance = renderSync(<Harness />, {
    stdin: new PassThrough() as any,
    stdout: Object.assign(new PassThrough(), { columns: 80, rows: 20, isTTY: false }) as any,
    stderr: new PassThrough() as any,
    patchConsole: false
  })

  return {
    gw,
    request,
    get lifecycle() {
      return lifecycle
    },
    get rows() {
      return rows
    },
    cleanup() {
      instance.unmount()
      gw.kill()
    }
  }
}

beforeEach(() => {
  resetUiState()
  vi.stubEnv('HERMES_TUI_ACTIVE_SESSION_FILE', '')
})
afterEach(() => {
  turnController.fullReset()
  resetUiState()
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

it.each([true, false])(
  'resume restores sealed background and btw lines after reset (canonical=%s)',
  async canonical => {
    const h = mount(canonical)

    try {
      await h.lifecycle.resumeById('a')
      await expect
        .poll(() => h.rows.filter(row => row.text.includes('sealed')).map(row => [row.role, row.text]))
        .toEqual([
          ['system', '[bg saved-bg] sealed background answer'],
          ['system', '[btw "side question"] sealed side answer']
        ])
      expect(getUiState().busy).toBe(false)
      expect(h.request.mock.calls.map(([method]) => method)).not.toContain('prompt.submit')
    } finally {
      h.cleanup()
    }
  }
)

it('repeated resume, live delivery and switch-away/activate restore exactly one row per sealed task', async () => {
  const h = mount()

  try {
    await h.lifecycle.resumeById('a')
    await expect.poll(() => h.rows.filter(row => row.text.includes('sealed')).length).toBe(2)

    for (const result of backgroundResults) {
      h.gw.publishLocalEvent({
        type: result.event_type,
        session_id: 'a',
        payload: { ...result.payload }
      } as AnyGatewayEvent)
      h.gw.publishLocalEvent({
        type: result.event_type,
        session_id: 'a',
        payload: { ...result.payload, text: 'redelivered text' }
      } as AnyGatewayEvent)
    }

    await new Promise(resolve => setImmediate(resolve))
    expect(h.rows.filter(row => row.backgroundResultId).map(row => row.text)).toEqual([
      '[bg saved-bg] sealed background answer',
      '[btw "side question"] sealed side answer'
    ])

    await h.lifecycle.resumeById('a')
    await expect.poll(() => h.rows.filter(row => row.text.includes('sealed')).length).toBe(2)
    h.lifecycle.activateLiveSession('b')
    await expect.poll(() => getUiState().sid).toBe('b')
    await expect.poll(() => h.rows.some(row => row.text.includes('sealed'))).toBe(false)
    h.gw.publishLocalEvent({
      type: 'background.complete',
      session_id: 'a',
      payload: { task_id: 'foreign', text: 'foreign result' }
    })
    h.lifecycle.activateLiveSession('a')
    await expect.poll(() => h.rows.filter(row => row.text.includes('sealed')).length).toBe(2)
    expect(h.rows.some(row => row.text.includes('foreign result'))).toBe(false)
  } finally {
    h.cleanup()
  }
})

it('a live event before the resume reply cannot disappear when snapshot hydration resets the view', async () => {
  const h = mount()

  try {
    patchUiState({ sid: 'a' })
    h.request.mockImplementationOnce(async () => {
      h.gw.publishLocalEvent({
        type: 'background.complete',
        session_id: 'a',
        payload: { ...backgroundResults[0].payload }
      })

      return saved()
    })
    await h.lifecycle.resumeById('a')
    await expect.poll(() => h.rows.filter(row => row.text === '[bg saved-bg] sealed background answer').length).toBe(1)
    h.gw.publishLocalEvent({
      type: 'background.complete',
      session_id: 'a',
      payload: { ...backgroundResults[0].payload }
    })
    await new Promise(resolve => setImmediate(resolve))
    expect(h.rows.filter(row => row.text === '[bg saved-bg] sealed background answer')).toHaveLength(1)
  } finally {
    h.cleanup()
  }
})

it('same-session foreign profile events cannot add or suppress a valid sealed task', async () => {
  const h = mount()

  try {
    h.request.mockResolvedValue(saved('a', []))
    await h.lifecycle.resumeById('a')
    patchUiState(state => ({ ...state, info: { ...state.info!, profile_name: 'work' } }))
    h.gw.publishLocalEvent({
      type: 'background.complete',
      session_id: 'a',
      profile: 'foreign',
      payload: { task_id: 'shared-id', text: 'foreign result' }
    })
    h.gw.publishLocalEvent({
      type: 'background.complete',
      session_id: 'a',
      profile: 'work',
      payload: { task_id: 'shared-id', text: 'valid result' }
    })
    await expect
      .poll(() => h.rows.filter(row => row.backgroundResultId).map(row => row.text))
      .toEqual(['[bg shared-id] valid result'])
  } finally {
    h.cleanup()
  }
})

it('fresh activation restores snapshot-only results without mutating parent transcript or busy state', async () => {
  const h = mount()

  try {
    const snapshot = saved()
    const before = JSON.stringify(snapshot)
    h.request.mockResolvedValue(snapshot)
    h.lifecycle.activateLiveSession('a')
    await expect
      .poll(() => h.rows.filter(row => row.backgroundResultId).map(row => row.text))
      .toEqual(['[bg saved-bg] sealed background answer', '[btw "side question"] sealed side answer'])
    expect(getUiState().busy).toBe(false)
    expect(JSON.stringify(snapshot)).toBe(before)
    expect(h.request.mock.calls.map(([method]) => method)).toEqual(['session.activate'])
  } finally {
    h.cleanup()
  }
})

it('public snapshot hydration ignores hidden preview/empty results and remains idempotent without a reset', async () => {
  const h = mount()

  try {
    await h.lifecycle.resumeById('b')
    await expect.poll(() => h.rows.some(row => row.text === 'parent question')).toBe(true)

    const snapshot = saved('b', [
      ...backgroundResults,
      { event_type: 'preview.restart.complete', payload: { task_id: 'preview', text: 'hidden result' } },
      { event_type: 'background.complete', payload: { task_id: 'empty', text: '   ' } }
    ])

    h.gw.hydrateSharedPrompts(snapshot)
    h.gw.hydrateSharedPrompts(snapshot)
    await expect
      .poll(() => h.rows.filter(row => row.backgroundResultId).map(row => row.text))
      .toEqual(['[bg saved-bg] sealed background answer', '[btw "side question"] sealed side answer'])
    expect(getUiState().sid).toBe('b')
    expect(getUiState().busy).toBe(false)
  } finally {
    h.cleanup()
  }
})
