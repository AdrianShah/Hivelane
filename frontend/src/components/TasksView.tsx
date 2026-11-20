import Icon from './Icon';
import { fromMaterial } from './iconNames';
import type { Task } from '../types';
import { groupTasks, leaseLabel, taskResult } from '../dashboard/taskReducer';
import { relativeTime } from '../dashboard/people';

interface TasksViewProps {
  /** The room's tasks by id (task_update frames, App.tsx). */
  tasks: Record<string, Task>;
  nowSeconds: number;
}

const COLUMNS: { key: keyof ReturnType<typeof groupTasks>; label: string; icon: string; empty: string }[] = [
  { key: 'ready', label: 'Ready', icon: 'inbox', empty: 'Nothing ready to claim.' },
  { key: 'waiting', label: 'Waiting on others', icon: 'hourglass_top', empty: 'No task is waiting on another.' },
  { key: 'claimed', label: 'Claimed', icon: 'assignment_ind', empty: 'Nobody is working on a task.' },
  { key: 'closed', label: 'Done', icon: 'task_alt', empty: 'No finished tasks yet.' },
];

/**
 * The room's shared task list, read-only: tasks are created and claimed from
 * the CLI (`hivelane task ...`) or the MCP tools. A claimed task's files are
 * reserved for its claimer, which is why they are listed on each card.
 */
export default function TasksView({ tasks, nowSeconds }: TasksViewProps) {
  const groups = groupTasks(tasks);
  const total = Object.keys(tasks).length;
  return (
    <div className="view-page animate-fade-up">
      <h1 className="view-title">Tasks</h1>
      <p className="view-sub">
        The room&apos;s shared task list. A claimed task reserves its files: teammates&apos; agents are
        blocked from editing them until it is done, released or its lease runs out. Add and claim tasks
        with <code>hivelane task</code> or the MCP task tools.
      </p>
      {total === 0 ? (
        <div className="lane-shell rounded-2xl px-5 py-12 text-center text-sm text-on-muted">
          No tasks yet. Try <code>hivelane task add &quot;Fix the login redirect&quot; --files web/login.tsx</code>
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
                      <TaskCard key={task.task_id} task={task} nowSeconds={nowSeconds} />
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

function TaskCard({ task, nowSeconds }: { task: Task; nowSeconds: number }) {
  const result = taskResult(task);
  const lastNote = task.notes.length ? task.notes[task.notes.length - 1] : null;
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
          <>added by {task.created_by} {relativeTime(task.created_at, nowSeconds)}</>
        ) : (
          <>
            {task.status === 'done' ? 'done' : 'cancelled'} {relativeTime(task.updated_at, nowSeconds)}
          </>
        )}
        {task.status === 'open' && task.depends_on.length ? <> · after {task.depends_on.join(', ')}</> : null}
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
    </li>
  );
}
