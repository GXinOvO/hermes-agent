import React from 'react'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { FollowupPane } from '../src/followup-pane.jsx'
import { followupStrings } from '../src/followup-i18n.mjs'

const assert = (condition, message) => { if (!condition) throw Error(message) }
const name = new URL(location.href).searchParams.get('db')
const routeA = { connectionId:'host-a', profile:'default', targetProfile:'backend-a', mode:'remote' }
const routeB = { connectionId:'host-b', profile:'default', targetProfile:'backend-b', mode:'remote' }
const calls = [], opens = []
const host = {
  state: { focusedStoredSessionId: { get: () => null }, focusedSessionOwner: { get: () => null } },
  profileRoutes: async () => [routeA, routeB],
  listPersistedSessions: async (route, options) => {
    calls.push({route,options})
    if (route.connectionId === 'host-b') throw Error('source offline')
    return { sessions: [{ id:'tip-a', _lineage_root_id:'root-a', profile:'backend-a', title:'部署验收会话', unread:true }], profile_totals:{'backend-a': 1}, errors:[], storage:{}, total:1, limit:100, offset:0 }
  },
  openExactSession: async (...args) => opens.push(args)
}
const t = key => followupStrings.zh[key] || key
async function run() {
  assert(name?.startsWith('followup_isolated_test_'), 'isolated database required')
  const app = render(<FollowupPane host={host} t={t} storageName={name}/>)
  await screen.findByText('尚未跟踪事项')
  assert(calls.length === 0, 'session discovery ran without explicit user action')
  fireEvent.click(screen.getByRole('button', {name:/会话待核对/}))
  assert(calls.length === 0, 'opening review tab performed network reads without consent to scan')
  assert(screen.getByText(/每个来源读取按创建时间排序的前 100 条及额外置顶会话/), 'bounded coverage explanation missing')
  fireEvent.click(screen.getByRole('button', {name:'加载会话'}))
  await screen.findByText('部署验收会话')
  assert(calls.length === 2, 'did not query each source once')
  assert(calls[0].options.profile === 'backend-a' && calls[0].options.limit === 100, 'target profile or limit wrong')
  assert(screen.getByText(/host-b.*不可用/), 'offline source is not shown')
  assert(screen.getByText(/无法判断事项是否完成/), 'unknown task status is not explicit')
  assert(screen.getByRole('button', {name:'打开 部署验收会话'}).disabled, 'unverified discovered session could be opened')
  assert(screen.getByText('此版本无法核实会话来源，已阻止打开以避免进入错误会话。'), 'disabled navigation was not explained')
  assert(opens.length === 0, 'unverified discovered session was opened')
  fireEvent.click(screen.getByRole('button',{name:/未完成/}))
  assert(screen.getByText('尚未跟踪事项'), 'discovering or opening a conversation created a business task')
  app.unmount(); cleanup()
  return {status:'PASS', cases:['explicit scan only','bounded coverage warning','source labels shown','unreachable source reported','no completion inference','session opening disabled','no implicit task creation']}
}
run().then(value => document.querySelector('#result').textContent=JSON.stringify(value)).catch(error=>document.querySelector('#result').textContent=JSON.stringify({status:'FAIL',error:String(error),stack:error?.stack}))
