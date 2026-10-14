import { describe, expect, it } from 'vitest';
import { interleaveTextAndToolCalls } from '../orderedParts';
import type { MessageContent } from '../types';

function toolPart(id: string, textOffset?: number): MessageContent {
  return { type: 'tool-call', toolCallId: id, toolName: 'get_note', textOffset };
}

describe('interleaveTextAndToolCalls', () => {
  it('places each call between the text before and after it', () => {
    const text = 'Let me check. Found it: milk.';

    const parts = interleaveTextAndToolCalls(text, [toolPart('a', 13)]);

    expect(parts).toEqual([
      { type: 'text', text: 'Let me check.' },
      toolPart('a', 13),
      { type: 'text', text: ' Found it: milk.' },
    ]);
  });

  it('keeps consecutive calls adjacent so they group together', () => {
    const parts = interleaveTextAndToolCalls('Done.', [toolPart('a', 0), toolPart('b', 0)]);

    expect(parts.map((part) => part.toolCallId ?? part.text)).toEqual(['a', 'b', 'Done.']);
  });

  it('drops whitespace between calls rather than splitting their group', () => {
    const parts = interleaveTextAndToolCalls('\n\nAnswer', [toolPart('a', 0), toolPart('b', 2)]);

    expect(parts.map((part) => part.toolCallId ?? part.text)).toEqual(['a', 'b', 'Answer']);
  });

  it('puts a call without an offset after the text', () => {
    const parts = interleaveTextAndToolCalls('Answer', [toolPart('a')]);

    expect(parts.map((part) => part.toolCallId ?? part.text)).toEqual(['Answer', 'a']);
  });
});
