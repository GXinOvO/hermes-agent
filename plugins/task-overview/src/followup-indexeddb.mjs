import { applyFollowupAction, assertStableRelatedModes, validateFollowupTasks } from './followup-tasks.mjs'

const STORE = 'tasks'
const DEFAULT_DATABASE = 'hermes.plugin.task-overview.followup.v1'

function checkedTask(task) {
  return validateFollowupTasks([task])[0]
}

function checkedRow(row) {
  if (!row || !Number.isSafeInteger(row.revision) || row.revision < 1) throw new Error('Invalid stored follow-up record')
  const task = checkedTask(row.task)
  if (row.id !== task.id) throw new Error('Invalid stored follow-up identity')
  return { task, revision: row.revision }
}

function transaction(db, mode, operation) {
  return new Promise((resolve, reject) => {
    let tx
    try {
      tx = db.transaction(STORE, mode)
    } catch (error) {
      reject(error)
      return
    }
    let result, failure
    tx.oncomplete = () => resolve(result)
    tx.onabort = () => reject(failure || tx.error || new Error('Follow-up storage transaction aborted'))
    tx.onerror = () => { failure ||= tx.error || new Error('Follow-up storage transaction failed') }
    const fail = error => {
      failure = error
      try { tx.abort() } catch { reject(error) }
    }
    try {
      operation(tx.objectStore(STORE), value => { result = value }, fail)
    } catch (error) {
      fail(error)
    }
  })
}

export function openFollowupStore(factory, name = DEFAULT_DATABASE) {
  if (!factory || typeof factory.open !== 'function' || typeof name !== 'string' || !name.trim()) return Promise.reject(new Error('Follow-up storage unavailable'))
  return new Promise((resolve, reject) => {
    let request, settled = false
    try {
      request = factory.open(name, 1)
    } catch (error) {
      reject(error)
      return
    }
    const rejectOnce = error => { if (!settled) { settled = true; reject(error) } }
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) request.result.createObjectStore(STORE, { keyPath: 'id' })
    }
    request.onerror = () => rejectOnce(request.error || new Error('Follow-up storage open failed'))
    request.onblocked = () => rejectOnce(new Error('Follow-up storage upgrade blocked by another window'))
    request.onsuccess = () => {
      const db = request.result
      if (settled) { db.close(); return }
      settled = true
      db.onversionchange = () => db.close()
      resolve({
        close: () => db.close(),
        list: () => transaction(db, 'readonly', (store, finish, fail) => {
          const req = store.getAll()
          req.onsuccess = () => {
            try { finish(req.result.map(checkedRow)) } catch (error) { fail(error) }
          }
        }),
        create: input => {
          let task
          try { task = checkedTask(input) } catch (error) { return Promise.reject(error) }
          const row = { id: task.id, task, revision: 1 }
          return transaction(db, 'readwrite', (store, finish) => {
            const req = store.add(row)
            req.onsuccess = () => finish({ task, revision: 1 })
          })
        },
        replace: (input, expectedRevision) => {
          let task
          try {
            task = checkedTask(input)
            if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 1 || expectedRevision >= Number.MAX_SAFE_INTEGER) throw new Error('Invalid follow-up revision')
          } catch (error) { return Promise.reject(error) }
          return transaction(db, 'readwrite', (store, finish, fail) => {
            const get = store.get(task.id)
            get.onsuccess = () => {
              try {
                if (!get.result) throw new Error('Follow-up task not found')
                const current = checkedRow(get.result)
                if (current.revision !== expectedRevision) throw new Error('Follow-up task revision conflict')
                if (current.task.createdAt !== task.createdAt || JSON.stringify(current.task.primary) !== JSON.stringify(task.primary)) throw new Error('Follow-up task identity changed')
                assertStableRelatedModes(current.task.related, task.related)
                if (task.updatedAt < current.task.updatedAt) throw new Error('Follow-up task update time moved backwards')
                const revision = current.revision + 1
                const put = store.put({ id: task.id, task, revision })
                put.onsuccess = () => finish({ task, revision })
              } catch (error) { fail(error) }
            }
          })
        },
        changePrimary: (id, expectedRevision, primary, at) => {
          if (typeof id !== 'string' || !id.trim() || !Number.isSafeInteger(expectedRevision) || expectedRevision < 1 || expectedRevision >= Number.MAX_SAFE_INTEGER) return Promise.reject(new Error('Invalid follow-up revision'))
          return transaction(db, 'readwrite', (store, finish, fail) => {
            const get = store.get(id)
            get.onsuccess = () => {
              try {
                if (!get.result) throw new Error('Follow-up task not found')
                const current = checkedRow(get.result)
                if (current.revision !== expectedRevision) throw new Error('Follow-up task revision conflict')
                const task = applyFollowupAction(current.task, { type: 'set_primary', primary, at })
                const revision = current.revision + 1
                const put = store.put({ id, task, revision })
                put.onsuccess = () => finish({ task, revision })
              } catch (error) { fail(error) }
            }
          })
        },
        remove: (id, expectedRevision) => {
          if (typeof id !== 'string' || !id.trim() || !Number.isSafeInteger(expectedRevision) || expectedRevision < 1) return Promise.reject(new Error('Invalid follow-up deletion'))
          return transaction(db, 'readwrite', (store, finish, fail) => {
            const get = store.get(id)
            get.onsuccess = () => {
              try {
                if (!get.result) throw new Error('Follow-up task not found')
                const current = checkedRow(get.result)
                if (current.revision !== expectedRevision) throw new Error('Follow-up task revision conflict')
                const request = store.delete(id)
                request.onsuccess = () => finish({ id })
              } catch (error) { fail(error) }
            }
          })
        }
      })
    }
  })
}
