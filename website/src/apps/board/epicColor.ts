/**
 * One deterministic accent per epic (hashed from its id, stable across
 * reloads and independent of fetch order) - shared between TimelinePage's
 * epic header dot and StoryModal's sidebar epic chip, so the same epic
 * reads as the same color wherever it shows up, same idea as Jira's
 * per-epic color chip.
 */
const EPIC_ACCENTS = [
  { bar: 'bg-accent', text: 'text-accent' },
  { bar: 'bg-aim', text: 'text-aim' },
  { bar: 'bg-ok', text: 'text-ok' },
  { bar: 'bg-warn', text: 'text-warn' },
  { bar: 'bg-danger', text: 'text-danger' },
]

export function epicAccent(epicId: string): { bar: string; text: string } {
  let h = 0
  for (let i = 0; i < epicId.length; i++) h = (h * 31 + epicId.charCodeAt(i)) >>> 0
  return EPIC_ACCENTS[h % EPIC_ACCENTS.length]
}
