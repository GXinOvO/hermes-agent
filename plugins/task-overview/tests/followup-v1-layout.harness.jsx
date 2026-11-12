import React from 'react'
import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'
import { FollowupView } from '../src/followup-view.jsx'
import { followupStrings } from '../src/followup-i18n.mjs'

const t = key => followupStrings.zh[key] || key
const ref = { connectionId:'示例连接', profile:'default', targetProfile:'default', storedId:'synthetic-session-123456789012345678901234567890', mode:'local' }
const names = ['会话恢复与失败重试', '导出范围需要确认', '测试失败后的处理', '技术资料整理']
const statuses = ['in_progress', 'waiting', 'todo', 'todo']
const priorities = ['high', 'normal', 'urgent', 'low']
const rows = names.map((title, i) => ({ task: { id:`synthetic-${i}`, title, description:'隔离测试用事项', primary: i ? null : ref, related:[], nextSteps:['核对当前结果'], status:statuses[i], priority:priorities[i], updatedAt:1000-i, resolution:{state:'open'} }, revision:1 }))
const props = {t, rows, onOpen:()=>{}, onTrack:()=>{}, onCreate:()=>{}, onEdit:()=>{}, onBind:()=>{}, onUnbind:()=>{}, onAccept:()=>{}, onDelete:()=>{}, onReopen:()=>{}, onDiscover:()=>{}}
try {
  flushSync(() => {
    createRoot(document.querySelector('#wide')).render(<FollowupView {...props}/> )
    createRoot(document.querySelector('#narrow')).render(<FollowupView {...props}/> )
  })
  const results = ['wide','narrow'].map(id => {
    const root = document.querySelector(`#${id} .followup-view`)
    const box = root.getBoundingClientRect()
    const overflow = [...root.querySelectorAll('button,input')].filter(element => {
      const r = element.getBoundingClientRect()
      return r.left < box.left - 1 || r.right > box.right + 1
    }).map(element => element.textContent.trim() || element.getAttribute('aria-label'))
    return {id, width:box.width, scrollWidth:root.scrollWidth, clientWidth:root.clientWidth, overflow}
  })
  const first = document.querySelector('#wide .list > button')
  if (!first?.textContent.includes('核对当前结果')) throw Error('Task list did not show the next step')
  if (!first?.querySelector('time')?.dateTime) throw Error('Task list did not expose an updated time')
  document.querySelector('#result').textContent = JSON.stringify({status: results.every(item => !item.overflow.length && item.scrollWidth <= item.clientWidth) ? 'PASS' : 'FAIL', results})
} catch (error) {
  document.querySelector('#result').textContent = JSON.stringify({status:'FAIL', error:String(error), stack:error?.stack})
}