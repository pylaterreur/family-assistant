import { createContext, useContext } from 'react';
import type { TurnRating } from './types';

/** Rating the turns of the open conversation. */
export interface TurnFeedbackActions {
  /**
   * Record the user's rating of a turn, or withdraw it with `null`. Rejects
   * when the server did not take it; the shown rating is restored by then.
   */
  rate: (turnId: string, rating: TurnRating | null, comment?: string) => Promise<void>;
}

export const TurnFeedbackContext = createContext<TurnFeedbackActions | null>(null);

/** Null where turns cannot be rated, such as a shared transcript. */
export const useTurnFeedback = (): TurnFeedbackActions | null => useContext(TurnFeedbackContext);

export async function submitTurnFeedback(
  conversationId: string,
  turnId: string,
  rating: TurnRating | null,
  comment?: string
): Promise<void> {
  const url = `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent(turnId)}/feedback`;
  const response =
    rating === null
      ? await fetch(url, { method: 'DELETE' })
      : await fetch(url, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ rating, comment: comment || null }),
        });
  if (!response.ok) {
    throw new Error(`Saving feedback failed with status ${response.status}`);
  }
}
