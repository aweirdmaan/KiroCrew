/**
 * Story Board API client — mirrors apps/workflows/WorkflowsRuns.tsx's own
 * `coreGet`/`corePost` pattern: these are gateway CORE routes (`/api/board/*`,
 * registered in dashboard/handlers/board.py), not proxied through a builtin
 * app's own `/apps/{name}/api/*` surface, so the shared `api` client object
 * (api/client.ts) isn't the right fit - that file's methods assume the
 * latter. A dedicated fetch wrapper, same as workflows uses for its own
 * `/api/workflows/*` routes.
 */

const CORE_API_BASE = '/api/board'

async function coreGet<T>(path: string): Promise<T> {
  const r = await fetch(`${CORE_API_BASE}${path}`, { credentials: 'same-origin' })
  if (!r.ok) throw new Error(`GET ${path} → ${r.status}`)
  return r.json() as Promise<T>
}

async function corePost<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`${CORE_API_BASE}${path}`, {
    method: 'POST',
    credentials: 'same-origin',
    ...(body !== undefined
      ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
      : {}),
  })
  if (!r.ok) {
    const errBody = await r.json().catch(() => null)
    throw new Error(errBody?.error || `POST ${path} → ${r.status}`)
  }
  return r.json() as Promise<T>
}

export interface PhaseDef {
  key: string
  label: string
  tasks: string[]
  manual: boolean
}

export interface RunRecord {
  phase: string
  task_key: string
  iteration: number
  task_id: string | null
  status: string
  started_at: string
  finished_at: string | null
}

export interface BoardStory {
  id: string
  title: string
  status: string
  epic_id: string
  epic_title: string
  project_path: string
  phase: string | null
  phase_label: string
  current_run: RunRecord | null
  priority: number | null
  owner: string
  pending_open_questions: boolean
}

export interface StoryHistory {
  current_run: RunRecord | null
  history: RunRecord[]
}

export interface BeadsComment {
  id: string
  author: string
  text: string
  created_at: string
}

export interface StoryDetail {
  id: string
  title: string
  description: string
  status: string
  issue_type: string
  priority: number | null
  owner: string
  created_at: string
  updated_at: string
  comments: BeadsComment[]
}

export const boardApi = {
  phases: () => coreGet<{ phases: PhaseDef[] }>('/phases'),
  stories: () => coreGet<{ stories: BoardStory[]; project_paths: string[] }>('/stories'),
  history: (storyId: string) => coreGet<StoryHistory>(`/stories/${encodeURIComponent(storyId)}/history`),
  detail: (storyId: string) => coreGet<StoryDetail>(`/stories/${encodeURIComponent(storyId)}/detail`),
  run: (storyId: string) => corePost<{ ok: boolean; task_id: string }>(`/stories/${encodeURIComponent(storyId)}/run`),
  advance: (storyId: string, mrUrl: string) =>
    corePost<{ ok: boolean; task_id: string }>(`/stories/${encodeURIComponent(storyId)}/advance`, { mr_url: mrUrl }),
  addComment: (storyId: string, text: string) =>
    corePost<{ ok: boolean }>(`/stories/${encodeURIComponent(storyId)}/comments`, { text }),
  updateStory: (storyId: string, fields: { title?: string; description?: string }) =>
    corePost<{ ok: boolean }>(`/stories/${encodeURIComponent(storyId)}/update`, fields),
}
