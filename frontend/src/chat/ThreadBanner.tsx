import { ArrowLeftIcon, CheckIcon, CornerDownRightIcon, Loader2Icon } from 'lucide-react';
import React from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { getProfileDisplayName } from './profileNames';
import { ThreadStateDot, threadStatusText } from './ThreadStateDot';
import type { ThreadSummary } from './types';

interface ThreadBannerProps {
  thread: ThreadSummary;
  onOpenHome: (homeConversationId: string) => void;
  onMarkDone: (conversationId: string) => void;
  /** A Mark done for this thread in flight, or the last one having failed. */
  markDoneStatus: 'pending' | 'failed' | null;
}

/**
 * Where an open thread came from, the way back to Home, where it stands, and
 * Mark done for a thread that is waiting on the user or has failed.
 */
export const ThreadBanner: React.FC<ThreadBannerProps> = ({
  thread,
  onOpenHome,
  onMarkDone,
  markDoneStatus,
}) => (
  <div
    className="flex-shrink-0 border-b border-border/50 bg-muted/20 px-4 py-2 md:px-6"
    data-testid="thread-banner"
    data-thread-state={thread.state}
  >
    <div className="mx-auto flex max-w-3xl flex-col gap-1.5">
      <div className="flex min-w-0 items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          className="h-7 shrink-0 gap-1 px-2"
          onClick={() => onOpenHome(thread.home_conversation_id)}
          data-testid="thread-breadcrumb-home"
        >
          <ArrowLeftIcon size={14} aria-hidden="true" />
          Home
        </Button>
        <span className="text-muted-foreground" aria-hidden="true">
          /
        </span>
        <ThreadStateDot state={thread.state} />
        <span className="min-w-0 truncate text-sm font-medium" data-testid="thread-banner-title">
          {thread.title}
        </span>
        <Badge variant="secondary" className="shrink-0 text-[10px] font-normal">
          {getProfileDisplayName(thread.profile_id)}
        </Badge>
        {thread.state === 'working' ? (
          <span
            className="ml-auto flex shrink-0 items-center gap-1 text-xs text-muted-foreground"
            data-testid="thread-working-indicator"
          >
            <Loader2Icon size={12} className="animate-spin" aria-hidden="true" />
            Working…
          </span>
        ) : (
          <span
            className={`ml-auto shrink-0 text-xs ${
              thread.state === 'failed' ? 'font-medium text-destructive' : 'text-muted-foreground'
            }`}
            data-testid="thread-banner-state"
          >
            {threadStatusText(thread)}
          </span>
        )}
        {/* Work in progress outranks a close, so a working thread would stay
            working; it can be marked done once it stops. */}
        {(thread.state === 'waiting' || thread.state === 'failed') && (
          <Button
            variant="outline"
            size="sm"
            className="h-7 shrink-0 gap-1 px-2 text-xs"
            onClick={() => onMarkDone(thread.conversation_id)}
            disabled={markDoneStatus === 'pending'}
            data-testid="thread-mark-done"
          >
            {markDoneStatus === 'pending' ? (
              <Loader2Icon size={12} className="animate-spin" aria-hidden="true" />
            ) : (
              <CheckIcon size={12} aria-hidden="true" />
            )}
            Mark done
          </Button>
        )}
      </div>
      {markDoneStatus === 'failed' && (
        <div
          role="alert"
          className="text-xs text-destructive"
          data-testid="thread-mark-done-failed"
        >
          Couldn't mark this thread done. Try again.
        </div>
      )}
      <div
        className="flex min-w-0 items-start gap-1.5 text-xs text-muted-foreground"
        data-testid="thread-origin"
      >
        <CornerDownRightIcon size={12} className="mt-0.5 shrink-0" aria-hidden="true" />
        <span className="min-w-0 line-clamp-2 break-words">
          Started from Home
          {thread.seed_request && (
            <>
              {' · Assistant asked: '}
              <span className="text-foreground/80">“{thread.seed_request}”</span>
            </>
          )}
        </span>
      </div>
    </div>
  </div>
);
