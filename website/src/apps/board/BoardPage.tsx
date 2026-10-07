/**
 * Story Board — a beads story moves across 9 columns (phases), one
 * crew-rocket skill (job) per column, driven by kiro_crew.board (backend:
 * dashboard/handlers/board.py). See the plan at
 * src/kiro_crew/board/__init__.py for the full design.
 */
import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, PlayCircle, CheckCircle2, AlertTriangle } from 'lucide-react'
import { Badge } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { boardApi, type BoardStory, type PhaseDef } from './boardApi'
import { needsHuman, NEEDS_HUMAN_LABEL_KEY } from './needsHuman'
import StoryModal from './StoryModal'
import { i18nT } from '../../i18n/t'

const POLL_MS = 3000

// manual: false - a backlog card's "Run next job" button is what calls
// POST /stories/{id}/run, and the backend (board.engine.run_job) already
// promotes a label-less story straight into "grooming" and runs its first
// job on that same call. There is no separate "start" action to model here.
const BACKLOG_PHASE: PhaseDef = { key: '', label: '', tasks: [], gate: false, manual: false }

// null = nothing noteworthy beyond the lane the card already sits in - the
// column itself is that signal, so there is no separate "idle" pill.
function statusBadge(story: BoardStory): { variant: 'ok' | 'err' | 'aim'; label: string } | null {
  const run = story.current_run
  if (run?.status === 'running') return { variant: 'aim', label: i18nT('apps.board.status_running') }
  if (run?.status === 'failed') return { variant: 'err', label: i18nT('apps.board.status_failed') }
  return null
}

function StoryCard({ story, phase, onOpen, onRun, onAdvance, busy }: {
  story: BoardStory
  phase: PhaseDef
  onOpen: () => void
  onRun: () => void
  onAdvance: () => void
  busy: boolean
}) {
  const badge = statusBadge(story)
  const humanReason = needsHuman(story, phase, story.pending_open_questions)
  const running = story.current_run?.status === 'running'
  const canRun = !phase.manual && !running
  const canAdvance = phase.key === 'review' && !running

  return (
    <div
      className={`bg-bg-elevated rounded-lg p-3 cursor-pointer transition-colors ${
        humanReason
          ? 'border-2 border-warn bg-warn/5 hover:border-warn'
          : 'border border-border hover:border-border-strong'
      }`}
      onClick={onOpen}
      data-testid={`board-card-${story.id}`}
    >
      {humanReason && (
        <div
          className="flex items-center gap-1 text-[11px] font-semibold text-warn mb-2"
          data-testid={`board-needs-human-${story.id}`}
        >
          <AlertTriangle size={12} />
          {i18nT(NEEDS_HUMAN_LABEL_KEY[humanReason])}
        </div>
      )}
      <div className="text-[11px] text-muted mb-1 flex items-center gap-1.5">
        <span>{story.epic_title}</span>
        <span className="font-mono text-[10px] text-muted/70" data-testid={`board-parent-${story.id}`}>{story.epic_id}</span>
      </div>
      <div className="text-[13px] text-text font-medium mb-2">{story.title}</div>
      <div className="flex items-center gap-1.5 mb-2 flex-wrap">
        {badge && (
          <Badge variant={badge.variant} className="text-[10px]">
            {running ? <Loader2 size={10} className="inline animate-spin mr-1" /> : null}
            {badge.label}
          </Badge>
        )}
        {story.priority !== null && story.priority !== undefined && (
          <Badge variant="muted" className="text-[10px]" data-testid={`board-priority-${story.id}`}>
            {i18nT('apps.board.priority_short', { priority: story.priority })}
          </Badge>
        )}
        {story.owner && (
          <span className="text-[10px] text-muted" data-testid={`board-owner-${story.id}`}>{story.owner}</span>
        )}
      </div>
      <div className="flex items-center justify-end gap-2">
        {canRun && (
          <button
            type="button"
            onClick={e => { e.stopPropagation(); onRun(); }}
            disabled={busy}
            className="flex items-center gap-1 text-[11px] px-2 py-1 rounded-md bg-accent text-accent-fg hover:bg-accent-hover disabled:opacity-50 cursor-pointer"
            data-testid={`board-run-${story.id}`}
          >
            <PlayCircle size={12} />
            {i18nT('apps.board.run_next_job')}
          </button>
        )}
        {canAdvance && (
          <button
            type="button"
            onClick={e => { e.stopPropagation(); onAdvance(); }}
            disabled={busy}
            className="flex items-center gap-1 text-[11px] px-2 py-1 rounded-md bg-ok text-accent-fg hover:opacity-90 disabled:opacity-50 cursor-pointer"
            data-testid={`board-advance-${story.id}`}
          >
            <CheckCircle2 size={12} />
            {i18nT('apps.board.mark_reviewed_done')}
          </button>
        )}
      </div>
    </div>
  );
}

export default function BoardPage() {
  const queryClient = useQueryClient()
  const [openStory, setOpenStory] = useState<BoardStory | null>(null)

  const phasesQuery = useQuery({ queryKey: ['board', 'phases'], queryFn: boardApi.phases })
  const storiesQuery = useQuery({
    queryKey: ['board', 'stories'],
    queryFn: boardApi.stories,
    refetchInterval: POLL_MS,
  })

  const runMutation = useMutation({
    mutationFn: (storyId: string) => boardApi.run(storyId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['board', 'stories'] }),
  })
  const advanceMutation = useMutation({
    mutationFn: (storyId: string) => boardApi.advance(storyId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['board', 'stories'] }),
  })

  const columns = useMemo(() => {
    const phases = phasesQuery.data?.phases ?? []
    const stories = storiesQuery.data?.stories ?? []
    const byPhase = new Map<string, BoardStory[]>()
    for (const story of stories) {
      const key = story.phase ?? ''
      if (!byPhase.has(key)) byPhase.set(key, [])
      byPhase.get(key)!.push(story)
    }
    const all: { phase: PhaseDef; stories: BoardStory[] }[] = [
      { phase: { ...BACKLOG_PHASE, label: i18nT('apps.board.backlog_column') }, stories: byPhase.get('') ?? [] },
    ]
    for (const phase of phases) {
      all.push({ phase, stories: byPhase.get(phase.key) ?? [] })
    }
    return all
  }, [phasesQuery.data, storiesQuery.data])

  const busy = runMutation.isPending || advanceMutation.isPending

  return (
    <div className="h-full flex flex-col p-4 overflow-hidden">
      <div className="mb-3">
        <h1 className="text-lg font-semibold text-text">{i18nT('apps.board.title')}</h1>
        {storiesQuery.data && storiesQuery.data.project_paths.length === 0 && (
          <div className="text-[13px] text-muted mt-1">{i18nT('apps.board.no_projects_configured')}</div>
        )}
      </div>
      {(phasesQuery.error || storiesQuery.error) && (
        <ErrorNotice message={String(phasesQuery.error || storiesQuery.error)} />
      )}
      <div className="flex-1 overflow-x-auto overflow-y-hidden">
        <div className="flex gap-3 h-full min-w-max pb-2">
          {columns.map(({ phase, stories }) => (
            <div
              key={phase.key || 'backlog'}
              className="w-[260px] shrink-0 flex flex-col bg-bg border border-border rounded-lg"
              data-testid={`board-column-${phase.key || 'backlog'}`}
            >
              <div className="px-3 py-2 border-b border-border text-[12px] font-medium text-text flex items-center justify-between">
                <span>{phase.label}</span>
                <span className="text-muted">{stories.length}</span>
              </div>
              <div className="flex-1 overflow-y-auto p-2 flex flex-col gap-2">
                {stories.length === 0 ? (
                  <div className="text-[11px] text-muted px-1">{i18nT('apps.board.no_stories')}</div>
                ) : (
                  stories.map(story => (
                    <StoryCard
                      key={story.id}
                      story={story}
                      phase={phase}
                      busy={busy}
                      onOpen={() => setOpenStory(story)}
                      onRun={() => runMutation.mutate(story.id)}
                      onAdvance={() => advanceMutation.mutate(story.id)}
                    />
                  ))
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
      {openStory && (
        <StoryModal story={openStory} phases={phasesQuery.data?.phases ?? []} onClose={() => setOpenStory(null)} />
      )}
    </div>
  );
}
