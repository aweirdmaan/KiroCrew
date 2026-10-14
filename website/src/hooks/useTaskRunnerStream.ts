import { useEffect, useRef, useState, useCallback } from 'react'

export interface TaskRunnerStreamFrame {
  type: 'run' | 'step' | 'progress' | 'result' | 'ended'
  status?: string
  completed?: number
  tasks?: number
  index?: number
  title?: string
  text?: string
  reason?: string
}

export type TaskRunnerLogLevel = 'info' | 'ok' | 'warn' | 'danger' | 'agent'

export interface TaskRunnerLogLine {
  key: string
  text: string
  /** True for a line carrying the agent's own live turn text (a "progress"
   *  or "result" frame), so the UI can render it distinctly from step/run
   *  status bookkeeping lines. */
  isAgentText?: boolean
  /** Present on a "progress" line: which step index it belongs to, so the
   *  UI can append later progress deltas onto the same line instead of
   *  starting a new one each poll tick. */
  stepIndex?: number
  /** True only while this line is still being grown by "progress" frames -
   *  cleared once the step reaches a terminal status, so the UI can blink a
   *  cursor on exactly the line still being typed. */
  streaming?: boolean
  /** Severity bucket derived from the frame's type/status, driving the
   *  level badge + color in the UI and the level filter chips. */
  level: TaskRunnerLogLevel
}

function formatFrame(frame: TaskRunnerStreamFrame): string[] {
  switch (frame.type) {
    case 'run':
      return [`RUN ${frame.status} (${frame.completed ?? 0}/${frame.tasks ?? 0})`]
    case 'step':
      return [`  [${frame.index}] ${frame.title}: ${frame.status}`]
    case 'result':
      return (frame.text || '').split('\n').map(line => `      ${line}`)
    case 'ended':
      return [`ENDED: ${frame.reason || ''} (${frame.status || ''})`]
    default:
      return [JSON.stringify(frame)]
  }
}

function levelForFrame(frame: TaskRunnerStreamFrame): TaskRunnerLogLevel {
  if (frame.type === 'progress') return 'agent'
  if (frame.type === 'result') return frame.status === 'failed' ? 'danger' : 'agent'
  const status = frame.status || ''
  if (status === 'failed' || status === 'cancelled') return 'danger'
  if (status === 'passed' || status === 'completed') return 'ok'
  if (status === 'reviewing' || status === 'skipped' || status === 'paused') return 'warn'
  return 'info'
}

/**
 * Live Task Runner progress over GET /api/taskrunner/{taskId}/stream.
 *
 * Same EventSource + auto-reconnect shape as useLogSSE.ts, pointed at a
 * per-run SSE endpoint instead of the general log stream: relative URL, so
 * the browser's own dashboard session cookie authenticates it the same way
 * every other same-origin dashboard fetch does - no token plumbing needed
 * here. The endpoint itself closes the stream once the run reaches a
 * terminal status (an "ended" frame is the last line), so there is normally
 * nothing left to reconnect to; the reconnect-on-error path exists for a
 * genuine connection drop mid-run, same as useLogSSE's.
 */
export function useTaskRunnerStream(taskId: string | null, active: boolean) {
  const [lines, setLines] = useState<TaskRunnerLogLine[]>([])
  const ref = useRef<EventSource | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const seq = useRef(0)
  const idRef = useRef<string | null>(null)

  const stop = useCallback(() => {
    if (timer.current) { clearTimeout(timer.current); timer.current = null }
    ref.current?.close()
    ref.current = null
  }, [])

  const start = useCallback((id: string) => {
    if (ref.current) return
    const sse = new EventSource(`/api/taskrunner/${encodeURIComponent(id)}/stream`)
    ref.current = sse
    sse.onmessage = (e) => {
      try {
        const frame: TaskRunnerStreamFrame = JSON.parse(e.data)
        const level = levelForFrame(frame)
        if (frame.type === 'progress') {
          // Grow the agent's current live-text line in place instead of
          // appending a new row per poll tick, so it reads as the agent
          // writing in real time rather than a scroll of near-duplicate
          // lines - the same feel as this chat's own streaming response.
          setLines(prev => {
            const last = prev[prev.length - 1]
            if (last?.streaming && last.stepIndex === frame.index) {
              const updated = { ...last, text: last.text + (frame.text || '') }
              return [...prev.slice(0, -1), updated]
            }
            return [
              ...prev,
              {
                key: `${seq.current++}`,
                text: frame.text || '',
                isAgentText: true,
                streaming: true,
                stepIndex: frame.index,
                level,
              },
            ]
          })
        } else {
          const rows = formatFrame(frame)
          setLines(prev => {
            // A "result" frame for the step still being streamed closes out
            // that same line (replacing its text with the authoritative
            // final text) instead of appending a duplicate block below it.
            const last = prev[prev.length - 1]
            if (frame.type === 'result' && last?.streaming && last.stepIndex === frame.index) {
              const updated = { ...last, text: rows.join('\n'), streaming: false, level }
              return [...prev.slice(0, -1), updated]
            }
            return [
              ...prev,
              ...rows.map(text => ({
                key: `${seq.current++}`,
                text,
                isAgentText: frame.type === 'result',
                stepIndex: frame.index,
                level,
              })),
            ]
          })
        }
        if (frame.type === 'ended') stop()
      } catch { /* ignore a malformed frame */ }
    }
    sse.onerror = () => {
      sse.close()
      ref.current = null
      timer.current = setTimeout(() => start(id), 3000)
    }
  }, [stop])

  useEffect(() => {
    if (!active || !taskId) { stop(); return }
    if (idRef.current !== taskId) {
      // A different run selected mid-stream: drop the old lines and
      // reconnect, rather than appending the new run's frames after the
      // previous one's "ENDED" line.
      idRef.current = taskId
      setLines([])
      stop()
    }
    start(taskId)
    return stop
  }, [taskId, active, start, stop])

  return lines
}
