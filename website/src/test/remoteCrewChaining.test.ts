import { describe, it, expect, vi, afterEach } from 'vitest'
import type { InstanceView } from '../api/client'
import { chainRows } from '../components/InstanceTabBar'
import { announceChainedCrew, CHAINED_CREW_MESSAGE } from '../lib/chainAnnounce'

/** Minimal InstanceView; only the fields the chain rules read matter. */
function inst(id: string, extra: Partial<InstanceView> = {}): InstanceView {
  return {
    id,
    name: id,
    ssh_host: `${id}-host`,
    remote_port: 5476,
    local_port: 0,
    ttl: '20h',
    remote_bin: '',
    connection_method: 'ssh',
    ssm_target: '',
    aws_profile: '',
    aws_region: '',
    ssm_run_as: '',
    was_connected: true,
    status: { instance_id: id, state: 'connected' },
    ...extra,
  }
}

function chained(id: string, parent: string, extra: Partial<InstanceView> = {}): InstanceView {
  return inst(id, { via_instance_id: parent, via_remote_port: 53999, ...extra })
}

describe('chainRows', () => {
  it('puts each crew before the crews reached through it, and records the depth', () => {
    const rows = chainRows([inst('b'), chained('c', 'b'), chained('d', 'c'), inst('other')])
    expect(rows.map(r => r.inst.id)).toEqual(['b', 'c', 'd', 'other'])
    expect(rows.map(r => r.depth)).toEqual([0, 1, 2, 0])
    expect(rows.map(r => r.parentName)).toEqual(['', 'b', 'c', ''])
  })

  it('keeps the incoming order among siblings', () => {
    // Tab order is a user-visible preference; the tree must reorder nothing it
    // does not have to.
    const rows = chainRows([inst('b'), inst('a'), chained('c', 'b')])
    expect(rows.map(r => r.inst.id)).toEqual(['b', 'c', 'a'])
  })

  it('treats a crew whose parent has no tab as a root rather than hiding it', () => {
    // The parent was never connected, so it has no tab. Dropping the child would
    // hide a crew that is genuinely connected.
    const rows = chainRows([chained('c', 'never-connected')])
    expect(rows).toHaveLength(1)
    expect(rows[0].depth).toBe(0)
    expect(rows[0].parentName).toBe('')
  })

  it('marks a crew unreachable when an ancestor hop is down', () => {
    // They share the ancestor's tunnel, so a child cannot outlive it however
    // recently its own state said 'connected'.
    const rows = chainRows([
      inst('b', { status: { instance_id: 'b', state: 'error' } }),
      chained('c', 'b'),
      chained('d', 'c'),
    ])
    expect(rows.map(r => [r.inst.id, r.reachable])).toEqual([
      ['b', true],
      ['c', false],
      ['d', false],
    ])
  })

  it('keeps a whole healthy chain reachable', () => {
    const rows = chainRows([inst('b'), chained('c', 'b'), chained('d', 'c')])
    expect(rows.every(r => r.reachable)).toBe(true)
  })

  it('emits every crew exactly once even when the registry holds a loop', () => {
    // A hand-edited registry can name a cycle. A row the user can see is what
    // lets them disconnect it and fix the file.
    const rows = chainRows([chained('a', 'b'), chained('b', 'a')])
    expect(rows.map(r => r.inst.id).sort()).toEqual(['a', 'b'])
    expect(new Set(rows.map(r => r.inst.id)).size).toBe(rows.length)
  })

  it('leaves a list with no chained crew exactly as it was', () => {
    const flat = [inst('a'), inst('b'), inst('c')]
    const rows = chainRows(flat)
    expect(rows.map(r => r.inst.id)).toEqual(['a', 'b', 'c'])
    expect(rows.every(r => r.depth === 0 && r.parentName === '' && r.reachable)).toBe(true)
  })
})

describe('announceChainedCrew', () => {
  const notice = { id: 'c', name: 'C', sshHost: 'c-host', remotePort: 5476, port: 53999 }

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  /** Stand in for a pane: `self !== top`, with a recording parent. */
  function asPane() {
    const posted: unknown[] = []
    const parent = { postMessage: (data: unknown) => posted.push(data) }
    vi.stubGlobal('window', { self: {}, top: {}, parent })
    return posted
  }

  it('does nothing at top level, where there is no host to tell', () => {
    const posted: unknown[] = []
    const shared = {}
    vi.stubGlobal('window', {
      self: shared,
      top: shared,
      parent: { postMessage: (d: unknown) => posted.push(d) },
    })
    expect(announceChainedCrew(notice)).toBe(false)
    expect(posted).toEqual([])
  })

  it('carries the crew and the hop port, and no credential', () => {
    const posted = asPane()
    expect(announceChainedCrew(notice)).toBe(true)
    expect(posted).toHaveLength(1)
    const msg = posted[0] as Record<string, unknown>
    expect(msg.type).toBe(CHAINED_CREW_MESSAGE)
    expect(msg.id).toBe('c')
    expect(msg.port).toBe(53999)
    // The whole reason this notice may travel through frame code: it names a
    // crew and a port, and nothing that grants access to either.
    const keys = Object.keys(msg).join(' ')
    expect(keys).not.toMatch(/token|secret|cookie|credential/i)
  })

  it('refuses a payload with no usable hop port', () => {
    const posted = asPane()
    for (const port of [0, -1, 70000, 1.5, Number.NaN]) {
      expect(announceChainedCrew({ ...notice, port })).toBe(false)
    }
    expect(announceChainedCrew({ ...notice, id: '' })).toBe(false)
    expect(posted).toEqual([])
  })

  it('stays silent when the post itself throws', () => {
    // A pane whose host predates the message, or a browser that refuses the
    // post, must not break the connect that just succeeded.
    vi.stubGlobal('window', {
      self: {},
      top: {},
      parent: {
        postMessage: () => {
          throw new Error('cross-origin')
        },
      },
    })
    expect(announceChainedCrew(notice)).toBe(false)
  })
})
