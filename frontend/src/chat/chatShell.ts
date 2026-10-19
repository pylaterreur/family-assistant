import type { Attachment } from '@assistant-ui/react';
import { createContext, useContext } from 'react';
import type { PendingToolConfirmation } from './ToolConfirmationContext';
import type { ThreadSummary } from './types';

/** What the composer held when the user left a conversation. */
export interface ConversationDraft {
  text: string;
  attachments: readonly Attachment[];
}

export interface NotificationRequest {
  conversationId: string;
  messageId: string;
  preview: string;
  timestamp: string;
}

/**
 * What the chat holds above its conversation panes: the conversation list, Home
 * and its threads, pending tool approvals, notifications and unsent drafts.
 * Every pane reads the same value; everything a pane shows of its own
 * conversation it keeps itself.
 */
export interface ChatShellValue {
  /** The caller's Home while threads are enabled, else null. */
  homeConversationId: string | null;
  homeThreads: ThreadSummary[];
  updateHomeThread: (conversationId: string, update: Partial<ThreadSummary>) => void;
  refreshHomeThreads: () => void;
  /** Navigate to a conversation, wherever the chat decides it belongs. */
  openConversation: (conversationId: string) => void;
  /** A turn was sent: show its conversation at the top of the list until it settles. */
  noteTurnStarted: (turnId: string, conversationId: string, preview: string) => void;
  /** A turn settled (or failed to start): the server's list is authoritative again. */
  noteTurnSettled: (turnId: string | undefined) => void;
  pendingConfirmations: Map<string, PendingToolConfirmation>;
  onConfirmationRequest: (request: PendingToolConfirmation) => void;
  onConfirmationResult: (result: { request_id: string; [key: string]: unknown }) => void;
  confirmTool: (
    requestId: string,
    approved: boolean,
    conversationId: string | null
  ) => Promise<void>;
  showNotification: (request: NotificationRequest) => void;
  /**
   * Unsent composer contents, by conversation, shared by every pane so a draft
   * follows its conversation from one pane to another.
   */
  drafts: Map<string, ConversationDraft>;
}

export const ChatShellContext = createContext<ChatShellValue | null>(null);

export function useChatShell(): ChatShellValue {
  const shell = useContext(ChatShellContext);
  if (!shell) {
    throw new Error('useChatShell must be used inside ChatShellContext');
  }
  return shell;
}
