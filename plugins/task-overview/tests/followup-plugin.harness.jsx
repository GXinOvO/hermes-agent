import React from 'react'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import plugin from '../src/plugin.jsx'
import { setTestTranslations } from './sdk-stub.jsx'

const check=(condition,message)=>{if(!condition)throw Error(message)}
const database = new URL(location.href).searchParams.get('db')
const originalOpen = indexedDB.open.bind(indexedDB)
const contributions=[]
const disposals=[]
let registered
const ctx={i18n:{register:value=>{registered=value;setTestTranslations(value)}},register:value=>{contributions.push(value)},onDispose:fn=>disposals.push(fn)}
async function run(){
  check(database?.startsWith('followup_isolated_test_'),'isolated database required')
  indexedDB.open = (name, version) => originalOpen(name === 'hermes.plugin.task-overview.followup.v1' ? database : name, version)
  plugin.register(ctx)
  check(plugin.id==='task-overview','plugin identity changed')
  check(contributions.some(c=>c.id==='pane'&&c.area==='panes'),'existing overview pane missing')
  check(contributions.some(c=>c.id==='record'),'existing transcript record contribution missing')
  const pane=contributions.find(c=>c.id==='followup'&&c.area==='panes')
  const entry=contributions.find(c=>c.id==='followup-entry'&&c.area==='titleBar.right')
  check(pane&&entry,'follow-up pane and titlebar entry were not registered')
  check(pane.data.placement==='right','follow-up pane not placed at right')
  check(registered?.zh?.followup?.scope && registered?.en?.followup?.scope,'scope localization missing')
  const entryRender=render(entry.render())
  await screen.findByRole('button',{name:/已跟踪/})
  const paneRender=render(pane.render())
  await screen.findByRole('region',{name:'事项跟进'})
  await waitFor(()=>check(screen.getByText('尚未跟踪事项'),'storage not loaded'))
  fireEvent.click(screen.getByRole('button',{name:'跟踪当前会话'}))
  await screen.findByLabelText('事项名称')
  fireEvent.change(screen.getByLabelText('事项名称'),{target:{value:'插件注册测试'}})
  fireEvent.click(screen.getByRole('button',{name:'保存跟踪'}))
  await screen.findByRole('heading',{name:'插件注册测试'})
  await waitFor(()=>check(screen.getByRole('button',{name:/已跟踪/}).textContent.includes('1'),'registered entry count did not update'))
  paneRender.unmount()
  check(screen.getByRole('button',{name:/已跟踪/}).textContent.includes('1'),'entry lost count when pane closed')
  entryRender.unmount();cleanup()
  for(const dispose of disposals)dispose()
  return {status:'PASS',cases:['existing overview retained','followup pane registration','titlebar contribution','localized scope','registered pane creates item','registered entry updates','entry remains after pane close']}
}
run().then(value=>document.querySelector('#result').textContent=JSON.stringify(value)).catch(error=>document.querySelector('#result').textContent=JSON.stringify({status:'FAIL',error:String(error),stack:error?.stack}))
