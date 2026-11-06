import React, { useEffect } from 'react'
import { Badge, Button, Codicon, Loader, Streamdown, host, useValue, useQuery, useQueryClient, usePluginI18n } from '@hermes/plugin-sdk'
import { acceptSnapshot, identityKey, loadOverview } from './core.mjs'

export const ID = 'task-overview'
export const strings = {
  zh: {
    persistedSnapshot: '持久化快照；尚未落盘的更新可能暂不可见。',
    staleUpdateNote: '登记后有新要求、执行或待办更新，需要重新核对。',
    todoSource: 'Hermes 原生配对待办 · 版本',
    pane: '任务概览', refresh: '刷新', requirements: '原始要求', current: '当前要求', result: '最新回复', remaining: '剩余必做', deferred: '延后建议', decision: '需要你决定', stages: '阶段与验收证据', checklist: 'Agent 待办清单', cancelled: '已取消', original: '查看原始要求', register: '补充任务登记', registerHint: '将核对请求放入当前会话输入框；由你发送。已有草稿会保留。', registerFail: '无法写入当前会话输入框，请在聊天中要求 Agent 登记任务状态。', unknown: '未验证', registered: '已登记 · 待核对', unregistered: '未登记验收', stale: '登记已过期', failed: '未通过', passed: '已通过', pending: '待完成', not_applicable: '不适用 · 已登记', in_progress: '进行中', completed: 'Agent 标记完成', working: 'Agent 正在工作', idle: 'Agent 当前空闲', gateway: '连接已断开 · 运行状态未知', live: '读取真实会话', noSession: '选择一个会话查看任务', noOwner: '当前会话归属尚未确定，暂不读取数据。', loading: '正在核对会话记录', error: '任务数据读取失败', retry: '重试', empty: '当前会话还没有任务记录。', noTodos: '未发现有效待办记录，剩余工作尚未核对。', noRemaining: '清单没有未完成项；整体验收仍需逐项核对。', noDeferred: '未登记延后建议', noDecision: '未登记待决定事项', none: '无', source: '记录来源', row: '消息', registration: 'Agent 登记', registrationNote: '登记是 Agent 的状态陈述；证据引用尚未独立复核。', staleNote: '登记后出现新要求或文件修改，需要重新核对。', verification: '原生验证账本', verificationScope: '仅覆盖所列命令与范围', noVerification: '暂无有效验证记录', unavailable: '验证数据不可用', show: '展开原文', updated: '最后读取', unknownTime: '时间未知', stageImplementation: '实现', stageTests: '测试', stageReview: '独立审查', stageIntegration: '集成', stageActivation: '实际生效', evidence: '证据引用', taskGoal: '任务目标', noResult: '暂无最终回复', detached: '真实会话快照；不是运行状态订阅', fetchStale: '上次读取已失败；以下是旧快照', choices: '切换会话', noRegistration: '未登记任务说明；原始要求和待办仍可查看。', detail: '数据与完整性', readOnly: '只读，不运行测试或修改任务', prompt: '请核对本会话原始目标、当前要求和实际证据，用 task-overview 的 ::task 指令登记任务标题、目标、结论、剩余必做、延后建议、待我决定事项，以及实现/测试/独立审查/集成/生效五个阶段。只填写有依据的状态；缺少证据填 pending，不能把 Agent 的自述当独立验收。新要求或修改后请更新登记。'
  },
  en: {
    persistedSnapshot: 'Persisted snapshot; updates not yet saved may be temporarily absent.',
    staleUpdateNote: 'Requirements, execution or checklist changes followed this record. Reconcile it again.',
    todoSource: 'Hermes native paired checklist · revision',
    pane: 'Task overview', refresh: 'Refresh', requirements: 'Original request', current: 'Current request', result: 'Latest reply', remaining: 'Required next steps', deferred: 'Deferred suggestions', decision: 'Your decision', stages: 'Stages and evidence', checklist: 'Agent checklist', cancelled: 'Cancelled', original: 'Original request', register: 'Update task record', registerHint: 'Append a review request to this chat draft. You choose when to send it.', registerFail: 'Cannot write to this chat composer. Ask the agent to update its task record in chat.', unknown: 'Unverified', registered: 'Recorded · needs review', unregistered: 'Acceptance not recorded', stale: 'Record outdated', failed: 'Failed', passed: 'Passed', pending: 'Pending', not_applicable: 'N/A · recorded', in_progress: 'In progress', completed: 'Marked done by agent', working: 'Agent working', idle: 'Agent idle', gateway: 'Disconnected · runtime unknown', live: 'Real session data', noSession: 'Select a conversation', noOwner: 'Session owner unresolved. No data will be loaded.', loading: 'Checking session records', error: 'Could not read task data', retry: 'Retry', empty: 'This chat has no task record yet.', noTodos: 'No valid checklist found. Remaining work has not been reconciled.', noRemaining: 'No pending checklist entries. Acceptance still needs review.', noDeferred: 'No deferred suggestions recorded', noDecision: 'No decisions recorded', none: 'None', source: 'Source', row: 'Message', registration: 'Agent record', registrationNote: 'This is an agent statement. Referenced evidence has not been independently verified.', staleNote: 'New requirements or edits followed this record. Reconcile it again.', verification: 'Native verification ledger', verificationScope: 'Covers only the listed command and scope', noVerification: 'No valid verification record', unavailable: 'Verification data unavailable', show: 'Read full text', updated: 'Last read', unknownTime: 'Unknown time', stageImplementation: 'Implementation', stageTests: 'Tests', stageReview: 'Independent review', stageIntegration: 'Integration', stageActivation: 'Live activation', evidence: 'Evidence reference', taskGoal: 'Task goal', noResult: 'No final reply yet', detached: 'Real session snapshot, not a live runtime subscription', fetchStale: 'Last refresh failed; this is an older snapshot', choices: 'Switch conversation', noRegistration: 'No task description registered. Original requests and checklist remain available.', detail: 'Data and completeness', readOnly: 'Read only. Does not run checks or change tasks.', prompt: 'Reconcile the original goal, current requirements, and real evidence in this chat. Use the task-overview ::task directive to record title, goal, result, remaining required work, deferred suggestions, owner decisions and five stages: implementation, tests, independent review, integration and activation. Only record supported states. Missing evidence is pending. An agent statement is not independent acceptance. Update after new requirements or edits.'
  }
}
export const panelCss = `.task-overview{font-size:13px;line-height:1.55;color:var(--ui-text-primary,var(--foreground));min-width:0;height:100%;overflow:auto;padding:16px;box-sizing:border-box}.task-overview *{box-sizing:border-box}.task-overview h2{font-size:18px;line-height:1.45;font-weight:600;letter-spacing:-.02em;margin:4px 0 12px;overflow-wrap:anywhere}.task-overview h3{font-size:12px;font-weight:600;color:var(--ui-text-secondary,var(--muted-foreground));margin:20px 0 8px}.task-overview p{margin:6px 0;overflow-wrap:anywhere}.task-overview .muted{color:var(--ui-text-secondary,var(--muted-foreground));font-size:12px}.task-overview .toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px;flex-wrap:wrap}.task-overview .stack{display:flex;gap:7px;flex-wrap:wrap;align-items:center}.task-overview .stage{display:flex;gap:10px;align-items:baseline;justify-content:space-between;padding:5px 0}.task-overview .stage span{overflow-wrap:anywhere}.task-overview .stage-name{flex-shrink:0}.task-overview .evidence{font-size:11px;color:var(--ui-text-secondary,var(--muted-foreground));overflow-wrap:anywhere;white-space:pre-wrap;margin:2px 0 10px}.task-overview details{margin:10px 0}.task-overview summary{cursor:pointer;color:var(--ui-text-secondary,var(--muted-foreground));font-size:12px;min-height:28px;line-height:28px}.task-overview pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;max-height:360px;overflow:auto;background:transparent;margin:8px 0;overscroll-behavior-y:auto}.task-overview ul{list-style:none;margin:0;padding:0}.task-overview li{display:flex;align-items:baseline;gap:8px;padding:4px 0;overflow-wrap:anywhere}.task-overview li>span:last-child{min-width:0}.task-overview .marker{color:var(--ui-text-secondary,var(--muted-foreground));flex-shrink:0}.task-overview .notice{padding:8px 0;color:var(--ui-text-secondary,var(--muted-foreground));font-size:12px}.task-overview footer{margin-top:22px;padding-top:12px;border-top:1px solid var(--ui-stroke-tertiary,var(--border));font-size:11px;color:var(--ui-text-secondary,var(--muted-foreground));overflow-wrap:anywhere}.task-overview [data-testid=acceptance]{margin-bottom:12px}.task-overview .request{max-height:96px;overflow:hidden;white-space:pre-wrap}.task-overview .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}@media(max-width:360px){.task-overview{padding:12px}.task-overview h2{font-size:16px}}`
const stamp = time => time ? new Date(time > 1e12 ? time : time * 1000).toLocaleString() : '—'
const Reference = ({ item, t }) => item?.rowId ? <div className="evidence">{t('source')} · {t('row')} #{item.rowId} · {stamp(item.timestamp)}</div> : null
const FullText = ({ label, item, t }) => item ? <details><summary>{label}</summary><pre>{item.text}</pre><Reference item={item} t={t}/></details> : null
const Items = ({ items, empty, t }) => items.length ? <ul>{items.map((item, index) => <li key={item.id ?? index}><span className="marker" aria-hidden="true">{item.status === 'in_progress' ? '◉' : '○'}</span><span>{typeof item === 'string' ? item : item.content}{item.status === 'in_progress' ? ` · ${t('in_progress')}` : ''}</span></li>)}</ul> : <p className="muted">{empty}</p>
const stageLabel = { implementation: 'stageImplementation', tests: 'stageTests', review: 'stageReview', integration: 'stageIntegration', activation: 'stageActivation' }
export function OverviewView({ value, t, error, refreshing, onRefresh, onRegister, runtime, snapshot = false, selector = null }) {
  return <section className="task-overview" aria-label={t('pane')}>
    <style>{panelCss}</style>
    <div className="toolbar"><span className="muted">{t('pane')}</span>{onRefresh ? <Button size="xs" variant="ghost" onClick={onRefresh} disabled={refreshing} aria-label={t('refresh')}><Codicon name="refresh"/>{t('refresh')}</Button> : null}</div>
    {selector}
    {error ? <div role="alert"><strong>{t('error')}</strong><p className="muted">{String(error.message || error)}</p>{value ? <p>{t('fetchStale')}</p> : null}</div> : null}
    {!value ? <p className="muted">{t('empty')}</p> : <>
      <div className="stack muted"><span data-testid="runtime">{snapshot ? t('detached') : t(runtime === 'working' ? 'working' : runtime === 'disconnected' ? 'gateway' : 'idle')}</span></div>
      <h2>{value.title || t('noSession')}</h2>
      <div data-testid="acceptance"><Badge variant={value.acceptance === 'stale' ? 'warn' : 'muted'}>{t(value.acceptance)}</Badge></div>
      <p className="notice">{value.acceptance === 'stale' ? t('staleUpdateNote') : value.registration ? t('registrationNote') : t('noRegistration')}</p>
      {value.goal ? <><h3>{t('taskGoal')}</h3><p>{value.goal}</p></> : null}
      <h3>{t('remaining')}</h3><Items items={value.remaining} empty={value.todos === null ? t('noTodos') : t('noRemaining')} t={t}/>
      {value.registeredRemaining.length ? <><p className="muted">{t('registration')}{value.registrationStale ? ` · ${t('stale')}` : ''}</p><Items items={value.registeredRemaining} t={t}/></> : null}
      <h3>{t('decision')}</h3><Items items={value.decisions} empty={t('noDecision')} t={t}/>
      <h3>{t('result')}</h3>
      {value.result ? <p>{value.result}</p> : value.latest ? <div className="request"><Streamdown mode="static">{value.latest.text.replace(/^::task\{.*\}$/gm, '').trim()}</Streamdown></div> : <p className="muted">{t('noResult')}</p>}
      <FullText label={t('show')} item={value.latest} t={t}/>
      <FullText label={t('requirements')} item={value.original} t={t}/>
      {value.current?.rowId !== value.original?.rowId ? <FullText label={t('current')} item={value.current} t={t}/> : null}
      <h3>{t('deferred')}</h3><Items items={value.deferred} empty={t('noDeferred')} t={t}/>
      <details><summary>{t('stages')}</summary>{value.stages.map(stage => <div key={stage.id}><div className="stage"><span className="stage-name">{t(stageLabel[stage.id])}</span><Badge variant={stage.status === 'failed' ? 'destructive' : 'muted'}>{t(stage.status)}</Badge></div>{stage.evidence ? <div className="evidence">{t('evidence')} · {stage.evidence}<Reference item={stage.source} t={t}/></div> : null}</div>)}</details>
      <details><summary>{t('verification')}</summary>{value.verificationCwd ? <p className="evidence">{value.verificationCwd}{value.verification?.root ? ` → ${value.verification.root}` : ''}</p> : null}{value.verificationError ? <p>{t('unavailable')} · {value.verificationError}</p> : value.verification?.evidence ? <><p>{value.verification.status} · {value.verification.evidence.kind} · {value.verification.evidence.scope}</p><pre>{value.verification.evidence.command}</pre><p className="muted">{t('verificationScope')}</p><pre>{value.verification.evidence.output_summary}</pre></> : <p className="muted">{t('noVerification')} · {value.verification?.status || 'unknown'}</p>}</details>
      <details><summary>{t('checklist')}{value.todos !== null ? ` · ${value.todos.length}` : ''}</summary><ul>{(value.todos || []).map(item => <li key={item.id}><span className="marker">{item.status === 'completed' ? '✓' : item.status === 'cancelled' ? '−' : '○'}</span><span>{item.content} · {t(item.status)}</span></li>)}</ul><p className="muted">{t('todoSource')} · {value.todoRevision ?? '—'}</p></details>
      {onRegister ? <div className="actions"><Button size="sm" variant="secondary" onClick={onRegister}>{t('register')}</Button><span className="muted">{t('registerHint')}</span></div> : null}
      <footer><div>{t('updated')} · {stamp(value.fetchedAt)}</div><div data-testid="snapshot-scope">{t('persistedSnapshot')}</div><div>{value.scope?.owner?.connectionId} / {value.scope?.owner?.profile} / {value.scope?.storedId}</div><div>{t('detail')} · {value.messageCount} messages</div><div>{t('readOnly')}</div></footer>
    </>}
  </section>
}

export function TaskPane() {
  const t = usePluginI18n(ID)
  const storedId = useValue(host.state.focusedStoredSessionId)
  const runtimeId = useValue(host.state.focusedSessionId)
  const owner = useValue(host.state.focusedSessionOwner)
  const busy = useValue(host.state.busy)
  const gateway = useValue(host.state.gateway)
  const visibleAtom = host.paneVisibility(`${ID}:pane`)
  const paneVisible = useValue(visibleAtom)
  const scope = { storedId, runtimeId, owner }
  const key = identityKey(scope)
  const client = useQueryClient()
  const query = useQuery({ queryKey: [ID, key], queryFn: () => loadOverview(host, scope), enabled: Boolean(storedId && runtimeId && owner && paneVisible && gateway === 'open'), refetchInterval: paneVisible ? 15000 : false, staleTime: 5000, retry: false, refetchOnWindowFocus: true })
  useEffect(() => {
    const types = ['todo.updated', 'turn.complete', 'turn.failed', 'session.title']
    const off = types.map(type => host.onEvent(type, event => {
      if (event.session_id === runtimeId) client.invalidateQueries({ queryKey: [ID, key] })
    }))
    return () => off.forEach(dispose => dispose())
  }, [client, key, runtimeId])
  const value = acceptSnapshot(key, query.data)
  if (!storedId) return <section className="task-overview"><style>{panelCss}</style><p>{t('noSession')}</p></section>
  if (!owner) return <section className="task-overview"><style>{panelCss}</style><p>{t('noOwner')}</p></section>
  if (gateway !== 'open' && !value) return <section className="task-overview"><style>{panelCss}</style><p>{t('gateway')}</p></section>
  if (query.isPending && !value && !query.isError) return <section className="task-overview"><style>{panelCss}</style><Loader/><p>{t('loading')}</p></section>
  const register = async () => {
    const target = host.state.focusedSessionId.get()
    if (!target || identityKey({ storedId: host.state.focusedStoredSessionId.get(), runtimeId: target, owner: host.state.focusedSessionOwner.get() }) !== key) return
    const ok = await host.composer.insertText(target, t('prompt'), { mode: 'block' })
    if (!ok) host.notify({ kind: 'error', message: t('registerFail') })
  }
  return <OverviewView value={value} t={t} error={query.error} refreshing={query.isFetching} onRefresh={() => query.refetch()} onRegister={register} runtime={gateway !== 'open' ? 'disconnected' : busy ? 'working' : 'idle'}/>
}
