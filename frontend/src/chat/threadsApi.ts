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
