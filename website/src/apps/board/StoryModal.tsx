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
import { X, ChevronDown, ChevronRight, Eye, Pencil, Send } from 'lucide-react'
import { Badge } from '../../components/ui'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import ErrorNotice from '../../components/ErrorNotice'
import { LiveLogPanel } from '../../components/LiveLogPanel'
import { boardApi, type BoardStory, type RunRecord, type PhaseDef } from './boardApi'
import { i18nT } from '../../i18n/t'

const POLL_MS = 3000

function statusVariant(status: string): 'ok' | 'err' | 'warn' | 'aim' | 'muted' {
  if (status === 'passed') return 'ok'
  if (status === 'failed') return 'err'
  if (status === 'gate_failed') return 'warn'
  if (status === 'running') return 'aim'
  return 'muted'
}

function TaskRow({ entry }: { entry: RunRecord }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="border border-border rounded-lg overflow-hidden" data-testid="board-timeline-row">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between gap-2 px-3 py-1.5 bg-bg-elevated text-left cursor-pointer"
      >
        <span className="flex items-center gap-2 text-[12px] text-text">
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          <span className="font-medium">{entry.task_key}</span>
          {entry.iteration > 0 && <span className="text-muted">#{entry.iteration + 1}</span>}
        </span>
        <Badge variant={statusVariant(entry.status)} className="text-[10px]">{entry.status}</Badge>
      </button>
      {open && entry.task_id && (
        <div className="p-2 border-t border-border">
          {/* Always active: the backend replays a finished run's full log
           * from disk regardless of its terminal status - gating this on
           * entry.status === 'running' (the original bug here) meant every
           * already-finished task silently showed nothing. */}
          <LiveLogPanel taskId={entry.task_id} active />
        </div>
      )}
    </div>
  );
}

function JobGroup({ phase, phaseLabel, entries }: { phase: string; phaseLabel: string; entries: RunRecord[] }) {
  const [open, setOpen] = useState(true)
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
        <span className="text-[11px] text-muted">{i18nT('apps.board.task_count', { count: entries.length })}</span>
      </button>
      {open && (
        <div className="p-2 flex flex-col gap-2 bg-bg">
          {entries.map((entry, i) => (
            <TaskRow key={`${entry.task_key}-${entry.iteration}-${entry.task_id ?? i}`} entry={entry} />
          ))}
        </div>
      )}
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

export default function StoryModal({ story, phases, onClose }: {
  story: BoardStory
  phases: PhaseDef[]
  onClose: () => void
}) {
  const detailQuery = useQuery({
    queryKey: ['board', 'detail', story.id],
    queryFn: () => boardApi.detail(story.id),
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
      if (!byPhase.has(entry.phase)) byPhase.set(entry.phase, [])
      byPhase.get(entry.phase)!.push(entry)
    }
    // Order jobs by the board's own phase order, not first-seen order, so the
    // timeline reads top-to-bottom the same way the board's columns do.
    const order = phases.map(p => p.key)
    return [...byPhase.entries()].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]))
  }, [historyQuery.data, phases])

  const detail = detailQuery.data

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" data-testid="board-timeline-drawer">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative w-full max-w-[880px] max-h-[88vh] bg-bg border border-border-strong rounded-xl flex flex-col shadow-lg">
        <div className="flex items-center justify-between px-5 py-4 border-b border-border">
          <div>
            <div className="text-[11px] text-muted">{story.epic_title}</div>
            <div className="text-[16px] font-semibold text-text">{story.title}</div>
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

        <div className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-5">
          {detailQuery.error && <ErrorNotice message={String(detailQuery.error)} />}

          <section>
            <div className="text-[12px] font-medium text-muted mb-2">{i18nT('apps.board.description_title')}</div>
            <div className="text-[13px]" data-testid="board-description">
              {detail?.description ? <MarkdownRenderer content={detail.description} /> : (
                <span className="text-muted">{i18nT('apps.board.no_description')}</span>
              )}
            </div>
          </section>

          <section>
            <div className="text-[12px] font-medium text-muted mb-2">
              {i18nT('apps.board.comments_title', { count: detail?.comments.length ?? 0 })}
            </div>
            <div className="flex flex-col gap-2 mb-3">
              {(detail?.comments ?? []).map(c => (
                <div key={c.id} className="border border-border rounded-lg px-3 py-2" data-testid="board-comment">
                  <div className="flex items-center gap-2 text-[11px] text-muted mb-1">
                    <span className="font-medium text-text">{c.author}</span>
                    <span>{c.created_at}</span>
                  </div>
                  <div className="text-[13px]">
                    <MarkdownRenderer content={c.text} />
                  </div>
                </div>
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
                    entries={entries}
                  />
                ))
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
