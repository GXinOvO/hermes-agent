import React from 'react'
import {render,screen,fireEvent,waitFor,cleanup} from '@testing-library/react'
import {FollowupPane} from '../src/followup-pane.jsx'
import {openFollowupStore} from '../src/followup-indexeddb.mjs'
import {createFollowupNotifications} from '../src/followup-notifications.mjs'
import {createFollowupTask} from '../src/followup-tasks.mjs'
const labels={title:'事项跟进',all:'未完成',attention:'待关注',accepted:'已验收',search:'搜索',scope:'仅显示已跟踪事项',unknown:'待核对',loading:'正在读取',storageError:'事项存储不可用',noMatches:'无匹配项',empty:'尚未跟踪事项',noAccepted:'没有已验收事项',retry:'重试',add:'跟踪当前会话',savedRefresh:'已保存但刷新失败',staleRefresh:'刷新失败，显示的事项可能已过期。',actionError:'操作失败'}
const t=key=>labels[key]||key
const host={state:{},profileRoutes:async()=>[]}
const name=new URL(location.href).searchParams.get('db')
const check=(condition,message)=>{if(!condition)throw Error(message)}
async function run(){
 if(!name?.startsWith('followup_isolated_test_'))throw Error('isolated database required')
 let opens=0
 const failOpenOnce=async (...args)=>{if(++opens===1)throw Error('injected open failure');return openFollowupStore(...args)}
 const first=render(<FollowupPane host={host} t={t} storageName={`${name}_open`} openStore={failOpenOnce}/> )
 await screen.findByText('事项存储不可用')
 check(!screen.queryByText('尚未跟踪事项'),'open failure must not appear empty')
 check(screen.getByRole('button',{name:/未完成/}).textContent.includes('—'),'open failure count must be unknown')
 fireEvent.click(screen.getByRole('button',{name:'重试'}))
 await screen.findByText('尚未跟踪事项')
 check(opens===2,'retry should reopen unavailable database')
 first.unmount();cleanup()
 let reads=0
 const failListOnce=async (...args)=>{const actual=await openFollowupStore(...args);return {...actual,list:async()=>{if(++reads===1)throw Error('injected list failure');return actual.list()}}}
 const second=render(<FollowupPane host={host} t={t} storageName={`${name}_list`} openStore={failListOnce}/> )
 await screen.findByText('事项存储不可用')
 check(!screen.queryByText('尚未跟踪事项'),'list failure must not appear empty')
 check(!screen.queryByText('已保存但刷新失败'),'initial read failure must not claim a save')
 fireEvent.click(screen.getByRole('button',{name:'重试'}))
 await screen.findByText('尚未跟踪事项')
 check(reads===2,'retry should re-read the open database')
 second.unmount();cleanup()
 const dbName=`${name}_stale`, real=await openFollowupStore(indexedDB, dbName)
 const task=createFollowupTask({id:'id',title:'已登记事项',primary:{connectionId:'host',profile:'default',targetProfile:'default',storedId:'session'},related:[],nextSteps:[]},1000)
 await real.create(task); real.close()
 let refreshReads=0
 const failBackgroundOnce=async (...args)=>{const actual=await openFollowupStore(...args);return {...actual,list:async()=>{if(++refreshReads===2)throw Error('injected background failure');return actual.list()}}}
 const bus=createFollowupNotifications(new EventTarget(),null)
 const third=render(<FollowupPane host={host} t={t} storageName={dbName} openStore={failBackgroundOnce} notifications={bus}/>)
 await screen.findByRole('heading',{name:'已登记事项'})
 bus.publish()
 await screen.findByText('刷新失败，显示的事项可能已过期。')
 check(screen.getByRole('heading',{name:'已登记事项'}),'last known items disappeared')
 check(!screen.queryByText('已保存但刷新失败'),'background failure falsely claims a save')
 check(screen.getByRole('button',{name:/未完成/}).textContent.includes('—'),'stale count must be unknown')
 fireEvent.click(screen.getByRole('button',{name:'重试'}))
 await waitFor(()=>check(!screen.queryByText('刷新失败，显示的事项可能已过期。'),'stale warning not cleared'))
 third.unmount();bus.close();cleanup()
 return {status:'PASS',cases:['open failure shows unknown','retry reopens database','initial read failure shows unknown','retry reuses open database','background refresh keeps stale list','stale count unknown','background retry']}
}
run().then(v=>document.querySelector('#result').textContent=JSON.stringify(v)).catch(e=>document.querySelector('#result').textContent=JSON.stringify({status:'FAIL',error:String(e),stack:e?.stack}))
