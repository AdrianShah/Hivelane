import { useState } from 'react';
import Icon, { type IconName } from './Icon';
import { fromMaterial } from './iconNames';
import type { Task } from '../types';
import {
  groupTasks,
  isMine,
  leaseLabel,
  planGroups,
  splitList,
  taskIds,
  taskResult,
  type PlanGroup,
} from '../dashboard/taskReducer';
import { relativeTime } from '../dashboard/people';
import { cleanName, savedTaskName, saveTaskName } from '../taskKey';

/** What the Add task form sends (App.tsx turns it into a task frame). */
export interface NewTask {
  asName: string;
  title: string;
  body: string;
  files: string[];
  dependsOn: string[];
  suggestedFor: string;
}

interface TasksViewProps {
  /** The room's tasks by id (task_update frames, App.tsx). */
  tasks: Record<string, Task>;
  nowSeconds: number;
  /** People in the room, for "suggested for". */
  people?: string[];
  /** This dashboard's tag: its own tasks get a Cancel button. */
  myTag?: string | null;
  /** Why this dashboard cannot add tasks right now (null: it can). */
  addBlocked?: string | null;
  onAdd?: (task: NewTask) => Promise<boolean>;
  onCancel?: (taskId: string) => void;
}

const COLUMNS: { key: keyof ReturnType<typeof groupTasks>; label: string; icon: string; empty: string }[] = [
  { key: 'ready', label: 'Ready', icon: 'inbox', empty: 'Nothing ready to claim.' },
  { key: 'waiting', label: 'Waiting on others', icon: 'hourglass_top', empty: 'No task is waiting on another.' },
  { key: 'claimed', label: 'Claimed', icon: 'assignment_ind', empty: 'Nobody is working on a task.' },
  { key: 'closed', label: 'Done', icon: 'task_alt', empty: 'No finished tasks yet.' },
];

// Finished agent plans shown below the active ones.
const MAX_FINISHED_PLANS = 4;

/**
 * The room's task board. Agents' own plans (their todo lists, mirrored by
 * the daemon) are grouped by lane; the team's tasks keep the Ready / Waiting /
 * Claimed / Done columns. A claimed task's files are reserved for its
 * claimer, which is why they are listed on each card. This dashboard can add
 * tasks and cancel the ones it added; claiming is for members' agents.
 */
export default function TasksView({ tasks, nowSeconds, people = [], myTag = null, addBlocked = null, onAdd, onCancel }: TasksViewProps) {
  const groups = groupTasks(tasks);
  const plans = planGroups(tasks);
  const activePlans = plans.filter((p) => !p.finished);
  const finishedPlans = plans.filter((p) => p.finished).slice(0, MAX_FINISHED_PLANS);
  const manualCount = COLUMNS.reduce((sum, c) => sum + groups[c.key].length, 0);
  const [adding, setAdding] = useState(false);
  return (
    <div className="view-page animate-fade-up">
      <div className="flex items-start gap-3 flex-wrap">
        <h1 className="view-title">Tasks</h1>
        {onAdd ? (
          <button
            type="button"
            className="v3-btn v3-btn--secondary v3-btn--sm ml-auto"
            onClick={() => setAdding((v) => !v)}
            aria-expanded={adding}
            disabled={!!addBlocked}
            title={addBlocked ?? undefined}
          >
            <Icon name="plus" size={14} /> Add task
          </button>
        ) : null}
      </div>
      <p className="view-sub">
        The room&apos;s shared task list. A claimed task reserves its files: teammates&apos; agents are
        blocked from editing them until it is done, released or its lease runs out. Agents&apos; own step
        plans show up here by themselves. Members claim tasks with <code>hivelane task claim --next</code> or
        the MCP task tools.
      </p>
      {addBlocked ? <p className="m-0 mb-3 text-[12px] text-on-dim">{addBlocked}</p> : null}
      {adding && onAdd && !addBlocked ? (
        <AddTaskForm people={people} onAdd={onAdd} onClose={() => setAdding(false)} />
      ) : null}

      {activePlans.length || finishedPlans.length ? (
        <section className="mb-5" aria-label="Agent plans">
          <h2 className="section-label m-0 mb-2 flex items-center gap-1.5">
            <Icon name={fromMaterial('checklist')} size={15} className="text-lavender" />
            Agent plans
          </h2>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3 items-start">
            {[...activePlans, ...finishedPlans].map((plan) => (
              <PlanCard key={plan.lane} plan={plan} nowSeconds={nowSeconds} />
            ))}
          </div>
        </section>
      ) : null}

      {manualCount === 0 && plans.length === 0 ? (
        <div className="lane-shell rounded-2xl px-5 py-12 text-center text-sm text-on-muted">
          No tasks yet. Add one above, or try{' '}
          <code>hivelane task add &quot;Fix the login redirect&quot; --files web/login.tsx</code>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4 items-start">
          {COLUMNS.map((column) => {
            const items = groups[column.key];
            return (
              <section key={column.key} className="lane-shell rounded-2xl p-4 min-w-0" aria-label={column.label}>
                <h2 className="section-label m-0 mb-3 flex items-center gap-1.5">
                  <Icon name={fromMaterial(column.icon)} size={15} className="text-lavender" />
                  {column.label}
                  <span className="ml-auto text-on-dim tabular-nums">{items.length}</span>
                </h2>
                {items.length === 0 ? (
                  <p className="m-0 text-[12.5px] italic text-on-dim">{column.empty}</p>
                ) : (
                  <ul className="list-none m-0 p-0 flex flex-col gap-2.5">
                    {items.map((task) => (
                      <TaskCard
                        key={task.task_id}
                        task={task}
                        nowSeconds={nowSeconds}
                        onCancel={onCancel && isMine(task, myTag) ? onCancel : undefined}
                      />
                    ))}
                  </ul>
                )}
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}

function PlanCard({ plan, nowSeconds }: { plan: PlanGroup; nowSeconds: number }) {
  return (
    <article className={`lane-shell rounded-2xl p-4 min-w-0${plan.finished ? ' opacity-70' : ''}`} aria-label={`${plan.label}'s plan`}>
      <p className="m-0 text-[13px] text-on-surface flex items-baseline gap-2">
        <span className="font-semibold">{plan.label}</span>
        <span className="ml-auto text-[12px] text-on-dim tabular-nums">
          {plan.done}/{plan.total} steps
        </span>
      </p>
      <div className="mt-2 h-1 rounded bg-shade/20 overflow-hidden" aria-hidden="true">
        <div className="h-full bg-primary" style={{ width: `${plan.total ? (100 * plan.done) / plan.total : 0}%` }} />
      </div>
      <ol className="list-none m-0 mt-2.5 p-0 flex flex-col gap-1.5">
        {plan.steps.map((step) => {
          const current = plan.current?.task_id === step.task_id;
          const mark: IconName =
            step.status === 'done' ? 'check' : step.status === 'cancelled' ? 'close' : current ? 'play' : 'dot';
          return (
            <li
              key={step.task_id}
              className={`text-[12.5px] leading-snug flex gap-1.5 min-w-0 ${
                current ? 'text-on-surface font-semibold' : step.status === 'open' || step.status === 'claimed' ? 'text-on-muted' : 'text-on-dim'
              } ${step.status === 'cancelled' ? 'line-through' : ''}`}
            >
              <Icon name={mark} size={14} className={`shrink-0 ${current ? 'text-primary' : ''}`} />
              <span className="break-words min-w-0">
                {step.title || <span className="italic">encrypted step</span>}
                {current && step.files.length ? (
                  <span className="block font-normal text-[11.5px] text-on-dim">
                    reserves {step.files.slice(0, 3).join(', ')}
                    {step.files.length > 3 ? ` +${step.files.length - 3}` : ''} · {leaseLabel(step, nowSeconds)}
                  </span>
                ) : null}
              </span>
            </li>
          );
        })}
      </ol>
      {plan.finished ? (
        <p className="m-0 mt-2 text-[11.5px] text-on-dim">finished {relativeTime(plan.updatedAt, nowSeconds)}</p>
      ) : null}
    </article>
  );
}

function AddTaskForm({ people, onAdd, onClose }: { people: string[]; onAdd: (task: NewTask) => Promise<boolean>; onClose: () => void }) {
  const [asName, setAsName] = useState(savedTaskName);
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [files, setFiles] = useState('');
  const [after, setAfter] = useState('');
  const [suggestedFor, setSuggestedFor] = useState('');
  const [sending, setSending] = useState(false);
  const name = cleanName(asName);
  const ready = !!name && !!title.trim() && !sending;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!ready) return;
    setSending(true);
    saveTaskName(asName.trim());
    const ok = await onAdd({
      asName: name,
      title: title.trim(),
      body: body.trim(),
      files: splitList(files),
      dependsOn: taskIds(after),
      suggestedFor,
    });
    setSending(false);
    if (ok) {
      setTitle('');
      setBody('');
      setFiles('');
      setAfter('');
      onClose();
    }
  };

  const field = 'w-full rounded-lg border border-outline-soft bg-shade/10 px-2.5 py-1.5 text-[13px] text-on-surface';
  return (
    <form className="lane-shell rounded-2xl p-4 mb-5 grid gap-3 md:grid-cols-2" onSubmit={submit} aria-label="Add task">
      <label className="flex flex-col gap-1 text-[12px] text-on-muted md:col-span-2">
        Title
        <input className={field} value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} placeholder="Fix the login redirect" required autoFocus />
      </label>
      <label className="flex flex-col gap-1 text-[12px] text-on-muted md:col-span-2">
        Details (optional)
        <textarea className={field} rows={2} value={body} maxLength={2000} onChange={(e) => setBody(e.target.value)} />
      </label>
      <label className="flex flex-col gap-1 text-[12px] text-on-muted">
        Files it covers (comma separated, repo-relative)
        <input className={field} value={files} onChange={(e) => setFiles(e.target.value)} placeholder="web/login.tsx, web/auth.ts" />
      </label>
      <label className="flex flex-col gap-1 text-[12px] text-on-muted">
        After tasks (optional)
        <input className={field} value={after} onChange={(e) => setAfter(e.target.value)} placeholder="T1, T2" />
      </label>
      <label className="flex flex-col gap-1 text-[12px] text-on-muted">
        Suggested for (optional)
        <select className={field} value={suggestedFor} onChange={(e) => setSuggestedFor(e.target.value)}>
          <option value="">Anyone</option>
          {people.map((person) => (
            <option key={person} value={person}>
              {person}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-[12px] text-on-muted">
        Your name (shown as who added it)
        <input className={field} value={asName} maxLength={40} onChange={(e) => setAsName(e.target.value)} placeholder="adrian" required />
        {asName && name !== asName.trim() ? <span className="text-[11px] text-on-dim">sent as “{name || '…'}”</span> : null}
      </label>
      <div className="md:col-span-2 flex gap-2 justify-end">
        <button type="button" className="v3-btn v3-btn--secondary v3-btn--sm" onClick={onClose}>
          Cancel
        </button>
        <button type="submit" className="v3-btn v3-btn--primary v3-btn--sm" disabled={!ready}>
          {sending ? 'Adding…' : 'Add task'}
        </button>
      </div>
    </form>
  );
}

function TaskCard({ task, nowSeconds, onCancel }: { task: Task; nowSeconds: number; onCancel?: (taskId: string) => void }) {
  const result = taskResult(task);
  const lastNote = task.notes.length ? task.notes[task.notes.length - 1] : null;
  const open = task.status === 'open' || task.status === 'claimed';
  return (
    <li className="rounded-xl border border-outline-soft bg-shade/10 p-3 min-w-0">
      <p className="m-0 text-[13px] text-on-surface leading-snug break-words">
        <span className="font-semibold tabular-nums text-primary mr-1.5">{task.task_id}</span>
        {task.title || <span className="italic text-on-dim">encrypted task</span>}
      </p>
      {task.body ? (
        <p className="m-0 mt-1 text-[12px] text-on-muted leading-snug line-clamp-3 break-words">{task.body}</p>
      ) : null}
      <p className="m-0 mt-1.5 text-[11.5px] text-on-dim">
        {task.status === 'claimed' ? (
          <>
            <span className="text-on-surface">{task.claimer}</span> · {leaseLabel(task, nowSeconds)}
          </>
        ) : task.status === 'open' ? (
          <>
            added by {task.created_by}
            {task.dashboard_tag ? ' (dashboard)' : ''} {relativeTime(task.created_at, nowSeconds)}
          </>
        ) : (
          <>
            {task.status === 'done' ? 'done' : 'cancelled'} {relativeTime(task.updated_at, nowSeconds)}
          </>
        )}
        {task.status === 'open' && task.depends_on.length ? <> · after {task.depends_on.join(', ')}</> : null}
        {task.status === 'open' && task.suggested_for ? <> · for {task.suggested_for}</> : null}
      </p>
      {task.files.length ? (
        <ul className="list-none m-0 mt-2 p-0 flex flex-wrap gap-1" aria-label="Files">
          {task.files.slice(0, 6).map((file) => (
            <li key={file}>
              <code
                className={`text-[11px] rounded px-1.5 py-0.5 ${
                  task.status === 'claimed' ? 'bg-primary/10 text-primary' : 'bg-shade/20 text-on-muted'
                }`}
                title={task.status === 'claimed' ? `reserved for ${task.claimer}` : undefined}
              >
                {file}
              </code>
            </li>
          ))}
          {task.files.length > 6 ? <li className="text-[11px] text-on-dim">+{task.files.length - 6}</li> : null}
        </ul>
      ) : null}
      {result ? (
        <p className="m-0 mt-2 text-[12px] text-on-muted break-words">
          <span className="text-on-dim">Result:</span> {result}
        </p>
      ) : lastNote && lastNote.text ? (
        <p className="m-0 mt-2 text-[12px] text-on-muted break-words">
          <span className="text-on-dim">{lastNote.user_id}:</span> {lastNote.text}
        </p>
      ) : null}
      {onCancel && open ? (
        <button type="button" className="v3-btn v3-btn--secondary v3-btn--sm mt-2" onClick={() => onCancel(task.task_id)}>
          Cancel task
        </button>
      ) : null}
    </li>
  );
}
