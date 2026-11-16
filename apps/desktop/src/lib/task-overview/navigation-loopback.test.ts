import { execFileSync, spawn } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createInterface } from 'node:readline'

import { renderHook } from '@testing-library/react'
import { expect, test, vi } from 'vitest'

import { HermesGateway } from '@/api/client'
import { useSessionTileDelegate } from '@/app/contrib/hooks/use-session-tile-delegate'
import { createClientSessionState } from '@/lib/chat-runtime'
import { exactSessionOwnerFromWire } from '@/lib/exact-session-owner'
import { setPrimaryGateway, setPrimaryGatewayConnectionId } from '@/store/gateway'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'
import { $sessionStates, $sessionTiles, publishSessionState } from '@/store/session-states'

import { openTaskSource } from './analysis'
import type { TaskReference } from './model'

for (const mode of ['local', 'messaging']) {
  test(`opens a production ${mode} compressed tip through its verified logical owner`, async () => {
    const root = resolve('../..')

    const python = process.env.HERMES_PYTHON || execFileSync('bash', ['scripts/run-in-hermes-env', 'sh', '-c', 'printf %s "$__HERMES_TEST_PYTHON"'], {
      cwd: root, encoding: 'utf8', env: { ...process.env, HERMES_HOME: undefined }
    })

    const scratch = await mkdtemp(join(tmpdir(), 'task-source-loopback-'))

    const child = spawn(python, ['tests/gateway/task_source_navigation_loopback.py', mode], {
      cwd: resolve('../..'), env: { ...process.env, HOME: scratch, HERMES_HOME: scratch, TMPDIR: scratch },
      stdio: ['pipe', 'pipe', 'pipe']
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

      if (reply.tip) { first(reply) } else { replies.get(reply.id)?.(reply); replies.delete(reply.id) }
    })
    child.on('exit', code => { if (code) { first(Promise.reject(new Error(errors))) } })
    const wsPackage = 'ws'
    const { WebSocketServer, WebSocket } = await import(wsPackage)
    const server = new WebSocketServer({ host: '127.0.0.1', port: 0 })
    await new Promise<void>(resolve => server.once('listening', resolve))
    const sent: any[] = []
    server.on('connection', (socket: any) => socket.on('message', (bytes: Buffer) => {
      const request = JSON.parse(bytes.toString())
      sent.push(request)
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
      $selectedStoredSessionId.set(source.session_id)
      $activeSessionId.set(source.session_id)
      $sessionTiles.set([])

      const reference: TaskReference = {
        storedId: source.tip, connectionId: 'local', profile: 'default', targetProfile: 'default', mode: 'local',
        expectedOwner: exactSessionOwnerFromWire(source.owner)
      }

      const original = JSON.stringify(reference)
      await openTaskSource(reference)
      const resume = sent.find(frame => frame.method === 'session.resume')
      expect(resume.params).toMatchObject({ session_id: source.session_id, expected_owner: source.owner })
      expect($sessionStates.get()[source.session_id].sourceOwner).toEqual(reference.expectedOwner)
      expect($sessionStates.get()[source.session_id].messages.length).toBeGreaterThan(0)
      expect(JSON.stringify(reference)).toBe(original)
      const count = sent.filter(frame => frame.method === 'session.resume').length
      await expect(openTaskSource({ ...reference, expectedOwner: { ...reference.expectedOwner!, instanceId: 'replacement' } })).rejects.toThrow()
      expect(sent.filter(frame => frame.method === 'session.resume')).toHaveLength(count)
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
}
