import { describe, it, expect } from 'vitest'
import { parseSpec, composeSpec } from '../apps/board/specFields'

describe('parseSpec', () => {
  it('splits a clean ## WHY / ## WHAT markdown shape', () => {
    const text = '## WHY\nbecause reasons\n\n## WHAT (acceptance criteria from parent story)\n1. a thing is true'
    expect(parseSpec(text)).toEqual({ why: 'because reasons', what: '1. a thing is true' })
  })

  it('splits a bare "WHY:" prefix with no markdown heading', () => {
    const text = 'WHY: because reasons\n\nWHAT (acceptance criteria):\n1. a thing is true'
    expect(parseSpec(text)).toEqual({ why: 'because reasons', what: '1. a thing is true' })
  })

  it('splits a single-hash "# WHY:" prefix', () => {
    const text = '# WHY: because reasons\n\nWHAT (acceptance criteria - all must be true):\n1. a thing is true'
    expect(parseSpec(text)).toEqual({ why: 'because reasons', what: '1. a thing is true' })
  })

  it('puts everything under WHY when there is no recognizable WHAT section', () => {
    expect(parseSpec('**bold** description')).toEqual({ why: '**bold** description', what: '' })
  })

  it('puts everything under WHY when there is no recognizable WHY marker at all', () => {
    expect(parseSpec('just some text')).toEqual({ why: 'just some text', what: '' })
  })

  it('handles an empty description', () => {
    expect(parseSpec('')).toEqual({ why: '', what: '' })
  })
})

describe('composeSpec', () => {
  it('combines WHY and WHAT into the normalized shape', () => {
    expect(composeSpec({ why: 'because reasons', what: '1. a thing is true' }))
      .toBe('WHY: because reasons\n\nWHAT (acceptance criteria):\n1. a thing is true')
  })

  it('omits the WHAT heading entirely when WHAT is blank', () => {
    expect(composeSpec({ why: 'because reasons', what: '' })).toBe('because reasons')
  })

  it('round-trips through parseSpec', () => {
    const original = { why: 'because reasons', what: '1. a thing is true' }
    expect(parseSpec(composeSpec(original))).toEqual(original)
  })
})
