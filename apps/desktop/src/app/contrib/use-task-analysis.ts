import { useCallback, useEffect, useRef, useState } from 'react'

import { cachedTaskAnalysis, loadTaskAnalysis } from '@/lib/task-overview/analysis'
import type { TaskAnalysis } from '@/lib/task-overview/analysis-model'
import { type Task, taskReferenceKey } from '@/lib/task-overview/model'
import { openTaskStore, type TaskRow, type TaskStore } from '@/lib/task-overview/store'

interface AnalysisState {
  result: TaskAnalysis | null
  busy: boolean
  error: string | null
  refresh: boolean
}

interface TaskAnalysisOptions {
  task: Task
  autoAnalyze: boolean
  onUpdated: (row: TaskRow) => void
  onAutoAnalyzeStarted: () => void
}

function analysisIdentity(task: Task): string {
  return JSON.stringify([task.id, task.title, taskReferenceKey(task.primary)])
}

export function useTaskAnalysis(options: TaskAnalysisOptions) {
  const identity = analysisIdentity(options.task)
  const latest = useRef(options)
  latest.current = options
  const mounted = useRef(false)
  const request = useRef(0)
  const busy = useRef(false)
  const [state, setState] = useState<AnalysisState>(() => ({ result: cachedTaskAnalysis(options.task), busy: false, error: null, refresh: false }))

  const run = useCallback(async (refresh: boolean) => {
    const { task } = latest.current

    if (!task.primary || busy.current || !mounted.current) { return }
    const target = analysisIdentity(task)
    const token = ++request.current
    const active = () => mounted.current && token === request.current && target === analysisIdentity(latest.current.task)
    busy.current = true
    setState(current => ({ ...current, busy: true, error: null, refresh }))
    let store: TaskStore | undefined

    try {
      const result = await loadTaskAnalysis(task, refresh)

      if (!active()) { return }
      setState(current => ({ ...current, result: result ?? current.result }))

      if (result) {
        store = await openTaskStore()

        if (!active()) { return }
        const row = await store.saveAnalysis(task.id, task.title, task.primary, result)

        if (active()) { latest.current.onUpdated(row) }
      }
    } catch (cause) {
      if (active()) { setState(current => ({ ...current, error: cause instanceof Error ? cause.message : String(cause) })) }
    } finally {
      store?.close()

      if (active()) { busy.current = false; setState(current => ({ ...current, busy: false })) }
    }
  }, [])

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (mount flag, see eslint rule comment)
  useEffect(() => {
    mounted.current = true
    busy.current = false
    const { task, autoAnalyze, onAutoAnalyzeStarted } = latest.current
    const token = ++request.current
    setState({ result: cachedTaskAnalysis(task), busy: false, error: null, refresh: autoAnalyze })
    queueMicrotask(() => {
      if (!mounted.current || token !== request.current) { return }

      if (autoAnalyze) { onAutoAnalyzeStarted() }
      void run(autoAnalyze)
    })

    return () => { mounted.current = false; request.current++; busy.current = false }
  }, [identity, run])

  return { ...state, run }
}
