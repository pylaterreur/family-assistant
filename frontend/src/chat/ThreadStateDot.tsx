import React from 'react';
import { cn } from '@/lib/utils';
import type { ThreadState } from './types';

export const THREAD_STATE_LABELS: Record<ThreadState, string> = {
  waiting: 'Waiting on you',
  working: 'Working',
  done: 'Done',
  failed: 'Failed',
};

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
