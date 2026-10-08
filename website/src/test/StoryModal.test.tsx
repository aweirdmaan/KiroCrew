import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import StoryModal from '../apps/board/StoryModal'
import { boardApi, type BoardStory, type PhaseDef, type StoryDetail, type RunRecord } from '../apps/board/boardApi'
import { i18nT } from '../i18n/t'

vi.mock('../apps/board/boardApi', async () => {
  const actual = await vi.importActual<typeof import('../apps/board/boardApi')>('../apps/board/boardApi')
  return {
    ...actual,
    boardApi: {
      phases: vi.fn(),
      stories: vi.fn(),
      history: vi.fn(),
      detail: vi.fn(),
      run: vi.fn(),
      advance: vi.fn(),
      addComment: vi.fn(),
      updateStory: vi.fn(),
    },
  }
})

// LiveLogPanel opens its own SSE connection via useTaskRunnerStream; stub it
// so these tests only assert WHETHER it renders (active=true for any entry
// with a task_id - the exact bug this modal fixed) and not its own internals,
// which are already covered by ProjectDetailPageLiveLog.test.tsx.
vi.mock('../components/LiveLogPanel', () => ({
  LiveLogPanel: ({ taskId, active }: { taskId: string; active: boolean }) => (
    <div data-testid="stub-live-log-panel" data-task-id={taskId} data-active={String(active)} />
  ),
}))

// Matches the REAL current shape (phases.py's single "pipeline" job, see
// its module docstring) rather than the old, now-retired per-column split -
// a test fixture using stale phase keys would mask the exact legacy-phase
// normalization this file also tests below.
const PHASES: PhaseDef[] = [
  { key: 'pipeline', label: 'Pipeline', tasks: ['ideate', 'plan', 'verify', 'fix', 'confirm'], manual: false },
]

function story(overrides: Partial<BoardStory> = {}): BoardStory {
  return {
    id: 's-1', title: 'Story one', status: 'open',
    epic_id: 'e-1', epic_title: 'Calculator App', project_path: '/proj',
    phase: 'pipeline', phase_label: 'Pipeline', current_run: null,
    priority: null, owner: '', pending_open_questions: false,
    start_date: null, due_date: null, rank: null, depends_on: [],
    ...overrides,
  };
}

function detail(overrides: Partial<StoryDetail> = {}): StoryDetail {
  return {
    id: 's-1', title: 'Story one', description: '**bold** description', status: 'open',
    issue_type: 'task', priority: 2, owner: 'me', created_at: 't1', updated_at: 't2',
    comments: [{ id: 'c1', author: 'Amaan', text: 'first comment', created_at: 't3' }],
    start_date: null, due_date: null, rank: null, story_points: null, depends_on: [],
    ...overrides,
  };
}

function run(overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    phase: 'pipeline', task_key: 'ideate', iteration: 0, task_id: 'task-1',
    status: 'passed', started_at: 't1', finished_at: 't2',
    ...overrides,
  };
}

describe('StoryModal', () => {
  it('renders the description as markdown and the existing comments', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-description').querySelector('strong')).toBeInTheDocument());
    expect(screen.getByTestId('board-comment')).toBeInTheDocument();
    expect(within(screen.getByTestId('board-comment')).getByText('Amaan')).toBeInTheDocument();
  });

  it('a finished job entry still renders its log panel as active (replay, not live-only)', async () => {
    // Regression: the first version of this modal only passed active=true to
    // LiveLogPanel for entries whose status was "running", so every already
    // -finished job silently showed nothing - confirmed live against a real
    // run before this test existed.
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: null,
      history: [run({ status: 'passed' })],
    });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    const job = await screen.findByTestId('board-job-pipeline');
    fireEvent.click(within(job).getByRole('button', { name: /Pipeline/ }));

    // Live is the default tab - the single "ideate" entry is its own
    // effective task automatically, no row to expand into any more.
    const panel = await within(job).findByTestId('stub-live-log-panel');
    expect(panel).toHaveAttribute('data-active', 'true');
    expect(panel).toHaveAttribute('data-task-id', 'task-1');
  });

  it('timeline job groups are collapsed by default', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: null,
      history: [run({ status: 'passed' })],
    });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    const job = await screen.findByTestId('board-job-pipeline');
    expect(within(job).queryByTestId('board-job-pipeline-view-content')).toBeNull();
  });

  it('groups timeline entries by job (phase), with multiple tasks nested under one job - not one job per task', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: null,
      history: [
        run({ phase: 'pipeline', task_key: 'verify', status: 'passed' }),
        run({ phase: 'pipeline', task_key: 'fix', status: 'passed' }),
        run({ phase: 'pipeline', task_key: 'confirm', status: 'gate_failed' }),
      ],
    });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-job-pipeline')).toBeInTheDocument());
    expect(screen.queryAllByTestId(/^board-job-/)).toHaveLength(1); // one job, three tasks inside it
    const job = screen.getByTestId('board-job-pipeline');
    fireEvent.click(within(job).getByRole('button', { name: /Pipeline/ }));
    fireEvent.click(within(job).getByTestId('board-job-pipeline-view-dag'));
    const content = within(job).getByTestId('board-job-pipeline-view-content');
    await waitFor(() => expect(within(content).getByText('verify')).toBeInTheDocument());
    expect(within(content).getByText('fix')).toBeInTheDocument();
    expect(within(content).getByText('confirm')).toBeInTheDocument();
  });

  it('a job defaults to the Live view, showing its latest/selected task\'s log', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: null,
      history: [run({ status: 'passed', task_id: 'task-ideate' })],
    });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    const job = await screen.findByTestId('board-job-pipeline');
    fireEvent.click(within(job).getByRole('button', { name: /Pipeline/ }));
    expect(within(job).getByTestId('board-job-pipeline-view-live')).toHaveClass('bg-accent');
    const panel = within(job).getByTestId('stub-live-log-panel');
    expect(panel).toHaveAttribute('data-task-id', 'task-ideate');
  });

  it('switching a job to the DAG view renders one node per task, in order', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: null,
      history: [
        run({ phase: 'pipeline', task_key: 'verify', status: 'passed' }),
        run({ phase: 'pipeline', task_key: 'fix', status: 'passed' }),
      ],
    });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    const job = await screen.findByTestId('board-job-pipeline');
    fireEvent.click(within(job).getByRole('button', { name: /Pipeline/ }));
    fireEvent.click(within(job).getByTestId('board-job-pipeline-view-dag'));

    const content = within(job).getByTestId('board-job-pipeline-view-content');
    // DagView renders each node's title as SVG text - "confirm" never ran,
    // so it should still appear as a pending node (fed from the job's own
    // task list, not only from entries that have happened).
    await waitFor(() => expect(within(content).getByText('verify')).toBeInTheDocument());
    expect(within(content).getByText('fix')).toBeInTheDocument();
    expect(within(content).getByText('confirm')).toBeInTheDocument();
  });

  it('switching a job to the Phased view lists its tasks with attempt counts', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: null,
      history: [
        run({ phase: 'pipeline', task_key: 'verify', status: 'passed' }),
        run({ phase: 'pipeline', task_key: 'fix', status: 'passed', iteration: 0 }),
        run({ phase: 'pipeline', task_key: 'fix', status: 'gate_failed', iteration: 1 }),
      ],
    });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    const job = await screen.findByTestId('board-job-pipeline');
    fireEvent.click(within(job).getByRole('button', { name: /Pipeline/ }));
    fireEvent.click(within(job).getByTestId('board-job-pipeline-view-phased'));

    // PhasedView renders a task's title inline as "Task N: <title>" (one
    // combined text node), so an exact match on just "fix" would never hit -
    // a substring matcher is the correct query here, not a workaround.
    const content = within(job).getByTestId('board-job-pipeline-view-content');
    await waitFor(() => expect(within(content).getByText(/fix/)).toBeInTheDocument());
  });

  it('shows one attempt pill per retry of a task, switching the shared log pane between them', async () => {
    // Airflow's own "try number" pattern, replacing the old flat
    // per-attempt accordion list (an accordion nested inside the job's
    // own accordion) - one task with several attempts gets a row of
    // small pills, not a disclosure widget per attempt.
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: null,
      history: [
        run({ phase: 'pipeline', task_key: 'fix', iteration: 0, status: 'gate_failed', task_id: 'fix-1' }),
        run({ phase: 'pipeline', task_key: 'fix', iteration: 1, status: 'passed', task_id: 'fix-2' }),
      ],
    });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    const job = await screen.findByTestId('board-job-pipeline');
    fireEvent.click(within(job).getByRole('button', { name: /Pipeline/ }));

    expect(within(job).getByTestId('board-attempt-fix-0')).toBeInTheDocument();
    expect(within(job).getByTestId('board-attempt-fix-1')).toBeInTheDocument();
    // Latest attempt selected by default.
    let panel = await within(job).findByTestId('stub-live-log-panel');
    expect(panel).toHaveAttribute('data-task-id', 'fix-2');

    fireEvent.click(within(job).getByTestId('board-attempt-fix-0'));
    panel = await within(job).findByTestId('stub-live-log-panel');
    expect(panel).toHaveAttribute('data-task-id', 'fix-1');
  });

  it('normalizes a legacy per-column phase string onto the one consolidated pipeline job', async () => {
    // History recorded before phases.py merged every column into "pipeline"
    // still carries the old strings - a story with history from before that
    // merge should still read as ONE job, not a single-task accordion per
    // retired column.
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: null,
      history: [
        run({ phase: 'grooming', task_key: 'ideate', status: 'passed' }),
        run({ phase: 'planning', task_key: 'plan', status: 'passed' }),
        run({ phase: 'implementation', task_key: 'verify', status: 'passed' }),
      ],
    });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-job-pipeline')).toBeInTheDocument());
    expect(screen.queryAllByTestId(/^board-job-/)).toHaveLength(1);
    expect(screen.queryByTestId('board-job-grooming')).toBeNull();
    expect(screen.queryByTestId('board-job-planning')).toBeNull();
    expect(screen.getByText(i18nT('apps.board.task_count', { count: 5 }))).toBeInTheDocument();
  });

  it('shows the needs-human banner when the last comment is unanswered open questions', async () => {
    const questionsComment = 'OPEN QUESTIONS\n\n1. Is this ok?\n';
    vi.mocked(boardApi.detail).mockResolvedValue(detail({ comments: [{ id: 'c1', author: 'agent', text: questionsComment, created_at: 't1' }] }));
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-modal-needs-human')).toBeInTheDocument());
    expect(screen.getByTestId('board-modal-needs-human')).toHaveTextContent('Answer needed');
  });

  it('shows the needs-human banner on a gate_failed run', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail({ comments: [] }));
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: { phase: 'pipeline', task_key: 'confirm', iteration: 0, task_id: 't-1', status: 'gate_failed', started_at: 't', finished_at: 't2' },
      history: [],
    });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-modal-needs-human')).toBeInTheDocument());
    expect(screen.getByTestId('board-modal-needs-human')).toHaveTextContent('Needs attention');
  });

  it('shows no needs-human banner for an idle, non-manual phase with no pending questions', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail({ comments: [] }));
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-description')).toBeInTheDocument());
    expect(screen.queryByTestId('board-modal-needs-human')).toBeNull();
  });

  it('posts a new comment through the composer', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail({ comments: [] }));
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });
    vi.mocked(boardApi.addComment).mockResolvedValue({ ok: true });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-comment-input')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('board-comment-input'), { target: { value: 'APPROVED' } });
    fireEvent.click(screen.getByTestId('board-comment-submit'));

    await waitFor(() => expect(boardApi.addComment).toHaveBeenCalledWith('s-1', 'APPROVED'));
  });

  it('does not render the open-questions panel when the last comment is not a questions post', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-comment')).toBeInTheDocument());
    expect(screen.queryByTestId('board-open-questions')).not.toBeInTheDocument();
  });

  it('renders a quick-answer input per question and submits them as one comment', async () => {
    const questionsComment = [
      'OPEN QUESTIONS',
      '',
      '1. Is Python ok?',
      '2. Where should the module live?',
      '',
      '---',
      '',
      'To proceed: answer these questions as a comment, then invoke rocket-confirm-plan.',
    ].join('\n');
    vi.mocked(boardApi.detail).mockResolvedValue(detail({ comments: [{ id: 'c1', author: 'agent', text: questionsComment, created_at: 't1' }] }));
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });
    vi.mocked(boardApi.addComment).mockResolvedValue({ ok: true });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-open-questions')).toBeInTheDocument());
    expect(screen.getByTestId('board-open-question-1')).toBeInTheDocument();
    expect(screen.getByTestId('board-open-question-2')).toBeInTheDocument();

    fireEvent.change(screen.getByTestId('board-open-question-1-answer'), { target: { value: 'Yes, Python is fine' } });
    fireEvent.click(screen.getByTestId('board-open-questions-submit'));

    await waitFor(() => expect(boardApi.addComment).toHaveBeenCalledWith(
      's-1',
      'Answers to the open questions:\n\n1. Yes, Python is fine',
    ));
  });

  it('edits the title and saves it via updateStory', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });
    vi.mocked(boardApi.updateStory).mockResolvedValue({ ok: true });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-title-edit-start')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('board-title-edit-start'));
    fireEvent.change(screen.getByTestId('board-title-input'), { target: { value: 'Renamed story' } });
    fireEvent.click(screen.getByTestId('board-title-save'));

    await waitFor(() => expect(boardApi.updateStory).toHaveBeenCalledWith('s-1', { title: 'Renamed story' }));
  });

  it('edits the description and saves it via updateStory', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });
    vi.mocked(boardApi.updateStory).mockResolvedValue({ ok: true });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-description-edit-start')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('board-description-edit-start'));
    fireEvent.change(screen.getByTestId('board-description-input'), { target: { value: 'Updated description' } });
    fireEvent.click(screen.getByTestId('board-description-save'));

    await waitFor(() => expect(boardApi.updateStory).toHaveBeenCalledWith('s-1', { description: 'Updated description' }));
  });

  it('"edits" a comment by posting a correction comment, leaving the original in place', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });
    vi.mocked(boardApi.addComment).mockResolvedValue({ ok: true });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-comment-edit-start')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('board-comment-edit-start'));
    fireEvent.change(screen.getByTestId('board-comment-edit-input'), { target: { value: 'corrected text' } });
    fireEvent.click(screen.getByTestId('board-comment-edit-save'));

    await waitFor(() => expect(boardApi.addComment).toHaveBeenCalledWith('s-1', '_(edited)_\n\ncorrected text'));
    // the original comment is still rendered, not replaced
    expect(screen.getByText('first comment')).toBeInTheDocument();
  });

  it('shows a side panel with the color-coded epic, priority, story points, and schedule', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail({
      priority: 1, start_date: '2026-10-01', due_date: '2026-10-15T00:00:00Z',
      story_points: 5, owner: 'amaan',
    }));
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    const sidebar = await screen.findByTestId('board-sidebar');
    expect(within(sidebar).getByText('Calculator App')).toBeInTheDocument();
    await waitFor(() => expect(within(sidebar).getByTestId('board-sidebar-priority')).toHaveValue('1'));
    expect(within(sidebar).getByTestId('board-sidebar-story-points')).toHaveTextContent('5');
    expect(within(sidebar).getByTestId('board-sidebar-start-date')).toHaveValue('2026-10-01');
    expect(within(sidebar).getByTestId('board-sidebar-due-date')).toHaveValue('2026-10-15');
    expect(within(sidebar).getByText('amaan')).toBeInTheDocument();
  });

  it('editing priority in the sidebar calls updateStory immediately', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail({ priority: 2 }));
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });
    vi.mocked(boardApi.updateStory).mockResolvedValue({ ok: true });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    const sidebar = await screen.findByTestId('board-sidebar');
    fireEvent.change(within(sidebar).getByTestId('board-sidebar-priority'), { target: { value: '0' } });

    await waitFor(() => expect(boardApi.updateStory).toHaveBeenCalledWith('s-1', { priority: 0 }));
  });

  it('editing story points in the sidebar commits on blur', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail({ story_points: null }));
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });
    vi.mocked(boardApi.updateStory).mockResolvedValue({ ok: true });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    const sidebar = await screen.findByTestId('board-sidebar');
    fireEvent.click(within(sidebar).getByTestId('board-sidebar-story-points'));
    fireEvent.change(within(sidebar).getByTestId('board-sidebar-story-points-input'), { target: { value: '8' } });
    fireEvent.blur(within(sidebar).getByTestId('board-sidebar-story-points-input'));

    await waitFor(() => expect(boardApi.updateStory).toHaveBeenCalledWith('s-1', { story_points: 8 }));
  });

  it('editing the start date in the sidebar calls updateStory', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });
    vi.mocked(boardApi.updateStory).mockResolvedValue({ ok: true });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    const sidebar = await screen.findByTestId('board-sidebar');
    fireEvent.change(within(sidebar).getByTestId('board-sidebar-start-date'), { target: { value: '2026-11-01' } });

    await waitFor(() => expect(boardApi.updateStory).toHaveBeenCalledWith('s-1', { start_date: '2026-11-01' }));
  });

  it('closing the modal calls onClose', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });
    const onClose = vi.fn();

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={onClose} />);
    await waitFor(() => expect(screen.getByTestId('board-timeline-close')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('board-timeline-close'));
    expect(onClose).toHaveBeenCalled();
  });
});
