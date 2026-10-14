import type { MessageContent, ToolCallDisplay } from './types';

/**
 * A streamed turn's content in the order it happened.
 *
 * The stream delivers one growing text and a list of tool calls; each call
 * records how much of the text had arrived when it started (`textOffset`).
 * Splitting the text at those offsets puts every tool call between the text
 * written before it and the text written after it, so text that follows a
 * lookup is drawn below the lookup rather than above it.
 *
 * A call without an offset sorts after all text. Whitespace-only stretches
 * between calls are dropped: they would split one group of calls into two.
 */
export function interleaveTextAndToolCalls(
  text: string,
  toolParts: readonly MessageContent[]
): MessageContent[] {
  const parts: MessageContent[] = [];
  let consumed = 0;
  const pushText = (end: number) => {
    const segment = text.slice(consumed, end);
    if (segment.trim()) {
      parts.push({ type: 'text', text: segment });
    }
    consumed = Math.max(consumed, end);
  };
  for (const toolPart of toolParts) {
    const offset =
      typeof toolPart.textOffset === 'number'
        ? Math.min(toolPart.textOffset, text.length)
        : text.length;
    if (offset > consumed) {
      pushText(offset);
    }
    parts.push(toolPart);
  }
  pushText(text.length);
  return parts;
}

export function toolCallParts(content: readonly MessageContent[] | undefined): MessageContent[] {
  return content?.filter((part) => part.type === 'tool-call') ?? [];
}

export function isToolCallDisplay(value: unknown): value is ToolCallDisplay {
  if (!value || typeof value !== 'object') {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.active === 'string' &&
    typeof candidate.done === 'string' &&
    typeof candidate.changes === 'boolean'
  );
}
