import { useEffect, useRef, useState } from 'react';
import { markConversationRead } from './threadsApi';

/** How long the newest message has to stay on screen before it is marked read. */
export const MARK_READ_DELAY_MS = 750;

function pageIsVisible(): boolean {
  return document.visibilityState === 'visible';
}

/**
 * Marks the open Home or thread read through its newest stored message, once
 * that message has been on screen in a visible tab for a moment.
 *
 * Keyed on the stored message id rather than on the transcript, so a streaming
 * reply costs nothing until its rows are stored, and each position is sent at
 * most once per conversation. A background tab never marks anything read; it
 * catches up when it becomes visible again. A failed request is logged and
 * retried with the next message, while the unread badge it would have cleared
 * stays as it was.
 */
export function useMarkRead({
  conversationId,
  throughMessageId,
  onMarked,
}: {
  /** The open conversation, when it is one that keeps read markers. */
  conversationId: string | null;
  /** The newest stored message on screen in it. */
  throughMessageId: number | null;
  onMarked: (conversationId: string, unreadCount: number) => void;
}): void {
  const [visible, setVisible] = useState(pageIsVisible);
  const markedThroughRef = useRef(new Map<string, number>());
  const onMarkedRef = useRef(onMarked);
  onMarkedRef.current = onMarked;

  useEffect(() => {
    const handleVisibilityChange = () => setVisible(pageIsVisible());
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
  }, []);

  useEffect(() => {
    if (!conversationId || throughMessageId === null || !visible) {
      return undefined;
    }
    if ((markedThroughRef.current.get(conversationId) ?? -1) >= throughMessageId) {
      return undefined;
    }
    const timer = setTimeout(() => {
      if (!pageIsVisible()) {
        return;
      }
      void (async () => {
        try {
          const unreadCount = await markConversationRead(conversationId, throughMessageId);
          const marked = markedThroughRef.current;
          marked.set(conversationId, Math.max(marked.get(conversationId) ?? -1, throughMessageId));
          onMarkedRef.current(conversationId, unreadCount);
        } catch (error) {
          console.error('Error marking conversation read:', error);
        }
      })();
    }, MARK_READ_DELAY_MS);
    return () => clearTimeout(timer);
  }, [conversationId, throughMessageId, visible]);
}
