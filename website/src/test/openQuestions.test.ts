import { describe, it, expect } from 'vitest'
import { extractOpenQuestions, findPendingOpenQuestions, formatAnswers } from '../apps/board/openQuestions'
import type { BeadsComment } from '../apps/board/boardApi'

// A trimmed-down but structurally real excerpt of what rocket-plan actually
// posted on crew-rocket-4da.1 this session (verified live via `bd comments`).
const REAL_PLAN_COMMENT = `## PLAN DRAFT: crew-rocket-4da.1

DECISION: Language is Python.
REASON: No existing application code in crew-rocket.

---

## Grapes

### Grape 1: Expression parser with tests

**Files:**
- calculator/parser.py

---

## OPEN QUESTIONS

1. **Language:** Python is assumed based on absence of evidence. Is there a language constraint or preference for the calculator?
2. **Module location:** The plan places the calculator at \`crew-rocket/calculator/\`. Should it live somewhere else?
3. **pytest availability:** The verification step assumes \`pytest\` is installed. Is there a preferred harness?

---

To proceed: answer these questions as a comment on crew-rocket-4da.1, then invoke \`rocket-confirm-plan crew-rocket-4da.1\`.
`

function comment(text: string, overrides: Partial<BeadsComment> = {}): BeadsComment {
  return { id: 'c1', author: 'agent', text, created_at: 't1', ...overrides }
}

describe('extractOpenQuestions', () => {
  it('extracts all numbered questions from the real plan comment shape', () => {
    const questions = extractOpenQuestions(REAL_PLAN_COMMENT)
    expect(questions).toHaveLength(3)
    expect(questions[0].number).toBe(1)
    expect(questions[0].text).toContain('Language')
    expect(questions[1].text).toContain('Module location')
    expect(questions[2].text).toContain('pytest availability')
  })

  it('does not include the trailing instruction line after the hr', () => {
    const questions = extractOpenQuestions(REAL_PLAN_COMMENT)
    expect(questions.every(q => !q.text.includes('rocket-confirm-plan'))).toBe(true)
  })

  it('returns empty for a comment with no OPEN QUESTIONS heading', () => {
    expect(extractOpenQuestions('just a regular comment, no heading here')).toEqual([])
  })

  it('is case-insensitive and tolerates a markdown heading prefix', () => {
    const text = '### open questions\n\n1. Is this ok?\n'
    expect(extractOpenQuestions(text)).toHaveLength(1)
  })

  it('handles a single question with no trailing hr at all', () => {
    const text = 'OPEN QUESTIONS\n\n1. Only one question here, nothing after it.'
    const questions = extractOpenQuestions(text)
    expect(questions).toHaveLength(1)
    expect(questions[0].text).toBe('Only one question here, nothing after it.')
  })
})

describe('findPendingOpenQuestions', () => {
  it('returns null when there are no comments', () => {
    expect(findPendingOpenQuestions([])).toBeNull()
  })

  it('returns the questions when the LAST comment carries them', () => {
    const comments = [comment('earlier note'), comment(REAL_PLAN_COMMENT)]
    expect(findPendingOpenQuestions(comments)).toHaveLength(3)
  })

  it('returns null once a later comment has superseded the questions', () => {
    const comments = [comment(REAL_PLAN_COMMENT), comment('1. Python is fine\n2. keep it here\n3. yes, pytest')]
    expect(findPendingOpenQuestions(comments)).toBeNull()
  })
})

describe('formatAnswers', () => {
  it('renders only the answered questions, numbered to match', () => {
    const questions = extractOpenQuestions(REAL_PLAN_COMMENT)
    const text = formatAnswers(questions, { 1: 'Python is fine', 3: 'yes, use pytest' })
    expect(text).toBe('Answers to the open questions:\n\n1. Python is fine\n3. yes, use pytest')
  })

  it('omits blank/whitespace-only answers', () => {
    const questions = extractOpenQuestions(REAL_PLAN_COMMENT)
    const text = formatAnswers(questions, { 1: '  ', 2: 'crew-rocket/calculator is fine' })
    expect(text).toBe('Answers to the open questions:\n\n2. crew-rocket/calculator is fine')
  })
})
