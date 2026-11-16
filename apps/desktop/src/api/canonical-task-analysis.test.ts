// @vitest-environment node
import { expect, test } from 'vitest'

import { exactSessionOwnerToWire } from '@/lib/exact-session-owner'

import { HermesGateway } from './client'

const owner = { instanceId: 'owner', profileId: '/profiles/a', authorityEpoch: 3, storeInstanceId: 'store' }

for (const method of ['session.task_source', 'session.task_analysis']) {
  test(`${method} verifies its read envelope without attaching a session or requiring replay`, async () => {
    const wsPackage = 'ws'
    const ws = await import(wsPackage)
    const server = new ws.WebSocketServer({ host: '127.0.0.1', port: 0 })
    await new Promise<void>(resolve => server.once('listening', resolve))
    const sent: string[] = []
    let value = { session_id: 'source', stored_session_id: 'source', owner: exactSessionOwnerToWire(owner), authority_epoch: 3 }
    server.on('connection', (socket: any) => socket.on('message', (bytes: Buffer) => {
      const frame = JSON.parse(bytes.toString())
      sent.push(frame.method)
      socket.send(JSON.stringify({ jsonrpc: '2.0', id: frame.id, result: frame.method === 'client.capabilities' ? { server_requests: [] } : value }))
    }))
    const client = new HermesGateway()

    try {
      const address = server.address() as { port: number }
      await client.connect(`ws://127.0.0.1:${address.port}/api/ws?native_dial=fixture&ticket=test`)

      const params = { session_id: 'source', profile: 'default', expected_owner: exactSessionOwnerToWire(owner),
        ...(method === 'session.task_analysis' ? { stored_id: 'source', task_id: 'matter', title: '目标' } : {}) }

      expect(await client.request(method, params)).toMatchObject(value)
      value = { ...value, owner: { ...value.owner, store_instance_id: 'replacement' } }
      await expect(client.request(method, params)).rejects.toThrow('来源会话身份')
      expect(sent.filter(name => name !== 'client.capabilities')).toEqual([method, method])
    } finally {
      client.close()

      for (const socket of server.clients) { socket.terminate() }
      await new Promise<void>(resolve => server.close(() => resolve()))
    }
  })
}
