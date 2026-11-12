import React from 'react'
import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'
import { FollowupView } from '../src/followup-view.jsx'
import { followupStrings } from '../src/followup-i18n.mjs'

const t = key => followupStrings.zh[key] || key
const ref = { connectionId:'示例连接', profile:'default', targetProfile:'default', storedId:'synthetic-session-123456789012345678901234567890' }
const names = ['会话恢复与失败重试','导出范围需要确认','测试失败后的处理','模型调度策略核对','技术资料整理','算子布局待核对','推理系统资料核对','界面整理与使用习惯优化']
const rows = names.map((title, i) => ({task:{id:`synthetic-${i}`,title,primary:{...ref,storedId:`${ref.storedId}-${i}`},related:[],nextSteps:['在实际桌面恢复原会话','核对恢复后的执行与结果'],resolution:{state:'open'}},revision:1}))
const props={t, rows, onOpen:()=>{}, onTrack:()=>{}, onAccept:()=>{}, onReopen:()=>{}, onDiscover:()=>{}}
flushSync(() => {
  createRoot(document.querySelector('#wide')).render(<FollowupView {...props}/>)
  createRoot(document.querySelector('#narrow')).render(<FollowupView {...props}/>)
})
try {
  const results=['wide','narrow'].map(id=>{
    const root=document.querySelector(`#${id} .followup-view`), rect=root.getBoundingClientRect()
    const tabs=[...root.querySelectorAll('.tabs button')].map(button=>({name:button.textContent.trim(),left:button.getBoundingClientRect().left,right:button.getBoundingClientRect().right}))
    return {id, width:rect.width, scrollWidth:root.scrollWidth, clientWidth:root.clientWidth, tabs, overflow:tabs.filter(button=>button.left<rect.left || button.right>rect.right).map(button=>button.name)}
  })
  document.querySelector('#result').textContent=JSON.stringify({status:results.every(x=>x.overflow.length===0 && x.scrollWidth<=x.clientWidth)?'PASS':'FAIL',results})
} catch(error) {
  document.querySelector('#result').textContent=JSON.stringify({status:'FAIL',error:String(error)})
}
