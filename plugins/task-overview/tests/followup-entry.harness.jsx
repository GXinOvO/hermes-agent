import React from 'react'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { FollowupEntry } from '../src/followup-entry.jsx'
import { createFollowupNotifications } from '../src/followup-notifications.mjs'
import { openFollowupStore } from '../src/followup-indexeddb.mjs'
import { createFollowupTask, applyFollowupAction } from '../src/followup-tasks.mjs'

const name = new URL(location.href).searchParams.get('db')
const assert = (condition, message) => { if (!condition) throw Error(message) }
const t = key => ({ entry:'已跟踪', entryHint:'仅限此设备已登记事项', entryUnavailable:'计数不可用' }[key] || key)
const reveals = []
const host = { revealPane: value => reveals.push(value) }
const settle = async (name, value) => waitFor(() => assert(screen.getByRole('button', {name:/已跟踪/}).textContent.includes(value), name))

async function run() {
  if (!name?.startsWith('followup_isolated_test_')) throw Error('isolated test name required')
  const bus = createFollowupNotifications(new EventTarget(), null)
  const store = await openFollowupStore(indexedDB, name)
  const first = render(<FollowupEntry host={host} t={t} notifications={bus} storageName={name}/>)
  await settle('initial zero', '0')
  assert(screen.getByRole('button', {name:/已跟踪/}).title.includes('仅限此设备已登记事项'), 'scope missing from entry')
  const reference = { connectionId:'synthetic-host',profile:'default',targetProfile:'default',storedId:'synthetic-session' }
  const task = createFollowupTask({id:'synthetic-id',title:'synthetic task',primary:reference,related:[],nextSteps:[]}, 1000)
  const row = await store.create(task)
  bus.publish()
  await settle('count did not update after commit', '1')
  fireEvent.click(screen.getByRole('button', {name:/已跟踪/}))
  assert(JSON.stringify(reveals) === JSON.stringify(['task-overview:followup']), 'wrong pane revealed')
  first.unmount()
  const again = render(<FollowupEntry host={host} t={t} notifications={bus} storageName={name}/>)
  await settle('count did not restore after remount', '1')
  const accepted = applyFollowupAction(row.task, {type:'accept',at:1100,note:'verified'})
  await store.replace(accepted,row.revision)
  bus.publish()
  await settle('accepted item still counted open', '0')
  again.unmount()
  const failure = render(<FollowupEntry host={host} t={t} notifications={bus} storageName={name} readSummary={async () => { throw Error('injected read failure') }}/>)
  await waitFor(() => assert(screen.getByRole('button',{name:/已跟踪/}).title.includes('计数不可用'), 'read failure lacks explanation'))
  assert(screen.getByRole('button',{name:/已跟踪/}).textContent.includes('—'), 'read failure displayed a precise count')
  failure.unmount(); store.close(); bus.close(); cleanup()
  return {status:'PASS',cases:['scope label','initial read without pane','post-commit update','pane reveal','remount recovery','acceptance count','unknown on read failure']}
}
run().then(value=>document.querySelector('#result').textContent=JSON.stringify(value)).catch(error=>document.querySelector('#result').textContent=JSON.stringify({status:'FAIL',error:String(error),stack:error?.stack}))
