import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createFollowupNotifications, FOLLOWUP_CHANNEL } from '../src/followup-notifications.mjs'

class Channel {
  static members = new Set()
  constructor(name) { this.name = name; this.closed = false; Channel.members.add(this) }
  postMessage(value) { for (const peer of Channel.members) if (peer !== this && !peer.closed && peer.name === this.name) peer.onmessage?.({ data: value }) }
  close() { this.closed = true; Channel.members.delete(this) }
}

test('a committed change notifies same-window subscribers and other windows, with no task contents', () => {
  const a = createFollowupNotifications(new EventTarget(), Channel)
  const b = createFollowupNotifications(new EventTarget(), Channel)
  const received = []
  a.subscribe(() => received.push('local'))
  b.subscribe(() => received.push('remote'))
  a.publish()
  assert.deepEqual(received, ['local', 'remote'])
  assert.equal([...Channel.members][0].name, FOLLOWUP_CHANNEL)
  a.close(); b.close()
  assert.equal(Channel.members.size, 0)
})

test('unsubscribing and closing stop delivery without disrupting other subscribers', () => {
  const bus = createFollowupNotifications(new EventTarget(), Channel)
  let a = 0, b = 0
  const stop = bus.subscribe(() => a++)
  bus.subscribe(() => b++)
  stop(); bus.publish(); bus.close(); bus.publish()
  assert.equal(a, 0)
  assert.equal(b, 1)
})

test('a failed channel creation leaves the local notification usable', () => {
  class Refused { constructor() { throw Error('disabled') } }
  const bus = createFollowupNotifications(new EventTarget(), Refused)
  let count = 0
  bus.subscribe(() => count++)
  bus.publish()
  assert.equal(count, 1)
  bus.close()
})

test('a faulty listener cannot prevent other listeners or cross-window notice', () => {
  const a = createFollowupNotifications(new EventTarget(), Channel)
  const b = createFollowupNotifications(new EventTarget(), Channel)
  let local = 0, remote = 0
  a.subscribe(() => { throw Error('bad listener') })
  a.subscribe(() => local++)
  b.subscribe(() => remote++)
  assert.doesNotThrow(() => a.publish())
  assert.equal(local, 1)
  assert.equal(remote, 1)
  a.close(); b.close()
})
