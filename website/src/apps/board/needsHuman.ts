/**
 * Every point in the pipeline where a story cannot move on its own and is
 * genuinely waiting on a person - shared between TimelinePage's row list
 * (where `pendingOpenQuestions` is the cheap `story.pending_open_questions`
 * flag the backend only computes for pipeline-phase stories, since that's
 * the only phase it can still be true for - see stories.py) and
 * StoryModal's detail view (where it's the modal's own, fully-parsed
 * `findPendingOpenQuestions(detail.comments)` result, which is strictly more
 * authoritative since it has read every comment, not just checked a flag).
 */
import type { BoardStory, PhaseDef } from './boardApi'

export type NeedsHumanReason = 'open_questions' | 'gate_failed' | 'manual_review'

export const NEEDS_HUMAN_LABEL_KEY: Record<NeedsHumanReason, string> = {
  open_questions: 'apps.board.needs_human_open_questions',
  gate_failed: 'apps.board.needs_human_gate_failed',
  manual_review: 'apps.board.needs_human_manual_review',
}

// Priority order: a story stuck on unanswered questions is called out
// first, even if it also sits in a column that's inherently manual.
// Terminal "done" is excluded - manual, but nothing left to do there.
export function needsHuman(
  story: Pick<BoardStory, 'current_run'>,
  phase: Pick<PhaseDef, 'key' | 'manual'>,
  pendingOpenQuestions: boolean,
): NeedsHumanReason | null {
  if (pendingOpenQuestions) return 'open_questions'
  // "missing" (the run vanished from Task Runner's own status, e.g. a
  // gateway restart) needs the same "click Run again" action as a real
  // gate failure, so it shares that banner rather than getting its own.
  if (story.current_run?.status === 'gate_failed' || story.current_run?.status === 'missing') return 'gate_failed'
  if (phase.manual && phase.key !== 'done') return 'manual_review'
  return null
}
