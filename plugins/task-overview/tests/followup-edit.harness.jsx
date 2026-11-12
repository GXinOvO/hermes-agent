import React from 'react'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { FollowupPane } from '../src/followup-pane.jsx'
import { followupStrings } from '../src/followup-i18n.mjs'
import { openFollowupStore } from '../src/followup-indexeddb.mjs'

const assert = (condition, message) => { if (!condition) throw Error(message) }
const name = new URL(location.href).searchParams.get('db')
const routeA = { connectionId:'host-a', profile:'default', targetProfile:'backend-a', mode:'remote' }
const routeB = { connectionId:'host-b', profile:'default', targetProfile:'backend-b', mode:'remote' }
let routes = [routeA, routeB]
let blockRoutes = false, releaseRoute, delayedResult = null
let focus = { storedId:'primary-session', owner:{connectionId:'host-a',profile:'default',targetProfile:'backend-a',mode:'remote'} }
const opens=[]
const host={ state:{ focusedStoredSessionId:{get:()=>focus.storedId}, focusedSessionOwner:{get:()=>focus.owner} }, profileRoutes:async()=>{ if(blockRoutes) { const captured=routes; await new Promise(resolve=>{ releaseRoute=resolve }); return delayedResult || captured } return routes }, openExactSession:async(...args)=>opens.push(args) }
const t = key => followupStrings.zh[key] || key
async function run() {
  assert(name?.startsWith('followup_isolated_test_'), 'isolated database required')
  let releaseRefresh, reads = 0
  const controlledStore = async (...args) => {
    const real = await openFollowupStore(...args)
    return { ...real, list: async () => {
      reads++
      if (reads === 2) await new Promise(resolve => { releaseRefresh = resolve })
      return real.list()
    } }
  }
  let app=render(<FollowupPane host={host} t={t} storageName={name} openStore={controlledStore}/>)
  await screen.findByText('尚未跟踪事项')
  fireEvent.click(screen.getByRole('button',{name:'跟踪当前会话'}))
  await screen.findByLabelText('事项名称')
  fireEvent.change(screen.getByLabelText('事项名称'),{target:{value:'取消的草稿'}})
  fireEvent.click(screen.getByRole('button',{name:'取消'}))
  assert(!screen.queryByLabelText('事项名称'),'cancelling tracking left the form open')
  const emptyCheck = await openFollowupStore(indexedDB,name)
  assert((await emptyCheck.list()).length===0,'cancelling tracking committed a record')
  emptyCheck.close()
  fireEvent.click(screen.getByRole('button',{name:'跟踪当前会话'}))
  fireEvent.change(await screen.findByLabelText('事项名称'),{target:{value:'原始事项'}})
  fireEvent.change(screen.getByLabelText('剩余步骤'),{target:{value:'初始步骤'}})
  fireEvent.click(screen.getByRole('button',{name:'保存跟踪'}))
  await screen.findByRole('heading',{name:'原始事项'})
  assert(screen.getByRole('button',{name:'编辑事项'}).disabled, 'editing remained available during an unfinished write')
  assert(screen.getByRole('button',{name:'标记已验收'}).disabled, 'acceptance remained available during an unfinished write')
  assert(typeof releaseRefresh === 'function', 'controlled refresh did not start')
  releaseRefresh()
  await waitFor(()=>assert(!screen.getByRole('button',{name:'编辑事项'}).disabled, 'editing did not resume after refresh'))
  fireEvent.click(screen.getByRole('button',{name:'编辑事项'}))
  fireEvent.change(screen.getByLabelText('事项名称'),{target:{value:'更新事项'}})
  fireEvent.change(screen.getByLabelText('剩余步骤'),{target:{value:'步骤一\n步骤二'}})
  fireEvent.click(screen.getByRole('button',{name:'关联当前会话'}))
  await screen.findByText(/该会话已关联/)
  focus={storedId:'related-session',owner:{connectionId:'host-b',profile:'default',targetProfile:'backend-b',mode:'remote'}}
  blockRoutes=true
  fireEvent.click(screen.getByRole('button',{name:'关联当前会话'}))
  assert(screen.getByRole('button',{name:'保存修改'}).disabled, 'save remained available while a related session was resolving')
  assert(screen.getByRole('button',{name:'关联当前会话'}).disabled, 'concurrent related-session capture was allowed')
  blockRoutes=false; releaseRoute()
  await screen.findByText(/host-b \/ default \/ backend-b \/ related-session/)
  routes=[routeA,{...routeB,targetProfile:'changed-backend'}]
  blockRoutes=true
  fireEvent.click(screen.getByRole('button',{name:'保存修改'}))
  assert(screen.getByLabelText('事项名称').disabled && screen.getByLabelText('剩余步骤').disabled, 'draft fields stayed editable while saving')
  blockRoutes=false; releaseRoute()
  await screen.findByText(/来源已变化/)
  const store=await openFollowupStore(indexedDB,name)
  let rows=await store.list()
  assert(rows.length===1 && rows[0].task.title==='原始事项' && rows[0].task.related.length===0,'route drift changed committed record')
  routes=[routeA,routeB]
  fireEvent.click(screen.getByRole('button',{name:'保存修改'}))
  await screen.findByRole('heading',{name:'更新事项'})
  assert(screen.getByText('步骤一') && screen.getByText('步骤二'),'updated steps not shown')
  const related=screen.getByRole('button',{name:/host-b \/ default \/ backend-b \/ related-session/})
  assert(related.disabled, 'unverified related session could be opened')
  assert(opens.length===0,'unverified related session was opened')
  app.unmount()
  app=render(<FollowupPane host={host} t={t} storageName={name}/>)
  await screen.findByRole('heading',{name:'更新事项'})
  assert(screen.getByRole('button',{name:/host-b \/ default \/ backend-b \/ related-session/}),'linked session not persisted')
  blockRoutes=true
  fireEvent.click(screen.getByRole('button',{name:'跟踪当前会话'}))
  assert(typeof releaseRoute==='function','deferred capture did not start')
  fireEvent.click(screen.getByRole('button',{name:'编辑事项'}))
  fireEvent.change(screen.getByLabelText('事项名称'),{target:{value:'不应丢失的草稿'}})
  blockRoutes=false; releaseRoute()
  await new Promise(resolve=>setTimeout(resolve,30))
  assert(screen.getByLabelText('事项名称').value==='不应丢失的草稿', 'older capture replaced the newer edit draft')
  fireEvent.click(screen.getByRole('button',{name:'取消'}))
  fireEvent.click(screen.getByRole('button',{name:'标记已验收'}))
  fireEvent.change(screen.getByLabelText('验收说明'),{target:{value:'暂时不验收'}})
  fireEvent.click(screen.getByRole('button',{name:'取消'}))
  rows=await store.list()
  assert(rows[0].task.resolution.state==='open','cancelling acceptance marked the task as accepted')
  fireEvent.click(screen.getByRole('button',{name:'编辑事项'}))
  fireEvent.click(screen.getByRole('button',{name:/移除关联.*related-session/}))
  fireEvent.click(screen.getByRole('button',{name:'保存修改'}))
  await waitFor(()=>assert(!screen.queryByRole('button',{name:/host-b \/ default \/ backend-b \/ related-session/}),'removed relation still shown'))
  rows=await store.list()
  assert(rows[0].task.title==='更新事项' && rows[0].task.primary.storedId==='primary-session' && rows[0].task.related.length===0,'wrong final record')
  routes=[{...routeA,targetProfile:'backend-new'},routeB]
  focus={storedId:'primary-session',owner:{connectionId:'host-a',profile:'default',targetProfile:'backend-new',mode:'remote'}}
  fireEvent.click(screen.getByRole('button',{name:'编辑事项'}))
  fireEvent.click(screen.getByRole('button',{name:'关联当前会话'}))
  await screen.findByText(/host-a \/ default \/ backend-new \/ primary-session/)
  fireEvent.click(screen.getByRole('button',{name:'保存修改'}))
  await waitFor(async()=>{const next=await store.list();assert(next[0].task.related.length===1 && next[0].task.related[0].targetProfile==='backend-new','remapped related session not persisted')})
  assert(screen.getByText('host-a / default / backend-a / primary-session'), 'primary backend target not visible')
  const remapped=screen.getByRole('button',{name:'host-a / default / backend-new / primary-session'})
  assert(remapped.disabled && opens.length===0,'unverified remapped session was opened')
  store.close(); app.unmount(); cleanup()
  return {status:'PASS',cases:['cancel tracking without saving','prevent edit or acceptance during write','edit title and remaining steps','reject duplicate primary','block save during link capture','reject route drift before save','disable draft during save','persist linked session','unverified related session disabled','remount recovery','reject stale capture over edit draft','cancel acceptance','remove related session without changing primary']}
}
run().then(result=>document.querySelector('#result').textContent=JSON.stringify(result)).catch(error=>document.querySelector('#result').textContent=JSON.stringify({status:'FAIL',error:String(error),stack:error?.stack}))
