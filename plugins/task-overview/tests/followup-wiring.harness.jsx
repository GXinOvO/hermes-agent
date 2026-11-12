import React from 'react'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { FollowupPane } from '../src/followup-pane.jsx'
import { FollowupEntry } from '../src/followup-entry.jsx'
import { createFollowupNotifications } from '../src/followup-notifications.mjs'
import { openFollowupStore } from '../src/followup-indexeddb.mjs'
import { createFollowupTask } from '../src/followup-tasks.mjs'

const labels={title:'事项跟进',entry:'已跟踪',entryHint:'仅限此设备已登记事项',entryUnavailable:'计数不可用',all:'未完成',attention:'待关注',accepted:'已验收',search:'搜索事项 / 关联会话',scope:'仅显示已手动跟踪的事项；其他会话尚未覆盖。',unknown:'待核对',loading:'正在读取事项',storageError:'事项存储不可用',noMatches:'没有匹配的事项',empty:'尚未跟踪事项',steps:'还剩什么',noSteps:'尚未填写剩余步骤',primary:'主会话',related:'相关会话',open:'打开主会话',noAction:'当前无法确定是否需要处理',retry:'重试',add:'跟踪当前会话',selected:'所选事项',execution:'执行状态',executionUnknown:'未知',taskTitle:'事项名称',nextSteps:'剩余步骤',save:'保存跟踪',cancel:'取消',tracking:'跟踪会话',actionError:'操作失败',savedRefresh:'事项已保存，但列表刷新失败；请重试。',accept:'标记已验收',confirmAccept:'确认已验收',acceptHint:'请在核对结果后确认。',acceptNote:'验收说明',reopen:'重新跟踪'}
const t=key=>labels[key]||key
const name=new URL(location.href).searchParams.get('db')
const check=(v,m)=>{if(!v)throw Error(m)}
const route={connectionId:'fixture-ssh',profile:'default',targetProfile:'worker',mode:'remote'}
const host={state:{focusedStoredSessionId:{get:()=> 'fixture-session'},focusedSessionOwner:{get:()=>({connectionId:'fixture-ssh',profile:'default',targetProfile:'worker',mode:'remote'})}},profileRoutes:async()=>[route],revealPane:()=>{},openExactSession:async()=>{}}
const entryCount=async n=>waitFor(()=>check(screen.getByRole('button',{name:/已跟踪/}).textContent.includes(`· ${n}`),`entry did not show ${n}`))
async function run(){
  if(!name?.startsWith('followup_isolated_test_'))throw Error('isolated name required')
  const local=createFollowupNotifications(window,BroadcastChannel)
  const remote=createFollowupNotifications(window,BroadcastChannel)
  const entry=render(<FollowupEntry host={host} t={t} notifications={local} storageName={name}/> )
  await entryCount(0)
  const pane=render(<FollowupPane host={host} t={t} notifications={local} storageName={name}/> )
  await screen.findByText('尚未跟踪事项')
  fireEvent.click(screen.getByRole('button',{name:'跟踪当前会话'}))
  await screen.findByLabelText('事项名称')
  fireEvent.change(screen.getByLabelText('事项名称'),{target:{value:'事项一'}})
  fireEvent.click(screen.getByRole('button',{name:'保存跟踪'}))
  await screen.findByRole('heading',{name:'事项一'})
  await entryCount(1)
  pane.unmount()
  const store=await openFollowupStore(indexedDB,name)
  const task=createFollowupTask({id:'second-id',title:'事项二',primary:{storedId:'session-b',connectionId:'fixture-ssh',profile:'default',targetProfile:'worker'},related:[],nextSteps:[]},Date.now())
  await store.create(task);remote.publish();await entryCount(2)
  const reopened=render(<FollowupPane host={host} t={t} notifications={local} storageName={name}/> )
  await screen.findByRole('button',{name:/事项二/})
  const third=createFollowupTask({id:'third-id',title:'事项三',primary:{storedId:'session-c',connectionId:'fixture-ssh',profile:'default',targetProfile:'worker'},related:[],nextSteps:[]},Date.now())
  await store.create(third);remote.publish()
  await screen.findByRole('button',{name:/事项三/})
  await entryCount(3)
  reopened.unmount();entry.unmount();store.close();local.close();remote.close();cleanup()
  return {status:'PASS',cases:['pane commit updates persistent entry','closed pane retains count','other context changes reach entry','pane reloads after reopen','open pane reloads on remote commit']}
}
run().then(v=>document.querySelector('#result').textContent=JSON.stringify(v)).catch(e=>document.querySelector('#result').textContent=JSON.stringify({status:'FAIL',error:String(e),stack:e?.stack}))
