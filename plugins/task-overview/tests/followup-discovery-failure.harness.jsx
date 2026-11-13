import React from 'react'
import {render,screen,fireEvent,cleanup} from '@testing-library/react'
import {FollowupView} from '../src/followup-view.jsx'
import {discoverSessions} from '../src/followup-discovery.mjs'
import {followupStrings} from '../src/followup-i18n.mjs'
const check=(v,m)=>{if(!v)throw Error(m)}
const route=id=>({connectionId:id,profile:'default',targetProfile:'default',mode:'remote'})
const page={sessions:[],profile_totals:{default:0},errors:[],storage:{}}
const t=key=>followupStrings.zh[key]||key
async function renderResult(discovery){
 const app=render(<FollowupView t={t} rows={[]} discovery={discovery} onDiscover={()=>{}} onOpen={()=>{}} onTrack={()=>{}}/>)
 fireEvent.click(screen.getByRole('button',{name:'会话待核对'}))
 return app
}
async function run(){
 const routes=[route('a'),route('b')]
 const all=await discoverSessions({profileRoutes:async()=>routes,listPersistedSessions:async()=>{throw Error('offline')}})
 let app=await renderResult(all)
 check(screen.getByText('部分来源未读取，无法判断是否存在会话。'),'all failed must be explicitly unknown')
 check(!screen.queryByText('当前扫描范围没有可显示的会话。'),'all failed looked like an empty successful scan')
 app.unmount();cleanup()
 const mixed=await discoverSessions({profileRoutes:async()=>routes,listPersistedSessions:async r=>{if(r.connectionId==='b')throw Error('offline');return page}})
 app=await renderResult(mixed)
 check(screen.getByText('部分来源未读取，无法判断是否存在会话。'),'partial failure must be explicit')
 check(!screen.queryByText('当前扫描范围没有可显示的会话。'),'partial failure looked like a completed empty scan')
 app.unmount();cleanup()
 const healthy=await discoverSessions({profileRoutes:async()=>routes,listPersistedSessions:async()=>page})
 app=await renderResult(healthy)
 check(screen.getByText('来源清单可能使用缓存，尚不能确认覆盖全部来源。'), 'cached inventory freshness must be explicit')
 check(screen.getByText('当前扫描范围没有可显示的会话。'),'healthy bounded empty result should be shown alongside the freshness warning')
 check(!screen.queryByText('部分来源未读取，无法判断是否存在会话。'),'healthy empty scan falsely shows failure')
 app.unmount();cleanup()
 const missing=await discoverSessions({profileRoutes:async()=>{throw Error('no registry')}})
 app=await renderResult(missing)
 check(screen.getByText('来源清单不可用或为空'),'missing inventory must be reported')
 check(!screen.queryByText('当前扫描范围没有可显示的会话。'),'unknown inventory looked empty')
 app.unmount();cleanup()
 return {status:'PASS',cases:['all sources unavailable','mixed empty and unavailable','healthy bounded empty','inventory missing']}
}
run().then(v=>document.querySelector('#result').textContent=JSON.stringify(v)).catch(e=>document.querySelector('#result').textContent=JSON.stringify({status:'FAIL',error:String(e),stack:e?.stack}))
