const MAX_SOURCES = 12
const SESSION_LIMIT = 100
const MAX_IN_FLIGHT = 4
const DEFAULT_TIMEOUT_MS = 10_000
const pending = new WeakMap()
const inventoryPending = new WeakMap()
const sourceQueue = new WeakMap()
const identity = route => JSON.stringify([route.connectionId, route.profile])
const requestIdentity = route => JSON.stringify(['sessions', route.connectionId, route.profile, route.targetProfile])
const identified = route => route && ['connectionId', 'profile'].every(key => typeof route[key] === 'string' && route[key] !== '' && route[key] === route[key].trim())
const usable = route => identified(route) && ['local', 'remote'].includes(route.mode) && typeof route.targetProfile === 'string' && route.targetProfile !== '' && route.targetProfile === route.targetProfile.trim()
const nonnegative = value => Number.isSafeInteger(value) && value >= 0

function pendingFor(host) {
  let entries = pending.get(host)
  if (!entries) pending.set(host, entries = new Map())
  return entries
}

function request(host, key, operation, onStarted) {
  const entries = pendingFor(host)
  if (entries.has(key) || entries.size >= MAX_IN_FLIGHT) {
    const error = new Error('Source request not started')
    error.code = 'FOLLOWUP_NOT_SCANNED'
    return Promise.reject(error)
  }
  const promise = Promise.resolve().then(operation).finally(() => {
    if (entries.get(key) === promise) entries.delete(key)
  })
  entries.set(key, promise)
  onStarted()
  return promise
}

function inventory(host) {
  const count = inventoryPending.get(host) || 0
  if (count >= 2) return Promise.reject(new Error('Source inventory request limit reached'))
  inventoryPending.set(host, count + 1)
  return Promise.resolve().then(() => host.profileRoutes()).finally(() => {
    const remaining = (inventoryPending.get(host) || 1) - 1
    if (remaining) inventoryPending.set(host, remaining)
    else inventoryPending.delete(host)
  })
}

async function within(promise, timeoutMs) {
  let timer
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Source request timed out')), timeoutMs) })
    ])
  } finally {
    clearTimeout(timer)
  }
}

export async function discoverSessions(host, options = {}) {
  const timeoutMs = Number.isFinite(options.timeoutMs) && options.timeoutMs > 0 ? options.timeoutMs : DEFAULT_TIMEOUT_MS
  const coverage = { complete: false, inventoryKnown: false, limitPerSource: SESSION_LIMIT }
  let routes
  try { routes = await within(inventory(host), timeoutMs) } catch { return { sessions: [], sources: [], coverage } }
  if (!Array.isArray(routes) || !routes.length) return { sessions: [], sources: [], coverage }
  routes = routes.map(route => route && typeof route === 'object' ? { ...route } : route)
  const counts = new Map()
  for (const route of routes.filter(identified)) counts.set(identity(route), (counts.get(identity(route)) || 0) + 1)
  const candidates = routes.map((route, index) => ({ route, index }))
    .filter(({ route }) => usable(route) && counts.get(identity(route)) === 1)
    .sort((a, b) => identity(a.route) < identity(b.route) ? -1 : identity(a.route) > identity(b.route) ? 1 : 0)
  const byIdentity = new Map(candidates.map(candidate => [identity(candidate.route), candidate]))
  const queue = (sourceQueue.get(host) || []).filter(key => byIdentity.has(key))
  const queued = new Set(queue)
  for (const key of byIdentity.keys()) {
    if (!queued.has(key)) queue.push(key)
  }
  sourceQueue.set(host, queue)
  const outstanding = pendingFor(host)
  const chosen = queue.filter(key => !outstanding.has(requestIdentity(byIdentity.get(key).route))).slice(0, MAX_SOURCES)
  const selected = new Set(chosen.map(key => byIdentity.get(key).index))
  const scanOrder = [...chosen.map(key => byIdentity.get(key).index), ...routes.map((_, index) => index).filter(index => !selected.has(index))]
  const results = new Array(routes.length)
  let cursor = 0
  const scan = async index => {
    const route = routes[index]
    if (!usable(route) || counts.get(identity(route)) !== 1) {
      results[index] = { route, status: 'unavailable', rows: [], reportedTotal: null }
      return
    }
    if (!selected.has(index)) {
      results[index] = { route, status: 'not_scanned', rows: [], reportedTotal: null }
      return
    }
    try {
      if (typeof host.listPersistedSessions !== 'function') throw new Error('Session listing unavailable')
      const key = requestIdentity(route)
      const data = await within(request(host, key, () => host.listPersistedSessions({ ...route }, { profile: route.targetProfile, limit: SESSION_LIMIT }), () => {
        const current = sourceQueue.get(host)
        const index = current?.indexOf(identity(route)) ?? -1
        if (index >= 0) current.push(current.splice(index, 1)[0])
      }), timeoutMs)
      if (!data || !Array.isArray(data.sessions)) throw new Error('Invalid session listing')
      const seen = new Set()
      const rows = data.sessions.map(row => {
        if (!row || typeof row.id !== 'string' || !row.id || row.id !== row.id.trim() || row.profile !== route.targetProfile) throw new Error('Invalid session owner')
        const storedId = row._lineage_root_id || row.id
        if (typeof storedId !== 'string' || !storedId || storedId !== storedId.trim() || seen.has(storedId)) throw new Error('Invalid session identity')
        seen.add(storedId)
        return { route: { ...route }, storedId, title: typeof row.title === 'string' && row.title.trim() ? row.title : null,
          unread: typeof row.unread === 'boolean' ? row.unread : null, review: 'unknown' }
      })
      const total = data.profile_totals?.[route.targetProfile]
      const reportedTotal = nonnegative(total) ? total : null
      const partial = reportedTotal === null || (data.errors?.length > 0) || (data.storage && Object.keys(data.storage).length > 0) || reportedTotal > rows.length
      results[index] = { route, status: partial ? 'partial' : 'sampled', rows, reportedTotal }
    } catch (error) {
      results[index] = { route, status: error?.code === 'FOLLOWUP_NOT_SCANNED' ? 'not_scanned' : 'unavailable', rows: [], reportedTotal: null }
    }
  }
  const worker = async () => {
    while (cursor < scanOrder.length) await scan(scanOrder[cursor++])
  }
  await Promise.all(Array.from({ length: Math.min(4, routes.length) }, worker))
  let current
  try { current = await within(inventory(host), timeoutMs) } catch { current = [] }
  for (const source of results) {
    if (!['sampled', 'partial'].includes(source.status)) continue
    const matches = Array.isArray(current) ? current.filter(candidate => identified(candidate) && identity(candidate) === identity(source.route)) : []
    if (matches.length !== 1 || !usable(matches[0]) || matches[0].targetProfile !== source.route.targetProfile || matches[0].mode !== source.route.mode) {
      source.status = 'unavailable'
      source.rows = []
      source.reportedTotal = null
    }
  }
  if (Array.isArray(current)) {
    const initial = new Set(routes.filter(usable).map(identity))
    for (const route of current) {
      if (usable(route) && !initial.has(identity(route))) {
        results.push({ route: { ...route }, status: 'not_scanned', rows: [], reportedTotal: null })
        initial.add(identity(route))
      }
    }
  }
  coverage.inventoryKnown = results.some(source => usable(source.route))
  return { sessions: results.flatMap(source => source.rows), sources: results.map(({ rows, ...source }) => source), coverage }
}
