import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchHomeThreads } from './threadsApi';
import type { ThreadSummary } from './types';

const NO_THREADS: ThreadSummary[] = [];

/**
 * The threads of Home, newest first. Fetched once Home is known (null while
 * threads are disabled or Home is still being looked up); `refresh` refetches,
 * superseding any request still in flight, and is stable so it can be handed
 * to activity pings.
 */
export function useHomeThreads(homeConversationId: string | null): {
  threads: ThreadSummary[];
  refresh: () => void;
} {
  const [threads, setThreads] = useState<ThreadSummary[]>(NO_THREADS);
  const abortRef = useRef<AbortController | null>(null);
  const homeRef = useRef(homeConversationId);
  homeRef.current = homeConversationId;

  const refresh = useCallback(() => {
    const home = homeRef.current;
    if (!home) {
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    void (async () => {
      try {
        const data = await fetchHomeThreads(controller.signal);
        if (controller.signal.aborted || homeRef.current !== home) {
          return;
        }
        setThreads(data.threads);
      } catch (error) {
        if (!controller.signal.aborted) {
          console.error('Error fetching threads:', error);
        }
      }
    })();
  }, []);

  useEffect(() => {
    if (!homeConversationId) {
      setThreads(NO_THREADS);
      return undefined;
    }
    refresh();
    return () => abortRef.current?.abort();
  }, [homeConversationId, refresh]);

  return { threads, refresh };
}
