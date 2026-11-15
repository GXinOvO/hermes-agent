import { act, cleanup, render } from '@testing-library/react'
import { useLayoutEffect } from 'react'
import { afterEach, expect, test, vi } from 'vitest'

import type { HermesGateway } from '@/api/client'
import { useComposerDraft } from '@/app/chat/composer/hooks/use-composer-draft'
import { createClientSessionState } from '@/lib/chat-runtime'
import { captureFocusedSessionReference } from '@/lib/exact-session'
import { type ExactSessionOwner, exactSessionOwnerToWire } from '@/lib/exact-session-owner'
import { mainComposerScope } from '@/store/composer'
import { closeSecondaryGateways, configureGatewayRegistry, gatewayRouteState, openGatewayForAgent, setPrimaryGateway, setPrimaryGatewayConnectionId } from '@/store/gateway'
import { $activeSessionId, $selectedStoredSessionId, setSessionOwnerHint } from '@/store/session'
import { $sessionStates, $sessionTiles, publishSessionState } from '@/store/session-states'

import { $focusedOverviewKey, appendFocusedOverviewRequest } from './focused-review'

vi.mock('@assistant-ui/react', () => ({
  useAui: () => ({ composer: () => ({ setText: vi.fn() }) }),
  useAuiState: (selector: (state: { composer: { text: string } }) => unknown) => selector({ composer: { text: '' } }),
  useComposerRuntime: () => ({ getState: () => ({ text: '' }), subscribe: () => () => undefined })
}))

const owner = { instanceId: 'owner-a', profileId: '/profile/a', authorityEpoch: 4, storeInstanceId: 'store-a' }
let draft!: ReturnType<typeof useComposerDraft>

function Composer() {
  const value = useComposerDraft({ activeQueueSessionKey: 'same', focusKey: null, inputDisabled: false,
    queueEditRef: { current: null }, sessionId: 'same' })

  useLayoutEffect(() => { draft = value })

  return null
}

afterEach(() => {
  cleanup()
  closeSecondaryGateways()
  setPrimaryGateway(null)
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  $sessionStates.set({})
  $sessionTiles.set([])
  mainComposerScope.clear()
  vi.restoreAllMocks()
})

async function secondary(run: (gateway: HermesGateway, setEpoch: (epoch: string) => void, reconnect: () => Promise<void>, setOwner: (value: ExactSessionOwner) => void) => Promise<void>) {
  const wsPackage = 'ws'
  const { WebSocketServer, WebSocket } = await import(wsPackage)
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 })
  await new Promise<void>(resolve => server.once('listening', resolve))
  let epoch = 'accepted'
  let currentOwner = owner
  server.on('connection', (socket: any) => socket.on('message', (bytes: Buffer) => {
    const frame = JSON.parse(bytes.toString())

    const result = frame.method === 'client.capabilities' ? { server_requests: [] } :
      frame.method === 'session.events.since' ? { events: [], latest_seq: frame.params.last_seen, replay_epoch: frame.params.replay_epoch, snapshot_required: false } :
        { session_id: 'same', stored_session_id: 'same', authority_epoch: currentOwner.authorityEpoch, replay_epoch: epoch,
          owner: exactSessionOwnerToWire(currentOwner), revision: 1, execution_generation: 2, messages: [], prompts: [], info: {} }

    socket.send(JSON.stringify({ jsonrpc: '2.0', id: frame.id, result }))
  }))
  const originalWebSocket = globalThis.WebSocket
  const originalDesktop = window.hermesDesktop
  globalThis.WebSocket = WebSocket as any
  const url = `ws://127.0.0.1:${(server.address() as { port: number }).port}/api/ws?native_dial=fixture&ticket=fixture`
  window.hermesDesktop = {
    getConnectionFor: vi.fn(async () => ({ mode: 'remote', profile: 'worker', wsUrl: url })),
    getGatewayWsUrlFor: vi.fn(async () => url)
  } as any
  configureGatewayRegistry({ onEvent: vi.fn() })
  setPrimaryGateway({ connectionState: 'open' } as HermesGateway)
  setPrimaryGatewayConnectionId('local', 'local')

  try {
    await openGatewayForAgent('secondary-fixture', 'worker')
    const gateway = gatewayRouteState('secondary-fixture', 'worker').gateway!
    await gateway.request('session.resume', { session_id: 'same', profile: 'worker', expected_owner: exactSessionOwnerToWire(owner) })
    setSessionOwnerHint('same', { connectionId: 'secondary-fixture', profile: 'worker', targetProfile: 'worker', mode: 'remote' })
    publishSessionState('same', { ...createClientSessionState('same'), sourceOwner: owner, sourceReplayEpoch: epoch })
    $activeSessionId.set('same')
    $selectedStoredSessionId.set('same')
    $sessionTiles.set([])
    render(<Composer />)
    expect(gateway.sessionOwnerWitness('same', 'worker', epoch)).toEqual(owner)
    await run(gateway, next => { epoch = next }, async () => { gateway.close(); await openGatewayForAgent('secondary-fixture', 'worker') }, next => { currentOwner = next })
  } finally {
    closeSecondaryGateways()
    globalThis.WebSocket = originalWebSocket
    window.hermesDesktop = originalDesktop

    for (const socket of server.clients) { socket.terminate() }
    await new Promise<void>(resolve => server.close(() => resolve()))
  }
}

test('appends for a secondary session while its live owner and replay witness remain valid', async () => secondary(async () => {
  const key = $focusedOverviewKey.get()
  await act(async () => { await expect(appendFocusedOverviewRequest(key, 'current request')).resolves.toBe(true) })
  expect(draft.draftRef.current).toContain('current request')
}))

test('refuses an append started after the same secondary gateway reconnect clears its witness', async () => secondary(async (gateway, _setEpoch, reconnect) => {
  const key = $focusedOverviewKey.get()
  await reconnect()
  expect(gatewayRouteState('secondary-fixture', 'worker').gateway).toBe(gateway)
  expect($focusedOverviewKey.get()).toBe(key)
  expect(gateway.sessionOwnerWitness('same', 'worker', 'accepted')).toBeUndefined()
  await expect(captureFocusedSessionReference()).rejects.toThrow('不一致')
  await act(async () => { await expect(appendFocusedOverviewRequest(key, 'stale request')).resolves.toBe(false) })
  expect(draft.draftRef.current).not.toContain('stale request')
}))

test('refuses a queued append when its secondary witness is invalidated before the event', async () => secondary(async gateway => {
  const key = $focusedOverviewKey.get()
  const pending = appendFocusedOverviewRequest(key, 'queued old request')
  gateway.close()
  expect($focusedOverviewKey.get()).toBe(key)
  await act(async () => { await expect(pending).resolves.toBe(false) })
  expect(draft.draftRef.current).not.toContain('queued old request')
}))

test('refuses an append with a stale state replay epoch after the secondary reattaches', async () => secondary(async (gateway, setEpoch, reconnect) => {
  const key = $focusedOverviewKey.get()
  await reconnect()
  setEpoch('replacement-epoch')
  await gateway.request('session.resume', { session_id: 'same', profile: 'worker', expected_owner: exactSessionOwnerToWire(owner) })
  expect(gateway.sessionOwnerWitness('same', 'worker', 'replacement-epoch')).toEqual(owner)
  expect($focusedOverviewKey.get()).toBe(key)
  await act(async () => { await expect(appendFocusedOverviewRequest(key, 'old replay request')).resolves.toBe(false) })
  expect(draft.draftRef.current).not.toContain('old replay request')
}))

test.each([
  { instanceId: 'replacement-instance' }, { profileId: '/replacement/profile' },
  { authorityEpoch: 5 }, { storeInstanceId: 'replacement-store' }
])('refuses replacement witness with the same session and replay ids: %j', async replacement => secondary(async (gateway, _setEpoch, reconnect, setOwner) => {
  const key = $focusedOverviewKey.get()
  await reconnect()
  const replaced = { ...owner, ...replacement }
  setOwner(replaced)
  await gateway.request('session.resume', { session_id: 'same', profile: 'worker' })
  expect(gateway.sessionOwnerWitness('same', 'worker', 'accepted')).toEqual(replaced)
  expect($focusedOverviewKey.get()).toBe(key)
  await act(async () => { await expect(appendFocusedOverviewRequest(key, 'replaced owner request')).resolves.toBe(false) })
  expect(draft.draftRef.current).not.toContain('replaced owner request')
}))

test('accepts an append after a legal secondary reattach publishes its new replay state', async () => secondary(async (gateway, setEpoch, reconnect) => {
  const previousKey = $focusedOverviewKey.get()
  await reconnect()
  setEpoch('next-epoch')
  await gateway.request('session.resume', { session_id: 'same', profile: 'worker', expected_owner: exactSessionOwnerToWire(owner) })
  publishSessionState('same', { ...$sessionStates.get().same, sourceReplayEpoch: 'next-epoch' })
  expect($focusedOverviewKey.get()).not.toBe(previousKey)
  await act(async () => { await expect(appendFocusedOverviewRequest($focusedOverviewKey.get(), 'reattached request')).resolves.toBe(true) })
  expect(draft.draftRef.current).toContain('reattached request')
}))
