import React, { useState } from 'react'
import { Badge, Button, RowButton, SearchField, SegmentedControl } from '@hermes/plugin-sdk'

const css = `.followup-view{box-sizing:border-box;min-width:0;height:100%;overflow:auto;padding:16px;color:var(--ui-text-primary);font-size:13px;line-height:1.5}.followup-view *{box-sizing:border-box}.followup-view .top{display:flex;justify-content:space-between;align-items:center;gap:8px}.followup-view .title{font-size:14px;font-weight:600}.followup-view .tabs{display:flex;flex-wrap:wrap;gap:4px;margin:14px 0 8px}.followup-view .search{width:100%}.followup-view .notice,.followup-view .meta{color:var(--ui-text-secondary);font-size:12px}.followup-view .notice{margin:10px 0}.followup-view .list{margin-top:10px}.followup-view .row{display:flex;align-items:baseline;justify-content:space-between;gap:8px;width:100%;min-width:0;text-align:left;padding:9px 6px;border:0;border-bottom:1px solid var(--ui-stroke-secondary);background:transparent;color:inherit;cursor:pointer}.followup-view .row[aria-pressed=true]{background:var(--ui-bg-quaternary)}.followup-view .row:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}.followup-view .row-main{min-width:0;display:flex;flex-direction:column;gap:2px}.followup-view .row-title,.followup-view .meta{white-space:normal;overflow-wrap:anywhere}.followup-view .detail{margin-top:20px;padding-top:14px;border-top:1px solid var(--ui-stroke-secondary)}.followup-view h2{font-size:16px;font-weight:600;line-height:1.4;margin:8px 0}.followup-view h3{font-size:12px;font-weight:600;margin:18px 0 6px;color:var(--ui-text-secondary)}.followup-view ul{padding-left:18px;margin:4px 0}.followup-view li{margin:4px 0;overflow-wrap:anywhere}.followup-view .open{margin-top:9px}.followup-view .error{margin:12px 0}.followup-view .empty{margin:18px 0;color:var(--ui-text-secondary)}.followup-view .related{display:flex;flex-wrap:wrap;gap:4px}`

export function FollowupView({ t, rows = [], coverage = 'partial', error, loading = false, uncertain = false, exactNavigationAvailable = false, unavailableReferences = {}, busy = false, discovery = null, discovering = false, onDiscover, onOpen, onTrack, onCreate, onEdit, onDelete, onBind, onUnbind, onAccept, onReopen, onRetry, form = null }) {
  const [filter, setFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [selectedId, setSelectedId] = useState(null)
  const [priority, setPriority] = useState('any')
  const [sort, setSort] = useState('updated')
  const tasks = rows.filter(row => row?.task?.resolution?.state === 'open')
  const accepted = rows.filter(row => row?.task?.resolution?.state === 'accepted')
  const term = search.trim().toLocaleLowerCase()
  const taskStatus = task => task.status ?? (task.resolution.state === 'accepted' ? 'done' : 'unspecified')
  const scoped = filter === 'accepted' ? accepted : filter === 'everything' ? rows : ['todo', 'in_progress', 'waiting'].includes(filter) ? tasks.filter(row => taskStatus(row.task) === filter) : tasks
  const matches = scoped.filter(({ task }) => {
    const searchable = [task.title, task.description, task.notes, ...task.nextSteps, task.primary?.connectionId, task.primary?.profile, task.primary?.targetProfile, task.primary?.storedId,
      ...task.related.flatMap(ref => [ref.connectionId, ref.profile, ref.targetProfile, ref.storedId])].join(' ').toLocaleLowerCase()
    return (!term || searchable.includes(term)) && (priority === 'any' || (task.priority ?? 'normal') === priority)
  })
  const priorityOrder = { urgent: 0, high: 1, normal: 2, low: 3 }
  const visible = (filter === 'attention' ? matches.filter(row => row.attentionRequired === true) : matches).slice().sort((a, b) =>
    sort === 'priority' ? (priorityOrder[a.task.priority ?? 'normal'] - priorityOrder[b.task.priority ?? 'normal'] || b.task.updatedAt - a.task.updatedAt) : b.task.updatedAt - a.task.updatedAt)
  const sessionMatches = (discovery?.sessions || []).filter(session => !term || [session.title, session.storedId, session.route.connectionId, session.route.profile, session.route.targetProfile].join(' ').toLocaleLowerCase().includes(term))
  const selected = visible.find(row => row.task.id === selectedId) || visible[0] || null
  const count = value => error || loading || uncertain ? '—' : value
  return <section className="followup-view" aria-label={t('title')}>
    <style>{css}</style>
    <div className="top"><span className="title">{t('title')}</span><div><Button size="xs" variant="ghost" onClick={onTrack} disabled={loading || busy || Boolean(error)}>{t('add')}</Button> <Button size="xs" variant="secondary" onClick={onCreate} disabled={loading || busy || Boolean(error)}>{t('newTask')}</Button></div></div>
    {form}
    <div className="tabs" aria-label={t('title')}>
      <Button size="xs" variant={filter === 'all' ? 'secondary' : 'ghost'} aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>{t('all')} · {count(tasks.length)}</Button>
      <Button size="xs" variant={filter === 'everything' ? 'secondary' : 'ghost'} aria-pressed={filter === 'everything'} onClick={() => setFilter('everything')}>{t('allTasks')} · {count(rows.length)}</Button>
      {['in_progress', 'todo', 'waiting'].map(id => <Button key={id} size="xs" variant={filter === id ? 'secondary' : 'ghost'} aria-pressed={filter === id} onClick={() => setFilter(id)}>{t(id)} · {count(tasks.filter(row => taskStatus(row.task) === id).length)}</Button>)}
      <Button size="xs" variant={filter === 'attention' ? 'secondary' : 'ghost'} aria-pressed={filter === 'attention'} onClick={() => setFilter('attention')}>{t('attention')} · —</Button>
      <Button size="xs" variant={filter === 'accepted' ? 'secondary' : 'ghost'} aria-pressed={filter === 'accepted'} onClick={() => setFilter('accepted')}>{t('accepted')} · {count(accepted.length)}</Button>
      <Button size="xs" variant={filter === 'sessions' ? 'secondary' : 'ghost'} aria-pressed={filter === 'sessions'} onClick={() => setFilter('sessions')}>{t('reviewSessions')}</Button>
    </div>
    <SearchField containerClassName="search" placeholder={t('search')} aria-label={t('search')} value={search} onChange={setSearch}/>
    {filter !== 'sessions' ? <><div aria-label={t('priorityFilter')}><span className="meta">{t('priorityFilter')} </span><SegmentedControl options={['any', 'urgent', 'high', 'normal', 'low'].map(id => ({id, label:t(id)}))} value={priority} onChange={setPriority}/></div><div aria-label={t('sortBy')}><span className="meta">{t('sortBy')} </span><SegmentedControl options={['updated', 'priority'].map(id => ({id, label:t(id)}))} value={sort} onChange={setSort}/></div></> : null}
    {filter === 'sessions' ? <div>
      <p className="notice">{t('discoveryScope')}</p>
      <p className="notice">{t('discoveryUnknown')}</p>
      {!exactNavigationAvailable ? <p className="notice">{t('exactNavigationUnavailable')}</p> : null}
      <Button size="sm" variant="secondary" onClick={onDiscover} disabled={discovering}>{t(discovering ? 'loadingSessions' : 'loadSessions')}</Button>
      {discovery ? <>
        {discovering ? <p role="status">{t('discoveryRefreshing')}</p> : null}
        {!discovery.coverage.inventoryKnown ? <p role="alert">{t('sourceUnknown')}</p> : <p role="status">{t('inventoryMayBeStale')}</p>}
        {discovery.sources.map((source, index) => <p className="meta" key={index}>{source.route?.connectionId || '—'} / {source.route?.profile || '—'} · {t(({sampled:'sourceSampled',partial:'sourcePartial',unavailable:'sourceUnavailable',not_scanned:'sourceNotScanned'})[source.status])}</p>)}
        {sessionMatches.map(session => <div className="row" key={JSON.stringify([session.route.connectionId, session.route.profile, session.storedId])}>
          <span className="row-main"><span className="row-title">{session.title || session.storedId}</span><span className="meta">{session.route.connectionId} · {session.route.profile} · {session.route.targetProfile} · {session.storedId}{session.unread === true ? ` · ${t('unreadBackend')}` : ''}</span></span>
          <Button size="xs" variant="secondary" disabled={!exactNavigationAvailable} aria-label={`${t('openConversation')} ${session.title || session.storedId}`} onClick={() => onOpen({ ...session.route, storedId: session.storedId })}>{t('openConversation')}</Button>
        </div>)}
        {term && discovery.sessions.length > 0 && !sessionMatches.length ? <p className="empty">{t('noSessionMatches')}</p> : null}
        {!discovery.sessions.length && discovery.coverage.inventoryKnown ? <p className="empty">{t(discovery.sources.some(source => source.status !== 'sampled') ? 'discoveryIncomplete' : 'discoveryEmpty')}</p> : null}
      </> : null}
    </div> : <>
    {coverage !== 'complete' ? <p className="notice">{t('scope')}</p> : null}
    {error ? <div role="alert" className="error"><strong>{t('storageError')}</strong><p className="meta">{String(error.message || error)}</p>{onRetry ? <Button size="xs" variant="secondary" onClick={onRetry}>{t('retry')}</Button> : null}</div> : null}
    {loading ? <p className="empty">{t('loading')}</p> : error ? null : <>
      {filter === 'attention' ? <p className="notice">{t('noAction')}</p> : null}
      {visible.length ? <div className="list" aria-label={filter === 'accepted' ? t('accepted') : t('all')}>{visible.map(({ task }) => <RowButton key={task.id} className="row" aria-pressed={selected?.task.id === task.id} onClick={() => setSelectedId(task.id)}>
        <span className="row-main"><span className="row-title">{task.title}</span>{task.nextSteps[0] ? <span className="meta">{t('nextAction')} · {task.nextSteps[0]}</span> : null}<span className="meta">{task.primary ? [task.primary.connectionId, task.primary.profile, task.primary.targetProfile, task.primary.storedId].filter(Boolean).join(' · ') : t('unbound')}{Number.isSafeInteger(task.updatedAt) ? <> · <time dateTime={new Date(task.updatedAt).toISOString()}>{new Date(task.updatedAt).toLocaleString()}</time></> : null}</span></span><Badge variant="muted">{t(taskStatus(task))} · {t(task.priority ?? 'normal')}</Badge>
      </RowButton>)}</div> : <p className="empty">{search ? t('noMatches') : filter === 'attention' ? t('noAction') : filter === 'accepted' ? t('noAccepted') : rows.length && (filter !== 'all' || priority !== 'any') ? t('emptyFilter') : t('empty')}</p>}
      {selected ? <div className="detail">
        <div className="meta">{t('selected')}</div>
        <h2>{selected.task.title}</h2>
        <p className="meta">{t('status')} · {t(taskStatus(selected.task))} &nbsp; {t('priority')} · {t(selected.task.priority ?? 'normal')}</p>
        {selected.task.description ? <p>{selected.task.description}</p> : null}
        {selected.task.notes ? <><h3>{t('notes')}</h3><p>{selected.task.notes}</p></> : null}
        {selected.task.resolution.state === 'accepted' && selected.task.resolution.note ? <p>{selected.task.resolution.note}</p> : null}
        <h3>{t('steps')}</h3>
        {selected.task.nextSteps.length ? <ul>{selected.task.nextSteps.map((step, index) => <li key={`${index}:${step}`}>{step}</li>)}</ul> : <p className="meta">{t('noSteps')}</p>}
        <h3>{t('primary')}</h3>
        {selected.task.primary ? <><p className="meta">{[selected.task.primary.connectionId, selected.task.primary.profile, selected.task.primary.targetProfile, selected.task.primary.storedId, selected.task.primary.storeInstanceId].filter(Boolean).join(' / ')}</p>{selected.task.primary.unverified ? <p role="status">{t('unverifiedSession')}</p> : !selected.task.primary.storeInstanceId ? <p role="status">{t('storeIdentityUnavailable')}</p> : !exactNavigationAvailable ? <p role="status">{t('exactNavigationUnavailable')}</p> : unavailableReferences[JSON.stringify([selected.task.primary.connectionId, selected.task.primary.profile, selected.task.primary.targetProfile, selected.task.primary.storedId])] ? <p role="status">{t('sessionUnavailable')}</p> : null}<Button className="open" size="sm" variant="secondary" disabled={selected.task.primary.unverified || !selected.task.primary.storeInstanceId || !exactNavigationAvailable} onClick={() => onOpen(selected.task.primary)}>{t('open')}</Button></> : <p className="meta">{t('unbound')}</p>}
        {selected.task.resolution.state === 'open' ? <><Button size="xs" variant="ghost" disabled={busy} onClick={() => onBind?.(selected)}>{t(selected.task.primary ? 'rebindPrimary' : 'bindPrimary')}</Button>{selected.task.primary ? <Button size="xs" variant="ghost" disabled={busy} onClick={() => onUnbind?.(selected)}>{t('unbindPrimary')}</Button> : null}</> : null}
        {selected.task.related.length ? <><h3>{t('related')}</h3><div className="related">{selected.task.related.map(ref => <div key={JSON.stringify(ref)}><Button size="xs" variant="ghost" disabled={ref.unverified || !ref.storeInstanceId || !exactNavigationAvailable} onClick={() => onOpen(ref)}>{[ref.connectionId, ref.profile, ref.targetProfile, ref.storedId].filter(Boolean).join(' / ')}</Button>{ref.unverified ? <span role="status">{t('unverifiedSession')}</span> : !ref.storeInstanceId ? <span role="status">{t('storeIdentityUnavailable')}</span> : !exactNavigationAvailable ? <span role="status">{t('exactNavigationUnavailable')}</span> : unavailableReferences[JSON.stringify([ref.connectionId, ref.profile, ref.targetProfile, ref.storedId])] ? <span role="status">{t('sessionUnavailable')}</span> : null}</div>)}</div></> : null}
        <div className="open">{selected.task.resolution.state === 'open' ? <><Button size="sm" variant="ghost" disabled={busy} onClick={() => onEdit?.(selected)}>{t('editTask')}</Button> <Button size="sm" variant="ghost" disabled={busy} onClick={() => onAccept?.(selected)}>{t('accept')}</Button></> : <Button size="sm" variant="ghost" disabled={busy} onClick={() => onReopen?.(selected)}>{t('reopen')}</Button>} <Button size="sm" variant="ghost" disabled={busy} onClick={() => onDelete?.(selected)}>{t('deleteTask')}</Button></div>
      </div> : null}
    </>}
    </>}
  </section>
}
