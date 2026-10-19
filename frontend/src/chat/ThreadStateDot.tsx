import React from 'react';
import { cn } from '@/lib/utils';
import type { ThreadState, ThreadSummary } from './types';

export const THREAD_STATE_LABELS: Record<ThreadState, string> = {
  waiting: 'Waiting on you',
  working: 'Working',
  done: 'Done',
  failed: 'Failed',
};

/** Where one thread stands, saying so when it is done because the user closed it. */
export function threadStatusText(thread: Pick<ThreadSummary, 'state' | 'closed_by'>): string {
  if (thread.state === 'done' && thread.closed_by === 'user') {
    return 'You marked this done';
  }
  return THREAD_STATE_LABELS[thread.state];
}

const DOT_CLASSES: Record<ThreadState, string> = {
  waiting: 'bg-violet-500',
  working: 'bg-amber-500 animate-pulse',
  done: 'bg-emerald-500',
  failed: 'bg-destructive',
};

export const ThreadStateDot: React.FC<{ state: ThreadState; className?: string }> = ({
  state,
  className,
}) => (
  <span
    role="img"
    aria-label={THREAD_STATE_LABELS[state]}
    title={THREAD_STATE_LABELS[state]}
    data-testid="thread-state-dot"
    data-state={state}
    className={cn('inline-block h-2 w-2 shrink-0 rounded-full', DOT_CLASSES[state], className)}
  />
);

/** The caller's unread count for one thread; nothing when all of it is read. */
export const ThreadUnreadBadge: React.FC<{ count?: number; className?: string }> = ({
  count,
  className,
}) =>
  count ? (
    <span
      className={cn(
        'inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-full bg-primary px-1 text-[10px] font-semibold leading-none text-primary-foreground',
        className
      )}
      aria-label={`${count} unread`}
      title={`${count} unread`}
      data-testid="thread-unread-badge"
    >
      {count > 99 ? '99+' : count}
    </span>
  ) : null;
