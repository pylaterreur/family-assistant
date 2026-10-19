import type { ThreadSummary } from './types';

/** The caller's Home. `conversation_id` is null exactly when threads are disabled. */
export interface HomeResponse {
  enabled: boolean;
  conversation_id: string | null;
}

export interface HomeThreadsResponse {
  home_conversation_id: string | null;
  threads: ThreadSummary[];
}

export async function fetchHome(signal?: AbortSignal): Promise<HomeResponse> {
  const response = await fetch('/api/v1/chat/home', { signal });
  if (!response.ok) {
    throw new Error(`Failed to fetch Home: ${response.status}`);
  }
  return (await response.json()) as HomeResponse;
}

export async function fetchHomeThreads(signal?: AbortSignal): Promise<HomeThreadsResponse> {
  const response = await fetch('/api/v1/chat/home/threads', { signal });
  if (!response.ok) {
    throw new Error(`Failed to fetch threads: ${response.status}`);
  }
  return (await response.json()) as HomeThreadsResponse;
}

/** Close a thread as done on the user's say-so; returns the thread as it now stands. */
export async function markThreadDone(conversationId: string): Promise<ThreadSummary> {
  const response = await fetch(
    `/api/v1/chat/home/threads/${encodeURIComponent(conversationId)}/done`,
    { method: 'POST' }
  );
  if (!response.ok) {
    throw new Error(`Failed to mark the thread done: ${response.status}`);
  }
  return (await response.json()) as ThreadSummary;
}

/**
 * Move the caller's read marker in Home or a thread forward to
 * `throughMessageId`, or to its newest message when that is omitted; returns
 * how many messages are still unread.
 */
export async function markConversationRead(
  conversationId: string,
  throughMessageId?: number
): Promise<number> {
  const response = await fetch(
    `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/read`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(
        throughMessageId === undefined ? {} : { through_message_id: throughMessageId }
      ),
    }
  );
  if (!response.ok) {
    throw new Error(`Failed to mark the conversation read: ${response.status}`);
  }
  const data = (await response.json()) as { unread_count: number };
  return data.unread_count;
}
