import React from 'react'
import { Button, host, PALETTE_AREA, TITLEBAR_AREAS, TRANSCRIPT_DIRECTIVE_AREA, usePluginI18n } from '@hermes/plugin-sdk'
import { ID, strings, TaskPane } from './ui.jsx'
import { parseTaskDirective } from './core.mjs'
import { FollowupPane } from './followup-pane.jsx'
import { FollowupEntry } from './followup-entry.jsx'
import { followupStrings } from './followup-i18n.mjs'
import { createFollowupNotifications } from './followup-notifications.mjs'

function FollowupPanel({ notifications }) {
  const translate = usePluginI18n(ID)
  const t = key => translate(`followup.${key}`)
  return <FollowupPane host={host} t={t} notifications={notifications}/>
}

function FollowupTitle({ notifications }) {
  const translate = usePluginI18n(ID)
  const t = key => translate(`followup.${key}`)
  return <FollowupEntry host={host} t={t} notifications={notifications}/>
}

function TaskRecord({ source }) {
  const t = usePluginI18n(ID)
  if (!parseTaskDirective(source)) return <span>{source}</span>
  return <Button size="sm" variant="secondary" onClick={() => host.revealPane(`${ID}:pane`)}>{t('pane')} · {t('registration')}</Button>
}
export default {
  id: ID,
  name: '任务概览 / Task overview',
  description: 'Read the focused session requirements, checklist, remaining work and evidence without confusing agent activity with acceptance.',
  register(ctx) {
    ctx.i18n.register(Object.fromEntries(Object.entries(strings).map(([locale, messages]) => [locale, { ...messages, followup: followupStrings[locale] || followupStrings.en }])))
    const notifications = createFollowupNotifications()
    ctx.onDispose(() => notifications.close())
    ctx.register({ id: 'pane', area: 'panes', title: '任务概览', data: { placement: 'right', width: '360px' }, render: () => <TaskPane/> })
    ctx.register({ id: 'followup', area: 'panes', title: '事项跟进', data: { placement: 'right', width: '520px', hideOnly: true }, render: () => <FollowupPanel notifications={notifications}/> })
    ctx.register({ id: 'followup-entry', area: TITLEBAR_AREAS.right, order: 110, render: () => <FollowupTitle notifications={notifications}/> })
    ctx.register({ id: 'open', area: PALETTE_AREA, data: { id: 'open', label: '任务概览 / Task overview', keywords: ['task', 'overview', '任务', '进度'], run: () => host.revealPane(`${ID}:pane`) } })
    ctx.register({ id: 'record', area: TRANSCRIPT_DIRECTIVE_AREA, data: { name: 'task', render: props => <TaskRecord {...props}/> } })
  }
}
