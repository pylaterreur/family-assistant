import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { mockLocalStorage, resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';
import type { ThreadSummary } from '../types';

const HOME = 'web_conv_home';

function thread(overrides: Partial<ThreadSummary> & { conversation_id: string }): ThreadSummary {
  return {
    home_conversation_id: HOME,
    title: 'Hot water system quotes',
    profile_id: 'research',
    state: 'waiting',
    source_tool_call_id: null,
    seed_request: 'Find three plumbers and get quotes.',
    preview: 'Gas or heat pump?',
    created_at: '2026-10-18T09:00:00Z',
    updated_at: '2026-10-18T09:05:00Z',
    ...overrides,
  };
}

const QUOTES = thread({ conversation_id: 'web_conv_quotes', source_tool_call_id: 'call_quotes' });

interface ThreadsServer {
  threads: ThreadSummary[];
  conversationListUrls: string[];
}

/** Threads on, with Home holding a start_thread call whose thread is QUOTES. */
function serveThreads(threads: ThreadSummary[] = [QUOTES]): ThreadsServer {
  const state: ThreadsServer = { threads, conversationListUrls: [] };
  server.use(
    http.get('/api/v1/chat/home', () =>
      HttpResponse.json({ enabled: true, conversation_id: HOME })
    ),
    http.get('/api/v1/chat/home/threads', () =>
      HttpResponse.json({ home_conversation_id: HOME, threads: state.threads })
    ),
    http.get('/api/v1/chat/conversations', ({ request }) => {
      state.conversationListUrls.push(request.url);
      return HttpResponse.json({
        conversations: [
          {
            conversation_id: HOME,
            last_message: 'Home preview',
            last_timestamp: '2026-10-18T09:10:00Z',
            message_count: 4,
            kind: 'home',
          },
          {
            conversation_id: 'web_conv_old',
            last_message: 'Kitchen reno budget',
            last_timestamp: '2026-10-01T09:00:00Z',
            message_count: 2,
            kind: 'conversation',
          },
        ],
        count: 2,
      });
    }),
    http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
      const conversationId = String(params.conversationId);
      if (conversationId === HOME) {
        return HttpResponse.json({
          conversation_kind: 'home',
          latest_user_profile_id: 'default_assistant',
          messages: [
            {
              internal_id: 1,
              role: 'user',
              content: 'Can you find a plumber and get a couple of quotes?',
              timestamp: '2026-10-18T09:00:00Z',
            },
            {
              internal_id: 2,
              role: 'assistant',
              content: '',
              timestamp: '2026-10-18T09:00:01Z',
              tool_calls: [
                {
                  id: 'call_lookup',
                  type: 'function',
                  function: { name: 'search_documents', arguments: '{"query":"plumber"}' },
                },
                {
                  id: 'call_quotes',
                  type: 'function',
                  function: {
                    name: 'start_thread',
                    arguments: JSON.stringify({
                      target_service_id: 'research',
                      title: 'Hot water system quotes',
                      request: 'Find three plumbers and get quotes.',
                    }),
                  },
                },
                {
                  id: 'call_refused',
                  type: 'function',
                  function: {
                    name: 'start_thread',
                    arguments: JSON.stringify({
                      target_service_id: 'coder',
                      title: 'Refused job',
                      request: 'Do something.',
                    }),
                  },
                },
              ],
            },
            {
              internal_id: 3,
              role: 'tool',
              tool_call_id: 'call_lookup',
              content: 'Two plumbers found.',
              timestamp: '2026-10-18T09:00:02Z',
            },
            {
              internal_id: 4,
              role: 'tool',
              tool_call_id: 'call_quotes',
              content: 'Started the thread.',
              timestamp: '2026-10-18T09:00:02Z',
            },
            {
              internal_id: 5,
              role: 'tool',
              tool_call_id: 'call_refused',
              content: 'Error: not allowed',
              timestamp: '2026-10-18T09:00:02Z',
            },
            {
              internal_id: 6,
              role: 'assistant',
              content: "On it. I've started a thread for it.",
              timestamp: '2026-10-18T09:00:03Z',
            },
          ],
        });
      }
      const opened = state.threads.find((entry) => entry.conversation_id === conversationId);
      if (opened) {
        return HttpResponse.json({
          conversation_kind: 'thread',
          thread: opened,
          latest_user_profile_id: opened.profile_id,
          messages: [
            {
              internal_id: 10,
              role: 'assistant',
              content: 'Gas or heat pump?',
              timestamp: '2026-10-18T09:05:00Z',
            },
          ],
        });
      }
      return HttpResponse.json({
        conversation_kind: 'conversation',
        messages: [
          {
            internal_id: 20,
            role: 'user',
            content: `Hello from ${conversationId}`,
            timestamp: '2026-10-01T09:00:00Z',
          },
        ],
      });
    })
  );
  return state;
}

describe('Home and threads', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/');
    document.documentElement.removeAttribute('data-app-ready');
  });

  it('lands on Home instead of the last conversation, on the default profile', async () => {
    serveThreads();
    mockLocalStorage.getItem.mockImplementation((key: string) => {
      if (key === 'lastConversationId') {
        return 'web_conv_old';
      }
      return key === 'selectedProfileId' ? 'research' : null;
    });

    await renderChatApp({ waitForReady: true });

    expect(await screen.findByText("On it. I've started a thread for it.")).toBeInTheDocument();
    expect(screen.queryByText('Hello from web_conv_old')).not.toBeInTheDocument();
    expect(screen.getByTestId('conversation-title')).toHaveTextContent('Home');
    expect(window.location.search).toBe(`?conversation_id=${HOME}`);
    const picker = await screen.findByRole('combobox', { name: 'Processing profile' });
    await waitFor(() => expect(picker).toHaveTextContent('Assistant'));
    expect(picker).toBeDisabled();
  });

  it('shows a start_thread call as a card that stays out of the collapsed tool group', async () => {
    serveThreads();

    await renderChatApp({ waitForReady: true });

    const card = await screen.findByTestId('thread-card');
    expect(card).toHaveAttribute('data-thread-state', 'waiting');
    expect(within(card).getByTestId('thread-card-title')).toHaveTextContent(
      'Hot water system quotes'
    );
    expect(within(card).getByTestId('thread-card-profile')).toHaveTextContent('Research');
    expect(within(card).getByTestId('thread-card-preview')).toHaveTextContent('Gas or heat pump?');
    expect(card.closest('[data-testid="tool-group-content"]')).toBeNull();
    // The other calls still fold into their groups.
    expect(screen.getAllByTestId('tool-group-content').length).toBeGreaterThan(0);
  });

  it('renders a start_thread call with no thread as an ordinary tool call', async () => {
    serveThreads();

    await renderChatApp({ waitForReady: true });

    await screen.findByTestId('thread-card');
    const standalone = screen.getAllByTestId('standalone-tool-call');
    expect(standalone).toHaveLength(2);
    expect(within(standalone[1]).queryByTestId('thread-card')).toBeNull();
    expect(within(standalone[1]).getByTestId('tool-call')).toBeInTheDocument();
  });

  it('opens a thread from its card and returns to Home from the breadcrumb', async () => {
    const user = userEvent.setup();
    serveThreads();

    await renderChatApp({ waitForReady: true });
    await user.click(await screen.findByTestId('thread-card'));

    const banner = await screen.findByTestId('thread-banner');
    expect(window.location.search).toBe('?conversation_id=web_conv_quotes');
    expect(within(banner).getByTestId('thread-origin')).toHaveTextContent(
      'Started from Home · Assistant asked: “Find three plumbers and get quotes.”'
    );
    expect(screen.getByTestId('conversation-title')).toHaveTextContent('Hot water system quotes');
    const picker = screen.getByRole('combobox', { name: 'Processing profile' });
    await waitFor(() => expect(picker).toHaveTextContent('Research'));
    expect(picker).toBeDisabled();

    await user.click(within(banner).getByTestId('thread-breadcrumb-home'));

    expect(await screen.findByText("On it. I've started a thread for it.")).toBeInTheDocument();
    expect(screen.queryByTestId('thread-banner')).not.toBeInTheDocument();
    expect(window.location.search).toBe(`?conversation_id=${HOME}`);
  });

  it('shows a working thread as working', async () => {
    serveThreads([{ ...QUOTES, state: 'working' }]);
    window.history.replaceState({}, '', '/chat?conversation_id=web_conv_quotes');

    await renderChatApp({ waitForReady: true });

    expect(await screen.findByTestId('thread-working-indicator')).toBeInTheDocument();
    expect(screen.getByTestId('thread-banner')).toHaveAttribute('data-thread-state', 'working');
  });

  it('groups the sidebar into Home, threads by state, and earlier conversations', async () => {
    const threads = serveThreads([
      QUOTES,
      thread({ conversation_id: 'web_conv_flights', title: 'Flights', state: 'working' }),
      thread({ conversation_id: 'web_conv_rego', title: 'Car rego', state: 'failed' }),
    ]);

    await renderChatApp({ waitForReady: true });

    const homeItem = await screen.findByTestId('home-item');
    expect(homeItem).toHaveAttribute('data-conversation-id', HOME);
    expect(
      within(await screen.findByTestId('thread-group-waiting')).getByTestId(
        'thread-item-web_conv_quotes'
      )
    ).toHaveTextContent('Gas or heat pump?');
    expect(
      within(screen.getByTestId('thread-group-working')).getByTestId('thread-item-web_conv_flights')
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId('thread-group-failed')).getByTestId('thread-item-web_conv_rego')
    ).toBeInTheDocument();
    expect(screen.getByText('Earlier')).toBeInTheDocument();
    expect(
      screen
        .getAllByTestId(/^conversation-item-/)
        .map((item) => item.getAttribute('data-conversation-id'))
    ).toEqual(['web_conv_old']);
    await waitFor(() =>
      expect(threads.conversationListUrls.some((url) => url.includes('kind=ordinary'))).toBe(true)
    );
  });

  it('says when Home could not be loaded, and lands on it once a retry succeeds', async () => {
    const user = userEvent.setup();
    serveThreads();
    let homeAvailable = false;
    server.use(
      http.get('/api/v1/chat/home', () =>
        homeAvailable
          ? HttpResponse.json({ enabled: true, conversation_id: HOME })
          : new HttpResponse(null, { status: 500 })
      )
    );
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

    await renderChatApp({ waitForReady: true });

    const alert = await screen.findByTestId('home-lookup-failed');
    expect(screen.queryByTestId('home-item')).not.toBeInTheDocument();

    homeAvailable = true;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText("On it. I've started a thread for it.")).toBeInTheDocument();
    expect(screen.queryByTestId('home-lookup-failed')).not.toBeInTheDocument();
    expect(screen.getByTestId('home-item')).toBeInTheDocument();
    consoleError.mockRestore();
  });

  it('says when the thread list could not be loaded, and shows it once a retry succeeds', async () => {
    const user = userEvent.setup();
    const threads = serveThreads();
    let threadsAvailable = false;
    server.use(
      http.get('/api/v1/chat/home/threads', () =>
        threadsAvailable
          ? HttpResponse.json({ home_conversation_id: HOME, threads: threads.threads })
          : new HttpResponse(null, { status: 500 })
      )
    );
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

    await renderChatApp({ waitForReady: true });

    const alert = await screen.findByTestId('threads-load-failed');
    threadsAvailable = true;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));

    expect(await screen.findByTestId('thread-item-web_conv_quotes')).toBeInTheDocument();
    expect(screen.queryByTestId('threads-load-failed')).not.toBeInTheDocument();
    consoleError.mockRestore();
  });

  it('keeps a draft typed before Home was known', async () => {
    const user = userEvent.setup();
    serveThreads();
    let releaseHome: () => void = () => {};
    const homeGate = new Promise<void>((resolve) => {
      releaseHome = resolve;
    });
    server.use(
      http.get('/api/v1/chat/home', async () => {
        await homeGate;
        return HttpResponse.json({ enabled: true, conversation_id: HOME });
      })
    );

    await renderChatApp({ waitForReady: true });
    await user.type(screen.getByRole('textbox'), 'Is the bin out?');
    releaseHome();

    expect(await screen.findByText("On it. I've started a thread for it.")).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('Is the bin out?');
  });

  it('moves a thread between groups when a turn ends and the list is refetched', async () => {
    const user = userEvent.setup();
    const threads = serveThreads();

    await renderChatApp({ waitForReady: true });
    expect(await screen.findByTestId('thread-group-waiting')).toBeInTheDocument();

    threads.threads = [{ ...QUOTES, state: 'working' }];
    await user.click(screen.getByTestId('thread-item-web_conv_quotes'));
    await screen.findByTestId('thread-banner');
    await user.type(screen.getByRole('textbox'), 'Either is fine');
    await user.keyboard('{Enter}');

    expect(await screen.findByTestId('thread-group-working')).toBeInTheDocument();
    expect(screen.queryByTestId('thread-group-waiting')).not.toBeInTheDocument();
  });
});
