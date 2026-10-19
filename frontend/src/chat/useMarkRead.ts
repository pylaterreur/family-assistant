import { useEffect, useRef, useState } from 'react';
import { markConversationRead } from './threadsApi';

/** How long the newest message has to stay on screen before it is marked read. */
export const MARK_READ_DELAY_MS = 750;

function pageIsVisible(): boolean {
  return document.visibilityState === 'visible';
}

interface MarkedPosition {
  through: number;
  localTurnEnd: number | null;
}

/**
 * Marks the open Home or thread read, once what it shows has been on screen in
 * a visible tab for a moment.
 *
 * Two things move the position. A stored message loaded from history marks the
 * conversation read through that message. A reply streamed here carries no
 * stored id, so when a turn sent from this client ends (`localTurnEnd`
 * changes), the conversation is marked read through its newest message, which
 * is that reply. Neither changes while tokens stream, and each position is sent
 * at most once per conversation. A background tab never marks anything read; it
 * catches up when it becomes visible again. A failed request is logged and
 * retried with the next change, while the unread badge it would have cleared
 * stays as it was.
 */
export function useMarkRead({
  conversationId,
  throughMessageId,
  localTurnEnd,
  onMarked,
}: {
  /** The open conversation, when it is one that keeps read markers. */
  conversationId: string | null;
  /** The newest stored message on screen in it. */
  throughMessageId: number | null;
  /** Changes each time a turn sent from this client ends in it; null if none has. */
  localTurnEnd: number | null;
  onMarked: (conversationId: string, unreadCount: number) => void;
}): void {
  const [visible, setVisible] = useState(pageIsVisible);
  const markedRef = useRef(new Map<string, MarkedPosition>());
  const onMarkedRef = useRef(onMarked);
  onMarkedRef.current = onMarked;

  useEffect(() => {
    const handleVisibilityChange = () => setVisible(pageIsVisible());
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
  }, []);

  useEffect(() => {
    if (!conversationId || !visible) {
      return undefined;
    }
    const marked = markedRef.current.get(conversationId);
    const turnEnded = localTurnEnd !== null && localTurnEnd !== (marked?.localTurnEnd ?? null);
    const advanced = throughMessageId !== null && throughMessageId > (marked?.through ?? -1);
    if (!turnEnded && !advanced) {
      return undefined;
    }
    const timer = setTimeout(() => {
      if (!pageIsVisible()) {
        return;
      }
      void (async () => {
        try {
          const unreadCount = await markConversationRead(
            conversationId,
            turnEnded ? undefined : (throughMessageId ?? undefined)
          );
          const previous = markedRef.current.get(conversationId);
          markedRef.current.set(conversationId, {
            through: Math.max(previous?.through ?? -1, throughMessageId ?? -1),
            localTurnEnd: turnEnded ? localTurnEnd : (previous?.localTurnEnd ?? null),
          });
          onMarkedRef.current(conversationId, unreadCount);
        } catch (error) {
          console.error('Error marking conversation read:', error);
        }
      })();
    }, MARK_READ_DELAY_MS);
    return () => clearTimeout(timer);
  }, [conversationId, throughMessageId, localTurnEnd, visible]);
}
