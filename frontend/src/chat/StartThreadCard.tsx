import { ChevronRightIcon } from 'lucide-react';
import React from 'react';
import { Badge } from '@/components/ui/badge';
import { getProfileDisplayName } from './profileNames';
import { THREAD_STATE_LABELS, ThreadStateDot } from './ThreadStateDot';
import { useThreadsContext } from './threadsContext';

interface StartThreadCardProps {
  toolCallId?: string;
  /** Shown instead when no thread is known for this call. */
  fallback: React.ReactNode;
}

/**
 * The live card a `start_thread` call leaves in Home: the thread's state, title,
 * profile and latest message, opening the thread on click. A call whose thread
 * is not (or not yet) in the threads list, such as one the tool refused, renders
 * as any other tool call.
 */
export const StartThreadCard: React.FC<StartThreadCardProps> = ({ toolCallId, fallback }) => {
  const threads = useThreadsContext();
  const thread = toolCallId ? threads?.threadsBySourceToolCallId.get(toolCallId) : undefined;
  if (!threads || !thread) {
    return <>{fallback}</>;
  }
  const detail = thread.preview || thread.seed_request;
  return (
    <button
      type="button"
      onClick={() => threads.openConversation(thread.conversation_id)}
      className="my-1 flex w-full min-w-0 flex-col gap-1 rounded-xl border border-border/70 bg-background px-4 py-3 text-left shadow-sm transition-colors hover:border-primary/40 hover:bg-muted/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      aria-label={`Open thread: ${thread.title} (${THREAD_STATE_LABELS[thread.state]})`}
      data-testid="thread-card"
      data-thread-state={thread.state}
      data-conversation-id={thread.conversation_id}
    >
      <span className="flex min-w-0 items-center gap-2">
        <ThreadStateDot state={thread.state} />
        <span className="min-w-0 flex-1 truncate font-medium" data-testid="thread-card-title">
          {thread.title}
        </span>
        <Badge
          variant="secondary"
          className="shrink-0 text-[10px] font-normal"
          data-testid="thread-card-profile"
        >
          {getProfileDisplayName(thread.profile_id)}
        </Badge>
        <span className="flex shrink-0 items-center text-xs text-muted-foreground">
          open
          <ChevronRightIcon size={12} aria-hidden="true" />
        </span>
      </span>
      <span className="flex min-w-0 items-center gap-1.5 pl-4 text-xs text-muted-foreground">
        <span className="shrink-0" data-testid="thread-card-state">
          {THREAD_STATE_LABELS[thread.state]}
        </span>
        {detail && (
          <>
            <span aria-hidden="true">·</span>
            <span className="min-w-0 truncate" data-testid="thread-card-preview">
              {detail}
            </span>
          </>
        )}
      </span>
    </button>
  );
};
