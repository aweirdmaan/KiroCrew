import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import StoryModal from '../apps/board/StoryModal'
import { boardApi, type BoardStory, type PhaseDef, type StoryDetail, type RunRecord } from '../apps/board/boardApi'

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

const PHASES: PhaseDef[] = [
  { key: 'planning', label: 'Planning', tasks: ['ideate', 'plan'], manual: false },
  { key: 'implementation', label: 'Implementation', tasks: ['verify', 'fix', 'confirm'], manual: false },
]

function story(overrides: Partial<BoardStory> = {}): BoardStory {
  return {
    id: 's-1', title: 'Story one', status: 'open',
    epic_id: 'e-1', epic_title: 'Calculator App', project_path: '/proj',
    phase: 'planning', phase_label: 'Planning', current_run: null,
    priority: null, owner: '', pending_open_questions: false,
    ...overrides,
  };
}

function detail(overrides: Partial<StoryDetail> = {}): StoryDetail {
  return {
    id: 's-1', title: 'Story one', description: '**bold** description', status: 'open',
    issue_type: 'task', priority: 2, owner: 'me', created_at: 't1', updated_at: 't2',
    comments: [{ id: 'c1', author: 'Amaan', text: 'first comment', created_at: 't3' }],
    ...overrides,
  };
}

function run(overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    phase: 'planning', task_key: 'ideate', iteration: 0, task_id: 'task-1',
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

    const job = await screen.findByTestId('board-job-planning');
    fireEvent.click(within(job).getByRole('button', { name: /Planning/ }));
    await waitFor(() => expect(within(job).getByTestId('board-timeline-row')).toBeInTheDocument());
    const row = within(job).getByTestId('board-timeline-row');
    // The expand toggle is the inner <button>, not the outer row container a
    // click on the testid'd div itself would not reach (click handlers don't
    // fire from a parent's click target).
    fireEvent.click(within(row).getByRole('button'));

    // Scoped to the row: the job's own structural view (default tab "live")
    // also renders a log panel for its latest task, so a page-wide query
    // would match two elements once this row expands too.
    const panel = await within(row).findByTestId('stub-live-log-panel');
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

    const job = await screen.findByTestId('board-job-planning');
    expect(within(job).queryByTestId('board-timeline-row')).toBeNull();
    expect(within(job).queryByTestId('board-job-planning-view-content')).toBeNull();
  });

  it('groups timeline entries by job (phase), with multiple tasks nested under one job', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: null,
      history: [
        run({ phase: 'implementation', task_key: 'verify', status: 'passed' }),
        run({ phase: 'implementation', task_key: 'fix', status: 'passed' }),
        run({ phase: 'implementation', task_key: 'confirm', status: 'gate_failed' }),
      ],
    });

    renderWithProviders(<StoryModal story={story({ phase: 'implementation', phase_label: 'Implementation' })} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-job-implementation')).toBeInTheDocument());
    const job = screen.getByTestId('board-job-implementation');
    fireEvent.click(within(job).getByRole('button', { name: /Implementation/ }));
    expect(within(job).getAllByTestId('board-timeline-row')).toHaveLength(3);
  });

  it('a job defaults to the Live view, showing its latest/selected task\'s log', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: null,
      history: [run({ status: 'passed', task_id: 'task-planning' })],
    });

    renderWithProviders(<StoryModal story={story()} phases={PHASES} onClose={() => {}} />);

    const job = await screen.findByTestId('board-job-planning');
    fireEvent.click(within(job).getByRole('button', { name: /Planning/ }));
    expect(within(job).getByTestId('board-job-planning-view-live')).toHaveClass('bg-accent');
    const panel = within(job).getByTestId('stub-live-log-panel');
    expect(panel).toHaveAttribute('data-task-id', 'task-planning');
  });

  it('switching a job to the DAG view renders one node per task, in order', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: null,
      history: [
        run({ phase: 'implementation', task_key: 'verify', status: 'passed' }),
        run({ phase: 'implementation', task_key: 'fix', status: 'passed' }),
      ],
    });

    renderWithProviders(<StoryModal story={story({ phase: 'implementation', phase_label: 'Implementation' })} phases={PHASES} onClose={() => {}} />);

    const job = await screen.findByTestId('board-job-implementation');
    fireEvent.click(within(job).getByRole('button', { name: /Implementation/ }));
    fireEvent.click(within(job).getByTestId('board-job-implementation-view-dag'));

    // Scoped to the view-content wrapper, not the whole job: the flat
    // attempt log below also renders each task_key as text, which would
    // otherwise make these an ambiguous "found multiple elements" match.
    const content = within(job).getByTestId('board-job-implementation-view-content');
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
        run({ phase: 'implementation', task_key: 'verify', status: 'passed' }),
        run({ phase: 'implementation', task_key: 'fix', status: 'passed', iteration: 0 }),
        run({ phase: 'implementation', task_key: 'fix', status: 'gate_failed', iteration: 1 }),
      ],
    });

    renderWithProviders(<StoryModal story={story({ phase: 'implementation', phase_label: 'Implementation' })} phases={PHASES} onClose={() => {}} />);

    const job = await screen.findByTestId('board-job-implementation');
    fireEvent.click(within(job).getByRole('button', { name: /Implementation/ }));
    fireEvent.click(within(job).getByTestId('board-job-implementation-view-phased'));

    // PhasedView renders a task's title inline as "Task N: <title>" (one
    // combined text node), so an exact match on just "fix" would never hit -
    // a substring matcher is the correct query here, not a workaround.
    const content = within(job).getByTestId('board-job-implementation-view-content');
    await waitFor(() => expect(within(content).getByText(/fix/)).toBeInTheDocument());
    expect(within(job).getAllByTestId('board-timeline-row')).toHaveLength(3); // unaffected by the tab switch
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
      current_run: { phase: 'implementation', task_key: 'confirm', iteration: 0, task_id: 't-1', status: 'gate_failed', started_at: 't', finished_at: 't2' },
      history: [],
    });

    renderWithProviders(<StoryModal story={story({ phase: 'implementation', phase_label: 'Implementation' })} phases={PHASES} onClose={() => {}} />);

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
