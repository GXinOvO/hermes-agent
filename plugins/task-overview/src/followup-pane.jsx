import React, { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Input, Textarea, SegmentedControl } from '@hermes/plugin-sdk'
import { applyTrackedAction, bindPrimarySession, captureSessionForTracking, createStandaloneTask, openTrackedReference, removeTrackedTask, reviseTrackedTask, snapshotFocusedSession, trackCapturedSession } from './followup-controller.mjs'
import { openFollowupStore } from './followup-indexeddb.mjs'
import { discoverSessions } from './followup-discovery.mjs'
import { FollowupView } from './followup-view.jsx'

export function FollowupPane({ host, t, storageName, openStore = openFollowupStore, notifications }) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)
  const [attempt, setAttempt] = useState(0)
  const [error, setError] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [refreshError, setRefreshError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [captured, setCaptured] = useState(null)
  const [creating, setCreating] = useState(false)
  const [title, setTitle] = useState('')
  const [steps, setSteps] = useState('')
  const [description, setDescription] = useState('')
  const [notes, setNotes] = useState('')
  const [status, setStatus] = useState('todo')
  const [priority, setPriority] = useState('normal')
  const [editing, setEditing] = useState(null)
  const [editRelated, setEditRelated] = useState([])
  const [linking, setLinking] = useState(false)
  const [saving, setSaving] = useState(false)
  const [accepting, setAccepting] = useState(null)
  const [acceptNote, setAcceptNote] = useState('')
  const [deleting, setDeleting] = useState(null)
  const [changingPrimary, setChangingPrimary] = useState(null)
  const [unavailableReferences, setUnavailableReferences] = useState({})
  const [discovery, setDiscovery] = useState(null)
  const [discovering, setDiscovering] = useState(false)
  const scanGeneration = useRef(0)
  const store = useRef(null)
  const writing = useRef(false)
  const generation = useRef(0)
  const mounted = useRef(false)
  const loaded = useRef(false)
  const source = useRef(Symbol('followup-pane'))
  const editGeneration = useRef(0)
  const linkingRef = useRef(false)

  const refresh = useCallback(async (afterCommit = false) => {
    const current = store.current
    if (!current) return
    const request = ++generation.current
    setRefreshing(true)
    try {
      const items = await current.list()
      if (mounted.current && store.current === current && generation.current === request) {
        setRows(items)
        loaded.current = true
        setLoading(false)
        setError(null)
        setRefreshError(null)
      }
    } catch (err) {
      if (mounted.current && store.current === current && generation.current === request) {
        setLoading(false)
        if (loaded.current) setRefreshError({ error: err, afterCommit })
        else setError(err)
      }
    } finally {
      if (mounted.current && store.current === current && generation.current === request) setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    let alive = true
    mounted.current = true
    loaded.current = false
    setDiscovering(false)
    setLoading(true)
    setError(null)
    openStore(globalThis.indexedDB, storageName).then(async next => {
      if (!alive) { next.close(); return }
      store.current = next
      await refresh()
    }).catch(err => { if (alive) { setError(err); setLoading(false) } })
    return () => { alive = false; mounted.current = false; generation.current++; scanGeneration.current++; editGeneration.current++; store.current?.close(); store.current = null }
  }, [storageName, openStore, refresh, attempt])

  useEffect(() => {
    const stop = notifications?.subscribe(origin => { if (origin !== source.current) void refresh() })
    const onFocus = () => void refresh()
    globalThis.window?.addEventListener('focus', onFocus)
    return () => { stop?.(); globalThis.window?.removeEventListener('focus', onFocus) }
  }, [notifications, refresh])

  const retryStorage = () => {
    if (store.current) void refresh()
    else setAttempt(value => value + 1)
  }
  const begin = async () => {
    const request = ++editGeneration.current
    setActionError(null)
    try {
      const identity = await captureSessionForTracking(host)
      if (!mounted.current || editGeneration.current !== request) return
      setAccepting(null)
      setDeleting(null)
      setChangingPrimary(null)
      setCreating(false)
      setEditing(null)
      setCaptured(identity)
      setTitle('')
      setSteps('')
      setDescription('')
      setNotes('')
      setStatus('todo')
      setPriority('normal')
    } catch (err) {
      if (mounted.current && editGeneration.current === request) setActionError(err)
    }
  }
  const beginStandalone = () => {
    editGeneration.current++
    setActionError(null)
    setAccepting(null)
    setDeleting(null)
    setChangingPrimary(null)
    setEditing(null)
    setCaptured(null)
    setTitle('')
    setSteps('')
    setDescription('')
    setNotes('')
    setStatus('todo')
    setPriority('normal')
    setCreating(true)
  }
  const save = async event => {
    event.preventDefault()
    if (!store.current || (!captured && !creating) || writing.current) return
    writing.current = true
    setSaving(true)
    setActionError(null)
    try {
      const id = globalThis.crypto?.randomUUID?.()
      if (!id) throw new Error('Secure identifier generation unavailable')
      const nextSteps = steps.split('\n').map(step => step.trim()).filter(Boolean)
      const fields = { id, title: title.trim(), description, notes, nextSteps, related: [], status, priority }
      const committed = captured
        ? await trackCapturedSession(host, store.current, fields, Date.now(), captured)
        : await createStandaloneTask(store.current, fields, Date.now())
      generation.current++
      setRows(current => [...current, committed])
      setCaptured(null)
      setCreating(false)
      notifications?.publish(source.current)
      await refresh(true)
    } catch (err) { setActionError(err) } finally { writing.current = false; setSaving(false) }
  }
  const update = async (row, type, note = '') => {
    if (!store.current || writing.current) return
    writing.current = true
    setSaving(true)
    setActionError(null)
    try {
      const committed = await applyTrackedAction(store.current, row, { type, at: Date.now(), note })
      generation.current++
      setRows(current => current.map(item => item.task.id === committed.task.id ? committed : item))
      setAccepting(null)
      setAcceptNote('')
      notifications?.publish(source.current)
      await refresh(true)
    } catch (err) { setActionError(err) } finally { writing.current = false; setSaving(false) }
  }
  const edit = row => {
    if (writing.current) return
    editGeneration.current++
    setActionError(null)
    setCaptured(null)
    setCreating(false)
    setAccepting(null)
    setDeleting(null)
    setChangingPrimary(null)
    setEditing(row)
    setTitle(row.task.title)
    setSteps(row.task.nextSteps.join('\n'))
    setDescription(row.task.description)
    setNotes(row.task.notes)
    setStatus(row.task.status)
    setPriority(row.task.priority)
    setEditRelated(row.task.related)
  }
  const addRelated = async () => {
    if (!editing || writing.current || linkingRef.current) return
    const request = editGeneration.current
    linkingRef.current = true
    setLinking(true)
    setActionError(null)
    try {
      const ref = await captureSessionForTracking(host)
      if (!mounted.current || editGeneration.current !== request) return
      const key = value => JSON.stringify([value.connectionId, value.profile, value.targetProfile, value.storedId])
      if ([editing.task.primary, ...editRelated].filter(Boolean).some(value => key(value) === key(ref))) {
        const error = new Error(t('duplicateRelated'))
        error.code = 'FOLLOWUP_DUPLICATE'
        throw error
      }
      setEditRelated(current => [...current, ref])
    } catch (err) {
      if (mounted.current && editGeneration.current === request) setActionError(err)
    } finally {
      linkingRef.current = false
      if (mounted.current) setLinking(false)
    }
  }
  const saveEdit = async event => {
    event.preventDefault()
    if (!store.current || !editing || writing.current || linkingRef.current) return
    writing.current = true
    setSaving(true)
    setActionError(null)
    try {
      const fields = { title: title.trim(), description, notes, status, priority, nextSteps: steps.split('\n').map(step => step.trim()).filter(Boolean), related: editRelated }
      const committed = await reviseTrackedTask(host, store.current, editing, fields, Date.now())
      generation.current++
      editGeneration.current++
      setRows(current => current.map(item => item.task.id === committed.task.id ? committed : item))
      setEditing(null)
      notifications?.publish(source.current)
      await refresh(true)
    } catch (err) { setActionError(err) } finally { writing.current = false; setSaving(false) }
  }
  const scan = async () => {
    if (discovering) return
    const request = ++scanGeneration.current
    setDiscovering(true)
    setActionError(null)
    try {
      const result = await discoverSessions(host)
      if (mounted.current && scanGeneration.current === request) setDiscovery(result)
    } catch (err) {
      if (mounted.current && scanGeneration.current === request) setActionError(err)
    } finally {
      if (mounted.current && scanGeneration.current === request) setDiscovering(false)
    }
  }
  const open = async reference => {
    setActionError(null)
    const key = JSON.stringify([reference.connectionId, reference.profile, reference.targetProfile, reference.storedId])
    try {
      await openTrackedReference(host, reference)
      setUnavailableReferences(current => { const next = { ...current }; delete next[key]; return next })
    } catch (err) {
      setUnavailableReferences(current => ({ ...current, [key]: true }))
      setActionError(err)
    }
  }
  const changePrimary = async (row, unlink = false, expectedFocus) => {
    if (!store.current || writing.current) return
    writing.current = true
    setSaving(true)
    setActionError(null)
    try {
      const committed = unlink
        ? await store.current.changePrimary(row.task.id, row.revision, null, Date.now())
        : await bindPrimarySession(host, store.current, row, Date.now(), expectedFocus)
      generation.current++
      setRows(current => current.map(item => item.task.id === committed.task.id ? committed : item))
      setChangingPrimary(null)
      notifications?.publish(source.current)
      await refresh(true)
    } catch (err) { setActionError(err) } finally { writing.current = false; setSaving(false) }
  }
  const remove = async row => {
    if (!store.current || writing.current) return
    writing.current = true
    setSaving(true)
    setActionError(null)
    try {
      await removeTrackedTask(store.current, row)
      generation.current++
      setRows(current => current.filter(item => item.task.id !== row.task.id))
      setDeleting(null)
      notifications?.publish(source.current)
      await refresh(true)
    } catch (err) { setActionError(err) } finally { writing.current = false; setSaving(false) }
  }
  const fields = <>
    <label>{t('taskTitle')}<Input aria-label={t('taskTitle')} value={title} disabled={saving} onChange={event => setTitle(event.target.value)} required /></label>
    <label>{t('description')}<Textarea aria-label={t('description')} value={description} disabled={saving} onChange={event => setDescription(event.target.value)} /></label>
    <label>{t('notes')}<Textarea aria-label={t('notes')} value={notes} disabled={saving} onChange={event => setNotes(event.target.value)} /></label>
    <label>{t('nextSteps')}<Textarea aria-label={t('nextSteps')} value={steps} disabled={saving} onChange={event => setSteps(event.target.value)} /></label>
    <fieldset><legend>{t('status')}</legend><SegmentedControl options={['todo', 'in_progress', 'waiting', ...(status === 'unspecified' ? ['unspecified'] : [])].map(id => ({ id, label: t(id) }))} value={status} onChange={setStatus} disabled={saving} /></fieldset>
    <fieldset><legend>{t('priority')}</legend><SegmentedControl options={['low', 'normal', 'high', 'urgent'].map(id => ({ id, label: t(id) }))} value={priority} onChange={setPriority} disabled={saving} /></fieldset>
  </>
  const form = <>
    {refreshError ? <div role="alert"><span>{t(refreshError.afterCommit ? 'savedRefresh' : 'staleRefresh')}</span> <Button size="xs" variant="secondary" onClick={() => void refresh()}>{t('retry')}</Button></div> : null}
    {actionError ? <div role="alert"><strong>{t('actionError')}</strong> · {actionError.code === 'FOLLOWUP_ROUTE_CHANGED' ? t('routeChanged') : actionError.code === 'FOLLOWUP_EXACT_NAVIGATION_UNAVAILABLE' ? t('exactNavigationUnavailable') : actionError.code === 'FOLLOWUP_OWNER_IDENTITY_UNAVAILABLE' ? t('ownerIdentityUnavailable') : actionError.code === 'FOLLOWUP_REFERENCE_IDENTITY_UNAVAILABLE' ? t('referenceIdentityUnavailable') : String(actionError.message || actionError)}</div> : null}
    {editing ? <form aria-label={t('editTask')} onSubmit={saveEdit}>
      <h3>{t('editTask')} · {editing.task.title}</h3>
      {fields}
      <h3>{t('related')}</h3>
      {editRelated.map(ref => <div key={JSON.stringify(ref)} className="related"><span className="meta">{[ref.connectionId, ref.profile, ref.targetProfile, ref.storedId].filter(Boolean).join(' / ')}{ref.unverified ? ` · ${t('unverifiedSession')}` : ''}</span><Button type="button" size="xs" variant="ghost" disabled={saving} aria-label={`${t('removeRelated')} ${ref.storedId}`} onClick={() => setEditRelated(current => current.filter(item => item !== ref))}>{t('removeRelated')}</Button></div>)}
      <Button type="button" size="xs" variant="secondary" disabled={saving || linking} onClick={() => void addRelated()}>{t('addRelated')}</Button>
      <div><Button type="submit" size="sm" disabled={saving || linking || !title.trim()}>{t('saveEdit')}</Button> <Button type="button" size="sm" variant="ghost" disabled={saving} onClick={() => { editGeneration.current++; setEditing(null); setActionError(null) }}>{t('cancel')}</Button></div>
    </form> : null}
    {accepting ? <form onSubmit={event => { event.preventDefault(); void update(accepting, 'accept', acceptNote.trim()) }}>
      <h3>{t('confirmAccept')} · {accepting.task.title}</h3>
      <p className="meta">{t('acceptHint')}</p>
      <label>{t('acceptNote')}<Textarea aria-label={t('acceptNote')} value={acceptNote} disabled={saving} onChange={event => setAcceptNote(event.target.value)} required /></label>
      <div><Button type="submit" size="sm" disabled={saving || !acceptNote.trim()}>{t('confirmAccept')}</Button> <Button type="button" size="sm" variant="ghost" disabled={saving} onClick={() => setAccepting(null)}>{t('cancel')}</Button></div>
    </form> : null}
    {deleting ? <div role="alertdialog" aria-label={t('deleteTask')}><p>{t('deleteHint')}</p><Button type="button" size="sm" variant="secondary" disabled={saving} onClick={() => setDeleting(null)}>{t('cancel')}</Button> <Button type="button" size="sm" variant="ghost" disabled={saving} onClick={() => void remove(deleting)}>{t('confirmDelete')}</Button></div> : null}
    {changingPrimary ? <div role="alertdialog" aria-label={t(changingPrimary.unlink ? 'unbindPrimary' : 'rebindPrimary')}><p>{t('confirmPrimaryChange')}</p>{!changingPrimary.unlink ? <p className="meta">{changingPrimary.expectedFocus ? [changingPrimary.expectedFocus.connectionId, changingPrimary.expectedFocus.profile, changingPrimary.expectedFocus.targetProfile, changingPrimary.expectedFocus.storedId].filter(Boolean).join(' / ') : t('noFocusedSession')}</p> : null}<Button type="button" size="sm" variant="secondary" disabled={saving} onClick={() => setChangingPrimary(null)}>{t('cancel')}</Button> <Button type="button" size="sm" variant="ghost" disabled={saving || (!changingPrimary.unlink && !changingPrimary.expectedFocus)} onClick={() => void changePrimary(changingPrimary.row, changingPrimary.unlink, changingPrimary.expectedFocus)}>{t('confirmChange')}</Button></div> : null}
    {captured || creating ? <form aria-label={t(creating ? 'newTask' : 'tracking')} onSubmit={save}>
      <h3>{t(creating ? 'newTask' : 'tracking')}</h3>
      {captured ? <><p className="meta">{[captured.connectionId, captured.profile, captured.targetProfile, captured.storedId].filter(Boolean).join(' / ')}</p>{captured.unverified ? <p role="status">{t('unverifiedSession')}</p> : null}</> : null}
      {fields}
      <div><Button type="submit" size="sm" disabled={saving || !title.trim()}>{t(creating ? 'saveTask' : 'save')}</Button> <Button type="button" size="sm" variant="ghost" disabled={saving} onClick={() => { setCaptured(null); setCreating(false) }}>{t('cancel')}</Button></div>
    </form> : null}
  </>
  return <FollowupView t={t} rows={rows} loading={loading} error={error} uncertain={refreshing || Boolean(refreshError)} exactNavigationAvailable={false} unavailableReferences={unavailableReferences} busy={saving || Boolean(captured || creating || editing || accepting || deleting || changingPrimary)} discovery={discovery} discovering={discovering} onDiscover={scan} onRetry={retryStorage} onOpen={open} onTrack={begin} onCreate={beginStandalone} onEdit={edit} onDelete={setDeleting} onBind={row => { if (!row.task.primary) { void changePrimary(row); return }; try { setChangingPrimary({row, unlink:false, expectedFocus:snapshotFocusedSession(host)}) } catch (err) { setActionError(err) } }} onUnbind={row => setChangingPrimary({row, unlink:true})} onAccept={row => { editGeneration.current++; setEditing(null); setCaptured(null); setCreating(false); setAccepting(row); setAcceptNote(''); setActionError(null) }} onReopen={row => void update(row, 'reopen')} form={form}/>
}
