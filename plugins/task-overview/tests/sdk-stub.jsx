import React from 'react'
export function Button({children, variant, size, ...props}) { return <button {...props}>{children}</button> }
export function Badge({children, variant, ...props}) { return <span {...props}>{children}</span> }
export function SearchField({value, onChange, placeholder, ...props}) { return <input type="text" value={value} onChange={event => onChange(event.target.value)} placeholder={placeholder} aria-label={props['aria-label'] || placeholder}/> }
export function Codicon({name}) { return <span aria-hidden="true" data-icon={name}/> }
export function RowButton({children, ...props}) { return <button type="button" {...props}>{children}</button> }
export function Input(props) { return <input {...props}/> }
export function Textarea(props) { return <textarea {...props}/> }
export function SegmentedControl({options, value, onChange, disabled}) { return <div>{options.map(option => <button key={option.id} type="button" disabled={disabled} aria-pressed={value === option.id} onClick={() => onChange(option.id)}>{option.label}</button>)}</div> }
export function Loader() { return <span/> }
export function Streamdown({children}) { return <div>{children}</div> }
export function useValue(value) { return value?.get?.() }
export function useQuery() { return {} }
export function useQueryClient() { return {} }
export const PALETTE_AREA = 'palette'
export const TRANSCRIPT_DIRECTIVE_AREA = 'transcript.directive'
export const TITLEBAR_AREAS = { right: 'titleBar.right' }
export const host = {
  state: {
    focusedStoredSessionId: { get: () => 'fixture-session' },
    focusedSessionOwner: { get: () => ({ connectionId: 'fixture-host', profile: 'default', targetProfile: 'backend', mode: 'remote' }) }
  },
  profileRoutes: async () => [{ connectionId: 'fixture-host', profile: 'default', targetProfile: 'backend', mode: 'remote' }],
  openExactSession: async () => {},
  revealPane: () => {}
}
let translations = {}
export function setTestTranslations(value) { translations = value }
export function usePluginI18n() { return key => key.split('.').reduce((part, field) => part?.[field], translations.zh) ?? key }
