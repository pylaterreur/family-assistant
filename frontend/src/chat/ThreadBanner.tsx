import { ArrowLeftIcon, CornerDownRightIcon, Loader2Icon } from 'lucide-react';
import React from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { getProfileDisplayName } from './profileNames';
import { THREAD_STATE_LABELS, ThreadStateDot } from './ThreadStateDot';
import type { ThreadSummary } from './types';

interface ThreadBannerProps {
  thread: ThreadSummary;
  onOpenHome: (homeConversationId: string) => void;
}

/** Where an open thread came from, the way back to Home, and whether it is still working. */
export const ThreadBanner: React.FC<ThreadBannerProps> = ({ thread, onOpenHome }) => (
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
          <span className="ml-auto shrink-0 text-xs text-muted-foreground">
            {THREAD_STATE_LABELS[thread.state]}
          </span>
        )}
      </div>
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
