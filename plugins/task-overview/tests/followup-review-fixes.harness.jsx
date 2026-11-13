import React from 'react'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { FollowupPane } from '../src/followup-pane.jsx'
import { openFollowupStore } from '../src/followup-indexeddb.mjs'
import { followupStrings } from '../src/followup-i18n.mjs'

const t = key => followupStrings.zh[key] || key
const check = (condition, message) => { if (!condition) throw Error(message) }
const name = new URL(location.href).searchParams.get('db')
const route = { connectionId: 'ui-host', profile: 'default', targetProfile: 'backend-special', mode: 'remote' }
const page = { sessions: [{ id: 'stored-1', profile: 'backend-special', title: '当前会话' }], profile_totals: { 'backend-special': 1 }, errors: [], storage: {} }
async function run() {
  check(name?.startsWith('followup_isolated_test_'), 'isolated database required')
  let release, started, scans = 0, opens = 0
  const delayed = new Promise(resolve => { release = resolve })
  const seen = new Promise(resolve => { started = resolve })
  const host = { state: {}, profileRoutes: async () => [route], listPersistedSessions: async () => {
    scans++
    if (scans === 1) { started(); return delayed }
    return page
  } }
  const openStore = async (...args) => { if (++opens === 1) throw Error('open failed'); return openFollowupStore(...args) }
  const app = render(<FollowupPane host={host} t={t} storageName={name} openStore={openStore}/>)
  await screen.findByText('事项存储不可用')
  fireEvent.click(screen.getByRole('button', {name: '会话待核对'}))
  fireEvent.click(screen.getByRole('button', {name: '加载会话'}))
  await seen
  fireEvent.click(screen.getByRole('button', {name: /未完成/}))
  fireEvent.click(screen.getByRole('button', {name: '重试'}))
  await screen.findByText('尚未跟踪事项')
  release(page)
  fireEvent.click(screen.getByRole('button', {name: '会话待核对'}))
  await waitFor(() => check(!screen.getByRole('button', {name: /加载会话/}).disabled, 'discovery stayed disabled after storage retry'))
  fireEvent.click(screen.getByRole('button', {name: '加载会话'}))
  await screen.findByText('当前会话')
  fireEvent.change(screen.getByRole('textbox', {name: '搜索事项 / 关联会话'}), {target: {value: 'backend-special'}})
  check(screen.getByText('当前会话'), 'target profile search hid the matching session')
  fireEvent.change(screen.getByRole('textbox', {name: '搜索事项 / 关联会话'}), {target: {value: 'no-such-session'}})
  check(screen.getByText('没有匹配的会话'), 'no search match was not explained')
  fireEvent.change(screen.getByRole('textbox', {name: '搜索事项 / 关联会话'}), {target: {value: ''}})
  let releaseAgain, markSecond
  const secondStarted = new Promise(resolve => { markSecond = resolve })
  host.listPersistedSessions = async () => { markSecond(); return new Promise(resolve => { releaseAgain = resolve }) }
  fireEvent.click(screen.getByRole('button', {name: '加载会话'}))
  await secondStarted
  check(screen.getByText('正在刷新；以下会话结果可能已过期。'), 'old results are not marked stale while rescanning')
  check(screen.getByText('当前会话'), 'previous results disappeared while refreshing')
  releaseAgain(page)
  await waitFor(() => check(!screen.queryByText('正在刷新；以下会话结果可能已过期。'), 'stale warning remains after refresh'))
  app.unmount(); cleanup()
  return {status:'PASS',cases:['storage retry clears invalidated scan','backend profile search','no session search matches','refresh labels previous results stale']}
}
run().then(value => { document.querySelector('#result').textContent = JSON.stringify(value) }).catch(error => { document.querySelector('#result').textContent = JSON.stringify({status:'FAIL',error:String(error),stack:error?.stack}) })
