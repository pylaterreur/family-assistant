import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

const CONVERSATION = 'conv-feedback';
const REPLY = 'Spaghetti bolognese tonight.';

interface FeedbackServer {
  writes: { method: string; turnId: string; body: Record<string, unknown> | null }[];
  failNext: boolean;
  /** Holds the next write's response until it resolves. */
  holdNext: Promise<void> | null;
}

function serveConversation(feedback: 'up' | 'down' | null = null): FeedbackServer {
  const state: FeedbackServer = { writes: [], failNext: false, holdNext: null };
  const rows = [
    {
      internal_id: 1,
      turn_id: 'turn-dinner',
      role: 'user',
      content: "What's for dinner?",
      timestamp: '2026-10-22T09:00:00Z',
      feedback,
    },
    {
      internal_id: 2,
      turn_id: 'turn-dinner',
      role: 'assistant',
      content: REPLY,
      timestamp: '2026-10-22T09:00:01Z',
      feedback,
    },
  ];
  const record = async (method: string, turnId: string, request: Request) => {
    const body = method === 'PUT' ? ((await request.json()) as Record<string, unknown>) : null;
    state.writes.push({ method, turnId, body });
    const hold = state.holdNext;
    state.holdNext = null;
    if (hold) {
      await hold;
    }
    if (state.failNext) {
      state.failNext = false;
      return HttpResponse.json({ detail: 'nope' }, { status: 500 });
    }
    return method === 'PUT'
      ? HttpResponse.json({ turn_id: turnId, ...body, updated_at: '2026-10-22T09:01:00Z' })
      : new HttpResponse(null, { status: 204 });
  };
  server.use(
    http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) =>
      HttpResponse.json({
        conversation_id: params.conversationId,
        messages: params.conversationId === CONVERSATION ? rows : [],
        count: rows.length,
        total_messages: rows.length,
        has_more_before: false,
        has_more_after: false,
        active_turns: [],
      })
    ),
    http.put(
      '/api/v1/chat/conversations/:conversationId/turns/:turnId/feedback',
      ({ params, request }) => record('PUT', String(params.turnId), request)
    ),
    http.delete(
      '/api/v1/chat/conversations/:conversationId/turns/:turnId/feedback',
      ({ params, request }) => record('DELETE', String(params.turnId), request)
    )
  );
  return state;
}

async function openReply() {
  await renderChatApp({
    waitForReady: true,
    initialEntries: [`/chat?conversation_id=${CONVERSATION}`],
  });
  await screen.findByText(REPLY);
  const reply = screen
    .getAllByTestId('assistant-message')
    .find((message) => message.textContent?.includes(REPLY));
  if (!reply) {
    throw new Error('reply not rendered');
  }
  return within(reply);
}

describe('rating a reply', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState(null, '', `/chat?conversation_id=${CONVERSATION}`);
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('records thumbs up, and a second press withdraws it', async () => {
    const state = serveConversation();
    const user = userEvent.setup();
    const reply = await openReply();

    const up = reply.getByRole('button', { name: 'Good response' });
    expect(up).toHaveAttribute('aria-pressed', 'false');
    await user.click(up);
    await waitFor(() => expect(up).toHaveAttribute('aria-pressed', 'true'));
    expect(state.writes).toEqual([
      { method: 'PUT', turnId: 'turn-dinner', body: { rating: 'up', comment: null } },
    ]);
    expect(reply.queryByTestId('feedback-comment-form')).not.toBeInTheDocument();

    await user.click(up);
    await waitFor(() => expect(up).toHaveAttribute('aria-pressed', 'false'));
    expect(state.writes[1]).toEqual({ method: 'DELETE', turnId: 'turn-dinner', body: null });
  });

  it('asks what was wrong after thumbs down and sends the answer', async () => {
    const state = serveConversation();
    const user = userEvent.setup();
    const reply = await openReply();

    await user.click(reply.getByRole('button', { name: 'Bad response' }));
    const input = await reply.findByRole('textbox', {
      name: 'What was wrong with this response',
    });
    await user.type(input, 'Forgot I am vegetarian');
    await user.click(reply.getByRole('button', { name: 'Send' }));

    await waitFor(() =>
      expect(reply.queryByTestId('feedback-comment-form')).not.toBeInTheDocument()
    );
    expect(state.writes.map((write) => write.body)).toEqual([
      { rating: 'down', comment: null },
      { rating: 'down', comment: 'Forgot I am vegetarian' },
    ]);
    expect(reply.getByRole('button', { name: 'Bad response' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
  });

  it('shows a rating given earlier', async () => {
    serveConversation('down');
    const reply = await openReply();
    expect(reply.getByRole('button', { name: 'Bad response' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(reply.getByRole('button', { name: 'Good response' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
  });

  it('restores the shown rating when saving fails', async () => {
    const state = serveConversation();
    state.failNext = true;
    const user = userEvent.setup();
    const reply = await openReply();

    const up = reply.getByRole('button', { name: 'Good response' });
    await user.click(up);
    expect(await reply.findByRole('alert')).toHaveTextContent("Couldn't save your feedback");
    expect(up).toHaveAttribute('aria-pressed', 'false');
  });

  it('keeps a slower earlier thumbs down from reopening the comment form', async () => {
    const state = serveConversation();
    let release = () => {};
    state.holdNext = new Promise((resolve) => {
      release = resolve;
    });
    const user = userEvent.setup();
    const reply = await openReply();

    await user.click(reply.getByRole('button', { name: 'Bad response' }));
    const up = reply.getByRole('button', { name: 'Good response' });
    await user.click(up);
    await waitFor(() => expect(state.writes).toHaveLength(2));
    release();

    await waitFor(() => expect(up).toHaveAttribute('aria-pressed', 'true'));
    // Let the held request's completion run before checking it changed nothing.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(reply.queryByTestId('feedback-comment-form')).not.toBeInTheDocument();
  });
});
