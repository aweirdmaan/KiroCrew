import { useState, useEffect, useMemo, useRef } from 'react';
import { Badge } from './ui';
import { useTaskRunnerStream, type TaskRunnerLogLevel } from '../hooks/useTaskRunnerStream';
import { i18nT } from '../i18n/t';

const LOG_LEVELS: TaskRunnerLogLevel[] = ['info', 'ok', 'warn', 'danger', 'agent'];

const LEVEL_BADGE_VARIANT: Record<TaskRunnerLogLevel, 'ok' | 'err' | 'warn' | 'aim' | 'muted'> = {
  info: 'muted',
  ok: 'ok',
  warn: 'warn',
  danger: 'err',
  agent: 'aim',
};

const LEVEL_LABEL_KEY: Record<TaskRunnerLogLevel, string> = {
  info: 'pages.projectDetailPage.live_log_level_info',
  ok: 'pages.projectDetailPage.live_log_level_success',
  warn: 'pages.projectDetailPage.live_log_level_warning',
  danger: 'pages.projectDetailPage.live_log_level_error',
  agent: 'pages.projectDetailPage.live_log_level_agent',
};

/** Splits `text` on every case-insensitive occurrence of `query`, wrapping
 *  matches in <mark> - so a search hit is visible without re-reading the
 *  whole line. Returns `text` unchanged (not wrapped in an array) when there
 *  is nothing to highlight, to keep the common case cheap. */
function highlightMatch(text: string, query: string): React.ReactNode {
  if (!query) return text;
  const lower = text.toLowerCase();
  const q = query.toLowerCase();
  const parts: React.ReactNode[] = [];
  let i = 0;
  for (; ;) {
    const found = lower.indexOf(q, i);
    if (found === -1) {
      parts.push(text.slice(i));
      break;
    }
    if (found > i) parts.push(text.slice(i, found));
    parts.push(
      <mark key={found} className="bg-warn/50 text-text rounded-sm">
        {text.slice(found, found + q.length)}
      </mark>
    );
    i = found + q.length;
  }
  return parts;
}

/** Live Task Runner output, streamed over SSE (useTaskRunnerStream). The
 * backend keeps a replayable per-run frame buffer for as long as the run
 * stays in task_runner._runs (not just while a connection is open), so
 * reopening this - even after the run finished, even after a reload -
 * replays the full history instead of starting from whatever happened to
 * still be in this component's own state. Each line carries a severity level
 * (derived from its frame's type/status) that drives a color badge and the
 * filter chips above the log, and a free-text search narrows the visible
 * lines and highlights the match.
 *
 * Shared between ProjectDetailPage's "Live" tab and the Board's per-story
 * timeline drawer (apps/board/StoryTimelineDrawer.tsx) - a historical job's
 * task_id replays exactly the same way a still-running one streams. */
export function LiveLogPanel({ taskId, active }: { taskId: string; active: boolean }) {
  const lines = useTaskRunnerStream(taskId, active);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const [query, setQuery] = useState('');
  // null = no isolation, every level shows. Clicking a pill isolates that
  // level (hides every other one); clicking the SAME pill again clears the
  // isolation back to showing everything - a highlight/solo toggle, not a
  // per-level hide toggle.
  const [onlyLevel, setOnlyLevel] = useState<TaskRunnerLogLevel | null>(null);

  const filteredLines = useMemo(() => {
    const q = query.trim().toLowerCase();
    return lines.filter(l => (!onlyLevel || l.level === onlyLevel) && (!q || l.text.toLowerCase().includes(q)));
  }, [lines, query, onlyLevel]);

  // Scroll only this panel's own log box to its latest line, not
  // scrollIntoView - that climbs every scrollable ancestor (the modal body,
  // the page), which is what dragged an unrelated parent down to reveal this
  // panel the moment it mounted, e.g. opening a board card whose Timeline
  // defaults to the Live tab.
  useEffect(() => {
    const box = bottomRef.current?.parentElement;
    if (box) box.scrollTop = box.scrollHeight;
  }, [filteredLines.length]);

  const toggleLevel = (lvl: TaskRunnerLogLevel) => {
    setOnlyLevel(prev => (prev === lvl ? null : lvl));
  };

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <input
          type="text"
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder={i18nT('pages.projectDetailPage.live_log_search_placeholder')}
          className="flex-1 min-w-[160px] px-3 py-1.5 text-[12px] rounded-md border border-border bg-bg-elevated text-text placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-accent"
          data-testid="project-detail-live-log-search"
        />
        <div className="flex items-center gap-1 flex-wrap">
          {LOG_LEVELS.map(lvl => {
            const selected = onlyLevel === lvl;
            const dimmed = onlyLevel !== null && !selected;
            return (
              <button
                key={lvl}
                type="button"
                onClick={() => toggleLevel(lvl)}
                className={`cursor-pointer rounded-full transition-all ${dimmed ? 'opacity-35' : 'opacity-100'} ${selected ? 'ring-2 ring-accent ring-offset-1 ring-offset-bg-elevated' : ''}`}
                data-testid={`project-detail-live-log-filter-${lvl}`}
                aria-pressed={selected}
              >
                <Badge variant={LEVEL_BADGE_VARIANT[lvl]} className="text-[9px] px-1.5 py-0">
                  {i18nT(LEVEL_LABEL_KEY[lvl])}
                </Badge>
              </button>
            );
          })}
        </div>
      </div>
      <div
        className="min-h-[300px] max-h-[70vh] overflow-auto bg-bg-elevated border border-border rounded-lg p-3 font-mono text-[12px] whitespace-pre-wrap"
        data-testid="project-detail-live-log"
      >
        {lines.length === 0 ? (
          <div className="text-muted">{i18nT('pages.projectDetailPage.live_log_waiting')}</div>
        ) : filteredLines.length === 0 ? (
          <div className="text-muted">{i18nT('pages.projectDetailPage.live_log_no_matches')}</div>
        ) : (
          filteredLines.map(l => (
            <div key={l.key} className="flex items-start gap-2 py-[1px]">
              <Badge variant={LEVEL_BADGE_VARIANT[l.level]} className="text-[9px] px-1.5 py-0 shrink-0 mt-[1px]">
                {i18nT(LEVEL_LABEL_KEY[l.level])}
              </Badge>
              <span className={l.isAgentText ? 'text-text' : 'text-muted'}>
                {highlightMatch(l.text, query)}
                {l.streaming ? <span className="animate-pulse">▋</span> : null}
              </span>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
