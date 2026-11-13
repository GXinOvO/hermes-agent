import { test } from 'node:test'
import assert from 'node:assert/strict'
import { discoverSessions } from '../src/followup-discovery.mjs'

const route = (connectionId, targetProfile = 'default') => ({ connectionId, profile: 'default', targetProfile, mode: 'remote' })
const row = (id, profile = 'default', extra = {}) => ({ id, profile, title: `title-${id}`, ...extra })
const page = (profile, sessions, total = sessions.length) => ({ sessions, total, offset: 0, limit: 100, profile_totals: { [profile]: total }, errors: [], storage: {} })

function host(routes, read) {
  const calls = []
  return {
    calls,
    profileRoutes: async () => routes,
    listPersistedSessions: async (source, options) => { calls.push([source, options]); return read(source, options) }
  }
}

test('discover visible sessions by exact connection and target profile without assuming business completion', async () => {
  const a = route('source-a', 'backend-a'), b = route('source-b', 'backend-b')
  const api = host([a, b], async source => page(source.targetProfile, [row('same', source.targetProfile, { _lineage_root_id: 'root', unread: true })]))
  const result = await discoverSessions(api)
  assert.deepEqual(api.calls.map(([r, options]) => [r.connectionId, options.profile, options.limit]), [['source-a', 'backend-a', 100], ['source-b', 'backend-b', 100]])
  assert.equal(result.sessions.length, 2)
  assert.deepEqual(result.sessions.map(x => [x.route.connectionId, x.storedId, x.unread]), [['source-a', 'root', true], ['source-b', 'root', true]])
  assert.equal(result.sessions.every(x => x.review === 'unknown'), true)
  assert.equal(result.coverage.complete, false)
})

test('preserve a reachable source while recording an unavailable source as unknown', async () => {
  const api = host([route('a'), route('b')], async source => { if (source.connectionId === 'b') throw Error('disconnected'); return page('default', [row('one')]) })
  const result = await discoverSessions(api)
  assert.equal(result.sessions.length, 1)
  assert.equal(result.coverage.complete, false)
  assert.deepEqual(result.sources.map(x => x.status), ['sampled', 'unavailable'])
})

test('a wrong backend profile or malformed row invalidates only that source', async () => {
  const api = host([route('a'), route('b')], async source => source.connectionId === 'a' ? page('default', [row('foreign', 'wrong')]) : page('default', [row('ok')]))
  const result = await discoverSessions(api)
  assert.deepEqual(result.sessions.map(x => x.storedId), ['ok'])
  assert.equal(result.sources[0].status, 'unavailable')
  assert.equal(result.coverage.complete, false)
})

test('an ambiguous source route is not queried and cannot yield a borrowed session', async () => {
  const a = route('a'), api = host([a, { ...a, targetProfile: 'different' }], async () => { throw Error('must not read') })
  const result = await discoverSessions(api)
  assert.equal(api.calls.length, 0)
  assert.equal(result.sessions.length, 0)
  assert.equal(result.coverage.complete, false)
  assert(result.sources.every(source => source.status === 'unavailable'))
})

test('an incomplete duplicate owner makes the initial inventory ambiguous', async () => {
  const a = route('a')
  for (const incomplete of [{ ...a, targetProfile: undefined }, { ...a, mode: undefined }]) {
    const api = host([a, incomplete], async () => page('default', [row('unsafe')]))
    const result = await discoverSessions(api)
    assert.equal(api.calls.length, 0)
    assert.deepEqual(result.sessions, [])
    assert(result.sources.every(source => source.status === 'unavailable'))
  }
})

test('an incomplete duplicate introduced during revalidation discards the result', async () => {
  const a = route('a')
  for (const incomplete of [{ ...a, targetProfile: undefined }, { ...a, mode: undefined }]) {
    let inventory = 0
    const api = {
      profileRoutes: async () => ++inventory === 1 ? [a] : [a, incomplete],
      listPersistedSessions: async () => page('default', [row('unsafe')])
    }
    const result = await discoverSessions(api)
    assert.deepEqual(result.sessions, [])
    assert.equal(result.sources[0].status, 'unavailable')
  }
})

test('session listing errors, corrupt storage and unknown totals remain partial', async () => {
  const api = host([route('a')], async () => ({ sessions: [row('one')], errors: [{ profile: 'default', error: 'read failed' }], storage: { default: 'corrupt' } }))
  const result = await discoverSessions(api)
  assert.equal(result.coverage.complete, false)
  assert.equal(result.sources[0].status, 'partial')
  assert.equal(result.sources[0].reportedTotal, null)
})

test('route drift during a read discards stale rows from the replaced backend', async () => {
  const oldRoute = route('a', 'old'), newRoute = route('a', 'new')
  let inventory = 0
  const api = { profileRoutes: async () => ++inventory === 1 ? [oldRoute] : [newRoute], listPersistedSessions: async () => page('old', [row('old-session', 'old')]) }
  const result = await discoverSessions(api)
  assert.equal(result.sessions.length, 0)
  assert.equal(result.sources[0].status, 'unavailable')
  assert.equal(result.coverage.complete, false)
})

test('route mode drift during a read discards rows from the old source', async () => {
  const oldRoute = route('a', 'backend')
  const newRoute = { ...oldRoute, mode: 'local' }
  let inventory = 0
  const api = {
    profileRoutes: async () => ++inventory === 1 ? [oldRoute] : [newRoute],
    listPersistedSessions: async () => page('backend', [row('old-session', 'backend')])
  }
  const result = await discoverSessions(api)
  assert.deepEqual(result.sessions, [])
  assert.equal(result.sources[0].status, 'unavailable')
  assert.equal(result.coverage.complete, false)
})

test('discovery does not read a source without a valid connection mode', async () => {
  for (const mode of [undefined, null, 'invalid']) {
    const api = host([{ ...route('a'), mode }], async () => page('default', [row('session')]))
    const result = await discoverSessions(api)
    assert.equal(api.calls.length, 0)
    assert.deepEqual(result.sessions, [])
    assert.equal(result.sources[0].status, 'unavailable')
    assert.equal(result.coverage.inventoryKnown, false)
  }
})

test('discovery discards rows if the route loses its mode during revalidation', async () => {
  const original = route('a')
  let inventory = 0
  const api = {
    profileRoutes: async () => ++inventory === 1 ? [original] : [{ ...original, mode: undefined }],
    listPersistedSessions: async () => page('default', [row('session')])
  }
  const result = await discoverSessions(api)
  assert.deepEqual(result.sessions, [])
  assert.equal(result.sources[0].status, 'unavailable')
})

test('pinned sessions outside the first window are retained without claiming complete coverage', async () => {
  const ordinary = Array.from({ length: 100 }, (_, i) => row(`recent-${i}`))
  const pinned = row('older-pinned', 'default', { pinned: true })
  const result = await discoverSessions(host([route('a')], async () => page('default', [...ordinary, pinned], 101)))
  assert.equal(result.sessions.length, 101)
  assert.equal(result.sessions.at(-1).storedId, 'older-pinned')
  assert.equal(result.coverage.complete, false)
})

test('limits avoid unbounded fan-out, record skipped sources and do not claim zero coverage', async () => {
  const routes = Array.from({ length: 14 }, (_, i) => route(`host-${i}`))
  const api = host(routes, async () => page('default', []))
  const result = await discoverSessions(api)
  assert.equal(api.calls.length, 12)
  assert.equal(result.sources.length, 14)
  assert.equal(result.sources.filter(x => x.status === 'not_scanned').length, 2)
  assert.equal(result.coverage.complete, false)
})

test('a slow source does not prevent independent sources from starting, with bounded concurrency', async () => {
  const routes = Array.from({ length: 8 }, (_, i) => route(`host-${i}`))
  let release, active = 0, peak = 0
  const blocked = new Promise(resolve => { release = resolve })
  const api = host(routes, async source => {
    active++; peak = Math.max(peak, active)
    if (source.connectionId === 'host-0') await blocked
    else await new Promise(resolve => setTimeout(resolve, 3))
    active--
    return page('default', [row(source.connectionId)])
  })
  const pending = discoverSessions(api)
  await new Promise(resolve => setTimeout(resolve, 30))
  const started = api.calls.map(([source]) => source.connectionId)
  release()
  const result = await pending
  assert(started.includes('host-1'), 'healthy sources must start before the slow source completes')
  assert(peak > 1 && peak <= 4, `source concurrency must stay bounded, observed ${peak}`)
  assert.equal(result.sessions.length, 8)
})

test('an in-place route mapping change during an empty listing is discarded', async () => {
  const owner = route('host-a', 'old')
  const api = {
    profileRoutes: async () => [owner],
    listPersistedSessions: async () => { owner.targetProfile = 'new'; return page('old', []) }
  }
  const result = await discoverSessions(api)
  assert.equal(result.sources[0].status, 'unavailable')
  assert.equal(result.sources[0].route.targetProfile, 'old')
  assert.deepEqual(result.sessions, [])
})

test('a hung source times out without hiding healthy sources or scanning unstarted ones', async () => {
  const routes = Array.from({ length: 5 }, (_, i) => route(`host-${i}`))
  const calls = []
  const api = {
    profileRoutes: async () => routes,
    listPersistedSessions: async source => {
      calls.push(source.connectionId)
      if (source.connectionId !== 'host-0') return new Promise(() => {})
      return page('default', [row('healthy')])
    }
  }
  const result = await Promise.race([
    discoverSessions(api, { timeoutMs: 25 }),
    new Promise((_, reject) => setTimeout(() => reject(Error('discovery never settled')), 200))
  ])
  assert.deepEqual(result.sessions.map(session => session.storedId), ['healthy'])
  assert.equal(result.sources.filter(source => source.status === 'unavailable').length, 4)
  assert.equal(calls.length, 5)
  assert.equal(result.coverage.complete, false)
})

test('a repeated scan does not launch a duplicate request while a prior source is hung', async () => {
  const owner = route('a')
  let calls = 0
  const api = { profileRoutes: async () => [owner], listPersistedSessions: async () => { calls++; return new Promise(() => {}) } }
  const guardedScan = () => Promise.race([
    discoverSessions(api, { timeoutMs: 20 }),
    new Promise((_, reject) => setTimeout(() => reject(Error('discovery never settled')), 200))
  ])
  const first = await guardedScan()
  const second = await guardedScan()
  assert.equal(calls, 1)
  assert.equal(first.sources[0].status, 'unavailable')
  assert.equal(second.sources[0].status, 'not_scanned')
})

test('hung requests remain globally bounded across repeated scans and changed source inventories', async () => {
  let generation = 0, calls = 0
  const api = {
    profileRoutes: async () => [route(`host-${generation++}`)],
    listPersistedSessions: async () => { calls++; return new Promise(() => {}) }
  }
  for (let i = 0; i < 10; i++) {
    const result = await Promise.race([
      discoverSessions(api, { timeoutMs: 10 }),
      new Promise((_, reject) => setTimeout(() => reject(Error('discovery never settled')), 200))
    ])
    assert.equal(result.coverage.complete, false)
    assert.equal(result.sessions.length, 0)
  }
  assert(calls <= 4, `only four actual source requests may remain unresolved; saw ${calls}`)
})

test('a source that settles after its timeout can be retried', async () => {
  let resolveFirst, calls = 0
  const api = {
    profileRoutes: async () => [route('recovering')],
    listPersistedSessions: () => ++calls === 1 ? new Promise(resolve => { resolveFirst = resolve }) : Promise.resolve(page('default', [row('recovered')]))
  }
  const first = await Promise.race([
    discoverSessions(api, { timeoutMs: 15 }),
    new Promise((_, reject) => setTimeout(() => reject(Error('discovery never settled')), 200))
  ])
  assert.equal(first.sources[0].status, 'unavailable')
  resolveFirst(page('default', [row('stale')]))
  await new Promise(resolve => setTimeout(resolve, 0))
  const second = await discoverSessions(api, { timeoutMs: 15 })
  assert.deepEqual(second.sessions.map(session => session.storedId), ['recovered'])
  assert.equal(calls, 2)
})

test('overlapping scans use their own fresh inventories', async () => {
  let invocations = 0, resolveRefresh, releaseRead
  const routes = [route('old-source')]
  const firstRead = new Promise(resolve => { releaseRead = resolve })
  const api = {
    profileRoutes: () => {
      invocations++
      if (invocations === 2) return new Promise(resolve => { resolveRefresh = resolve })
      return Promise.resolve(routes)
    },
    listPersistedSessions: () => firstRead
  }
  const first = discoverSessions(api, { timeoutMs: 100 })
  await new Promise(resolve => setTimeout(resolve, 0))
  releaseRead(page('default', [row('old-row')]))
  await new Promise(resolve => setTimeout(resolve, 0))
  const second = discoverSessions(api, { timeoutMs: 100 })
  resolveRefresh(routes)
  const [a, b] = await Promise.all([first, second])
  assert.deepEqual(a.sessions.map(session => session.storedId), ['old-row'])
  assert.deepEqual(b.sessions.map(session => session.storedId), ['old-row'])
  assert.equal(invocations, 4)
})

test('inventory retries remain bounded when previous requests never resolve', async () => {
  let calls = 0
  const api = { profileRoutes: () => { calls++; return new Promise(() => {}) } }
  for (let i = 0; i < 3; i++) {
    const result = await discoverSessions(api, { timeoutMs: 15 })
    assert.deepEqual(result.sessions, [])
    assert.equal(result.coverage.inventoryKnown, false)
  }
  assert.equal(calls, 2)
})

test('a later scan does not reuse an unresolved listing from an earlier scan', async () => {
  let resolveListing, started, calls = 0
  const readStarted = new Promise(resolve => { started = resolve })
  const api = {
    profileRoutes: async () => [route('a')],
    listPersistedSessions: () => {
      calls++
      started()
      return new Promise(resolve => { resolveListing = resolve })
    }
  }
  const first = discoverSessions(api, { timeoutMs: 500 })
  await readStarted
  const second = discoverSessions(api, { timeoutMs: 500 })
  const early = await Promise.race([
    second,
    new Promise(resolve => setTimeout(() => resolve('still-waiting'), 60))
  ])
  resolveListing(page('default', [row('old-request')]))
  const a = await first
  assert.notEqual(early, 'still-waiting', 'a new scan must not wait for an earlier scan request')
  assert.deepEqual(a.sessions.map(session => session.storedId), ['old-request'])
  assert.deepEqual(early.sessions, [])
  assert.equal(early.sources[0].status, 'not_scanned')
  assert.equal(calls, 1)
})

test('a source added during revalidation is reported as not scanned', async () => {
  let inventory = 0
  const api = {
    profileRoutes: async () => ++inventory === 1 ? [route('a')] : [route('a'), route('new')],
    listPersistedSessions: async () => page('default', [])
  }
  const result = await discoverSessions(api)
  assert.deepEqual(result.sources.map(source => [source.route.connectionId, source.status]), [['a', 'sampled'], ['new', 'not_scanned']])
  assert.equal(result.coverage.complete, false)
})

test('repeated scans reach sources beyond the first twelve', async () => {
  const routes = Array.from({ length: 14 }, (_, i) => route(`host-${i}`))
  const api = host(routes, async () => page('default', []))
  await discoverSessions(api)
  await discoverSessions(api)
  assert(api.calls.some(([source]) => source.connectionId === 'host-12'))
  assert(api.calls.some(([source]) => source.connectionId === 'host-13'))
})

test('reordering a fixed source inventory cannot starve a source across scans', async () => {
  const routes = Array.from({ length: 14 }, (_, i) => route(`source-${i}`))
  const victim = routes.at(-1)
  const rest = routes.slice(0, -1)
  const calls = []
  let inventoryCalls = 0
  const api = {
    profileRoutes: async () => {
      const round = Math.floor(inventoryCalls++ / 2)
      const start = (round * 12) % routes.length
      const skipped = (start + 12) % routes.length
      const order = [...rest]
      order.splice(skipped, 0, victim)
      return order
    },
    listPersistedSessions: async source => {
      calls.push(source.connectionId)
      return page('default', [])
    }
  }

  for (let i = 0; i < 3; i++) {
    const result = await discoverSessions(api)
    assert.equal(result.sources.filter(source => source.status === 'sampled').length, 12)
    assert.equal(result.sources.filter(source => source.status === 'not_scanned').length, 2)
    assert.equal(result.coverage.complete, false)
  }
  assert.deepEqual(new Set(calls), new Set(routes.map(source => source.connectionId)))
})

test('a continuously present source is not starved by changing other sources when request slots are available', async () => {
  let inventoryCalls = 0
  const calls = []
  const api = {
    profileRoutes: async () => {
      const round = Math.floor(inventoryCalls++ / 2)
      const victimIndex = ((round * 12) % 14 + 12) % 14
      const before = Array.from({ length: victimIndex }, (_, i) => route(`a-${String(i).padStart(2, '0')}`))
      const after = Array.from({ length: 13 - victimIndex }, (_, i) => route(`z-${String(i).padStart(2, '0')}`))
      return [...before, route('m-victim'), ...after]
    },
    listPersistedSessions: async source => {
      calls.push(source.connectionId)
      return page('default', [])
    }
  }

  for (let i = 0; i < 7; i++) {
    const result = await discoverSessions(api)
    assert.equal(result.sources.filter(source => source.status === 'sampled').length, 12)
    assert.equal(result.sources.filter(source => source.status === 'not_scanned').length, 2)
    assert.equal(result.coverage.complete, false)
  }
  assert(calls.includes('m-victim'), 'a continuously eligible source must eventually be scanned')
})

test('newly appearing sources do not overtake a continuously eligible source', async () => {
  let inventoryCalls = 0
  const calls = []
  const stable = Array.from({ length: 60 }, (_, i) => route(`a-${String(i).padStart(2, '0')}`))
  const api = {
    profileRoutes: async () => {
      const round = Math.floor(inventoryCalls++ / 2)
      const newcomers = Array.from({ length: round * 20 }, (_, i) => route(`b-${String(i).padStart(3, '0')}`))
      return [...stable, ...newcomers, route('z-victim')]
    },
    listPersistedSessions: async source => {
      calls.push(source.connectionId)
      return page('default', [])
    }
  }

  for (let i = 0; i < 6; i++) {
    const result = await discoverSessions(api)
    assert.equal(result.sources.filter(source => source.status === 'sampled').length, 12)
    assert.equal(result.coverage.complete, false)
  }
  assert(calls.includes('z-victim'), 'new arrivals must stay behind the waiting source')
})

test('a source not started because slots were occupied keeps priority on the next scan', async () => {
  let routes = ['a', 'b', 'c'].map(name => route(name))
  const calls = []
  const api = {
    profileRoutes: async () => routes,
    listPersistedSessions: async source => {
      calls.push(source.connectionId)
      if (['a', 'b', 'c'].includes(source.connectionId)) return new Promise(() => {})
      await new Promise(resolve => setTimeout(resolve, 10))
      return page('default', [])
    }
  }

  await discoverSessions(api, { timeoutMs: 60 })
  routes = ['a', 'b', 'c', 'd', 'e'].map(name => route(name))
  const first = await discoverSessions(api, { timeoutMs: 60 })
  const second = await discoverSessions(api, { timeoutMs: 60 })
  const statuses = result => Object.fromEntries(result.sources.map(source => [source.route.connectionId, source.status]))
  assert.equal(statuses(first).d, 'sampled')
  assert.equal(statuses(first).e, 'not_scanned')
  assert.equal(statuses(second).e, 'sampled')
  assert.equal(statuses(second).d, 'not_scanned')
  assert.deepEqual(calls, ['a', 'b', 'c', 'd', 'e'])
})

test('unstarted healthy source is not classified as unreachable when request slots are occupied', async () => {
  const api = host(Array.from({ length: 5 }, (_, i) => route(`host-${i}`)), async source => {
    if (source.connectionId === 'host-4') return page('default', [row('healthy')])
    return new Promise(() => {})
  })
  const result = await discoverSessions(api, { timeoutMs: 15 })
  assert.equal(api.calls.length, 4)
  assert.equal(result.sources[4].status, 'not_scanned')
  assert.equal(result.coverage.complete, false)
})

test('a new inventory call can recover while a prior inventory remains hung', async () => {
  let inventory = 0
  const api = {
    profileRoutes: () => ++inventory === 1 ? new Promise(() => {}) : Promise.resolve([route('recovered')]),
    listPersistedSessions: async () => page('default', [row('recovered-session')])
  }
  const first = await discoverSessions(api, { timeoutMs: 15 })
  assert.equal(first.coverage.inventoryKnown, false)
  const second = await discoverSessions(api, { timeoutMs: 15 })
  assert.deepEqual(second.sessions.map(session => session.storedId), ['recovered-session'])
  assert.equal(inventory, 3)
})

test('a successful read survives an overlapping scan revalidating its own inventory', async () => {
  let inventory = 0, releaseRead, releaseOtherInventory, markRead, markOtherInventory
  const readStarted = new Promise(resolve => { markRead = resolve })
  const otherStarted = new Promise(resolve => { markOtherInventory = resolve })
  const api = {
    profileRoutes: () => {
      inventory++
      if (inventory === 3) { markOtherInventory(); return new Promise(resolve => { releaseOtherInventory = resolve }) }
      return Promise.resolve([route('a')])
    },
    listPersistedSessions: () => { markRead(); return new Promise(resolve => { releaseRead = resolve }) }
  }
  const first = discoverSessions(api, { timeoutMs: 400 })
  await readStarted
  const second = discoverSessions(api, { timeoutMs: 400 })
  const observedOther = await Promise.race([otherStarted.then(() => true), new Promise(resolve => setTimeout(() => resolve(false), 80))])
  releaseRead(page('default', [row('first-result')]))
  const winner = await Promise.race([first, new Promise(resolve => setTimeout(() => resolve(null), 80))])
  if (releaseOtherInventory) releaseOtherInventory([route('a')])
  await second
  assert.equal(observedOther, true)
  assert.notEqual(winner, null)
  assert.deepEqual(winner.sessions.map(session => session.storedId), ['first-result'])
})

test('empty or unavailable inventory cannot be presented as complete', async () => {
  const result = await discoverSessions(host([], async () => { throw Error('not expected') }))
  assert.equal(result.sessions.length, 0)
  assert.equal(result.coverage.complete, false)
  assert.equal(result.coverage.inventoryKnown, false)
})
