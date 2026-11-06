import React from 'react'
import { Button, host, PALETTE_AREA, TRANSCRIPT_DIRECTIVE_AREA, usePluginI18n } from '@hermes/plugin-sdk'
import { ID, strings, TaskPane } from './ui.jsx'
import { parseTaskDirective } from './core.mjs'

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
    ctx.i18n.register(strings)
    ctx.register({ id: 'pane', area: 'panes', title: '任务概览', data: { placement: 'right', width: '360px' }, render: () => <TaskPane/> })
    ctx.register({ id: 'open', area: PALETTE_AREA, data: { id: 'open', label: '任务概览 / Task overview', keywords: ['task', 'overview', '任务', '进度'], run: () => host.revealPane(`${ID}:pane`) } })
    ctx.register({ id: 'record', area: TRANSCRIPT_DIRECTIVE_AREA, data: { name: 'task', render: props => <TaskRecord {...props}/> } })
  }
}
