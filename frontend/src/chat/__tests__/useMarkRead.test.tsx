import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { markConversationRead } from '../threadsApi';
import { MARK_READ_DELAY_MS, useMarkRead } from '../useMarkRead';

vi.mock('../threadsApi', () => ({
  markConversationRead: vi.fn(),
}));

const markRead = vi.mocked(markConversationRead);

type Props = {
  conversationId: string | null;
  throughMessageId: number | null;
  localTurnEnd?: number | null;
};

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}

function renderMarkRead(initial: Props, onMarked = vi.fn()) {
  const hook = renderHook(
    (props: Props) => useMarkRead({ localTurnEnd: null, ...props, onMarked }),
    {
      initialProps: initial,
    }
  );
  return { ...hook, onMarked };
}

async function elapse(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe('useMarkRead', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    markRead.mockReset();
    markRead.mockResolvedValue(0);
    setVisibility('visible');
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('marks read once, through the newest message, after the transcript settles', async () => {
    const { rerender, onMarked } = renderMarkRead({ conversationId: 'c1', throughMessageId: 5 });
    await elapse(MARK_READ_DELAY_MS / 2);
    rerender({ conversationId: 'c1', throughMessageId: 6 });
    await elapse(MARK_READ_DELAY_MS / 2);
    rerender({ conversationId: 'c1', throughMessageId: 7 });
    expect(markRead).not.toHaveBeenCalled();

    await elapse(MARK_READ_DELAY_MS);

    expect(markRead).toHaveBeenCalledTimes(1);
    expect(markRead).toHaveBeenCalledWith('c1', 7);
    expect(onMarked).toHaveBeenCalledWith('c1', 0);
  });

  it('does not send a position it has already sent', async () => {
    const { rerender } = renderMarkRead({ conversationId: 'c1', throughMessageId: 7 });
    await elapse(MARK_READ_DELAY_MS);
    expect(markRead).toHaveBeenCalledTimes(1);

    rerender({ conversationId: 'c2', throughMessageId: 3 });
    await elapse(MARK_READ_DELAY_MS);
    rerender({ conversationId: 'c1', throughMessageId: 7 });
    await elapse(MARK_READ_DELAY_MS * 2);

    expect(markRead.mock.calls).toEqual([
      ['c1', 7],
      ['c2', 3],
    ]);
  });

  it('never marks read in a background tab, and catches up once visible', async () => {
    setVisibility('hidden');
    renderMarkRead({ conversationId: 'c1', throughMessageId: 7 });
    await elapse(MARK_READ_DELAY_MS * 4);
    expect(markRead).not.toHaveBeenCalled();

    act(() => setVisibility('visible'));
    await elapse(MARK_READ_DELAY_MS);

    expect(markRead).toHaveBeenCalledWith('c1', 7);
  });

  it('does not send if the tab goes to the background before the delay ends', async () => {
    renderMarkRead({ conversationId: 'c1', throughMessageId: 7 });
    await elapse(MARK_READ_DELAY_MS / 2);
    act(() => setVisibility('hidden'));
    await elapse(MARK_READ_DELAY_MS * 4);

    expect(markRead).not.toHaveBeenCalled();
  });

  it('marks nothing without a markable conversation or a stored message', async () => {
    const { rerender } = renderMarkRead({ conversationId: null, throughMessageId: 7 });
    await elapse(MARK_READ_DELAY_MS);
    rerender({ conversationId: 'c1', throughMessageId: null });
    await elapse(MARK_READ_DELAY_MS);

    expect(markRead).not.toHaveBeenCalled();
  });

  it('marks read through the newest message when a turn sent from here ends', async () => {
    const { rerender } = renderMarkRead({ conversationId: 'c1', throughMessageId: 7 });
    await elapse(MARK_READ_DELAY_MS);

    // The streamed reply has no stored id, so only the turn ending moves the position.
    rerender({ conversationId: 'c1', throughMessageId: 7, localTurnEnd: 1 });
    await elapse(MARK_READ_DELAY_MS);
    rerender({ conversationId: 'c1', throughMessageId: 7, localTurnEnd: 1 });
    await elapse(MARK_READ_DELAY_MS);
    rerender({ conversationId: 'c1', throughMessageId: 7, localTurnEnd: 2 });
    await elapse(MARK_READ_DELAY_MS);

    expect(markRead.mock.calls).toEqual([
      ['c1', 7],
      ['c1', undefined],
      ['c1', undefined],
    ]);
  });

  it('retries a failed position with the next message', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    markRead.mockRejectedValueOnce(new Error('boom'));
    const { rerender, onMarked } = renderMarkRead({ conversationId: 'c1', throughMessageId: 7 });
    await elapse(MARK_READ_DELAY_MS);
    expect(onMarked).not.toHaveBeenCalled();
    expect(consoleError).toHaveBeenCalled();

    rerender({ conversationId: 'c1', throughMessageId: 8 });
    await elapse(MARK_READ_DELAY_MS);

    expect(markRead.mock.calls).toEqual([
      ['c1', 7],
      ['c1', 8],
    ]);
    expect(onMarked).toHaveBeenCalledWith('c1', 0);
    consoleError.mockRestore();
  });
});
