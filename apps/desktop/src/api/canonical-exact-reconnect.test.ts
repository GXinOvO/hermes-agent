// @vitest-environment node
import { expect, test, vi } from 'vitest'

import { exactSessionOwnerToWire } from '@/lib/exact-session-owner'

import { HermesGateway } from './client'

const owner = { instanceId: 'owner-a', profileId: '/profile/a', authorityEpoch: 4, storeInstanceId: 'store-a' }
const wireOwner = exactSessionOwnerToWire(owner)

test.each(['same owner', 'replacement resume', 'replacement replay'] as const)('exact source reconnect isolates %s', async outcome => {
  const wsPackage = 'ws'
  const { WebSocketServer } = await import(wsPackage)
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 })
  await new Promise<void>(resolve => server.once('listening', resolve))
  const peers: any[] = []
  const calls: any[] = []
  const epoch = outcome === 'same owner' ? 'accepted' : 'replacement'
  let releaseResume: (() => void) | undefined
  let releaseReplay: (() => void) | undefined

  const push = (peer: any, replay_epoch: string, seq: number) => peer.send(JSON.stringify({ jsonrpc: '2.0',
    method: 'event', params: { type: 'message.complete', session_id: 'same', replay_epoch, seq, payload: { text: replay_epoch } } }))

  const openRequests = [{ id: 'replayed-owner-request', method: 'approval.request', params: { session_id: 'same' } }]

  const snapshot = { session_id: 'same', stored_session_id: 'same', authority_epoch: 4, replay_epoch: 'accepted',
    owner: wireOwner, revision: 1, execution_generation: 1, messages: [], prompts: [], info: {} }

  server.on('connection', (peer: any) => {
    peers.push(peer)
    const reconnected = peers.length > 1
    peer.on('message', (bytes: Buffer) => {
      const frame = JSON.parse(bytes.toString())
      const reply = (result: unknown) => peer.send(JSON.stringify({ jsonrpc: '2.0', id: frame.id, result }))

      if (frame.method === 'client.capabilities') { reply({ server_requests: [] });

 return }

      calls.push(frame)

      if (frame.method === 'session.events.since') {
        push(peer, epoch, 12)
        releaseReplay = () => reply({ replay_epoch: epoch, latest_seq: 12, snapshot_required: outcome !== 'same owner',
          events: [{ type: 'message.complete', session_id: 'same', replay_epoch: epoch, seq: 11, payload: { text: epoch } }],
          open_requests: openRequests })

        return
      }

      if (reconnected && frame.method === 'session.resume') {
        push(peer, epoch, 12)
        releaseResume = () => reply(outcome === 'replacement resume' ? {
          ...snapshot, replay_epoch: epoch, owner: { ...wireOwner, instance_id: 'owner-b' }, open_requests: openRequests
        } : snapshot)

        return
      }

      reply(snapshot)
    })
  })
  const client = new HermesGateway()
  const seen: any[] = []
  const requests: any[] = []
  client.onAny(event => { if (event.session_id === 'same') { seen.push(event) } })
  client.onRequest(request => { requests.push(request) })
  const url = `ws://127.0.0.1:${(server.address() as { port: number }).port}/api/ws?native_dial=fixture&ticket=one-use`

  try {
    await client.connect(url)
    await client.request('session.resume', { session_id: 'same', profile: 'default', expected_owner: wireOwner })
    push(peers[0], 'accepted', 10)
    await vi.waitFor(() => expect(client.getSeqWatermarks().same).toBe(10))
    seen.length = 0
    calls.length = 0
    const closed = new Promise<void>(resolve => client.onState(state => { if (state === 'closed') { resolve() } }))
    peers[0].terminate()
    await closed
    await client.connect(url)
    const barrier = client.sessionReplayBarrier('same')
    await vi.waitFor(() => expect(releaseResume).toBeDefined())
    expect(calls.find(frame => frame.method === 'session.events.since')).toBeUndefined()
    expect(seen).toEqual([])
    expect(requests).toEqual([])
    expect(client.getSeqWatermarks().same).toBe(10)
    releaseResume!()

    if (outcome !== 'replacement resume') {
      await vi.waitFor(() => expect(releaseReplay).toBeDefined())
      releaseReplay!()
    }

    await barrier
    await new Promise(resolve => setImmediate(resolve))

    if (outcome === 'same owner') {
      await vi.waitFor(() => expect(seen.map(event => event.seq)).toEqual([11, 12]))
      expect(requests).toHaveLength(1)
      expect(client.getSeqWatermarks().same).toBe(12)
    } else {
      expect(requests).toEqual([])
      expect(seen).toEqual([])
      expect(client.getSeqWatermarks().same).toBe(10)
    }

    expect(calls.find(frame => frame.method === 'session.resume').params.expected_owner).toEqual(wireOwner)

    if (outcome === 'replacement resume') {
      const count = calls.filter(frame => frame.method === 'session.resume').length
      const reattach = client.request('session.resume', { session_id: 'same', profile: 'default' })
      const refused = expect(reattach).rejects.toThrow()
      await vi.waitFor(() => expect(calls.filter(frame => frame.method === 'session.resume')).toHaveLength(count + 1))
      releaseResume!()
      await refused
    }
  } finally {
    client.close()

    for (const peer of server.clients) { peer.terminate() }
    await new Promise<void>(resolve => server.close(() => resolve()))
  }
})
