import { humanizeToolName, summarizeToolArgs } from './toolSummary';
import type { ToolCallDisplay } from './types';

export type ToolGroupCallState = 'running' | 'awaiting-approval' | 'succeeded' | 'unsuccessful';

export interface ToolGroupCall {
  toolName: string;
  display?: ToolCallDisplay;
  args?: Record<string, unknown>;
  state: ToolGroupCallState;
}

export interface ToolGroupSummary {
  /** One line naming what is happening now, or the call that mattered most. */
  headline: string;
  /** Secondary text: progress while running, failures once finished. */
  detail: string | null;
  isRunning: boolean;
  needsApproval: boolean;
  /** The call the headline names. */
  subject?: ToolGroupCall;
}

/**
 * The phrase for one call. The server describes every call it sends; a call
 * without a description (a stream from before it did) reads as its name and
 * most telling argument.
 */
export function toolCallPhrase(call: ToolGroupCall, tense: 'active' | 'done'): string {
  if (call.display) {
    return call.display[tense];
  }
  const name = humanizeToolName(call.toolName);
  const argument = summarizeToolArgs(call.args);
  return argument ? `${name} · ${argument}` : name;
}

function last<T>(items: readonly T[], predicate: (item: T) => boolean): T | undefined {
  for (let index = items.length - 1; index >= 0; index--) {
    if (predicate(items[index])) {
      return items[index];
    }
  }
  return undefined;
}

/**
 * The collapsed header of a group of consecutive tool calls.
 *
 * While work is in flight it names the latest call still running, so the
 * line reads as a live ticker. Once everything has finished it names the call
 * that mattered most — the latest one that changed something, otherwise the
 * latest that succeeded — and counts the rest.
 */
export function summarizeToolGroup(calls: readonly ToolGroupCall[]): ToolGroupSummary {
  const unsuccessful = calls.filter((call) => call.state === 'unsuccessful').length;
  const failureDetail = unsuccessful > 0 ? `${unsuccessful} didn't finish` : null;

  const awaiting = last(calls, (call) => call.state === 'awaiting-approval');
  if (awaiting) {
    return {
      headline: `Needs your approval: ${toolCallPhrase(awaiting, 'active')}`,
      detail: failureDetail,
      isRunning: true,
      needsApproval: true,
      subject: awaiting,
    };
  }

  const running = last(calls, (call) => call.state === 'running');
  if (running) {
    const finished = calls.filter(
      (call) => call.state === 'succeeded' || call.state === 'unsuccessful'
    ).length;
    return {
      headline: toolCallPhrase(running, 'active'),
      detail: finished > 0 ? `${finished} done` : null,
      isRunning: true,
      needsApproval: false,
      subject: running,
    };
  }

  const key =
    last(calls, (call) => call.state === 'succeeded' && Boolean(call.display?.changes)) ??
    last(calls, (call) => call.state === 'succeeded') ??
    calls[calls.length - 1];
  if (!key) {
    return { headline: 'No tool calls', detail: null, isRunning: false, needsApproval: false };
  }
  const phrase = toolCallPhrase(key, key.state === 'succeeded' ? 'done' : 'active');
  const others = calls.length - 1;
  return {
    headline: others > 0 ? `${phrase} and ${others} more` : phrase,
    detail: failureDetail,
    isRunning: false,
    needsApproval: false,
    subject: key,
  };
}
