/**
 * WHY / WHAT (acceptance criteria) are the two fields a "Spec" (a board
 * story) is actually built from - not an invented shape, this is what
 * `rocket-ideate` writes and `rocket-plan`/`rocket-confirm-plan` build on
 * (its own SKILL.md calls the acceptance-criteria half "the spec"
 * explicitly). Confirmed live against every real spec in this project: all
 * of them already carry a WHY paragraph followed by a WHAT/acceptance-
 * criteria list, just with inconsistent markdown (a "## WHY" heading, a
 * bare "WHY:" prefix, or "# WHY:" - agents aren't perfectly consistent).
 *
 * Beads still stores this as ONE `description` string - there is no
 * separate WHY/WHAT field server-side, and this module does not add one.
 * It only gives the EDIT form two boxes instead of one: parseSpec splits
 * existing text best-effort when editing starts, composeSpec puts it back
 * together as one normalized string on save. Read-only display still
 * renders the whole description as markdown, unsplit - a parse that gets
 * a real document wrong is a textarea a human can immediately fix; a
 * parse that gets a READ-ONLY view wrong is just broken.
 */

const WHY_RE = /^#{0,3}\s*WHY\b\s*:?\s*/im
const WHAT_RE = /^#{0,3}\s*WHAT\b[^\n]*/im

export interface SpecFields {
  why: string
  what: string
}

export function parseSpec(description: string): SpecFields {
  const text = description ?? ''
  const whyMatch = WHY_RE.exec(text)
  if (!whyMatch) return { why: text.trim(), what: '' }

  const afterWhy = text.slice(whyMatch.index + whyMatch[0].length)
  const whatMatch = WHAT_RE.exec(afterWhy)
  if (!whatMatch) return { why: afterWhy.trim(), what: '' }

  const why = afterWhy.slice(0, whatMatch.index).trim()
  const what = afterWhy.slice(whatMatch.index + whatMatch[0].length).trim()
  return { why, what }
}

export function composeSpec({ why, what }: SpecFields): string {
  const whyText = why.trim()
  const whatText = what.trim()
  if (!whatText) return whyText
  if (!whyText) return `WHAT (acceptance criteria):\n${whatText}`
  return `WHY: ${whyText}\n\nWHAT (acceptance criteria):\n${whatText}`
}
