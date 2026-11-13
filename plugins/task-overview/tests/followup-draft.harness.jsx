import React from 'react'
import {render,screen,fireEvent,waitFor,cleanup} from '@testing-library/react'
import {FollowupPane} from '../src/followup-pane.jsx'
import {followupStrings} from '../src/followup-i18n.mjs'
import {openFollowupStore} from '../src/followup-indexeddb.mjs'
import {createFollowupTask} from '../src/followup-tasks.mjs'
const check=(v,m)=>{if(!v)throw Error(m)}
const name=new URL(location.href).searchParams.get('db')
const primary={connectionId:'source-a',profile:'default',targetProfile:'backend-a',storedId:'session-a'}
const route={...primary,mode:'remote'}
const host={state:{focusedStoredSessionId:{get:()=>primary.storedId},focusedSessionOwner:{get:()=>primary}},profileRoutes:async()=>[route],openExactSession:async()=>{}}
const t=key=>followupStrings.zh[key]||key
async function run(){
  check(name?.startsWith('followup_isolated_test_'),'isolated database required')
  const store=await openFollowupStore(indexedDB,name)
  for(const [id,title] of [['alpha','甲事项'],['beta','乙事项']]) await store.create(createFollowupTask({id,title,primary,related:[],nextSteps:[]},1000))
  const app=render(<FollowupPane host={host} t={t} storageName={name}/>)
  await screen.findByRole('heading',{name:'甲事项'})
  fireEvent.click(screen.getByRole('button',{name:'编辑事项'}))
  fireEvent.change(screen.getByLabelText('事项名称'),{target:{value:'尚未保存的甲事项'}})
  fireEvent.click(screen.getByRole('button',{name:/乙事项/}))
  check(screen.getByRole('heading',{name:'乙事项'}),'cannot select another item')
  check(screen.getByLabelText('事项名称').value==='尚未保存的甲事项','draft disappeared on selection')
  check(screen.getByRole('button',{name:'编辑事项'}).disabled,'another edit can discard a draft')
  check(screen.getByRole('button',{name:'标记已验收'}).disabled,'another acceptance can discard a draft')
  check(screen.getByRole('button',{name:'跟踪当前会话'}).disabled,'starting another item can discard a draft')
  check(screen.getByRole('heading',{name:'编辑事项 · 甲事项'}),'draft target is not visible')
  fireEvent.click(screen.getByRole('button',{name:'保存修改'}))
  await waitFor(async()=>{const rows=await store.list();check(rows.find(x=>x.task.id==='alpha').task.title==='尚未保存的甲事项','the intended draft was not saved')})
  const rows=await store.list()
  check(rows.find(x=>x.task.id==='beta').task.title==='乙事项','the other item was changed')
  app.unmount();store.close();cleanup()
  return {status:'PASS',cases:['switch selected row without losing draft','editing target remains visible','conflicting actions disabled during draft','save affects only original item']}
}
run().then(v=>document.querySelector('#result').textContent=JSON.stringify(v)).catch(e=>document.querySelector('#result').textContent=JSON.stringify({status:'FAIL',error:String(e),stack:e?.stack}))
