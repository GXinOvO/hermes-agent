// @vitest-environment node
import { expect, test, vi } from 'vitest'

import { exactSessionOwnerToWire } from '@/lib/exact-session-owner'

import { HermesGateway } from './client'

const owner = { instanceId: 'owner-a', profileId: '/profile/a', authorityEpoch: 4, storeInstanceId: 'store-a' }

const snapshot = (over: Record<string, unknown> = {}) => ({ session_id: 'same', stored_session_id: 'same',
  authority_epoch: 4, replay_epoch: 'accepted', owner: exactSessionOwnerToWire(owner), revision: 1,
  execution_generation: 2, messages: [], prompts: [], info: {}, ...over })

async function fixture(run: (client: HermesGateway, setReply: (value: any) => void, sent: any[], emit: (sessionId: string, epoch: string, seq: number) => void, reconnect: () => Promise<void>) => Promise<void>, injectAlien = true) {
  const wsPackage = 'ws'
  const { WebSocketServer } = await import(wsPackage)
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 })
  await new Promise<void>(resolve => server.once('listening', resolve))
  let reply: any = snapshot()
  const sent: any[] = []
  let sendEvent!: (sessionId: string, epoch: string, seq: number) => void
  server.on('connection', (socket: any) => socket.on('message', (bytes: Buffer) => {
    const frame = JSON.parse(bytes.toString())

    if (frame.method === 'client.capabilities') {
      socket.send(JSON.stringify({ jsonrpc: '2.0', id: frame.id, result: { server_requests: [] } }))

      return
    }

    sent.push(frame)

    if (frame.method === 'session.events.since') {
      socket.send(JSON.stringify({ jsonrpc: '2.0', id: frame.id,
        result: { events: [], latest_seq: frame.params.last_seen, replay_epoch: frame.params.replay_epoch, snapshot_required: false } }))

      return
    }

    const event = (session_id: string, replay_epoch: string, seq: number) => socket.send(JSON.stringify({
      jsonrpc: '2.0', method: 'event', params: { type: 'message.complete', session_id, replay_epoch, seq, payload: { text: replay_epoch } }
    }))

    sendEvent = event
    event('same', 'accepted', 10)

    if (injectAlien) { event('same', 'alien', 99) }

    event('sibling', 'sibling', 2)
    void Promise.resolve(reply).then(value => socket.send(JSON.stringify({ jsonrpc: '2.0', id: frame.id,
      ...(value.error ? { error: value.error } : { result: value }) })))
  }))
  const client = new HermesGateway()

  try {
    const address = server.address() as { port: number }
    const connect = () => client.connect(`ws://127.0.0.1:${address.port}/api/ws?native_dial=fixture&ticket=one-use`)
    await connect()
    await run(client, value => { reply = value }, sent, (...args) => sendEvent(...args), connect)
  } finally {
    client.close()

    for (const socket of server.clients) { socket.terminate() }
    await new Promise<void>(resolve => server.close(() => resolve()))
  }
}

const resume = (client: HermesGateway) => client.request('session.resume', {
  session_id: 'same', profile: 'default', expected_owner: exactSessionOwnerToWire(owner)
})

test('accepted exact resume releases only its epoch and leaves sibling traffic live', async () => fixture(async client => {
  const seen: any[] = []
  client.onAny(event => seen.push(event))
  const resumed = await resume(client)
  expect((resumed as any).info.session_owner).toEqual(exactSessionOwnerToWire(owner))
  expect((resumed as any).info.replay_epoch).toBe('accepted')
  expect(seen.map(event => event.replay_epoch)).toEqual(['sibling', 'accepted'])
  expect(client.getSeqWatermarks()).toEqual({ sibling: 2, same: 10 })
}))

test('accepted exact resume still delivers its validated open requests', async () => fixture(async (client, setReply) => {
  setReply(snapshot({ open_requests: [
    { id: 'accepted-request', method: 'approval.request', params: { session_id: 'same', command: 'accepted-owner' } }
  ] }))
  const shown: any[] = []
  client.onRequest(request => { shown.push(request) })
  await resume(client)
  expect(shown).toHaveLength(1)
  expect(shown[0].id).toBe('accepted-request')
  expect(shown[0].replayed).toBe(true)
}))

test.each([
  ['wrong target', snapshot({ session_id: 'other', stored_session_id: 'other', prompts: [{ kind: 'approval', prompt_id: 'bad' }] })],
  ['wrong target with open requests', snapshot({ session_id: 'other', stored_session_id: 'other', open_requests: [
    { id: 'bad-open', method: 'approval.request', params: { session_id: 'same', command: 'wrong-owner' } }
  ] })],
  ['copied store/new instance', snapshot({ owner: { ...exactSessionOwnerToWire(owner), instance_id: 'replacement' } })],
  ['same id/different profile', snapshot({ owner: { ...exactSessionOwnerToWire(owner), profile_id: '/profile/b' } })],
  ['missing proof', snapshot({ owner: undefined })],
  ['backend refusal', { error: { code: 4001, message: 'session_owner_changed' } }]
])('rejects %s before owner, prompt or cursor adoption', async (_name, response) => fixture(async (client, setReply, sent) => {
  setReply(response)
  const shown: any[] = []
  const seen: any[] = []
  client.onRequest(request => { shown.push(request) })
  client.onAny(event => seen.push(event))
  await expect(resume(client)).rejects.toThrow()
  expect(shown).toEqual([])
  expect(seen.map(event => event.session_id)).toEqual(['sibling'])
  expect(client.getSeqWatermarks()).toEqual({ sibling: 2 })
  expect(client.sessionOwnerWitness('same', 'default', 'accepted')).toBeUndefined()
  expect(sent).toHaveLength(1)
}))

test.each(['accepted', 'refused', 'wrong owner', 'wrong target'])('revalidates a new epoch after an established exact subscription: %s', async outcome => fixture(async (client, setReply, sent, emit) => {
  await resume(client)
  const seen: any[] = []
  const requests: any[] = []
  client.onRequest(request => { requests.push(request) })
  client.onAny(event => seen.push(event))
  let reply!: (value: any) => void
  setReply(new Promise(resolve => { reply = resolve }))
  emit('same', 'renewed', 1)
  emit('same', 'accepted', 11)
  emit('sibling', 'sibling', 3)
  await vi.waitFor(() => expect(sent).toHaveLength(2))
  expect(sent[1].params.expected_owner).toEqual(exactSessionOwnerToWire(owner))
  await vi.waitFor(() => expect(client.getSeqWatermarks().sibling).toBe(3))
  expect(seen.filter(event => event.replay_epoch === 'renewed')).toEqual([])
  expect(client.getSeqWatermarks().same).toBe(11)
  reply(outcome === 'accepted' ? snapshot({ replay_epoch: 'renewed' }) :
    outcome === 'wrong owner' ? snapshot({ replay_epoch: 'renewed', owner: { ...exactSessionOwnerToWire(owner), instance_id: 'replacement' } }) :
    outcome === 'wrong target' ? snapshot({ session_id: 'other', stored_session_id: 'other', replay_epoch: 'renewed', open_requests: [
      { id: 'wrong-target-request', method: 'approval.request', params: { session_id: 'same' } }
    ] }) : { error: { code: 4001, message: 'session_owner_changed' } })

  if (outcome === 'accepted') {
    await vi.waitFor(() => expect(seen.filter(event => event.replay_epoch === 'renewed')).toHaveLength(1))
    expect(client.getSeqWatermarks().same).toBe(1)
    expect(client.sessionOwnerWitness('same', 'default', 'renewed')).toEqual(owner)
  } else {
    await vi.waitFor(() => expect((client as any).exactResumes.size).toBe(0))
    expect(seen.filter(event => event.replay_epoch === 'renewed')).toEqual([])
    expect(client.getSeqWatermarks().same).toBe(11)
    expect(client.sessionOwnerWitness('same', 'default', 'accepted')).toEqual(owner)
  }

  expect(sent).toHaveLength(2)
  expect(requests).toEqual([])
}, false))

test.each(['renewal', 'reopen'].flatMap(mode => ['accepted', 'refused', 'wrong owner', 'wrong target'].map(outcome => [mode, outcome])))('retains a final epoch buffered during guarded %s: %s', async (mode, outcome) => fixture(async (client, setReply, sent, emit) => {
  await resume(client)
  await vi.waitFor(() => {
    expect((client as any).exactResumes.size).toBe(0)
    expect([...((client as any).exactSources as Map<string, any>).values()].every(source => !source.events)).toBe(true)
  })
  const initialRequests = sent.length
  const seen: any[] = []
  const requests: any[] = []
  client.onAny(event => seen.push(event))
  client.onRequest(request => { requests.push(request) })
  let firstReply!: (value: any) => void
  setReply(new Promise(resolve => { firstReply = resolve }))
  const reopening = mode === 'reopen' ? resume(client) : undefined

  if (!reopening) { emit('same', 'renewed', 1) }

  await vi.waitFor(() => expect(sent).toHaveLength(initialRequests + 1))
  emit('same', 'final', 1)
  emit('sibling', 'sibling', 3)
  await vi.waitFor(() => expect(client.getSeqWatermarks().sibling).toBe(3))
  expect(seen.filter(event => event.replay_epoch === 'final')).toEqual([])
  expect(client.getSeqWatermarks().same).toBe(10)
  let finalReply!: (value: any) => void
  setReply(new Promise(resolve => { finalReply = resolve }))
  firstReply(snapshot({ replay_epoch: 'renewed' }))
  await reopening
  await vi.waitFor(() => expect(sent).toHaveLength(initialRequests + 2))
  expect(sent.at(-1).params.expected_owner).toEqual(exactSessionOwnerToWire(owner))
  expect(seen.filter(event => event.replay_epoch === 'final')).toEqual([])
  expect(client.getSeqWatermarks().same).toBe(10)
  expect(client.sessionOwnerWitness('same', 'default', 'renewed')).toEqual(owner)
  finalReply(outcome === 'accepted' ? snapshot({ replay_epoch: 'final' }) :
    outcome === 'wrong owner' ? snapshot({ replay_epoch: 'final', owner: { ...exactSessionOwnerToWire(owner), instance_id: 'replacement' } }) :
    outcome === 'wrong target' ? snapshot({ session_id: 'other', stored_session_id: 'other', replay_epoch: 'final', open_requests: [
      { id: 'wrong-final-request', method: 'approval.request', params: { session_id: 'same' } }
    ] }) : { error: { code: 4001, message: 'session_owner_changed' } })
  await vi.waitFor(() => {
    expect((client as any).exactResumes.size).toBe(0)
    expect([...((client as any).exactSources as Map<string, any>).values()].every(source => !source.events)).toBe(true)
  })
  expect(seen.filter(event => event.replay_epoch === 'final')).toHaveLength(outcome === 'accepted' ? 1 : 0)
  expect(client.getSeqWatermarks().same).toBe(outcome === 'accepted' ? 1 : 10)
  expect(client.sessionOwnerWitness('same', 'default', outcome === 'accepted' ? 'final' : 'renewed')).toEqual(owner)
  expect(requests).toEqual([])
  expect(sent).toHaveLength(initialRequests + 2)
}, false))

test.each(['close', 'reconnect'])('stops buffered epoch delivery after a synchronous %s', async action => fixture(async (client, setReply, sent, emit, reconnect) => {
  await resume(client)
  const seen: any[] = []
  let redial: Promise<void> | undefined
  let closedCursor: Record<string, number> | undefined
  let closedResumeCount = 0
  client.onAny(event => {
    seen.push(event)

    if (event.replay_epoch === 'renewed') {
      client.close()
      closedCursor = client.getSeqWatermarks()
      closedResumeCount = sent.filter(frame => frame.method === 'session.resume').length

      if (action === 'reconnect') { redial = reconnect() }
    }
  })
  let reply!: (value: any) => void
  setReply(new Promise(resolve => { reply = resolve }))
  emit('same', 'renewed', 1)
  await vi.waitFor(() => expect(sent).toHaveLength(2))
  emit('same', 'final', 1)
  emit('sibling', 'sibling', 3)
  await vi.waitFor(() => expect(client.getSeqWatermarks().sibling).toBe(3))
  reply(snapshot({ replay_epoch: 'renewed' }))
  await vi.waitFor(() => expect(closedCursor).toBeDefined())
  await redial
  expect(seen.filter(event => event.session_id === 'same').map(event => event.replay_epoch)).toEqual(['accepted', 'renewed'])
  expect(client.getSeqWatermarks()).toEqual(closedCursor)
  expect(client.sessionOwnerWitness('same', 'default', 'final')).toBeUndefined()
  expect((client as any).exactSources.size).toBe(1)
  expect((client as any).exactSources.get('same').events).toBeUndefined()
  expect(closedResumeCount).toBe(2)
  expect(sent.filter(frame => frame.method === 'session.resume').every(frame =>
    JSON.stringify(frame.params.expected_owner) === JSON.stringify(exactSessionOwnerToWire(owner)))).toBe(true)
}, false))

test('overview reads verify their own echo without adopting a resume subscription', async () => fixture(async (client, setReply) => {
  setReply({ version: 1, session_id: 'same', stored_session_id: 'stored', resolved_id: 'tip',
    authority_epoch: owner.authorityEpoch, owner: exactSessionOwnerToWire(owner) })
  const result = await client.request<any>('session.task_overview', { session_id: 'same', stored_id: 'stored', profile: 'default', expected_owner: exactSessionOwnerToWire(owner) })
  expect(result.resolved_id).toBe('tip')
  expect((client as any).exactResumes.size).toBe(0)
  expect((client as any).exactSources.size).toBe(0)
  expect(client.sessionOwnerWitness('same', 'default', 'accepted')).toBeUndefined()
}, false))

test('overview rejects a wrong durable echo before response-carried requests', async () => fixture(async (client, setReply) => {
  setReply({ version: 1, session_id: 'same', stored_session_id: 'other', resolved_id: 'tip',
    authority_epoch: owner.authorityEpoch, owner: exactSessionOwnerToWire(owner), open_requests: [
      { id: 'wrong-overview', method: 'approval.request', params: { session_id: 'same' } }
    ] })
  const requests: any[] = []
  client.onRequest(request => { requests.push(request) })
  await expect(client.request('session.task_overview', { session_id: 'same', stored_id: 'stored', expected_owner: exactSessionOwnerToWire(owner) })).rejects.toThrow()
  expect(requests).toEqual([])
}, false))

test('overview never adopts response-carried requests even with a matching owner', async () => fixture(async (client, setReply) => {
  setReply({ version: 1, session_id: 'same', stored_session_id: 'stored', resolved_id: 'tip',
    authority_epoch: owner.authorityEpoch, owner: exactSessionOwnerToWire(owner), open_requests: [
      { id: 'unexpected-overview', method: 'approval.request', params: { session_id: 'same' } }
    ] })
  const requests: any[] = []
  client.onRequest(request => { requests.push(request) })
  await expect(client.request('session.task_overview', { session_id: 'same', stored_id: 'stored', expected_owner: exactSessionOwnerToWire(owner) })).rejects.toThrow()
  expect(requests).toEqual([])
}, false))

test('a refused reopen retains the already accepted owner stream', async () => fixture(async (client, setReply) => {
  await resume(client)
  await vi.waitFor(() => {
    expect((client as any).exactResumes.size).toBe(0)
    expect([...((client as any).exactSources as Map<string, any>).values()].every(source => !source.events)).toBe(true)
  })
  const seen: any[] = []
  client.onAny(event => seen.push(event))
  setReply({ error: { code: 4001, message: 'session_owner_changed' } })
  await expect(resume(client)).rejects.toThrow('session_owner_changed')
  expect(seen.filter(event => event.session_id === 'same').map(event => event.replay_epoch)).toEqual(['accepted'])
  expect(client.getSeqWatermarks().same).toBe(10)
  expect(client.sessionOwnerWitness('same', 'default', 'accepted')).toEqual(owner)
}, false))
