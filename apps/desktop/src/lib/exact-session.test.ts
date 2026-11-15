import { execFileSync, spawn } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { createInterface } from 'node:readline'

import { render, renderHook, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, expect, test, vi } from 'vitest'

import { HermesGateway } from '@/api/client'
import { useSessionTileDelegate } from '@/app/contrib/hooks/use-session-tile-delegate'
import { SessionReviewView } from '@/app/contrib/session-review-pane'
import { I18nProvider } from '@/i18n'
import { createClientSessionState } from '@/lib/chat-runtime'
import { exactSessionOwnerFromWire } from '@/lib/exact-session-owner'
import { setPrimaryGateway, setPrimaryGatewayConnectionId } from '@/store/gateway'
import { $activeSessionId, $selectedStoredSessionId, setSessionOwnerHint } from '@/store/session'
import { $sessionStates, $sessionTiles, publishSessionState, sessionTileDelegate } from '@/store/session-states'

import { captureFocusedSessionReference, openExactSession } from './exact-session'
import { loadFocusedSessionOverview } from './task-overview/focused-review'

afterEach(() => vi.restoreAllMocks())

test('real authority producer → focused capture → saved JSON → guarded exact open', async () => {
  const root = resolve('../..')
  const scratch = await mkdtemp(join(process.env.TMPDIR!, 'exact-loopback-'))

  const python = process.env.HERMES_PYTHON || execFileSync('bash', ['scripts/run-in-hermes-env', 'sh', '-c', 'printf %s "$__HERMES_TEST_PYTHON"'], {
    cwd: root, encoding: 'utf8', env: { ...process.env, HERMES_HOME: undefined }
  })

  const child = spawn(python, ['tests/gateway/exact_session_loopback.py'], {
    cwd: root, env: { ...process.env, HOME: scratch, HERMES_HOME: scratch, TMPDIR: scratch }, stdio: ['pipe', 'pipe', 'pipe']
  })

  let errors = ''
  child.stderr.on('data', chunk => { errors += chunk.toString() })
  const lines = createInterface({ input: child.stdout })
  const replies = new Map<number, (reply: any) => void>()
  let first!: (value: any) => void
  const ready = new Promise<any>(resolve => { first = resolve })
  lines.on('line', line => {
    if (!line.startsWith('{')) { return }
    const reply = JSON.parse(line)

    if (reply.session_id) { first(reply) } else { replies.get(reply.id)?.(reply); replies.delete(reply.id) }
  })
  child.on('exit', code => { if (code) { first(Promise.reject(new Error(errors))) } })
  const wsPackage = 'ws'
  const { WebSocketServer, WebSocket } = await import(wsPackage)
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 })
  await new Promise<void>(resolve => server.once('listening', resolve))
  server.on('connection', (socket: any) => socket.on('message', (bytes: Buffer) => {
    const request = JSON.parse(bytes.toString())
    replies.set(request.id, reply => socket.send(JSON.stringify(reply)))
    child.stdin.write(`${JSON.stringify(request)}\n`)
  }))
  const originalWebSocket = globalThis.WebSocket
  globalThis.WebSocket = WebSocket as any
  const client = new HermesGateway()
  const cache = { current: new Map() }
  const bindings = { current: new Map<string, string>() }

  const hook = renderHook(() => useSessionTileDelegate({
    archiveSession: vi.fn(), branchLoadedSession: vi.fn(), branchStoredSession: vi.fn(),
    executeSlashCommand: vi.fn(), removeSession: vi.fn(), requestGateway: client.request.bind(client),
    runtimeIdByStoredSessionIdRef: bindings, sessionStateByRuntimeIdRef: cache,
    updateSessionState: ((id: string, update: any, storedId?: string) => {
      const next = update(cache.current.get(id) ?? createClientSessionState(storedId))
      cache.current.set(id, next)

      if (storedId) { bindings.current.set(storedId, id) }
      publishSessionState(id, next)
    }) as any
  } as any))

  try {
    const source = await ready
    const address = server.address() as { port: number }
    await client.connect(`ws://127.0.0.1:${address.port}/api/ws?native_dial=fixture&ticket=fixture`)
    setPrimaryGateway(client, 'default')
    setPrimaryGatewayConnectionId('local', 'local')
    setSessionOwnerHint(source.session_id, { connectionId: 'local', profile: 'default', targetProfile: 'default', mode: 'local' })
    $selectedStoredSessionId.set(source.session_id)
    $activeSessionId.set(source.session_id)
    $sessionTiles.set([])
    const resumed = await sessionTileDelegate()!.resumeTile(source.session_id, { authoritativeSnapshot: true })
    expect(resumed).toBe(source.session_id)
    const reference = await captureFocusedSessionReference()
    expect(reference.expectedOwner.instanceId).toBe('loopback-owner')
    expect(reference.expectedOwner.storeInstanceId).toBeTruthy()
    const review = await loadFocusedSessionOverview()
    expect(review.original?.content).toBe('Source transcript must remain visible')
    expect(review.current?.content).toBe('Current requirement must remain separate')
    expect(review.remaining.map(item => item.content)).toEqual(['Check remote adoption'])
    expect(review.registrationStale).toBe(true)
    expect(review.stages.find(stage => stage.id === 'tests')?.evidence).toBe('source-receipt.json')
    expect(review.stages.find(stage => stage.id === 'tests')?.status).toBe('stale')
    render(createElement(I18nProvider, { configClient: null, initialLocale: 'en', children: createElement(SessionReviewView, { value: review }) }))
    expect(screen.getByText('Source transcript must remain visible')).toBeTruthy()
    expect(screen.getByText('Current requirement must remain separate')).toBeTruthy()
    expect(screen.getAllByText('Check remote adoption').length).toBeGreaterThan(0)
    expect(screen.getByText('source-receipt.json')).toBeTruthy()
    expect(screen.getAllByText('Record outdated').length).toBeGreaterThan(0)
    $activeSessionId.set('another-session')
    $selectedStoredSessionId.set('another-session')
    $sessionStates.set({ [resumed]: { ...cache.current.get(resumed), messages: [] } })
    await sessionTileDelegate()!.resumeTile(source.session_id, { exactReference: JSON.parse(JSON.stringify(reference)) })
    expect($sessionStates.get()[resumed].messages.length).toBeGreaterThan(0)
    $activeSessionId.set(resumed)
    $selectedStoredSessionId.set(source.session_id)
    $sessionTiles.set([])
    await openExactSession(JSON.parse(JSON.stringify(reference)))
    expect($sessionStates.get()[resumed].sourceOwner).toEqual(reference.expectedOwner)
    await expect(openExactSession({ ...reference, expectedOwner: { ...reference.expectedOwner, instanceId: 'replacement' } })).rejects.toThrow('另一来源')
    await expect(openExactSession({ ...reference, expectedOwner: undefined } as any)).rejects.toThrow('完整身份证据')
    cache.current.set(resumed, { ...cache.current.get(resumed), sourceOwner: undefined })
    $sessionStates.set({ [resumed]: cache.current.get(resumed) })
    await expect(captureFocusedSessionReference()).rejects.toThrow('缺少已绑定')
    expect(exactSessionOwnerFromWire({ store_instance_id: reference.expectedOwner.storeInstanceId })).toBeUndefined()
  } finally {
    hook.unmount()
    client.close()
    setPrimaryGateway(null)
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    $sessionStates.set({})
    $sessionTiles.set([])
    globalThis.WebSocket = originalWebSocket
    child.stdin.end()
    child.kill()
    lines.close()

    for (const socket of server.clients) { socket.terminate() }
    await new Promise<void>(resolve => server.close(() => resolve()))
    await rm(scratch, { recursive: true, force: true })
  }
}, 60_000)
