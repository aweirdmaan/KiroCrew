/**
 * Story Timeline — replaces the old kanban board. Every story now runs as
 * ONE pipeline (ideate through retro, see kiro_crew.board.phases's module
 * docstring), so there's nothing left to usefully show as columns; what's
 * left to show is WHEN each story is scheduled, in what order relative to
 * its siblings, and whether it's quietly running or needs a human right
 * now - a Jira-style timeline, not a board.
 *
 * - Horizontal axis: each story's start_date/due_date (see boardApi.ts and
 *   kiro_crew.board.stories's module comment for where these actually live
 *   in beads - due_at natively, start_date/rank on its metadata map).
 *   Dragging a bar's body reschedules it; dragging an edge resizes it.
 * - Vertical order: stories within an epic are reorderable (persisted via
 *   `rank`, a float - see stories.py) using @dnd-kit/sortable, the same
 *   library already used for ChatSidebar/ArtifactsPage's drag-and-drop.
 * - Dependency arrows: drawn between two stories under the same epic that
 *   have a non-parent-child beads dependency edge (story.depends_on).
 * - Clicking a row (not dragging) opens the same StoryModal the old board
 *   used - full pipeline DAG, comments, timeline, all unchanged there.
 */
import { useMemo, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  DndContext, closestCenter, PointerSensor, useSensor, useSensors, type DragEndEvent,
} from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy, useSortable, arrayMove } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  GripVertical, PlayCircle, CheckCircle2, AlertTriangle, Loader2, Check, X, CalendarPlus,
  GanttChart, ChevronDown, ChevronRight,
} from 'lucide-react'
import { Badge } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { boardApi, type BoardStory, type PhaseDef } from './boardApi'
import { needsHuman, NEEDS_HUMAN_LABEL_KEY } from './needsHuman'
import { epicAccent } from './epicColor'
import StoryModal from './StoryModal'
import { i18nT } from '../../i18n/t'

const POLL_MS = 3000
const DAY_MS = 86_400_000
const ROW_H = 44
const LABEL_W = 272

type Zoom = 'week' | 'quarter'
const PX_PER_DAY: Record<Zoom, number> = { week: 32, quarter: 6 }
// How many days the scrollable canvas spans, centered so "today" lands
// roughly a third of the way in (room to schedule ahead, some history
// still visible behind it without needing to scroll left immediately).
const SPAN_DAYS: Record<Zoom, number> = { week: 140, quarter: 540 }
const LEAD_DAYS: Record<Zoom, number> = { week: 21, quarter: 90 }


function parseDate(s: string | null): Date | null {
  if (!s) return null
  const d = new Date(s.length <= 10 ? `${s}T00:00:00Z` : s)
  return Number.isNaN(d.getTime()) ? null : d
}
function toDateInputValue(d: Date): string {
  return d.toISOString().slice(0, 10)
}
function daysBetween(a: Date, b: Date): number {
  return Math.round((b.getTime() - a.getTime()) / DAY_MS)
}
function addDays(d: Date, n: number): Date {
  return new Date(d.getTime() + n * DAY_MS)
}
function startOfDay(d: Date): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()))
}
function isWeekend(d: Date): boolean {
  const day = d.getUTCDay()
  return day === 0 || day === 6
}

/** The bar's own fill color: a distinct, Jira-like status read at a glance,
 * independent of the per-epic accent used on the label/header chip. */
function barColorClass(story: BoardStory, humanReason: unknown): string {
  if (humanReason) return 'bg-warn'
  if (story.phase === 'done') return 'bg-ok'
  const status = story.current_run?.status
  if (status === 'failed') return 'bg-danger'
  if (status === 'cancelled') return 'bg-muted'
  if (status === 'running') return 'bg-aim'
  return 'bg-accent/70'
}

interface TimeScale {
  anchor: Date
  pxPerDay: number
  totalPx: number
  x: (d: Date) => number
  dateAt: (px: number) => Date
}

function useTimeScale(zoom: Zoom): TimeScale {
  return useMemo(() => {
    const today = startOfDay(new Date())
    const anchor = addDays(today, -LEAD_DAYS[zoom])
    const pxPerDay = PX_PER_DAY[zoom]
    const totalPx = SPAN_DAYS[zoom] * pxPerDay
    return {
      anchor, pxPerDay, totalPx,
      x: d => daysBetween(anchor, d) * pxPerDay,
      dateAt: px => addDays(anchor, Math.round(px / pxPerDay)),
    }
  }, [zoom])
}

/** Shaded weekend columns, the same full height as the body - a cheap,
 * constant visual rhythm Jira's own timeline uses so a bar's length reads
 * against the calendar at a glance, not just against a ruler at the top. */
function WeekendBands({ scale, height }: { scale: TimeScale; height: number | string }) {
  const bands = useMemo(() => {
    if (scale.pxPerDay < 10) return [] // too thin to read as a column once zoomed out
    const out: { x: number }[] = []
    const days = Math.ceil(scale.totalPx / scale.pxPerDay)
    for (let i = 0; i <= days; i++) {
      const d = addDays(scale.anchor, i)
      if (isWeekend(d)) out.push({ x: i * scale.pxPerDay })
    }
    return out
  }, [scale])
  if (bands.length === 0) return null
  return (
    <div className="absolute inset-0 pointer-events-none" style={{ width: scale.totalPx }}>
      {bands.map(b => (
        <div key={b.x} className="absolute top-0 bg-[var(--bg-hover)]/40" style={{ left: b.x, width: scale.pxPerDay, height }} />
      ))}
    </div>
  )
}

function TimelineHeader({ scale, zoom }: { scale: TimeScale; zoom: Zoom }) {
  const ticks = useMemo(() => {
    const out: { x: number; label: string; major: boolean }[] = []
    const days = SPAN_DAYS[zoom]
    if (zoom === 'week') {
      for (let i = 0; i <= days; i += 7) {
        const d = addDays(scale.anchor, i)
        out.push({ x: i * scale.pxPerDay, label: d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }), major: d.getUTCDate() <= 7 })
      }
    } else {
      let d = new Date(Date.UTC(scale.anchor.getUTCFullYear(), scale.anchor.getUTCMonth(), 1))
      const end = addDays(scale.anchor, days)
      while (d < end) {
        out.push({
          x: daysBetween(scale.anchor, d) * scale.pxPerDay,
          label: d.toLocaleDateString(undefined, { month: 'short', year: 'numeric' }),
          major: d.getUTCMonth() % 3 === 0,
        })
        d = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1))
      }
    }
    return out
  }, [scale, zoom])

  return (
    <div className="relative h-8 border-b border-border-strong bg-bg" style={{ width: scale.totalPx }} data-testid="timeline-header">
      <WeekendBands scale={scale} height="100%" />
      {ticks.map(t => (
        <div
          key={t.x}
          className={`absolute top-0 h-full border-l ${t.major ? 'border-border-strong' : 'border-border'} pl-1.5 pt-1.5 text-[10px] whitespace-nowrap ${t.major ? 'text-text font-semibold' : 'text-muted'}`}
          style={{ left: t.x }}
        >
          {t.label}
        </div>
      ))}
    </div>
  )
}

function TodayLine({ scale, height }: { scale: TimeScale; height: number | string }) {
  const x = scale.x(startOfDay(new Date()))
  if (x < 0 || x > scale.totalPx) return null
  return (
    <div className="absolute top-0 w-px bg-danger z-10 pointer-events-none" style={{ left: x, height }} data-testid="timeline-today-line">
      <div className="absolute -top-1 -left-[3px] w-[7px] h-[7px] rounded-full bg-danger" />
    </div>
  )
}

type DragMode = 'move' | 'resize-start' | 'resize-end'

function StoryBar({
  story, scale, onReschedule, onOpen,
}: {
  story: BoardStory
  scale: TimeScale
  onReschedule: (startDate: string, dueDate: string) => void
  onOpen: () => void
}) {
  const start = parseDate(story.start_date)
  const due = parseDate(story.due_date)
  const [dragPxDelta, setDragPxDelta] = useState<{ mode: DragMode; delta: number } | null>(null)
  const dragState = useRef<{ mode: DragMode; startX: number; startDate: Date; dueDate: Date } | null>(null)

  if (!start || !due) {
    return (
      <button
        type="button"
        onClick={() => {
          const today = startOfDay(new Date())
          onReschedule(toDateInputValue(today), toDateInputValue(addDays(today, 3)))
        }}
        className="absolute top-2 flex items-center gap-1 text-[11px] text-muted hover:text-accent hover:border-accent border border-dashed border-border rounded-full px-2.5 py-1 cursor-pointer transition-colors"
        style={{ left: scale.x(startOfDay(new Date())) }}
        data-testid={`timeline-schedule-${story.id}`}
      >
        <CalendarPlus size={11} />
        {i18nT('apps.board.schedule_story')}
      </button>
    )
  }

  const baseX = scale.x(start)
  const baseW = Math.max(scale.pxPerDay, scale.x(due) - baseX + scale.pxPerDay)
  let left = baseX
  let width = baseW
  if (dragPxDelta) {
    if (dragPxDelta.mode === 'move') left = baseX + dragPxDelta.delta
    if (dragPxDelta.mode === 'resize-start') { left = baseX + dragPxDelta.delta; width = baseW - dragPxDelta.delta }
    if (dragPxDelta.mode === 'resize-end') width = baseW + dragPxDelta.delta
  }
  width = Math.max(scale.pxPerDay, width)

  const beginDrag = (mode: DragMode) => (e: React.PointerEvent) => {
    e.stopPropagation()
    e.preventDefault()
    const startX = e.clientX
    dragState.current = { mode, startX, startDate: start, dueDate: due }
    // Pointer capture: without it, a fast real-world drag that briefly
    // crosses another element with its own pointer handling (a resize
    // handle, a sortable row's grip) can stop delivering move/up events to
    // this gesture entirely, silently abandoning the drag mid-flight -
    // exactly the kind of bug a slow, synthetic jsdom pointermove sequence
    // in a test never reproduces. Capturing on the element that started the
    // gesture is the standard fix.
    try { (e.target as Element).setPointerCapture(e.pointerId) } catch { /* unsupported in some test environments */ }
    const onMove = (ev: PointerEvent) => {
      setDragPxDelta({ mode, delta: ev.clientX - startX })
    }
    const cleanup = () => {
      document.removeEventListener('pointermove', onMove)
      document.removeEventListener('pointerup', onUp)
      document.removeEventListener('pointercancel', onCancel)
      setDragPxDelta(null)
    }
    const onCancel = () => {
      // The gesture was interrupted (tab switch, OS-level gesture, etc.) -
      // snap back to the pre-drag position rather than leaving stale
      // listeners registered, which would otherwise pile up across drags
      // and could fire a reschedule from an abandoned gesture's stale data.
      cleanup()
    }
    const onUp = (ev: PointerEvent) => {
      const deltaDays = Math.round((ev.clientX - startX) / scale.pxPerDay)
      cleanup()
      if (deltaDays === 0) { if (mode === 'move') onOpen(); return }
      const state = dragState.current
      if (!state) return
      let newStart = state.startDate
      let newDue = state.dueDate
      if (mode === 'move') { newStart = addDays(state.startDate, deltaDays); newDue = addDays(state.dueDate, deltaDays) }
      if (mode === 'resize-start') { newStart = addDays(state.startDate, deltaDays); if (newStart >= newDue) newStart = addDays(newDue, -1) }
      if (mode === 'resize-end') { newDue = addDays(state.dueDate, deltaDays); if (newDue <= newStart) newDue = addDays(newStart, 1) }
      onReschedule(toDateInputValue(newStart), toDateInputValue(newDue))
    }
    document.addEventListener('pointermove', onMove)
    document.addEventListener('pointerup', onUp)
    document.addEventListener('pointercancel', onCancel)
  }

  const humanReason = needsHuman(story, { key: story.phase ?? '', manual: story.phase === 'review' || story.phase === 'done' }, story.pending_open_questions)
  const color = barColorClass(story, humanReason)
  const labelOutside = width < 90

  return (
    <div
      className={`absolute top-1/2 -translate-y-1/2 h-6 rounded-full flex items-center px-2.5 text-[11px] font-medium text-accent-fg cursor-grab select-none shadow-sm hover:brightness-110 transition-[filter] ${color}`}
      style={{ left, width }}
      onPointerDown={beginDrag('move')}
      data-testid={`timeline-bar-${story.id}`}
      title={story.title}
    >
      <div
        className="absolute left-0 top-0 bottom-0 w-2 cursor-ew-resize"
        onPointerDown={beginDrag('resize-start')}
        data-testid={`timeline-bar-resize-start-${story.id}`}
      />
      {!labelOutside && (
        <span className="truncate flex-1">
          {humanReason && <AlertTriangle size={10} className="inline mr-1 -mt-0.5" />}
          {story.title}
        </span>
      )}
      {story.current_run?.status === 'running' && <Loader2 size={11} className="animate-spin ml-1 shrink-0" />}
      <div
        className="absolute right-0 top-0 bottom-0 w-2 cursor-ew-resize"
        onPointerDown={beginDrag('resize-end')}
        data-testid={`timeline-bar-resize-end-${story.id}`}
      />
      {labelOutside && (
        <span className="absolute left-full ml-2 text-text text-[11px] whitespace-nowrap">
          {humanReason && <AlertTriangle size={10} className="inline mr-1 -mt-0.5 text-warn" />}
          {story.title}
        </span>
      )}
    </div>
  )
}

function StoryRow({
  story, scale, phases, busy, striped, onReschedule, onOpen, onRun, onAdvance,
}: {
  story: BoardStory
  scale: TimeScale
  phases: PhaseDef[]
  busy: boolean
  striped: boolean
  onReschedule: (startDate: string, dueDate: string) => void
  onOpen: () => void
  onRun: () => void
  onAdvance: (mrUrl: string) => void
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: story.id })
  const phase = phases.find(p => p.key === story.phase) ?? { key: '', label: '', tasks: [], manual: false }
  const humanReason = needsHuman(story, phase, story.pending_open_questions)
  const running = story.current_run?.status === 'running'
  const canRun = !phase.manual && !running
  const canAdvance = story.phase === 'review' && !running
  const [showAdvanceForm, setShowAdvanceForm] = useState(false)
  const [mrUrl, setMrUrl] = useState('')

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform), transition, height: ROW_H, opacity: isDragging ? 0.5 : 1 }}
      className={`flex group ${striped ? 'bg-[var(--bg-hover)]/30' : ''} hover:bg-[var(--bg-hover)]/60 transition-colors`}
      data-testid={`timeline-row-${story.id}`}
    >
      <div
        className="shrink-0 flex items-center gap-1.5 px-2 border-r border-border-strong cursor-pointer"
        style={{ width: LABEL_W }}
        onClick={onOpen}
      >
        <button
          type="button"
          {...attributes}
          {...listeners}
          onClick={e => e.stopPropagation()}
          className="text-muted hover:text-text cursor-grab shrink-0 opacity-40 group-hover:opacity-100 transition-opacity"
          data-testid={`timeline-reorder-${story.id}`}
        >
          <GripVertical size={14} />
        </button>
        <div className="min-w-0 flex-1">
          <div className="text-[12px] text-text truncate font-medium leading-tight">{story.title}</div>
          <div className="flex items-center gap-1 mt-0.5 flex-wrap">
            {humanReason && (
              <Badge variant="warn" className="text-[9px]" data-testid={`timeline-needs-human-${story.id}`}>
                {i18nT(NEEDS_HUMAN_LABEL_KEY[humanReason])}
              </Badge>
            )}
            {story.priority !== null && story.priority !== undefined && (
              <Badge variant="muted" className="text-[9px]">{i18nT('apps.board.priority_short', { priority: story.priority })}</Badge>
            )}
          </div>
        </div>
        {canRun && (
          <button
            type="button"
            onClick={e => { e.stopPropagation(); onRun(); }}
            disabled={busy}
            className="text-accent hover:text-accent-hover cursor-pointer disabled:opacity-50 shrink-0"
            data-testid={`timeline-run-${story.id}`}
            title={i18nT('apps.board.run_next_job')}
          >
            <PlayCircle size={16} />
          </button>
        )}
        {canAdvance && !showAdvanceForm && (
          <button
            type="button"
            onClick={e => { e.stopPropagation(); setShowAdvanceForm(true); }}
            disabled={busy}
            className="text-ok hover:opacity-80 cursor-pointer disabled:opacity-50 shrink-0"
            data-testid={`timeline-advance-${story.id}`}
            title={i18nT('apps.board.mark_reviewed_done')}
          >
            <CheckCircle2 size={16} />
          </button>
        )}
        {canAdvance && showAdvanceForm && (
          <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
            <input
              value={mrUrl}
              onChange={e => setMrUrl(e.target.value)}
              placeholder={i18nT('apps.board.mr_url_placeholder')}
              className="w-20 px-1 py-0.5 text-[10px] rounded border border-border bg-bg text-text placeholder:text-muted focus:outline-none"
              data-testid={`timeline-advance-mr-url-${story.id}`}
              autoFocus
            />
            <button
              type="button"
              disabled={!mrUrl.trim() || busy}
              onClick={() => onAdvance(mrUrl.trim())}
              className="text-ok cursor-pointer disabled:opacity-50"
              data-testid={`timeline-advance-confirm-${story.id}`}
            >
              <Check size={13} />
            </button>
            <button
              type="button"
              onClick={() => setShowAdvanceForm(false)}
              className="text-muted cursor-pointer"
              data-testid={`timeline-advance-cancel-${story.id}`}
            >
              <X size={13} />
            </button>
          </div>
        )}
      </div>
      <div className="relative flex-1" style={{ width: scale.totalPx }}>
        <WeekendBands scale={scale} height={ROW_H} />
        <TodayLine scale={scale} height={ROW_H} />
        <StoryBar story={story} scale={scale} onReschedule={onReschedule} onOpen={onOpen} />
      </div>
    </div>
  );
}

function DependencyArrows({ stories, scale, rowIndex }: { stories: BoardStory[]; scale: TimeScale; rowIndex: Map<string, number> }) {
  const byId = useMemo(() => new Map(stories.map(s => [s.id, s])), [stories]);
  const lines = useMemo(() => {
    const out: { x1: number; y1: number; x2: number; y2: number; key: string }[] = []
    for (const story of stories) {
      const due = parseDate(story.due_date)
      const myStart = parseDate(story.start_date)
      const myRow = rowIndex.get(story.id)
      if (myRow === undefined || !myStart) continue
      for (const depId of story.depends_on) {
        const dep = byId.get(depId)
        const depRow = rowIndex.get(depId)
        if (!dep || depRow === undefined) continue
        const depDue = parseDate(dep.due_date)
        if (!depDue) continue
        out.push({
          x1: scale.x(depDue) + scale.pxPerDay,
          y1: depRow * ROW_H + ROW_H / 2,
          x2: scale.x(myStart),
          y2: myRow * ROW_H + ROW_H / 2,
          key: `${depId}->${story.id}`,
        })
      }
      void due
    }
    return out
  }, [stories, scale, rowIndex, byId])

  if (lines.length === 0) return null
  return (
    <svg
      className="absolute top-0 left-0 pointer-events-none z-[5]"
      width={scale.totalPx}
      height={stories.length * ROW_H}
      data-testid="timeline-dependency-arrows"
    >
      <defs>
        <marker id="timeline-arrowhead" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" className="fill-muted" />
        </marker>
      </defs>
      {lines.map(l => (
        <path
          key={l.key}
          d={`M${l.x1},${l.y1} C${l.x1 + 20},${l.y1} ${l.x2 - 20},${l.y2} ${l.x2},${l.y2}`}
          className="stroke-muted"
          strokeWidth={1.5}
          fill="none"
          markerEnd="url(#timeline-arrowhead)"
        />
      ))}
    </svg>
  )
}

function EpicSection({
  epicId, epicTitle, stories, scale, phases, busy, onReorder, onReschedule, onOpen, onRun, onAdvance,
}: {
  epicId: string
  epicTitle: string
  stories: BoardStory[]
  scale: TimeScale
  phases: PhaseDef[]
  busy: boolean
  onReorder: (orderedIds: string[]) => void
  onReschedule: (storyId: string, startDate: string, dueDate: string) => void
  onOpen: (story: BoardStory) => void
  onRun: (storyId: string) => void
  onAdvance: (storyId: string, mrUrl: string) => void
}) {
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }))
  const rowIndex = useMemo(() => new Map(stories.map((s, i) => [s.id, i])), [stories])
  const [collapsed, setCollapsed] = useState(false)
  const accent = epicAccent(epicId)

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event
    if (!over || active.id === over.id) return
    const oldIndex = stories.findIndex(s => s.id === active.id)
    const newIndex = stories.findIndex(s => s.id === over.id)
    if (oldIndex === -1 || newIndex === -1) return
    onReorder(arrayMove(stories, oldIndex, newIndex).map(s => s.id))
  }

  // One bordered, rounded card for the WHOLE epic (header + rows), not a
  // narrow label-width "tab" sitting above a much wider body - that mismatch
  // (rounded-t on a 272px box, rounded-b on a multi-thousand-px one right
  // below it) is what made the corners look broken. The header is a row
  // with the exact same two-column split as every StoryRow beneath it, so
  // the card's silhouette is consistent top to bottom.
  return (
    <div className="mb-4 rounded-lg border border-border shadow-sm overflow-hidden" data-testid={`timeline-epic-${epicId}`}>
      <div className="flex border-b border-border">
        <button
          type="button"
          onClick={() => setCollapsed(c => !c)}
          className="shrink-0 flex items-center gap-1.5 px-2 py-1.5 bg-bg-elevated text-[12px] font-semibold text-text cursor-pointer text-left border-r border-border"
          style={{ width: LABEL_W }}
          data-testid={`timeline-epic-toggle-${epicId}`}
        >
          {collapsed ? <ChevronRight size={13} className="text-muted shrink-0" /> : <ChevronDown size={13} className="text-muted shrink-0" />}
          <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${accent.bar}`} />
          <span className="truncate">{epicTitle}</span>
          <span className="text-muted font-normal shrink-0">({stories.length})</span>
        </button>
        <div className="bg-bg-elevated" style={{ width: scale.totalPx }} />
      </div>
      {!collapsed && (
        <div className="relative">
          <div className="absolute pointer-events-none" style={{ left: LABEL_W, top: 0 }}>
            <DependencyArrows stories={stories} scale={scale} rowIndex={rowIndex} />
          </div>
          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
            <SortableContext items={stories.map(s => s.id)} strategy={verticalListSortingStrategy}>
              <div className="divide-y divide-border">
                {stories.map((story, i) => (
                  <StoryRow
                    key={story.id}
                    story={story}
                    scale={scale}
                    phases={phases}
                    busy={busy}
                    striped={i % 2 === 1}
                    onReschedule={(s, d) => onReschedule(story.id, s, d)}
                    onOpen={() => onOpen(story)}
                    onRun={() => onRun(story.id)}
                    onAdvance={mrUrl => onAdvance(story.id, mrUrl)}
                  />
                ))}
              </div>
            </SortableContext>
          </DndContext>
        </div>
      )}
    </div>
  );
}

// Fractional ranking: a card dropped between two others gets a rank between
// theirs, so reordering never needs to renumber the whole list. Falls back
// to a fresh integer band at either edge.
export function computeRank(orderedIds: string[], movedId: string, ranksById: Map<string, number | null>): number {
  const idx = orderedIds.indexOf(movedId)
  const before = idx > 0 ? ranksById.get(orderedIds[idx - 1]) : null
  const after = idx < orderedIds.length - 1 ? ranksById.get(orderedIds[idx + 1]) : null
  if (before != null && after != null) return (before + after) / 2
  if (before != null) return before + 1000
  if (after != null) return after - 1000
  return 0
}

function TimelineSkeleton() {
  return (
    <div className="flex-1 flex flex-col gap-3 animate-pulse" data-testid="timeline-loading">
      {[0, 1, 2].map(i => (
        <div key={i} className="flex flex-col gap-1.5">
          <div className="h-5 w-40 rounded bg-bg-elevated" />
          <div className="h-11 w-full rounded-md bg-bg-elevated" />
          <div className="h-11 w-full rounded-md bg-bg-elevated" />
        </div>
      ))}
    </div>
  )
}

export default function TimelinePage() {
  const queryClient = useQueryClient()
  const [openStory, setOpenStory] = useState<BoardStory | null>(null)
  const [zoom, setZoom] = useState<Zoom>('week')
  const scale = useTimeScale(zoom)

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
    mutationFn: ({ storyId, mrUrl }: { storyId: string; mrUrl: string }) => boardApi.advance(storyId, mrUrl),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['board', 'stories'] }),
  })
  type StoriesData = { stories: BoardStory[]; project_paths: string[] }
  const updateMutation = useMutation({
    mutationFn: ({ storyId, fields }: { storyId: string; fields: Parameters<typeof boardApi.updateStory>[1] }) =>
      boardApi.updateStory(storyId, fields),
    // Optimistic: a dragged bar/row applying its new position only once the
    // `bd update` round-trip (and the subsequent refetch) completes reads as
    // "nothing happened" for however long that takes - `bd` is a real
    // subprocess call, not instant. Patch the cached list immediately so the
    // drop sticks right away; onError rolls it back if the write failed.
    onMutate: async ({ storyId, fields }) => {
      await queryClient.cancelQueries({ queryKey: ['board', 'stories'] })
      const previous = queryClient.getQueryData<StoriesData>(['board', 'stories'])
      queryClient.setQueryData<StoriesData>(['board', 'stories'], old => old && {
        ...old,
        stories: old.stories.map(s => (s.id === storyId ? { ...s, ...fields } : s)),
      })
      return { previous }
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(['board', 'stories'], context.previous)
    },
    // Also invalidate THIS story's own detail cache, not just the list -
    // the modal's sidebar reads from ['board','detail', id] separately, and
    // without this a drag on the timeline left it showing stale dates until
    // its own poll next happened to fire (same bug class the modal's own
    // editable fields already dodge, since those invalidate both).
    onSettled: (_data, _err, { storyId }) => {
      queryClient.invalidateQueries({ queryKey: ['board', 'stories'] })
      queryClient.invalidateQueries({ queryKey: ['board', 'detail', storyId] })
    },
  })

  const epics = useMemo(() => {
    const stories = storiesQuery.data?.stories ?? []
    const byEpic = new Map<string, { epicTitle: string; stories: BoardStory[] }>()
    for (const story of stories) {
      if (!byEpic.has(story.epic_id)) byEpic.set(story.epic_id, { epicTitle: story.epic_title, stories: [] })
      byEpic.get(story.epic_id)!.stories.push(story)
    }
    for (const group of byEpic.values()) {
      group.stories.sort((a, b) => {
        const ra = a.rank ?? Infinity
        const rb = b.rank ?? Infinity
        if (ra !== rb) return ra - rb
        return (a.priority ?? 99) - (b.priority ?? 99) || a.title.localeCompare(b.title)
      })
    }
    return [...byEpic.entries()].map(([epicId, g]) => ({ epicId, ...g }))
  }, [storiesQuery.data])

  const busy = runMutation.isPending || advanceMutation.isPending || updateMutation.isPending
  const loading = phasesQuery.isLoading || storiesQuery.isLoading

  const handleReorder = (epicStories: BoardStory[], movedId: string, orderedIds: string[]) => {
    const ranksById = new Map(epicStories.map(s => [s.id, s.rank]))
    const rank = computeRank(orderedIds, movedId, ranksById)
    updateMutation.mutate({ storyId: movedId, fields: { rank } })
  }

  return (
    <div className="h-full flex flex-col p-4 overflow-hidden">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-text flex items-center gap-2">
            <GanttChart size={20} className="text-accent" />
            {i18nT('apps.board.title')}
          </h1>
          {storiesQuery.data && storiesQuery.data.project_paths.length === 0 && (
            <div className="text-[13px] text-muted mt-1">{i18nT('apps.board.no_projects_configured')}</div>
          )}
        </div>
        <div className="flex items-center gap-0.5 bg-bg-elevated rounded-full p-0.5" data-testid="timeline-zoom-toggle">
          {(['week', 'quarter'] as const).map(z => (
            <button
              key={z}
              type="button"
              onClick={() => setZoom(z)}
              className={`text-[11px] px-3 py-1 rounded-full cursor-pointer transition-colors ${zoom === z ? 'bg-accent text-accent-fg' : 'text-muted hover:text-text'}`}
              data-testid={`timeline-zoom-${z}`}
            >
              {i18nT(z === 'week' ? 'apps.board.zoom_week' : 'apps.board.zoom_quarter')}
            </button>
          ))}
        </div>
      </div>
      {(phasesQuery.error || storiesQuery.error || updateMutation.error) && (
        <ErrorNotice message={String(phasesQuery.error || storiesQuery.error || updateMutation.error)} />
      )}
      {loading ? (
        <TimelineSkeleton />
      ) : (
        <div className="flex-1 overflow-auto">
          <div className="sticky top-0 z-20 bg-bg flex">
            <div className="shrink-0 border-r border-border-strong" style={{ width: LABEL_W }} />
            <TimelineHeader scale={scale} zoom={zoom} />
          </div>
          {epics.length === 0 ? (
            <div className="text-[12px] text-muted px-1 py-4">{i18nT('apps.board.no_stories')}</div>
          ) : (
            epics.map(({ epicId, epicTitle, stories }) => (
              <EpicSection
                key={epicId}
                epicId={epicId}
                epicTitle={epicTitle}
                stories={stories}
                scale={scale}
                phases={phasesQuery.data?.phases ?? []}
                busy={busy}
                onReorder={orderedIds => {
                  const movedId = orderedIds.find((id, i) => stories[i]?.id !== id) ?? orderedIds[0]
                  handleReorder(stories, movedId, orderedIds)
                }}
                onReschedule={(storyId, startDate, dueDate) =>
                  updateMutation.mutate({ storyId, fields: { start_date: startDate, due_date: dueDate } })}
                onOpen={setOpenStory}
                onRun={storyId => runMutation.mutate(storyId)}
                onAdvance={(storyId, mrUrl) => advanceMutation.mutate({ storyId, mrUrl })}
              />
            ))
          )}
          {/* Continues the calendar grid (weekend shading, today line) into
             whatever's left of the viewport instead of trailing into a
             blank void the moment the rows run out - Jira's own timeline
             does the same, the grid reads as the canvas, not as content. */}
          <div className="flex h-[480px]">
            <div className="shrink-0 border-r border-border-strong" style={{ width: LABEL_W }} />
            <div className="relative flex-1" style={{ width: scale.totalPx }}>
              <WeekendBands scale={scale} height="100%" />
              <TodayLine scale={scale} height="100%" />
            </div>
          </div>
        </div>
      )}
      {openStory && (
        <StoryModal story={openStory} phases={phasesQuery.data?.phases ?? []} onClose={() => setOpenStory(null)} />
      )}
    </div>
  );
}
