import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import TimelinePage, { computeRank } from '../apps/board/TimelinePage'
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
      updateStory: vi.fn(),
      addDependency: vi.fn(),
      removeDependency: vi.fn(),
    },
  }
})

const PHASES: PhaseDef[] = [
  { key: 'pipeline', label: 'Pipeline', tasks: ['ideate', 'plan', 'confirm_plan'], manual: false },
  { key: 'review', label: 'Review', tasks: [], manual: true },
  { key: 'done', label: 'Done', tasks: [], manual: true },
]

function story(overrides: Partial<BoardStory> = {}): BoardStory {
  return {
    id: 's-1', title: 'Story one', status: 'open',
    epic_id: 'e-1', epic_title: 'Calculator App', project_path: '/proj',
    phase: null, phase_label: 'Backlog', current_run: null,
    priority: null, owner: '', pending_open_questions: false,
    start_date: null, due_date: null, rank: null, depends_on: [],
    ...overrides,
  };
}

describe('TimelinePage', () => {
  it('shows a loading skeleton until phases and stories both resolve, then shows the real timeline', async () => {
    let resolveStories: (() => void) | undefined;
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockReturnValue(
      new Promise(resolve => { resolveStories = () => resolve({ stories: [story()], project_paths: ['/proj'] }); }),
    );

    renderWithProviders(<TimelinePage />);

    expect(screen.getByTestId('timeline-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('timeline-header')).toBeNull();

    resolveStories?.();

    await waitFor(() => expect(screen.getByTestId('timeline-header')).toBeInTheDocument());
    expect(screen.queryByTestId('timeline-loading')).toBeNull();
  });

  it('groups stories by epic and renders a row per story', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story(), story({ id: 's-2', epic_id: 'e-2', epic_title: 'Other Epic' })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<TimelinePage />);

    await waitFor(() => expect(screen.getByTestId('timeline-epic-e-1')).toBeInTheDocument());
    expect(screen.getByTestId('timeline-epic-e-2')).toBeInTheDocument();
    expect(screen.getByTestId('timeline-row-s-1')).toBeInTheDocument();
    expect(screen.getByTestId('timeline-row-s-2')).toBeInTheDocument();
  });

  it('sorts stories within an epic by rank', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [
        story({ id: 's-high-rank', title: 'Second', rank: 10 }),
        story({ id: 's-low-rank', title: 'First', rank: 1 }),
      ],
      project_paths: ['/proj'],
    });

    renderWithProviders(<TimelinePage />);

    const epic = await screen.findByTestId('timeline-epic-e-1');
    const rows = epic.querySelectorAll('[data-testid^="timeline-row-"]');
    expect(rows[0].getAttribute('data-testid')).toBe('timeline-row-s-low-rank');
    expect(rows[1].getAttribute('data-testid')).toBe('timeline-row-s-high-rank');
  });

  it('an unscheduled story shows a "Schedule" affordance instead of a bar', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({ stories: [story()], project_paths: ['/proj'] });

    renderWithProviders(<TimelinePage />);

    await waitFor(() => expect(screen.getByTestId('timeline-schedule-s-1')).toBeInTheDocument());
    expect(screen.queryByTestId('timeline-bar-s-1')).toBeNull();
  });

  it('clicking "Schedule" sets a default date range via updateStory', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({ stories: [story()], project_paths: ['/proj'] });
    vi.mocked(boardApi.updateStory).mockResolvedValue({ ok: true });

    renderWithProviders(<TimelinePage />);

    await waitFor(() => expect(screen.getByTestId('timeline-schedule-s-1')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('timeline-schedule-s-1'));

    await waitFor(() => expect(boardApi.updateStory).toHaveBeenCalledWith(
      's-1', expect.objectContaining({ start_date: expect.any(String), due_date: expect.any(String) }),
    ));
  });

  it('a scheduled story renders a bar instead of the schedule affordance', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({ start_date: '2026-10-01', due_date: '2026-10-05' })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<TimelinePage />);

    await waitFor(() => expect(screen.getByTestId('timeline-bar-s-1')).toBeInTheDocument());
    expect(screen.queryByTestId('timeline-schedule-s-1')).toBeNull();
  });

  it('dragging a bar body reschedules it via updateStory', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({ start_date: '2026-10-01', due_date: '2026-10-05' })],
      project_paths: ['/proj'],
    });
    vi.mocked(boardApi.updateStory).mockResolvedValue({ ok: true });

    renderWithProviders(<TimelinePage />);
    const bar = await screen.findByTestId('timeline-bar-s-1');

    fireEvent.pointerDown(bar, { clientX: 100 });
    fireEvent(document, new PointerEvent('pointermove', { clientX: 100 + 28 * 3, bubbles: true }));
    fireEvent(document, new PointerEvent('pointerup', { clientX: 100 + 28 * 3, bubbles: true }));

    await waitFor(() => expect(boardApi.updateStory).toHaveBeenCalledWith(
      's-1', { start_date: '2026-10-04', due_date: '2026-10-08' },
    ));
  });

  it('the dropped position sticks right away, before the server write resolves', async () => {
    // Regression: the old version reset the drag's visual offset to zero on
    // pointerup and waited for boardApi.updateStory's round trip (a real
    // `bd` subprocess call, not instant) before the bar reflected its new
    // date - which read as "nothing happened" for however long that took.
    // An optimistic cache update should make the new position stick
    // immediately, independent of when (or whether yet) the write settles.
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({ start_date: '2026-10-01', due_date: '2026-10-05' })],
      project_paths: ['/proj'],
    });
    let resolveUpdate: (() => void) | undefined;
    vi.mocked(boardApi.updateStory).mockReturnValue(
      new Promise(resolve => { resolveUpdate = () => resolve({ ok: true }); }),
    );

    renderWithProviders(<TimelinePage />);
    const bar = await screen.findByTestId('timeline-bar-s-1');
    const leftBefore = bar.style.left;

    fireEvent.pointerDown(bar, { clientX: 100 });
    fireEvent(document, new PointerEvent('pointermove', { clientX: 100 + 28 * 3, bubbles: true }));
    fireEvent(document, new PointerEvent('pointerup', { clientX: 100 + 28 * 3, bubbles: true }));

    // The write is still pending (resolveUpdate hasn't been called), but the
    // bar should already show its new position.
    await waitFor(() => expect(screen.getByTestId('timeline-bar-s-1').style.left).not.toBe(leftBefore));

    resolveUpdate?.();
  });

  it('a needs-human story is flagged in its row', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({ pending_open_questions: true, start_date: '2026-10-01', due_date: '2026-10-05' })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<TimelinePage />);

    await waitFor(() => expect(screen.getByTestId('timeline-needs-human-s-1')).toBeInTheDocument());
  });

  it('draws a dependency arrow between two stories under the same epic', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [
        story({ id: 's-1', start_date: '2026-10-01', due_date: '2026-10-05' }),
        story({ id: 's-2', title: 'Story two', start_date: '2026-10-06', due_date: '2026-10-10', depends_on: ['s-1'] }),
      ],
      project_paths: ['/proj'],
    });

    renderWithProviders(<TimelinePage />);

    const svg = await screen.findByTestId('timeline-dependency-arrows');
    expect(svg.querySelectorAll('path').length).toBe(2); // 1 arrow line + 1 arrowhead marker def
  });

  it('clicking a row opens the story modal', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({ stories: [story()], project_paths: ['/proj'] });
    vi.mocked(boardApi.history).mockResolvedValue({ current_run: null, history: [] });
    vi.mocked(boardApi.detail).mockResolvedValue({
      id: 's-1', title: 'Story one', description: 'why/what', status: 'open',
      issue_type: 'task', priority: 2, owner: 'me', created_at: '', updated_at: '',
      comments: [], start_date: null, due_date: null, rank: null, depends_on: [],
    });

    renderWithProviders(<TimelinePage />);
    const row = await screen.findByTestId('timeline-row-s-1');
    fireEvent.click(row.firstElementChild as Element);

    await waitFor(() => expect(screen.getByTestId('board-timeline-drawer')).toBeInTheDocument());
  });

  it('clicking "Run next job" calls boardApi.run', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({ stories: [story()], project_paths: ['/proj'] });
    vi.mocked(boardApi.run).mockResolvedValue({ ok: true, task_id: 'task-1' });

    renderWithProviders(<TimelinePage />);
    await waitFor(() => expect(screen.getByTestId('timeline-run-s-1')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('timeline-run-s-1'));

    await waitFor(() => expect(boardApi.run).toHaveBeenCalledWith('s-1'));
  });

  it('a Review-phase story shows "Mark reviewed" and collects an MR URL', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story({ phase: 'review', phase_label: 'Review' })],
      project_paths: ['/proj'],
    });
    vi.mocked(boardApi.advance).mockResolvedValue({ ok: true, task_id: 'task-2' });

    renderWithProviders(<TimelinePage />);
    await waitFor(() => expect(screen.getByTestId('timeline-advance-s-1')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('timeline-advance-s-1'));

    const input = await screen.findByTestId('timeline-advance-mr-url-s-1');
    fireEvent.change(input, { target: { value: 'https://example.com/mr/9' } });
    fireEvent.click(screen.getByTestId('timeline-advance-confirm-s-1'));

    await waitFor(() => expect(boardApi.advance).toHaveBeenCalledWith('s-1', 'https://example.com/mr/9'));
  });

  it('collapsing an epic hides its rows without losing the other epic', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({
      stories: [story(), story({ id: 's-2', epic_id: 'e-2', epic_title: 'Other Epic' })],
      project_paths: ['/proj'],
    });

    renderWithProviders(<TimelinePage />);
    await waitFor(() => expect(screen.getByTestId('timeline-row-s-1')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('timeline-epic-toggle-e-1'));

    expect(screen.queryByTestId('timeline-row-s-1')).toBeNull();
    expect(screen.getByTestId('timeline-row-s-2')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('timeline-epic-toggle-e-1'));
    expect(screen.getByTestId('timeline-row-s-1')).toBeInTheDocument();
  });

  it('toggling zoom switches between week and quarter', async () => {
    vi.mocked(boardApi.phases).mockResolvedValue({ phases: PHASES });
    vi.mocked(boardApi.stories).mockResolvedValue({ stories: [story()], project_paths: ['/proj'] });

    renderWithProviders(<TimelinePage />);
    await waitFor(() => expect(screen.getByTestId('timeline-zoom-week')).toHaveClass('bg-accent'));
    fireEvent.click(screen.getByTestId('timeline-zoom-quarter'));
    expect(screen.getByTestId('timeline-zoom-quarter')).toHaveClass('bg-accent');
    expect(screen.getByTestId('timeline-zoom-week')).not.toHaveClass('bg-accent');
  });
});

describe('computeRank', () => {
  it('splits the midpoint between two neighboring ranks', () => {
    const ranks = new Map([['a', 1], ['moved', null], ['b', 3]]);
    expect(computeRank(['a', 'moved', 'b'], 'moved', ranks)).toBe(2);
  });

  it('gives a fresh higher band when moved to the end', () => {
    const ranks = new Map([['a', 5], ['moved', null]]);
    expect(computeRank(['a', 'moved'], 'moved', ranks)).toBe(1005);
  });

  it('gives a fresh lower band when moved to the start', () => {
    const ranks = new Map([['moved', null], ['b', 5]]);
    expect(computeRank(['moved', 'b'], 'moved', ranks)).toBe(-995);
  });

  it('defaults to 0 when the list has only the moved item', () => {
    const ranks = new Map([['moved', null]]);
    expect(computeRank(['moved'], 'moved', ranks)).toBe(0);
  });
});
