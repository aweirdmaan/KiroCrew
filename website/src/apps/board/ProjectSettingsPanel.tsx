/**
 * Project settings - add/edit/remove the project paths this Timeline
 * schedules specs from. Before this panel existed the only way to change
 * `project_paths` was a raw `PUT /api/apps/board/config` curl call (see this
 * app's own manifest description, and TimelinePage's "no projects configured"
 * hint) - there was no UI path at all.
 *
 * Whole-array replace, matching the backend's whole-file-write contract:
 * every add/edit/remove reads the CURRENT list from the config query, mutates
 * it client-side, and PUTs the full updated array back. There is no
 * per-field PATCH on this endpoint.
 *
 * Jira credentials are deliberately NOT a field here - they resolve
 * server-side from `jira_host` alone against `dashboard.jira_hosts`/
 * `JiraAuthEntry`, so this form only ever needs the host + project key that
 * LOCATE the project, never a token.
 */
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, ChevronDown, ChevronRight, Pencil, Plus, Trash2 } from 'lucide-react'
import Modal from '../../components/Modal'
import { Badge, Btn, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { boardApi, type ProjectConfigEntry } from './boardApi'
import { i18nT } from '../../i18n/t'

interface Draft {
  type: 'beads' | 'jira'
  path: string
  jiraHost: string
  jiraProjectKey: string
  storyPointsField: string
  dependencyLinkType: string
  advancedOpen: boolean
}

const EMPTY_DRAFT: Draft = {
  type: 'beads', path: '', jiraHost: '', jiraProjectKey: '', storyPointsField: '', dependencyLinkType: '', advancedOpen: false,
}

function draftFromEntry(entry: ProjectConfigEntry): Draft {
  return {
    type: entry.type,
    path: entry.path,
    jiraHost: entry.jira_host ?? '',
    jiraProjectKey: entry.jira_project_key ?? '',
    storyPointsField: entry.story_points_field ?? '',
    dependencyLinkType: entry.dependency_link_type ?? '',
    advancedOpen: !!(entry.story_points_field || entry.dependency_link_type),
  }
}

function entryFromDraft(d: Draft): ProjectConfigEntry {
  const path = d.path.trim()
  if (d.type === 'beads') return { type: 'beads', path }
  const entry: ProjectConfigEntry = { type: 'jira', path }
  if (d.jiraHost.trim()) entry.jira_host = d.jiraHost.trim()
  if (d.jiraProjectKey.trim()) entry.jira_project_key = d.jiraProjectKey.trim()
  if (d.storyPointsField.trim()) entry.story_points_field = d.storyPointsField.trim()
  if (d.dependencyLinkType.trim()) entry.dependency_link_type = d.dependencyLinkType.trim()
  return entry
}

/** A project's own entry row, read-only, plus Edit/Remove. Rendered both in
 *  the list and (swapped for a form) while a row is being edited. */
function ProjectRow({
  entry, onEdit, onRemove, busy,
}: {
  entry: ProjectConfigEntry
  onEdit: () => void
  onRemove: () => void
  busy: boolean
}) {
  return (
    <div className="flex items-center gap-2 border border-border rounded-lg px-3 py-2" data-testid="board-settings-project-row">
      <Badge variant={entry.type === 'jira' ? 'aim' : 'muted'} className="text-[10px] shrink-0">
        {entry.type === 'jira' ? i18nT('apps.board.project_type_jira') : i18nT('apps.board.project_type_beads')}
      </Badge>
      <div className="min-w-0 flex-1">
        <div className="text-[12px] font-mono text-text truncate" title={entry.path}>{entry.path}</div>
        {entry.type === 'jira' && (
          <div className="text-[11px] text-muted truncate">
            {entry.jira_host || '—'}{entry.jira_project_key ? ` · ${entry.jira_project_key}` : ''}
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={onEdit}
        disabled={busy}
        className="text-muted hover:text-text cursor-pointer disabled:opacity-50 shrink-0"
        aria-label={i18nT('apps.board.edit_project')}
        data-testid="board-settings-project-edit"
      >
        <Pencil size={13} />
      </button>
      <button
        type="button"
        onClick={onRemove}
        disabled={busy}
        className="text-muted hover:text-danger cursor-pointer disabled:opacity-50 shrink-0"
        aria-label={i18nT('apps.board.remove_project')}
        data-testid="board-settings-project-remove"
      >
        <Trash2 size={13} />
      </button>
    </div>
  );
}

/** The add/edit form - identical fields either way, just seeded from the
 *  entry being edited (or EMPTY_DRAFT for a fresh add) and swapping its
 *  submit label/callback. */
function ProjectForm({
  draft, setDraft, onSubmit, onCancel, submitLabel, busy,
}: {
  draft: Draft
  setDraft: (d: Draft) => void
  onSubmit: () => void
  onCancel?: () => void
  submitLabel: string
  busy: boolean
}) {
  const canSubmit = draft.path.trim().length > 0
  return (
    <div className="flex flex-col gap-3 border border-border-strong rounded-lg p-3 bg-bg-elevated" data-testid="board-settings-project-form">
      <div className="flex flex-col gap-1.5">
        <span className="text-[11px] font-medium text-muted">{i18nT('apps.board.project_path_label')}</span>
        <Input
          className="w-full font-mono text-[12px]"
          value={draft.path}
          placeholder={i18nT('apps.board.project_path_placeholder')}
          onChange={e => setDraft({ ...draft, path: e.target.value })}
          data-testid="board-settings-path-input"
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <span className="text-[11px] font-medium text-muted">{i18nT('apps.board.project_type_label')}</span>
        <div className="flex items-center gap-3">
          {(['beads', 'jira'] as const).map(t => (
            <label key={t} className="flex items-center gap-1.5 text-[12px] text-text cursor-pointer">
              <input
                type="radio"
                name="board-settings-project-type"
                aria-label={t === 'jira' ? i18nT('apps.board.project_type_jira') : i18nT('apps.board.project_type_beads')}
                checked={draft.type === t}
                onChange={() => setDraft({ ...draft, type: t })}
                data-testid={`board-settings-type-${t}`}
              />
              {t === 'jira' ? i18nT('apps.board.project_type_jira') : i18nT('apps.board.project_type_beads')}
            </label>
          ))}
        </div>
      </div>
      {draft.type === 'jira' && (
        <>
          <div className="flex flex-col gap-1.5">
            <span className="text-[11px] font-medium text-muted">{i18nT('apps.board.jira_host_label')}</span>
            <Input
              className="w-full"
              value={draft.jiraHost}
              placeholder={i18nT('apps.board.jira_host_placeholder')}
              onChange={e => setDraft({ ...draft, jiraHost: e.target.value })}
              data-testid="board-settings-jira-host-input"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <span className="text-[11px] font-medium text-muted">{i18nT('apps.board.jira_project_key_label')}</span>
            <Input
              className="w-full"
              value={draft.jiraProjectKey}
              placeholder={i18nT('apps.board.jira_project_key_placeholder')}
              onChange={e => setDraft({ ...draft, jiraProjectKey: e.target.value })}
              data-testid="board-settings-jira-key-input"
            />
          </div>
          <div>
            <button
              type="button"
              onClick={() => setDraft({ ...draft, advancedOpen: !draft.advancedOpen })}
              className="flex items-center gap-1 text-[11px] text-muted hover:text-text cursor-pointer"
              data-testid="board-settings-advanced-toggle"
            >
              {draft.advancedOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              {i18nT('apps.board.advanced_settings')}
            </button>
            {draft.advancedOpen && (
              <div className="flex flex-col gap-2 mt-2">
                <div className="flex flex-col gap-1.5">
                  <span className="text-[11px] font-medium text-muted">{i18nT('apps.board.story_points_field_label')}</span>
                  <Input
                    className="w-full font-mono text-[12px]"
                    value={draft.storyPointsField}
                    placeholder={i18nT('apps.board.story_points_field_placeholder')}
                    onChange={e => setDraft({ ...draft, storyPointsField: e.target.value })}
                    data-testid="board-settings-story-points-field-input"
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <span className="text-[11px] font-medium text-muted">{i18nT('apps.board.dependency_link_type_label')}</span>
                  <Input
                    className="w-full"
                    value={draft.dependencyLinkType}
                    placeholder={i18nT('apps.board.dependency_link_type_placeholder')}
                    onChange={e => setDraft({ ...draft, dependencyLinkType: e.target.value })}
                    data-testid="board-settings-dependency-link-type-input"
                  />
                </div>
              </div>
            )}
          </div>
        </>
      )}
      <div className="flex items-center gap-2 self-end">
        {onCancel && (
          <Btn onClick={onCancel} disabled={busy}>{i18nT('apps.board.edit_cancel')}</Btn>
        )}
        <Btn primary disabled={!canSubmit || busy} onClick={onSubmit} data-testid="board-settings-form-submit">
          <Check size={12} /> {submitLabel}
        </Btn>
      </div>
    </div>
  );
}

export default function ProjectSettingsPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient()
  const configQuery = useQuery({
    queryKey: ['board', 'config'],
    queryFn: boardApi.getConfig,
    enabled: open,
  })
  const projectPaths = configQuery.data?.project_paths ?? []

  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState<Draft>(EMPTY_DRAFT)
  const [addOpen, setAddOpen] = useState(false)
  const [addDraft, setAddDraft] = useState<Draft>(EMPTY_DRAFT)

  // Re-close any in-progress add/edit whenever the modal reopens, so a stale
  // draft from a previous open never resurfaces.
  useEffect(() => {
    if (!open) return
    setEditingIndex(null)
    setAddOpen(false)
    setAddDraft(EMPTY_DRAFT)
  }, [open])

  const saveMutation = useMutation({
    mutationFn: (entries: ProjectConfigEntry[]) => boardApi.putConfig(entries),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['board', 'config'] })
      // project_paths affects what the timeline itself renders (the "no
      // projects configured" hint and which specs show up at all).
      queryClient.invalidateQueries({ queryKey: ['board', 'stories'] })
    },
  })

  const submitAdd = () => {
    saveMutation.mutate([...projectPaths, entryFromDraft(addDraft)], {
      onSuccess: () => { setAddOpen(false); setAddDraft(EMPTY_DRAFT) },
    })
  }
  const submitEdit = () => {
    if (editingIndex === null) return
    const next = projectPaths.map((p, i) => (i === editingIndex ? entryFromDraft(editDraft) : p))
    saveMutation.mutate(next, { onSuccess: () => setEditingIndex(null) })
  }
  const remove = (index: number) => {
    saveMutation.mutate(projectPaths.filter((_, i) => i !== index))
  }

  const busy = saveMutation.isPending

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={i18nT('apps.board.project_settings_title')}
      maxWidth={520}
      footer={<Btn onClick={onClose}>{i18nT('apps.board.settings_close')}</Btn>}
    >
      <div className="flex flex-col gap-3">
        <p className="text-[12px] text-muted">{i18nT('apps.board.project_settings_intro')}</p>

        {configQuery.error && <ErrorNotice message={String(configQuery.error)} />}
        {saveMutation.isError && (
          <ErrorNotice message={i18nT('apps.board.settings_save_failed')} />
        )}

        {configQuery.isLoading ? (
          <div className="text-[12px] text-muted">…</div>
        ) : projectPaths.length === 0 && !addOpen ? (
          <div className="text-[12px] text-muted">{i18nT('apps.board.no_projects_in_settings')}</div>
        ) : (
          <div className="flex flex-col gap-2" data-testid="board-settings-project-list">
            {projectPaths.map((entry, i) => (
              editingIndex === i ? (
                <ProjectForm
                  key={i}
                  draft={editDraft}
                  setDraft={setEditDraft}
                  onSubmit={submitEdit}
                  onCancel={() => setEditingIndex(null)}
                  submitLabel={i18nT('apps.board.edit_save')}
                  busy={busy}
                />
              ) : (
                <ProjectRow
                  key={i}
                  entry={entry}
                  busy={busy}
                  onEdit={() => { setEditingIndex(i); setEditDraft(draftFromEntry(entry)) }}
                  onRemove={() => remove(i)}
                />
              )
            ))}
          </div>
        )}

        {addOpen ? (
          <ProjectForm
            draft={addDraft}
            setDraft={setAddDraft}
            onSubmit={submitAdd}
            onCancel={() => { setAddOpen(false); setAddDraft(EMPTY_DRAFT) }}
            submitLabel={i18nT('apps.board.add_project_submit')}
            busy={busy}
          />
        ) : (
          <div>
            <Btn onClick={() => setAddOpen(true)} disabled={busy} data-testid="board-settings-add-open">
              <Plus size={13} /> {i18nT('apps.board.add_project')}
            </Btn>
          </div>
        )}
      </div>
    </Modal>
  );
}
