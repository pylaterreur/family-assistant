import { createContext, useContext } from 'react';
import type { ThreadSummary } from './types';

interface ThreadsContextValue {
  /** Each thread of Home, keyed by the Home tool call that started it. */
  threadsBySourceToolCallId: ReadonlyMap<string, ThreadSummary>;
  threadsByConversationId: ReadonlyMap<string, ThreadSummary>;
  openConversation: (conversationId: string) => void;
  /** Whether the open conversation's newest message is scrolled into view. */
  setViewportAtBottom: (atBottom: boolean) => void;
}

export const ThreadsContext = createContext<ThreadsContextValue | null>(null);

/** Null outside a chat with threads enabled. */
export const useThreadsContext = (): ThreadsContextValue | null => useContext(ThreadsContext);
