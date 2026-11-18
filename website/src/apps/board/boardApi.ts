/**
 * Story Board API client - board is a builtin app like any other, so its
 * routes live under `/api/apps/board/*` (registered via this app's own
 * `backend.routes` manifest hook - see
 * `apps/builtins/board/backend/routes.py:register_routes`), same convention
 * every other builtin app's own backend uses (e.g. issue_radar's
 * `/api/apps/issue-radar/*`). A dedicated fetch wrapper rather than the
 * shared `api` client object, same as workflows uses for its own
 * `/api/workflows/*` routes - this module's shape predates that shared
 * client and there has been no reason to migrate it.
 */

const CORE_API_BASE = '/api/apps/board'

async function coreGet<T>(path: string): Promise<T> {
  const r = await fetch(`${CORE_API_BASE}${path}`, { credentials: 'same-origin' })
  if (!r.ok) throw new Error(`GET ${path} → ${r.status}`)
  return r.json() as Promise<T>
}

async function corePost<T>(path: string, body?: unknown, method: 'POST' | 'DELETE' | 'PUT' = 'POST'): Promise<T> {
  const r = await fetch(`${CORE_API_BASE}${path}`, {
    method,
    credentials: 'same-origin',
    ...(body !== undefined
      ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
      : {}),
  })
  if (!r.ok) {
    const errBody = await r.json().catch(() => null)
    throw new Error(errBody?.error || `${method} ${path} → ${r.status}`)
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
  recommendation: string | null
  start_date: string | null
  due_date: string | null
  rank: number | null
  story_points: number | null
  depends_on: string[]
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
  start_date: string | null
  due_date: string | null
  rank: number | null
  story_points: number | null
  depends_on: string[]
}

export interface UpdateStoryFields {
  title?: string
  description?: string
  start_date?: string
  due_date?: string
  rank?: number
  priority?: number
  story_points?: number
}

/**
 * One configured project this board schedules specs from — `GET`/`PUT
 * /config`'s `project_paths` entries (see `apps/builtins/board/backend` for
 * the authoritative shape; this mirrors it, not the other way round). A
 * `beads` entry only ever needs `path`; a `jira` entry adds the fields that
 * locate the Jira project. Jira CREDENTIALS are never part of this shape -
 * they're resolved server-side from `jira_host` alone against
 * `dashboard.jira_hosts`, so this config is safe to read/write unauthenticated
 * the same way every other per-app config is.
 */
export interface ProjectConfigEntry {
  type: 'beads' | 'jira'
  path: string
  jira_host?: string
  jira_project_key?: string
  story_points_field?: string
  dependency_link_type?: string
}

export const boardApi = {
  phases: () => coreGet<{ phases: PhaseDef[] }>('/phases'),
  stories: () => coreGet<{ stories: BoardStory[]; project_paths: string[] }>('/stories'),
  history: (storyId: string) => coreGet<StoryHistory>(`/stories/${encodeURIComponent(storyId)}/history`),
  detail: (storyId: string) => coreGet<StoryDetail>(`/stories/${encodeURIComponent(storyId)}/detail`),
  run: (storyId: string) => corePost<{ ok: boolean; task_id: string }>(`/stories/${encodeURIComponent(storyId)}/run`),
  advance: (storyId: string, mrUrl: string, outcome: 'approved' | 'changes_requested' = 'approved') =>
    corePost<{ ok: boolean; task_id: string }>(`/stories/${encodeURIComponent(storyId)}/advance`, {
      mr_url: mrUrl, outcome,
    }),
  resumeImplementation: (storyId: string) =>
    corePost<{ ok: boolean; task_id: string }>(`/stories/${encodeURIComponent(storyId)}/resume-implementation`),
  resumePlanning: (storyId: string) =>
    corePost<{ ok: boolean; task_id: string }>(`/stories/${encodeURIComponent(storyId)}/resume-planning`),
  addComment: (storyId: string, text: string) =>
    corePost<{ ok: boolean }>(`/stories/${encodeURIComponent(storyId)}/comments`, { text }),
  updateStory: (storyId: string, fields: UpdateStoryFields) =>
    corePost<{ ok: boolean }>(`/stories/${encodeURIComponent(storyId)}/update`, fields),
  addDependency: (storyId: string, dependsOnId: string) =>
    corePost<{ ok: boolean }>(`/stories/${encodeURIComponent(storyId)}/dependencies`, { depends_on_id: dependsOnId }),
  removeDependency: (storyId: string, dependsOnId: string) =>
    corePost<{ ok: boolean }>(
      `/stories/${encodeURIComponent(storyId)}/dependencies/${encodeURIComponent(dependsOnId)}`,
      undefined, 'DELETE',
    ),
  // Generic per-app config (apps_routes_listing.handle_app_config): `{}` when
  // nothing has been saved yet, so callers default `project_paths` to `[]`
  // rather than assuming the key is always present.
  getConfig: () => coreGet<{ project_paths?: ProjectConfigEntry[] }>('/config'),
  // Whole-array replace, matching the backend's whole-file-write contract -
  // callers must read the current list, mutate it client-side, and PUT the
  // full updated array back rather than sending a partial diff.
  putConfig: (projectPaths: ProjectConfigEntry[]) =>
    corePost<{ ok: boolean }>('/config', { project_paths: projectPaths }, 'PUT'),
}
