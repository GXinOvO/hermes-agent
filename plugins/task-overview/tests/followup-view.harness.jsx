import React from 'react'
import { render, screen, fireEvent, within, cleanup } from '@testing-library/react'
import { FollowupView } from '../src/followup-view.jsx'

const check = (condition, message) => { if (!condition) throw Error(message) }
const ref = (storedId, connectionId = 'source-a') => ({ storedId, connectionId, profile: 'default', targetProfile: 'default', storeInstanceId: 'fixture' })
const task = (id, title, primary, nextSteps = []) => ({ version: 1, id, title, primary, related: [], nextSteps, createdAt: 1000, updatedAt: 1000, resolution: { state: 'open' } })
const rows = [
  { task: task('first', '恢复并验证', ref('session-a'), ['核对恢复结果']), revision: 1 },
  { task: task('second', '研究资料核对', ref('session-b', 'source-b')), revision: 2 }
]
const t = key => ({ title:'事项跟进', all:'未完成', attention:'待关注', search:'搜索事项 / 关联会话', scope:'仅显示已手动跟踪的事项；其他会话尚未覆盖。', unknown:'待核对', loading:'正在读取事项', storageError:'事项存储不可用', noMatches:'没有匹配的事项', empty:'尚未跟踪事项', steps:'还剩什么', noSteps:'尚未填写剩余步骤', primary:'主会话', related:'相关会话', open:'打开主会话', sourceUnavailable:'来源当前不可用', sessionError:'无法打开会话', retry:'重试', add:'跟踪当前会话', selected:'所选事项', execution:'执行状态', executionUnknown:'未知', noAction:'当前无法确定是否需要处理' }[key] || key)
const show = (props = {}) => render(<FollowupView t={t} rows={rows} coverage="partial" onOpen={() => {}} onTrack={() => {}} {...props}/>)

try {
  let opened = null
  const view = show({ onOpen: ref => { opened = ref } })
  check(screen.getByRole('button', { name: /恢复并验证/ }) && screen.getByRole('button', { name: /研究资料核对/ }), 'Both saved tasks must remain visible')
  check(screen.getByRole('heading', { name: '恢复并验证' }), 'First task should be selected')
  check(screen.getByText('仅显示已手动跟踪的事项；其他会话尚未覆盖。'), 'Incomplete coverage must be visible')
  check(screen.getByText('核对恢复结果'), 'Selected task must show remaining work')
  check(screen.getByRole('button', { name: '打开主会话' }).disabled, 'Navigation must be disabled by default')
  fireEvent.click(screen.getByRole('button', { name: '打开主会话' }))
  check(opened === null, 'Disabled navigation invoked the open callback')
  fireEvent.change(screen.getByRole('textbox', { name: '搜索事项 / 关联会话' }), { target: { value: '研究' } })
  check(!screen.queryByRole('button', { name: /恢复并验证/ }) && screen.getByRole('button', { name: /研究资料核对/ }), 'Search did not filter the list')
  check(screen.getByRole('heading', { name: '研究资料核对' }), 'Visible selection did not follow the filter')
  check(!screen.queryByText('核对恢复结果'), 'Details retained an item outside the filtered list')
  fireEvent.change(screen.getByRole('textbox', { name: '搜索事项 / 关联会话' }), { target: { value: 'not-present' } })
  check(screen.getByText('没有匹配的事项'), 'Search empty state missing')
  view.unmount()
  show({ rows: [], error: new Error('read failed') })
  check(screen.getByRole('alert').textContent.includes('事项存储不可用'), 'Storage failure looked like an empty list')
  check(!screen.queryByText('尚未跟踪事项'), 'Storage failure must not masquerade as empty')
  check(screen.getByRole('button', { name: /未完成/ }).textContent.includes('—'), 'Failed storage must show an unknown count')
  cleanup()
  show({ rows: [], loading: true })
  check(screen.getByRole('button', { name: /未完成/ }).textContent.includes('—'), 'Loading must not show a confirmed zero')
  cleanup()
  document.querySelector('#result').textContent = JSON.stringify({ status: 'PASS', cases: ['tracked rows', 'coverage label', 'selected detail', 'navigation disabled by default', 'search', 'filtered selection', 'empty search', 'storage error'] })
} catch (error) {
  document.querySelector('#result').textContent = JSON.stringify({ status: 'FAIL', error: String(error), stack: error?.stack })
}
