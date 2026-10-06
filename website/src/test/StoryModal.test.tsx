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
  { key: 'grooming', label: 'Grooming', tasks: ['ideate'], gate: false, manual: false },
  { key: 'verification', label: 'Verification', tasks: ['verify', 'fix', 'confirm'], gate: true, manual: false },
]

function story(overrides: Partial<BoardStory> = {}): BoardStory {
  return {
    id: 's-1', title: 'Story one', status: 'open',
    epic_id: 'e-1', epic_title: 'Calculator App', project_path: '/proj',
    phase: 'grooming', phase_label: 'Grooming', current_run: null,
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
    phase: 'grooming', task_key: 'ideate', iteration: 0, task_id: 'task-1',
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

    await waitFor(() => expect(screen.getByTestId('board-timeline-row')).toBeInTheDocument());
    // The expand toggle is the inner <button>, not the outer row container a
    // click on the testid'd div itself would not reach (click handlers don't
    // fire from a parent's click target).
    fireEvent.click(within(screen.getByTestId('board-timeline-row')).getByRole('button'));

    const panel = await screen.findByTestId('stub-live-log-panel');
    expect(panel).toHaveAttribute('data-active', 'true');
    expect(panel).toHaveAttribute('data-task-id', 'task-1');
  });

  it('groups timeline entries by job (phase), with multiple tasks nested under one job', async () => {
    vi.mocked(boardApi.detail).mockResolvedValue(detail());
    vi.mocked(boardApi.history).mockResolvedValue({
      current_run: null,
      history: [
        run({ phase: 'verification', task_key: 'verify', status: 'passed' }),
        run({ phase: 'verification', task_key: 'fix', status: 'passed' }),
        run({ phase: 'verification', task_key: 'confirm', status: 'gate_failed' }),
      ],
    });

    renderWithProviders(<StoryModal story={story({ phase: 'verification', phase_label: 'Verification' })} phases={PHASES} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('board-job-verification')).toBeInTheDocument());
    const job = screen.getByTestId('board-job-verification');
    expect(within(job).getAllByTestId('board-timeline-row')).toHaveLength(3);
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
