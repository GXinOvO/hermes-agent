import React from 'react'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { FollowupPane } from '../src/followup-pane.jsx'

const labels = { title:'事项跟进', all:'未完成', attention:'待关注', search:'搜索事项 / 关联会话', scope:'仅显示已手动跟踪的事项；其他会话尚未覆盖。', unknown:'待核对', loading:'正在读取事项', storageError:'事项存储不可用', noMatches:'没有匹配的事项', empty:'尚未跟踪事项', steps:'还剩什么', noSteps:'尚未填写剩余步骤', primary:'主会话', related:'相关会话', open:'打开主会话', sourceUnavailable:'来源当前不可用', sessionError:'无法打开会话', retry:'重试', add:'跟踪当前会话', selected:'所选事项', execution:'执行状态', executionUnknown:'未知', noAction:'当前无法确定是否需要处理', taskTitle:'事项名称', nextSteps:'剩余步骤', save:'保存跟踪', cancel:'取消', tracking:'跟踪会话', actionError:'操作失败', ownerIdentityUnavailable:'无法核实当前会话的完整来源，已阻止登记。', savedRefresh:'事项已保存，但列表刷新失败；请重试。', accepted:'已验收', accept:'标记已验收', confirmAccept:'确认已验收', acceptHint:'请在核对结果后确认。', acceptNote:'验收说明', reopen:'重新跟踪', actionRefresh:'变更已保存，但列表刷新失败；请重试。' }
const t = key => labels[key] || key
const route = { connectionId: 'fixture-ssh', profile: 'default', targetProfile: 'worker', mode: 'remote' }
let focus = { storedId:'fixture-session', owner:{ connectionId:'fixture-ssh', profile:'default', targetProfile:'worker', mode:'remote' } }
const opens = []
const host = { state: { focusedStoredSessionId:{get:()=>focus.storedId}, focusedSessionOwner:{get:()=>focus.owner} }, profileRoutes:async()=>[route], openExactSession:async (...args)=>opens.push(args) }
const name = new URL(location.href).searchParams.get('db')
const check = (condition, message) => { if (!condition) throw Error(message) }
async function run() {
  if (!name?.startsWith('followup_isolated_test_')) throw Error('Expected isolated database name')
  const view = render(<FollowupPane host={host} t={t} storageName={name} />)
  await screen.findByText('尚未跟踪事项')
  focus = { storedId:'fixture-session', owner:{ connectionId:'fixture-ssh', profile:'default' } }
  fireEvent.click(screen.getByRole('button', { name:'跟踪当前会话' }))
  await screen.findByLabelText('事项名称')
  check(screen.getByRole('status').textContent === 'unverifiedSession', 'An unverified source was not disclosed')
  fireEvent.click(screen.getByRole('button', {name:'取消'}))
  const { openFollowupStore } = await import('../src/followup-indexeddb.mjs')
  const initialStore = await openFollowupStore(indexedDB, name)
  check((await initialStore.list()).length === 0, 'Cancelling an unverified capture created a record')
  initialStore.close()
  focus = { storedId:'fixture-session', owner:{ connectionId:'fixture-ssh', profile:'default', targetProfile:'worker', mode:'remote' } }
  fireEvent.click(screen.getByRole('button', { name:'跟踪当前会话' }))
  await screen.findByLabelText('事项名称')
  check(screen.getByText(/fixture-session/), 'Tracking form did not show the captured session')
  fireEvent.change(screen.getByLabelText('事项名称'), { target:{value:'实际验收'} })
  fireEvent.change(screen.getByLabelText('剩余步骤'), { target:{value:'检查输出\n验证恢复'} })
  fireEvent.click(screen.getByRole('button', {name:'保存跟踪'}))
  await screen.findByRole('heading', {name:'实际验收'})
  check(screen.getByText('检查输出') && screen.getByText('验证恢复'), 'Stored next steps not displayed')
  view.unmount()
  const reopened = render(<FollowupPane host={host} t={t} storageName={name} />)
  await screen.findByRole('heading', {name:'实际验收'})
  check(screen.getByText('检查输出'), 'Reopened pane lost the saved task')
  check(screen.getByRole('button', {name:'打开主会话'}).disabled, 'Unverified session open was enabled')
  check(opens.length === 0, 'Unverified session was opened')
  check(screen.getByRole('heading', {name:'实际验收'}), 'Disabled navigation removed the task')
  fireEvent.click(screen.getByRole('button', {name:'标记已验收'}))
  await screen.findByText('请在核对结果后确认。')
  fireEvent.change(screen.getByLabelText('验收说明'), {target:{value:'已核对实际结果'}})
  fireEvent.click(screen.getByRole('button', {name:'确认已验收'}))
  await screen.findByText('尚未跟踪事项')
  fireEvent.click(screen.getByRole('button', {name:/已验收/}))
  await screen.findByRole('heading', {name:'实际验收'})
  check(screen.getByText('已核对实际结果'), 'Acceptance note was not displayed')
  reopened.unmount()
  const afterAccepted = render(<FollowupPane host={host} t={t} storageName={name} />)
  await screen.findByText('尚未跟踪事项')
  fireEvent.click(screen.getByRole('button', {name:/已验收/}))
  await screen.findByRole('heading', {name:'实际验收'})
  fireEvent.click(screen.getByRole('button', {name:'重新跟踪'}))
  await waitFor(() => check(!screen.queryByRole('heading', {name:'实际验收'}), 'Reopened task remained in accepted filter'))
  fireEvent.click(screen.getByRole('button', {name:/未完成/}))
  await screen.findByRole('heading', {name:'实际验收'})
  afterAccepted.unmount()
  cleanup()
  let capturedStore
  const failRefreshOnce = async (...args) => {
    const real = await openFollowupStore(...args)
    let reads = 0
    capturedStore = real
    return { ...real, list: async () => { reads++; if (reads === 2) throw Error('synthetic read failure'); return real.list() } }
  }
  const failure = render(<FollowupPane host={host} t={t} storageName={`${name}_refresh`} openStore={failRefreshOnce} />)
  await screen.findByText('尚未跟踪事项')
  fireEvent.click(screen.getByRole('button', { name:'跟踪当前会话' }))
  await screen.findByLabelText('事项名称')
  fireEvent.change(screen.getByLabelText('事项名称'), { target:{value:'已提交但刷新失败'} })
  fireEvent.click(screen.getByRole('button', {name:'保存跟踪'}))
  await screen.findByText('事项已保存，但列表刷新失败；请重试。')
  check(!screen.queryByRole('button', {name:'保存跟踪'}), 'Committed task can be accidentally submitted again')
  check((await capturedStore.list()).length === 1, 'Committed task missing after refresh failure')
  fireEvent.click(screen.getByRole('button', {name:'重试'}))
  await screen.findByRole('heading', {name:'已提交但刷新失败'})
  failure.unmount()
  cleanup()
  return { status:'PASS', cases:['capture focused session','create through view','real browser database','unmount/remount readback','unverified session open disabled','committed write with failed refresh','retry readback'] }
}
run().then(result=>document.querySelector('#result').textContent=JSON.stringify(result)).catch(error=>document.querySelector('#result').textContent=JSON.stringify({status:'FAIL',error:String(error),stack:error?.stack}))
