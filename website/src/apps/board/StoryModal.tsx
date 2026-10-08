/**
 * Jira-style story detail modal: full beads attributes (description, status,
 * comments - with a markdown write/preview composer), plus the pipeline
 * timeline grouped by Job (a board column/phase) containing its Tasks, each
 * expandable to its live/replayed log via the same LiveLogPanel the Task
 * Runner "Live" tab uses. Opens as a centered modal, not a side drawer -
 * there's a lot to show at once (description + comments + a multi-task
 * timeline), and a drawer makes that cramped.
 */
import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { X, ChevronDown, ChevronRight, Eye, Pencil, Send, HelpCircle, Check, AlertTriangle } from 'lucide-react'
import { Badge } from '../../components/ui'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import ErrorNotice from '../../components/ErrorNotice'
import { LiveLogPanel } from '../../components/LiveLogPanel'
import DagView from '../../pages/aidlc/DagView'
import PhasedView from '../../pages/aidlc/PhasedView'
import type { TaskDetail } from '../../types'
import { boardApi, type BoardStory, type RunRecord, type PhaseDef, type BeadsComment, type StoryDetail } from './boardApi'
import { findPendingOpenQuestions, formatAnswers, type ParsedQuestion } from './openQuestions'
import { needsHuman, NEEDS_HUMAN_LABEL_KEY } from './needsHuman'
import { epicAccent } from './epicColor'
import { i18nT } from '../../i18n/t'

const POLL_MS = 3000

// Phase keys that existed before phases.py merged ideate..retro into one
// "pipeline" job - a story whose history predates that merge still has
// entries tagged with these, and they all belong to today's single
// "pipeline" job. "review"/"done" were never split out of anything, so
// they're left alone.
const LEGACY_PIPELINE_PHASES = new Set([
  'grooming', 'planning', 'plan_review', 'approval', 'implementation', 'verification', 'pr',
])
function normalizeLegacyPhaseKey(phase: string): string {
  return LEGACY_PIPELINE_PHASES.has(phase) ? 'pipeline' : phase
}

function statusVariant(status: string): 'ok' | 'err' | 'warn' | 'aim' | 'muted' {
  if (status === 'passed') return 'ok'
  if (status === 'failed') return 'err'
  if (status === 'gate_failed') return 'warn'
  if (status === 'running') return 'aim'
  return 'muted'
}

/** One row per attempt of the CURRENTLY selected task, Airflow's own
 * "try number" pattern: small pills across the top, one shared log pane
 * below reflecting whichever is selected - not a disclosure widget per
 * attempt. A looping job (the implement task, or verify->fix->confirm)
 * can run the same task several times; this is how any one of those past
 * attempts stays inspectable without nesting an accordion inside the
 * job's own accordion. */
function AttemptPills({
  entries, selectedIteration, onSelect,
}: {
  entries: RunRecord[]
  selectedIteration: number | null
  onSelect: (iteration: number) => void
}) {
  if (entries.length < 2) return null
  const effective = selectedIteration ?? entries[entries.length - 1].iteration
  return (
    <div className="flex items-center gap-1 flex-wrap" data-testid="board-attempt-pills">
      {entries.map(e => (
        <button
          key={e.iteration}
          type="button"
          onClick={() => onSelect(e.iteration)}
          className={`flex items-center gap-1 text-[10px] pl-2 pr-1.5 py-0.5 rounded-full border cursor-pointer transition-colors ${
            e.iteration === effective
              ? 'border-accent text-accent bg-accent/10'
              : 'border-border text-muted hover:text-text'
          }`}
          data-testid={`board-attempt-${e.task_key}-${e.iteration}`}
        >
          {i18nT('apps.board.attempt_try', { n: e.iteration + 1 })}
          <Badge variant={statusVariant(e.status)} className="text-[9px]">{e.status}</Badge>
        </button>
      ))}
    </div>
  );
}

/** Maps a board RunRecord status onto the vocabulary DagView/PhasedView
 *  already speak (the same one Task Runner's own task_details carry), so
 *  a job's tasks render with the identical dots/colors/icons a native
 *  Task Runner run would use - not a second status language. */
function toTaskRunnerStatus(status: string | undefined): string {
  if (!status) return 'pending'
  if (status === 'running') return 'in_progress'
  if (status === 'gate_failed') return 'failed'
  return status
}

type JobViewMode = 'dag' | 'phased' | 'live'

/** A job (board column/phase) grouping its tasks two ways at once: the
 * structural view (DAG or Phased, reusing the exact same components the
 * Task Runner "Tasks" tab renders - just fed this job's own task list
 * instead of a whole project's) for "what does this job look like", and
 * the flat attempt log below for "what actually happened, including every
 * retry" - a looping job like Verification can run `confirm` several times
 * and the structural view only ever shows its LATEST attempt per task. */
function JobGroup({ phase, phaseLabel, taskKeys, entries }: {
  phase: string; phaseLabel: string; taskKeys: string[]; entries: RunRecord[]
}) {
  const [open, setOpen] = useState(false)
  const [view, setView] = useState<JobViewMode>('live')
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  // null = "latest attempt of the selected task" - reset whenever the
  // selected TASK changes (a fresh task's own latest attempt, not whatever
  // iteration number happened to be selected on the last task).
  const [selectedIteration, setSelectedIteration] = useState<number | null>(null)

  // taskKeys comes from the job's OWN definition (/api/board/phases), so a
  // not-yet-run task still shows up as a "pending" node - entries only
  // exist once a task has actually run at least once.
  const keys = taskKeys.length ? taskKeys : [...new Set(entries.map(e => e.task_key))]

  const latestByKey = useMemo(() => {
    const map = new Map<string, RunRecord>()
    for (const e of entries) map.set(e.task_key, e) // chronological: last write wins
    return map
  }, [entries]);

  const attemptsByKey = useMemo(() => {
    const map = new Map<string, number>()
    for (const e of entries) map.set(e.task_key, (map.get(e.task_key) ?? 0) + 1)
    return map
  }, [entries]);

  const effectiveKey = selectedKey
    ?? keys.find(k => latestByKey.get(k)?.status === 'running')
    ?? [...keys].reverse().find(k => latestByKey.has(k))
    ?? keys[0];

  const selectKey = (key: string | null) => { setSelectedKey(key); setSelectedIteration(null) }

  const entriesForKey = useMemo(
    () => entries.filter(e => e.task_key === effectiveKey).sort((a, b) => a.iteration - b.iteration),
    [entries, effectiveKey],
  );
  const selectedEntry = selectedIteration !== null
    ? entriesForKey.find(e => e.iteration === selectedIteration) ?? entriesForKey[entriesForKey.length - 1]
    : entriesForKey[entriesForKey.length - 1];

  const dagNodes = keys.map((key, i) => ({
    id: String(i + 1), title: key, status: toTaskRunnerStatus(latestByKey.get(key)?.status),
  }));
  const dagEdges = keys.slice(0, -1).map((_, i) => ({ from: String(i + 1), to: String(i + 2) }));

  const phasedTasks: TaskDetail[] = keys.map((key, i) => ({
    index: i + 1, title: key, description: '',
    status: toTaskRunnerStatus(latestByKey.get(key)?.status),
    error: '', result: '', attempts: attemptsByKey.get(key) ?? 0,
    depends_on: i === 0 ? [] : [i], requires_approval: false,
  }));

  const selectedIndex = effectiveKey ? keys.indexOf(effectiveKey) + 1 : undefined;

  return (
    <div className="border border-border-strong rounded-lg overflow-hidden" data-testid={`board-job-${phase}`}>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 bg-bg-elevated text-left cursor-pointer"
      >
        <span className="flex items-center gap-2 text-[13px] font-semibold text-text">
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          {phaseLabel}
        </span>
        <span className="text-[11px] text-muted">{i18nT('apps.board.task_count', { count: keys.length })}</span>
      </button>
      {open && (
        <div className="p-2 flex flex-col gap-3 bg-bg">
          <div>
            <div className="flex items-center gap-1 mb-2">
              {(['dag', 'phased', 'live'] as const).map(v => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setView(v)}
                  className={`text-[11px] px-2 py-1 rounded cursor-pointer ${view === v ? 'bg-accent text-accent-fg' : 'text-muted hover:text-text bg-bg-elevated'}`}
                  data-testid={`board-job-${phase}-view-${v}`}
                >
                  {i18nT(`apps.board.view_${v}`)}
                </button>
              ))}
            </div>
            <div data-testid={`board-job-${phase}-view-content`}>
              {view === 'dag' && (
                <DagView
                  nodes={dagNodes}
                  edges={dagEdges}
                  onNodeClick={id => selectKey(keys[Number(id) - 1] ?? null)}
                  selectedId={selectedIndex ? String(selectedIndex) : undefined}
                />
              )}
              {view === 'phased' && (
                <PhasedView
                  tasks={phasedTasks}
                  onTaskClick={index => selectKey(keys[index - 1] ?? null)}
                  selectedIndex={selectedIndex ?? null}
                />
              )}
              {view === 'live' && (
                <div className="flex flex-col gap-2">
                  {/* Airflow's own "try number" pattern: pills across the
                   * top pick WHICH attempt of the currently selected task
                   * to show, one shared log pane below - not a disclosure
                   * widget nested per attempt. */}
                  <AttemptPills
                    entries={entriesForKey}
                    selectedIteration={selectedIteration}
                    onSelect={setSelectedIteration}
                  />
                  {selectedEntry?.task_id ? (
                    <LiveLogPanel taskId={selectedEntry.task_id} active />
                  ) : (
                    <div className="text-[12px] text-muted px-1">{i18nT('apps.board.no_log_yet')}</div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** A phone-friendly alternative to writing a whole markdown comment: one
 * input per open question, answered right below it, submitted as a single
 * numbered comment matching what rocket-confirm-plan (and a human reading
 * the thread) expects. Only rendered while the most recent comment is still
 * an unanswered OPEN QUESTIONS post (see findPendingOpenQuestions) - once
 * an answer comment goes out, this panel's own query refetch makes it
 * disappear on its own, same as a normal comment would. */
/** beads comments are append-only - there's no `bd` command to edit or
 * delete one in place (confirmed against the installed CLI; even Beadbox,
 * a dedicated beads GUI, only deletes and only on a server-mode workspace
 * crew-rocket's embedded-mode one isn't). So "editing" a comment here posts
 * a new comment carrying the correction, with the original left in place
 * underneath for history - the same effect as an edit, honestly labeled. */
function CommentItem({ storyId, comment }: { storyId: string; comment: BeadsComment }) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(comment.text)

  const mutation = useMutation({
    mutationFn: (text: string) => boardApi.addComment(storyId, text),
    onSuccess: () => {
      setEditing(false)
      queryClient.invalidateQueries({ queryKey: ['board', 'detail', storyId] })
    },
  })

  const submit = () => {
    const text = draft.trim()
    if (!text) return
    mutation.mutate(i18nT('apps.board.comment_edit_marker', { text }))
  }

  return (
    <div className="border border-border rounded-lg px-3 py-2" data-testid="board-comment">
      <div className="flex items-center gap-2 text-[11px] text-muted mb-1">
        <span className="font-medium text-text">{comment.author}</span>
        <span>{comment.created_at}</span>
        {!editing && (
          <button
            type="button"
            onClick={() => { setDraft(comment.text); setEditing(true) }}
            className="ml-auto text-muted hover:text-text cursor-pointer opacity-60 hover:opacity-100"
            data-testid="board-comment-edit-start"
          >
            <Pencil size={11} />
          </button>
        )}
      </div>
      {editing ? (
        <div className="flex flex-col gap-2" data-testid="board-comment-edit">
          <textarea
            value={draft}
            onChange={e => setDraft(e.target.value)}
            rows={3}
            className="w-full px-2 py-1.5 text-[13px] rounded-md border border-border bg-bg text-text focus:outline-none focus:ring-1 focus:ring-accent resize-y"
            data-testid="board-comment-edit-input"
            autoFocus
          />
          <div className="text-[11px] text-muted">{i18nT('apps.board.comment_edit_hint')}</div>
          {mutation.isError && <span className="text-[11px] text-danger">{String(mutation.error)}</span>}
          <div className="flex items-center gap-2 self-end">
            <button type="button" onClick={() => setEditing(false)} className="text-[12px] text-muted hover:text-text cursor-pointer px-2 py-1">
              {i18nT('apps.board.edit_cancel')}
            </button>
            <button
              type="button"
              disabled={!draft.trim() || mutation.isPending}
              onClick={submit}
              className="flex items-center gap-1 text-[12px] py-1 px-3 rounded-md bg-accent text-accent-fg hover:bg-accent-hover disabled:opacity-50 cursor-pointer"
              data-testid="board-comment-edit-save"
            >
              <Check size={12} />
              {i18nT('apps.board.edit_save')}
            </button>
          </div>
        </div>
      ) : (
        <div className="text-[13px]">
          <MarkdownRenderer content={comment.text} />
        </div>
      )}
    </div>
  );
}

function OpenQuestionsPanel({ storyId, questions }: { storyId: string; questions: ParsedQuestion[] }) {
  const queryClient = useQueryClient()
  const [answers, setAnswers] = useState<Record<number, string>>({})

  const mutation = useMutation({
    mutationFn: (body: string) => boardApi.addComment(storyId, body),
    onSuccess: () => {
      setAnswers({})
      queryClient.invalidateQueries({ queryKey: ['board', 'detail', storyId] })
    },
  })

  const hasAnyAnswer = Object.values(answers).some(a => a.trim());

  return (
    <div
      className="border-2 border-accent/40 bg-accent/5 rounded-lg p-3 flex flex-col gap-3"
      data-testid="board-open-questions"
    >
      <div className="flex items-center gap-2 text-[13px] font-semibold text-text">
        <HelpCircle size={15} className="text-accent" />
        {i18nT('apps.board.open_questions_title')}
      </div>
      <div className="text-[11px] text-muted -mt-2">{i18nT('apps.board.open_questions_hint')}</div>
      {questions.map(q => (
        <div key={q.number} className="flex flex-col gap-1.5" data-testid={`board-open-question-${q.number}`}>
          <div className="text-[13px] text-text">
            <span className="font-semibold text-accent mr-1">{q.number}.</span>
            <MarkdownRenderer content={q.text} />
          </div>
          <textarea
            value={answers[q.number] ?? ''}
            onChange={e => setAnswers(prev => ({ ...prev, [q.number]: e.target.value }))}
            placeholder={i18nT('apps.board.open_questions_answer_placeholder')}
            rows={2}
            // py-3 + text-[15px], not the composer's denser text-[12px]/py-1.5 -
            // this panel is explicitly for a phone, where a thumb needs a
            // bigger, easier-to-hit field more than the screen needs density.
            className="w-full px-3 py-3 text-[15px] rounded-md border border-border bg-bg text-text placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-accent resize-y"
            data-testid={`board-open-question-${q.number}-answer`}
          />
        </div>
      ))}
      <div className="text-[11px] text-muted">{i18nT('apps.board.open_questions_skip_note')}</div>
      {mutation.isError && <span className="text-[11px] text-danger">{String(mutation.error)}</span>}
      <button
        type="button"
        disabled={!hasAnyAnswer || mutation.isPending}
        onClick={() => mutation.mutate(formatAnswers(questions, answers))}
        className="w-full flex items-center justify-center gap-1.5 text-[14px] py-3 rounded-md bg-accent text-accent-fg hover:bg-accent-hover disabled:opacity-50 cursor-pointer"
        data-testid="board-open-questions-submit"
      >
        <Send size={14} />
        {i18nT('apps.board.open_questions_submit')}
      </button>
    </div>
  );
}

function CommentComposer({ storyId }: { storyId: string }) {
  const queryClient = useQueryClient()
  const [text, setText] = useState('')
  const [tab, setTab] = useState<'write' | 'preview'>('write')

  const mutation = useMutation({
    mutationFn: (body: string) => boardApi.addComment(storyId, body),
    onSuccess: () => {
      setText('')
      setTab('write')
      queryClient.invalidateQueries({ queryKey: ['board', 'detail', storyId] })
    },
  })

  return (
    <div className="border border-border rounded-lg overflow-hidden" data-testid="board-comment-composer">
      <div className="flex items-center gap-1 px-2 py-1 border-b border-border bg-bg-elevated">
        <button
          type="button"
          onClick={() => setTab('write')}
          className={`flex items-center gap-1 text-[11px] px-2 py-1 rounded cursor-pointer ${tab === 'write' ? 'bg-accent text-accent-fg' : 'text-muted hover:text-text'}`}
        >
          <Pencil size={11} />{i18nT('apps.board.comment_write')}
        </button>
        <button
          type="button"
          onClick={() => setTab('preview')}
          className={`flex items-center gap-1 text-[11px] px-2 py-1 rounded cursor-pointer ${tab === 'preview' ? 'bg-accent text-accent-fg' : 'text-muted hover:text-text'}`}
        >
          <Eye size={11} />{i18nT('apps.board.comment_preview')}
        </button>
      </div>
      {tab === 'write' ? (
        <textarea
          value={text}
          onChange={e => setText(e.target.value)}
          placeholder={i18nT('apps.board.comment_placeholder')}
          rows={4}
          className="w-full px-3 py-2 text-[12px] font-mono bg-bg text-text placeholder:text-muted focus:outline-none resize-y"
          data-testid="board-comment-input"
        />
      ) : (
        <div className="px-3 py-2 min-h-[80px] text-[13px]">
          {text.trim() ? <MarkdownRenderer content={text} /> : (
            <span className="text-muted text-[12px]">{i18nT('apps.board.comment_nothing_to_preview')}</span>
          )}
        </div>
      )}
      <div className="flex items-center justify-between px-2 py-1.5 border-t border-border bg-bg-elevated">
        {mutation.isError && <span className="text-[11px] text-danger">{String(mutation.error)}</span>}
        <div className="flex-1" />
        <button
          type="button"
          disabled={!text.trim() || mutation.isPending}
          onClick={() => mutation.mutate(text)}
          className="flex items-center gap-1 text-[11px] px-3 py-1.5 rounded-md bg-accent text-accent-fg hover:bg-accent-hover disabled:opacity-50 cursor-pointer"
          data-testid="board-comment-submit"
        >
          <Send size={12} />
          {i18nT('apps.board.comment_submit')}
        </button>
      </div>
    </div>
  );
}

const PRIORITY_LEVELS = [0, 1, 2, 3, 4]

/** Jira-style right rail: epic (color-coded, same hash as the timeline's
 * own epic dot - see epicColor.ts), priority, story points, and schedule -
 * all editable inline, same generic update_story endpoint the timeline's
 * drag-to-reschedule already uses. Kept separate from the main column so
 * glanceable metadata doesn't compete with the description/comments/DAG
 * for width, same split Jira's own issue view makes. */
function StorySidebar({ story, detail, onUpdate, busy }: {
  story: BoardStory
  detail: StoryDetail | undefined
  onUpdate: (fields: Parameters<typeof boardApi.updateStory>[1]) => void
  busy: boolean
}) {
  const accent = epicAccent(story.epic_id)
  const [pointsDraft, setPointsDraft] = useState('')
  const [editingPoints, setEditingPoints] = useState(false)

  const commitPoints = () => {
    setEditingPoints(false)
    const n = Number(pointsDraft)
    if (pointsDraft.trim() !== '' && !Number.isNaN(n)) onUpdate({ story_points: n })
  }

  return (
    <aside className="w-[240px] shrink-0 flex flex-col gap-4 border-l border-border pl-5" data-testid="board-sidebar">
      <div>
        <div className="text-[11px] font-medium text-muted mb-1">{i18nT('apps.board.sidebar_epic')}</div>
        <div className="flex items-center gap-1.5 text-[13px] text-text">
          <span className={`w-2 h-2 rounded-full shrink-0 ${accent.bar}`} />
          <span className="truncate">{story.epic_title}</span>
        </div>
      </div>

      <div>
        <div className="text-[11px] font-medium text-muted mb-1">{i18nT('apps.board.sidebar_priority')}</div>
        <select
          value={detail?.priority ?? ''}
          onChange={e => onUpdate({ priority: Number(e.target.value) })}
          disabled={busy}
          className="w-full px-2 py-1 text-[12px] rounded-md border border-border bg-bg text-text focus:outline-none focus:ring-1 focus:ring-accent disabled:opacity-50"
          data-testid="board-sidebar-priority"
        >
          <option value="" disabled>{i18nT('apps.board.sidebar_unset')}</option>
          {PRIORITY_LEVELS.map(p => (
            <option key={p} value={p}>{i18nT('apps.board.priority_short', { priority: p })}</option>
          ))}
        </select>
      </div>

      <div>
        <div className="text-[11px] font-medium text-muted mb-1">{i18nT('apps.board.sidebar_story_points')}</div>
        {editingPoints ? (
          <input
            type="number"
            value={pointsDraft}
            onChange={e => setPointsDraft(e.target.value)}
            onBlur={commitPoints}
            onKeyDown={e => { if (e.key === 'Enter') commitPoints() }}
            className="w-full px-2 py-1 text-[12px] rounded-md border border-border bg-bg text-text focus:outline-none focus:ring-1 focus:ring-accent"
            data-testid="board-sidebar-story-points-input"
            autoFocus
          />
        ) : (
          <button
            type="button"
            onClick={() => { setPointsDraft(detail?.story_points != null ? String(detail.story_points) : ''); setEditingPoints(true) }}
            className="w-full text-left px-2 py-1 text-[12px] rounded-md border border-transparent hover:border-border text-text cursor-pointer"
            data-testid="board-sidebar-story-points"
          >
            {detail?.story_points ?? <span className="text-muted">{i18nT('apps.board.sidebar_unset')}</span>}
          </button>
        )}
      </div>

      <div>
        <div className="text-[11px] font-medium text-muted mb-1">{i18nT('apps.board.sidebar_start_date')}</div>
        <input
          type="date"
          value={detail?.start_date ?? ''}
          onChange={e => onUpdate({ start_date: e.target.value })}
          disabled={busy}
          className="w-full px-2 py-1 text-[12px] rounded-md border border-border bg-bg text-text focus:outline-none focus:ring-1 focus:ring-accent disabled:opacity-50"
          data-testid="board-sidebar-start-date"
        />
      </div>

      <div>
        <div className="text-[11px] font-medium text-muted mb-1">{i18nT('apps.board.sidebar_due_date')}</div>
        <input
          type="date"
          value={detail?.due_date ? detail.due_date.slice(0, 10) : ''}
          onChange={e => onUpdate({ due_date: e.target.value })}
          disabled={busy}
          className="w-full px-2 py-1 text-[12px] rounded-md border border-border bg-bg text-text focus:outline-none focus:ring-1 focus:ring-accent disabled:opacity-50"
          data-testid="board-sidebar-due-date"
        />
      </div>

      {detail?.owner && (
        <div>
          <div className="text-[11px] font-medium text-muted mb-1">{i18nT('apps.board.sidebar_owner')}</div>
          <div className="text-[12px] text-text">{detail.owner}</div>
        </div>
      )}
    </aside>
  );
}

export default function StoryModal({ story, phases, onClose }: {
  story: BoardStory
  phases: PhaseDef[]
  onClose: () => void
}) {
  const detailQuery = useQuery({
    queryKey: ['board', 'detail', story.id],
    queryFn: () => boardApi.detail(story.id),
    // Comments live here, and a job like Planning posts its own comment
    // (OPEN QUESTIONS) as a side effect of running - polling, same as
    // historyQuery below, means a comment posted while this modal is
    // already open actually shows up instead of needing a reopen.
    refetchInterval: POLL_MS,
  })
  const historyQuery = useQuery({
    queryKey: ['board', 'history', story.id],
    queryFn: () => boardApi.history(story.id),
    refetchInterval: POLL_MS,
  })

  const jobs = useMemo(() => {
    const entries = historyQuery.data?.history ?? []
    const current = historyQuery.data?.current_run ?? null
    const all = current ? [...entries, current] : entries
    const byPhase = new Map<string, RunRecord[]>()
    for (const entry of all) {
      // History recorded before phases.py merged ideate..retro into one
      // "pipeline" job still carries the old per-column phase strings
      // (grooming, planning, plan_review, approval, implementation,
      // verification, pr) - normalize those so an old story's timeline
      // reads as ONE consolidated job too, not one single-task accordion
      // per legacy column. "review"/"done" are unaffected - still their
      // own thing, same as always.
      const key = normalizeLegacyPhaseKey(entry.phase)
      if (!byPhase.has(key)) byPhase.set(key, [])
      byPhase.get(key)!.push(entry)
    }
    // Order jobs by the board's own phase order, not first-seen order, so the
    // timeline reads top-to-bottom the same way the board's columns do.
    const order = phases.map(p => p.key)
    return [...byPhase.entries()].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]))
  }, [historyQuery.data, phases])

  const detail = detailQuery.data
  const pendingQuestions = useMemo(
    () => findPendingOpenQuestions(detail?.comments ?? []),
    [detail],
  )
  const currentPhase = phases.find(p => p.key === story.phase)
  const humanReason = currentPhase
    ? needsHuman({ current_run: historyQuery.data?.current_run ?? null }, currentPhase, pendingQuestions !== null)
    : null

  const queryClient = useQueryClient()
  const [editingTitle, setEditingTitle] = useState(false)
  const [titleDraft, setTitleDraft] = useState('')
  const [editingDescription, setEditingDescription] = useState(false)
  const [descriptionDraft, setDescriptionDraft] = useState('')

  const updateMutation = useMutation({
    mutationFn: (fields: Parameters<typeof boardApi.updateStory>[1]) => boardApi.updateStory(story.id, fields),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['board', 'detail', story.id] })
      queryClient.invalidateQueries({ queryKey: ['board', 'stories'] })
    },
  })

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" data-testid="board-timeline-drawer">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative w-full max-w-[1280px] max-h-[94vh] bg-bg border border-border-strong rounded-xl flex flex-col shadow-lg">
        <div className="flex items-center justify-between px-5 py-4 border-b border-border gap-3">
          <div className="min-w-0 flex-1">
            <div className="text-[11px] text-muted">{story.epic_title}</div>
            {editingTitle ? (
              <div className="flex items-center gap-2 mt-0.5" data-testid="board-title-edit">
                <input
                  value={titleDraft}
                  onChange={e => setTitleDraft(e.target.value)}
                  className="flex-1 min-w-0 px-2 py-1 text-[16px] font-semibold rounded-md border border-border bg-bg text-text focus:outline-none focus:ring-1 focus:ring-accent"
                  data-testid="board-title-input"
                  autoFocus
                />
                <button
                  type="button"
                  disabled={!titleDraft.trim() || updateMutation.isPending}
                  onClick={() => updateMutation.mutate({ title: titleDraft.trim() }, { onSuccess: () => setEditingTitle(false) })}
                  className="text-accent hover:text-accent-hover cursor-pointer disabled:opacity-50"
                  data-testid="board-title-save"
                >
                  <Check size={16} />
                </button>
                <button type="button" onClick={() => setEditingTitle(false)} className="text-muted hover:text-text cursor-pointer" data-testid="board-title-cancel">
                  <X size={16} />
                </button>
              </div>
            ) : (
              <div className="flex items-center gap-1.5 group">
                <div className="text-[16px] font-semibold text-text">{detail?.title ?? story.title}</div>
                <button
                  type="button"
                  onClick={() => { setTitleDraft(detail?.title ?? story.title); setEditingTitle(true) }}
                  className="text-muted hover:text-text cursor-pointer opacity-60 hover:opacity-100"
                  data-testid="board-title-edit-start"
                >
                  <Pencil size={12} />
                </button>
              </div>
            )}
            <div className="flex items-center gap-2 mt-1.5">
              <Badge variant="muted" className="text-[10px]">{story.phase_label}</Badge>
              {detail?.status && <Badge variant="muted" className="text-[10px]">{detail.status}</Badge>}
              {detail?.owner && <span className="text-[11px] text-muted">{detail.owner}</span>}
            </div>
          </div>
          <button type="button" onClick={onClose} className="cursor-pointer text-muted hover:text-text" data-testid="board-timeline-close">
            <X size={18} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 flex gap-5">
        <div className="flex-1 min-w-0 flex flex-col gap-5">
          {detailQuery.error && <ErrorNotice message={String(detailQuery.error)} />}

          {humanReason && (
            <div
              className="flex items-center gap-2 text-[13px] font-semibold text-warn bg-warn/10 border-2 border-warn rounded-lg px-3 py-2"
              data-testid="board-modal-needs-human"
            >
              <AlertTriangle size={15} />
              {i18nT(NEEDS_HUMAN_LABEL_KEY[humanReason])}
            </div>
          )}

          <section>
            <div className="flex items-center gap-1.5 mb-2">
              <div className="text-[12px] font-medium text-muted">{i18nT('apps.board.description_title')}</div>
              {!editingDescription && (
                <button
                  type="button"
                  onClick={() => { setDescriptionDraft(detail?.description ?? ''); setEditingDescription(true) }}
                  className="text-muted hover:text-text cursor-pointer opacity-60 hover:opacity-100"
                  data-testid="board-description-edit-start"
                >
                  <Pencil size={11} />
                </button>
              )}
            </div>
            {editingDescription ? (
              <div className="flex flex-col gap-2" data-testid="board-description-edit">
                <textarea
                  value={descriptionDraft}
                  onChange={e => setDescriptionDraft(e.target.value)}
                  rows={8}
                  className="w-full px-3 py-2 text-[13px] font-mono rounded-md border border-border bg-bg text-text focus:outline-none focus:ring-1 focus:ring-accent resize-y"
                  data-testid="board-description-input"
                  autoFocus
                />
                <div className="flex items-center gap-2 self-end">
                  <button type="button" onClick={() => setEditingDescription(false)} className="text-[12px] text-muted hover:text-text cursor-pointer px-2 py-1">
                    {i18nT('apps.board.edit_cancel')}
                  </button>
                  <button
                    type="button"
                    disabled={updateMutation.isPending}
                    onClick={() => updateMutation.mutate({ description: descriptionDraft }, { onSuccess: () => setEditingDescription(false) })}
                    className="flex items-center gap-1 text-[12px] py-1 px-3 rounded-md bg-accent text-accent-fg hover:bg-accent-hover disabled:opacity-50 cursor-pointer"
                    data-testid="board-description-save"
                  >
                    <Check size={12} />
                    {i18nT('apps.board.edit_save')}
                  </button>
                </div>
              </div>
            ) : (
              <div className="text-[13px]" data-testid="board-description">
                {detail?.description ? <MarkdownRenderer content={detail.description} /> : (
                  <span className="text-muted">{i18nT('apps.board.no_description')}</span>
                )}
              </div>
            )}
          </section>

          {pendingQuestions && (
            <section>
              <OpenQuestionsPanel storyId={story.id} questions={pendingQuestions} />
            </section>
          )}

          <section>
            <div className="text-[12px] font-medium text-muted mb-2">
              {i18nT('apps.board.comments_title', { count: detail?.comments.length ?? 0 })}
            </div>
            <div className="flex flex-col gap-2 mb-3">
              {(detail?.comments ?? []).map(c => (
                <CommentItem key={c.id} storyId={story.id} comment={c} />
              ))}
            </div>
            <CommentComposer storyId={story.id} />
          </section>

          <section>
            <div className="text-[12px] font-medium text-muted mb-2">{i18nT('apps.board.timeline_title')}</div>
            <div className="flex flex-col gap-2">
              {jobs.length === 0 ? (
                <div className="text-[12px] text-muted">{i18nT('apps.board.no_history_yet')}</div>
              ) : (
                jobs.map(([phase, entries]) => (
                  <JobGroup
                    key={phase}
                    phase={phase}
                    phaseLabel={phases.find(p => p.key === phase)?.label ?? phase}
                    taskKeys={phases.find(p => p.key === phase)?.tasks ?? []}
                    entries={entries}
                  />
                ))
              )}
            </div>
          </section>
        </div>
          <StorySidebar
            story={story}
            detail={detail}
            busy={updateMutation.isPending}
            onUpdate={fields => updateMutation.mutate(fields)}
          />
        </div>
      </div>
    </div>
  );
}
