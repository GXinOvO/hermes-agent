import { applyTaskAction, createTask, type Task, type TaskAction, type TaskReference, taskReferenceKey, validateTasks } from './model'

const STORE = 'tasks'
const DEFAULT_DATABASE = 'hermes.plugin.task-overview.followup.v1'

export interface TaskRow {
  task: Task
  revision: number
}

export interface TaskStore {
  changePrimary: (id: string, expectedRevision: number, primary: TaskReference, at: number) => Promise<TaskRow>
  close: () => void
  create: (input: Record<string, unknown>, at: number) => Promise<TaskRow>
  createIfAbsent: (input: Record<string, unknown>, at: number, primary: TaskReference, nextStep: string) => Promise<{ created: boolean; row: TaskRow }>
  list: () => Promise<TaskRow[]>
  replace: (task: Task, expectedRevision: number) => Promise<TaskRow>
}

function checkedRow(value: unknown): TaskRow {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Invalid stored task')
  }

  const row = value as { id?: unknown; revision?: unknown; task?: unknown }

  if (!Number.isSafeInteger(row.revision) || (row.revision as number) < 1) {
    throw new Error('Invalid stored task revision')
  }

  const task = validateTasks([row.task])[0]

  if (row.id !== task.id) {
    throw new Error('Invalid stored task identity')
  }

  return { revision: row.revision as number, task }
}

function transaction<T>(db: IDBDatabase, mode: IDBTransactionMode, operation: (store: IDBObjectStore, finish: (value: T) => void, fail: (error: unknown) => void) => void): Promise<T> {
  return new Promise((resolve, reject) => {
    let tx: IDBTransaction

    try {
      tx = db.transaction(STORE, mode)
    } catch (error) {
      reject(error)

      return
    }

    let result: T | undefined
    let failure: unknown
    tx.oncomplete = () => resolve(result as T)

    tx.onerror = () => { failure ||= tx.error || new Error('Task storage transaction failed') }
    tx.onabort = () => reject(failure || tx.error || new Error('Task storage transaction aborted'))

    const fail = (error: unknown) => {
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

export function openTaskStore(factory: IDBFactory = globalThis.indexedDB, name = DEFAULT_DATABASE): Promise<TaskStore> {
  if (!factory || typeof factory.open !== 'function' || !name.trim()) {
    return Promise.reject(new Error('Task storage unavailable'))
  }

  return new Promise((resolve, reject) => {
    let request: IDBOpenDBRequest

    try {
      request = factory.open(name, 2)
    } catch (error) {
      reject(error)

      return
    }

    let settled = false

    const rejectOnce = (error: unknown) => {
      if (!settled) {
        settled = true
        reject(error)
      }
    }

    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) {
        request.result.createObjectStore(STORE, { keyPath: 'id' })
      }
    }

    request.onerror = () => rejectOnce(request.error || new Error('Task storage open failed'))
    request.onblocked = () => rejectOnce(new Error('Task storage upgrade blocked'))

    request.onsuccess = () => {
      const db = request.result

      if (settled) {
        db.close()

        return
      }

      settled = true
      db.onversionchange = () => db.close()

      resolve({
        changePrimary: (id, expectedRevision, primary, at) => transaction(db, 'readwrite', (store, finish, fail) => {
          const get = store.get(id)

          get.onsuccess = () => {
            try {
              const current = checkedRow(get.result)

              if (current.revision !== expectedRevision) { throw new Error('Task revision conflict') }
              const task = applyTaskAction(current.task, { at, primary, type: 'set_primary' })
              const revision = current.revision + 1
              const put = store.put({ id, revision, task })
              put.onsuccess = () => finish({ revision, task })
            } catch (error) { fail(error) }
          }
        }),
        close: () => db.close(),
        list: () => transaction(db, 'readonly', (store, finish, fail) => {
          const get = store.getAll()

          get.onsuccess = () => {
            try { finish(get.result.map(checkedRow)) } catch (error) { fail(error) }
          }
        }),
        create: (input, at) => {
          let task: Task

          try { task = createTask(input, at) } catch (error) { return Promise.reject(error) }

          return transaction(db, 'readwrite', (store, finish) => {
            const add = store.add({ id: task.id, revision: 1, task })
            add.onsuccess = () => finish({ revision: 1, task })
          })
        },
        createIfAbsent: (input, at, primary, nextStep) => {
          let task: Task

          try { task = createTask(input, at) } catch (error) { return Promise.reject(error) }

          return transaction(db, 'readwrite', (store, finish, fail) => {
            const get = store.getAll()

            get.onsuccess = () => {
              try {
                const duplicate = get.result.map(checkedRow).find(row =>
                  taskReferenceKey(row.task.primary) === taskReferenceKey(primary) && row.task.nextSteps.includes(nextStep)
                )

                if (duplicate) {
                  finish({ created: false, row: duplicate })

                  return
                }

                const add = store.add({ id: task.id, revision: 1, task })
                add.onsuccess = () => finish({ created: true, row: { revision: 1, task } })
                add.onerror = () => fail(add.error || new Error('Task creation failed'))
              } catch (error) { fail(error) }
            }
          })
        },
        replace: (task, expectedRevision) => {
          if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 1) {
            return Promise.reject(new Error('Invalid task revision'))
          }

          return transaction(db, 'readwrite', (store, finish, fail) => {
            const get = store.get(task.id)

            get.onsuccess = () => {
              try {
                const current = checkedRow(get.result)

                if (current.revision !== expectedRevision) {throw new Error('Task revision conflict')}

                if (
                  current.task.createdAt !== task.createdAt ||
                  taskReferenceKey(current.task.primary) !== taskReferenceKey(task.primary)
                ) {
                  throw new Error('Task identity changed')
                }

                const revision = current.revision + 1
                const put = store.put({ id: task.id, revision, task: validateTasks([task])[0] })
                put.onsuccess = () => finish({ revision, task })
              } catch (error) { fail(error) }
            }
          })
        }
      })
    }
  })
}

export function taskChanged(task: Task, action: TaskAction): Task {
  return applyTaskAction(task, action)
}
