import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectDetailPage from '../pages/ProjectDetailPage'
import type { ProjectRun } from '../types'

vi.mock('../pages/aidlc/DagView', () => ({ default: () => <div data-testid="dag-view" /> }))
vi.mock('../pages/aidlc/PhasedView', () => ({ default: () => <div data-testid="phased-view" /> }))
vi.mock('../pages/aidlc/TaskDetailPanel', () => ({ default: () => <div data-testid="task-panel" /> }))

const mockRun = (overrides: Partial<ProjectRun> = {}): ProjectRun => ({
  task_id: 'run-1', name: 'Test Run', running: true, status: 'running',
  tasks: 1, completed: 0, failed: 0, skipped: 0, current_task: 1,
  spec: 'test.md', spec_name: 'Test', error: '',
  tokens_used: 0, replan_count: 0,
  started_at: Date.now() / 1000 - 5, finished_at: 0,
  work_dir: '/tmp/test', branch_name: 'main', spec_content: '# Test spec',
  lessons_learned: [], commits: 0, original_input: 'test input', source: 'text',
  groups: [[1]],
  task_details: [
    { index: 1, title: 'Explain', description: '', status: 'in_progress', error: '', result: '', attempts: 1, depends_on: [], requires_approval: false },
  ],
  ...overrides,
})

interface StubStream { onmessage?: (ev: { data: string }) => void; onerror?: () => void; close(): void }

function installEventSource() {
  const streams: StubStream[] = []
  class StubEventSource implements StubStream {
    onmessage?: (ev: { data: string }) => void
    onerror?: () => void
    constructor(readonly url: string) { streams.push(this) }
    close() {}
  }
  vi.stubGlobal('EventSource', StubEventSource)
  return streams
}

function send(streams: StubStream[], frame: Record<string, unknown>) {
  act(() => { streams[0]?.onmessage?.({ data: JSON.stringify(frame) }) })
}

describe('ProjectDetailPage — live log', () => {
  it('shows the Live tab only while the run is running, and streams agent text', async () => {
    const streams = installEventSource()
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    const liveTab = screen.getByTestId('project-detail-live-tab')
    fireEvent.click(liveTab)
    await waitFor(() => expect(streams.length).toBe(1))

    send(streams, { type: 'run', status: 'running', completed: 0, tasks: 1 })
    send(streams, { type: 'step', index: 1, title: 'Explain', status: 'in_progress' })
    send(streams, { type: 'progress', index: 1, text: 'Once upon a time' })
    send(streams, { type: 'progress', index: 1, text: ', a B-tree grew.' })

    const log = screen.getByTestId('project-detail-live-log')
    expect(within(log).getByText(/Once upon a time, a B-tree grew\./)).toBeInTheDocument()
  })

  it('still shows the Live tab for a completed run, for replay', () => {
    // The backend keeps a replayable frame buffer for as long as the run
    // stays around, so a finished run's live log is still worth opening -
    // the tab should only disappear before a run has actually started.
    renderWithProviders(<ProjectDetailPage run={mockRun({ status: 'completed', running: false })} />)
    expect(screen.getByTestId('project-detail-live-tab')).toBeInTheDocument()
  })

  it('hides the Live tab before a run has started (planning/planned)', () => {
    renderWithProviders(<ProjectDetailPage run={mockRun({ status: 'planning', running: false })} />)
    expect(screen.queryByTestId('project-detail-live-tab')).toBeNull()
  })

  it('filters visible lines by search text', async () => {
    const streams = installEventSource()
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByTestId('project-detail-live-tab'))
    await waitFor(() => expect(streams.length).toBe(1))

    send(streams, { type: 'step', index: 1, title: 'Explain', status: 'in_progress' })
    send(streams, { type: 'progress', index: 1, text: 'alpha beta gamma' })

    const log = screen.getByTestId('project-detail-live-log')
    expect(within(log).getByText(/alpha beta gamma/)).toBeInTheDocument()

    fireEvent.change(screen.getByTestId('project-detail-live-log-search'), { target: { value: 'zzz-no-match' } })
    expect(within(log).queryByText(/alpha beta gamma/)).toBeNull()
    expect(within(log).getByText('No log lines match your filters.')).toBeInTheDocument()

    fireEvent.change(screen.getByTestId('project-detail-live-log-search'), { target: { value: 'beta' } })
    expect(log.textContent).toContain('alpha beta gamma')
    expect(within(log).getByText('beta').tagName).toBe('MARK')
  })

  it('toggling a level filter chip hides lines of that level', async () => {
    const streams = installEventSource()
    renderWithProviders(<ProjectDetailPage run={mockRun()} />)
    fireEvent.click(screen.getByTestId('project-detail-live-tab'))
    await waitFor(() => expect(streams.length).toBe(1))

    send(streams, { type: 'step', index: 1, title: 'Explain', status: 'in_progress' })
    send(streams, { type: 'progress', index: 1, text: 'live agent text' })

    const log = screen.getByTestId('project-detail-live-log')
    expect(within(log).getByText(/live agent text/)).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('project-detail-live-log-filter-agent'))
    expect(within(log).queryByText(/live agent text/)).toBeNull()

    fireEvent.click(screen.getByTestId('project-detail-live-log-filter-agent'))
    expect(within(log).getByText(/live agent text/)).toBeInTheDocument()
  })
})
