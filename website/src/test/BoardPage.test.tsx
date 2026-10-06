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
  { key: 'grooming', label: 'Grooming', tasks: ['ideate'], gate: false, manual: false },
  { key: 'planning', label: 'Planning', tasks: ['plan'], gate: false, manual: false },
  { key: 'review', label: 'Review', tasks: [], gate: false, manual: true },
  { key: 'done', label: 'Done', tasks: [], gate: false, manual: true },
]

function story(overrides: Partial<BoardStory> = {}): BoardStory {
  return {
    id: 's-1', title: 'Story one', status: 'open',
    epic_id: 'e-1', epic_title: 'Calculator App', project_path: '/proj',
    phase: null, phase_label: 'Backlog', current_run: null,
    ...overrides,
  };
}

describe('BoardPage', () => {
  it('renders backlog plus every phase column, with stories grouped by phase', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story(), story({ id: 's-2', phase: 'planning', phase_label: 'Planning' })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<BoardPage />);

    // board-column-backlog alone is a weak signal (it renders unconditionally,
    // before the phases query resolves) - wait for a phase-derived column
    // instead, which only exists once both queries have actually loaded.
    await waitFor(() => expect(screen.getByTestId('board-column-grooming')).toBeInTheDocument());
    expect(screen.getByTestId('board-column-backlog')).toBeInTheDocument();
    expect(screen.getByTestId('board-column-planning')).toBeInTheDocument();
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
    vi.mocked(boardApi.advance).mockResolvedValue({ ok: true, task_id: 'task-2' });

    renderWithProviders(<BoardPage />);
    await waitFor(() => expect(screen.getByTestId('board-advance-s-1')).toBeInTheDocument());
    expect(screen.queryByTestId('board-run-s-1')).toBeNull();

    fireEvent.click(screen.getByTestId('board-advance-s-1'));
    await waitFor(() => expect(boardApi.advance).toHaveBeenCalledWith('s-1'));
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
});
