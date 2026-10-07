import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import BoardPage from '../apps/board/BoardPage'
import { boardApi, type BoardStory, type PhaseDef } from '../apps/board/boardApi'

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

const PHASES: PhaseDef[] = [
  { key: 'planning', label: 'Planning', tasks: ['ideate', 'plan'], manual: false },
  { key: 'implementation', label: 'Implementation', tasks: ['confirm_plan', 'approval_check', 'implement', 'verify', 'fix', 'confirm', 'pr', 'retro'], manual: false },
  { key: 'review', label: 'Review', tasks: [], manual: true },
  { key: 'done', label: 'Done', tasks: [], manual: true },
]

function story(overrides: Partial<BoardStory> = {}): BoardStory {
  return {
    id: 's-1', title: 'Story one', status: 'open',
    epic_id: 'e-1', epic_title: 'Calculator App', project_path: '/proj',
    phase: null, phase_label: 'Backlog', current_run: null,
    priority: null, owner: '', pending_open_questions: false,
    ...overrides,
  };
}

describe('BoardPage', () => {
  it('renders backlog plus every phase column, with stories grouped by phase', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story(), story({ id: 's-2', phase: 'implementation', phase_label: 'Implementation' })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<BoardPage />);

    // board-column-backlog alone is a weak signal (it renders unconditionally,
    // before the phases query resolves) - wait for a phase-derived column
    // instead, which only exists once both queries have actually loaded.
    await waitFor(() => expect(screen.getByTestId('board-column-planning')).toBeInTheDocument());
    expect(screen.getByTestId('board-column-backlog')).toBeInTheDocument();
    expect(screen.getByTestId('board-column-implementation')).toBeInTheDocument();
    expect(screen.getByTestId('board-column-review')).toBeInTheDocument();
    expect(screen.getByTestId('board-column-done')).toBeInTheDocument();
    expect(screen.getByTestId('board-card-s-1')).toBeInTheDocument();
    expect(screen.getByTestId('board-card-s-2')).toBeInTheDocument();
  });

  it('shows a "no projects configured" notice when nothing is set up', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({ stories: [], project_paths: [] });

    renderWithProviders(<BoardPage />);

    await waitFor(() => expect(screen.getByText(/knowledge.beads_project_paths/)).toBeInTheDocument());
  });

  it('clicking "Run next job" calls boardApi.run for that story', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({ stories: [story()], project_paths: ['/proj'] });
    vi.mocked(boardApi.run).mockResolvedValue({ ok: true, task_id: 'task-1' });

    renderWithProviders(<BoardPage />);
    await waitFor(() => expect(screen.getByTestId('board-run-s-1')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('board-run-s-1'));

    await waitFor(() => expect(boardApi.run).toHaveBeenCalledWith('s-1'));
  });

  it('a manual-phase story in Review shows "Mark reviewed" instead of "Run next job"', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({ phase: 'review', phase_label: 'Review' })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<BoardPage />);
    await waitFor(() => expect(screen.getByTestId('board-advance-s-1')).toBeInTheDocument());
    expect(screen.queryByTestId('board-run-s-1')).toBeNull();
  });

  it('marking reviewed collects an MR URL and calls boardApi.advance with it', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({ phase: 'review', phase_label: 'Review' })],
      project_paths: ['/proj'],
    });
    vi.mocked(boardApi.advance).mockResolvedValue({ ok: true, task_id: 'task-2' });

    renderWithProviders(<BoardPage />);
    await waitFor(() => expect(screen.getByTestId('board-advance-s-1')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('board-advance-s-1'));

    const input = await screen.findByTestId('board-advance-mr-url-s-1');
    fireEvent.change(input, { target: { value: 'https://example.com/mr/42' } });
    fireEvent.click(screen.getByTestId('board-advance-confirm-s-1'));

    await waitFor(() => expect(boardApi.advance).toHaveBeenCalledWith('s-1', 'https://example.com/mr/42'));
  });

  it('cancelling the mark-reviewed form hides it again without calling advance', async () => {
    vi.mocked(boardApi.advance).mockClear();
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({ phase: 'review', phase_label: 'Review' })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<BoardPage />);
    await waitFor(() => expect(screen.getByTestId('board-advance-s-1')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('board-advance-s-1'));
    await screen.findByTestId('board-advance-mr-url-s-1');
    fireEvent.click(screen.getByTestId('board-advance-cancel-s-1'));

    expect(screen.queryByTestId('board-advance-mr-url-s-1')).toBeNull();
    expect(boardApi.advance).not.toHaveBeenCalled();
  });

  it('clicking a card opens the story modal', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({ stories: [story()], project_paths: ['/proj'] });
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });
    vi.mocked(boardApi.detail).mockResolvedValue({
      id: 's-1', title: 'Story one', description: 'the why/what', status: 'open',
      issue_type: 'task', priority: 2, owner: 'me', created_at: '', updated_at: '',
      comments: [],
    });

    renderWithProviders(<BoardPage />);
    await waitFor(() => expect(screen.getByTestId('board-card-s-1')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('board-card-s-1'));

    await waitFor(() => expect(screen.getByTestId('board-timeline-drawer')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('board-timeline-close'));
    expect(screen.queryByTestId('board-timeline-drawer')).toBeNull();
  });

  it('shows the parent epic id, priority, and assignee on a card, but no lane pill for an idle story', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({ priority: 1, owner: 'amaan' })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<BoardPage />);
    const card = await screen.findByTestId('board-card-s-1');
    expect(screen.getByTestId('board-parent-s-1')).toHaveTextContent('e-1');
    expect(screen.getByTestId('board-priority-s-1')).toHaveTextContent('P1');
    expect(screen.getByTestId('board-owner-s-1')).toHaveTextContent('amaan');
    // no run is in progress or failed - nothing to flag beyond the column
    // the card already sits in, so there should be no extra status badge
    expect(card.querySelector('[class*="bg-muted"]')).toBeNull();
  });

  it('a running story still shows its status badge alongside priority/assignee', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({
        priority: 0, owner: 'amaan',
        current_run: { phase: 'planning', task_key: 'ideate', iteration: 0, task_id: 't-1', status: 'running', started_at: 't', finished_at: null },
      })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<BoardPage />);
    await screen.findByTestId('board-card-s-1');
    expect(screen.getByText('Running')).toBeInTheDocument();
    expect(screen.getByTestId('board-priority-s-1')).toHaveTextContent('P0');
  });

  it('a cancelled run shows a muted "Cancelled" badge, not an alarming one', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({
        current_run: { phase: 'planning', task_key: 'ideate', iteration: 0, task_id: 't-1', status: 'cancelled', started_at: 't', finished_at: 't2' },
      })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<BoardPage />);
    await screen.findByTestId('board-card-s-1');
    expect(screen.getByText('Cancelled')).toBeInTheDocument();
  });

  it('a missing (vanished) run is flagged as needing a human, same as a gate failure', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({
        current_run: { phase: 'planning', task_key: 'ideate', iteration: 0, task_id: 't-1', status: 'missing', started_at: 't', finished_at: 't2' },
      })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<BoardPage />);
    await screen.findByTestId('board-card-s-1');
    expect(screen.getByTestId('board-needs-human-s-1')).toHaveTextContent('Needs attention');
  });

  it('a gate_failed run is called out with the needs-human banner', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({
        current_run: { phase: 'implementation', task_key: 'confirm_plan', iteration: 0, task_id: 't-1', status: 'gate_failed', started_at: 't', finished_at: 't2' },
      })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<BoardPage />);
    await screen.findByTestId('board-card-s-1');
    expect(screen.getByTestId('board-needs-human-s-1')).toHaveTextContent('Needs attention');
  });

  it('a story with unanswered open questions is called out with the needs-human banner', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({ phase: 'implementation', phase_label: 'Implementation', pending_open_questions: true })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<BoardPage />);
    await screen.findByTestId('board-card-s-1');
    expect(screen.getByTestId('board-needs-human-s-1')).toHaveTextContent('Answer needed');
  });

  it('a card in the manual Review column is called out as ready for review', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({ phase: 'review', phase_label: 'Review' })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<BoardPage />);
    await screen.findByTestId('board-card-s-1');
    expect(screen.getByTestId('board-needs-human-s-1')).toHaveTextContent('Ready for your review');
  });

  it('a card in the terminal Done column gets no needs-human banner', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({ phase: 'done', phase_label: 'Done' })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<BoardPage />);
    await screen.findByTestId('board-card-s-1');
    expect(screen.queryByTestId('board-needs-human-s-1')).toBeNull();
  });
});
