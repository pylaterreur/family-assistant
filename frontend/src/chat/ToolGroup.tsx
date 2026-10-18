import { useAuiState } from '@assistant-ui/react';
import React, { useContext, useEffect, useMemo, useRef, useState } from 'react';
import { ToolConfirmationContext } from './ToolConfirmationContext';
import { isToolCallDisplay } from './orderedParts';
import type { ToolGroupCall } from './toolGroupSummary';
import { ToolGroupShell } from './ToolGroupShell';
import { isTerminalToolOutcome, resolveToolOutcome } from './toolOutcome';
import type { ToolCallDisplay } from './types';

interface ToolGroupProps {
  startIndex: number;
  endIndex: number;
  children: React.ReactNode;
}

interface ToolGroupCallSnapshot {
  toolName: string;
  toolCallId?: string;
  display?: ToolCallDisplay;
  args?: Record<string, unknown>;
  state: 'running' | 'succeeded' | 'unsuccessful';
}

interface MessagePartLike {
  type: string;
  toolName?: string;
  toolCallId?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  artifact?: unknown;
  attachments?: unknown[];
  outcome?: unknown;
  isError?: boolean;
  awaitingResult?: boolean;
  display?: unknown;
  status?: {
    type?: string;
    reason?: string;
  };
}

// `status` is the runtime's part status (from `message.parts`), which it derives
// from the message status for a tool call without a result: so a historical
// call whose result was never recorded is complete, not running.
function isTerminalToolPart(part: MessagePartLike): boolean {
  return (
    (part.status?.type !== 'running' && part.status?.type !== 'requires-action') ||
    part.result !== undefined ||
    part.artifact !== undefined ||
    part.attachments !== undefined ||
    (part.toolName === 'attach_to_response' && Array.isArray(part.args?.attachment_ids))
  );
}

// A finished group collapses, so a call that did not succeed is counted in the
// group's header rather than left for the user to find by opening it.
function isUnsuccessfulToolPart(part: MessagePartLike): boolean {
  const outcome = resolveToolOutcome({
    status: part.status?.type ? { type: part.status.type, reason: part.status.reason } : undefined,
    result: part.result,
    outcome: isTerminalToolOutcome(part.outcome) ? part.outcome : undefined,
    isError: part.isError,
    awaitingResult: part.awaitingResult,
  });
  return outcome === 'failed' || outcome === 'rejected' || outcome === 'unknown';
}

function getToolGroupCalls(
  parts: readonly MessagePartLike[],
  startIndex: number,
  endIndex: number
): ToolGroupCallSnapshot[] {
  const calls: ToolGroupCallSnapshot[] = [];
  for (let i = startIndex; i <= endIndex && i < parts.length; i++) {
    const part = parts[i];
    if (part.type !== 'tool-call') {
      continue;
    }
    let state: ToolGroupCallSnapshot['state'] = 'succeeded';
    if (!isTerminalToolPart(part)) {
      state = 'running';
    } else if (isUnsuccessfulToolPart(part)) {
      state = 'unsuccessful';
    }
    calls.push({
      toolName: part.toolName ?? 'tool',
      toolCallId: part.toolCallId,
      display: isToolCallDisplay(part.display) ? part.display : undefined,
      args: part.args,
      state,
    });
  }
  return calls;
}

// Serialized so the selector returns a stable value: the runtime hands out a
// new parts array on every streamed token.
function useToolGroupCalls(startIndex: number, endIndex: number): ToolGroupCallSnapshot[] {
  const serialized = useAuiState((s) => {
    const message = s.optional.message;
    if (!message) {
      return undefined;
    }
    return JSON.stringify(
      getToolGroupCalls(message.parts as readonly MessagePartLike[], startIndex, endIndex)
    );
  });

  return useMemo(
    () => (serialized ? (JSON.parse(serialized) as ToolGroupCallSnapshot[]) : []),
    [serialized]
  );
}

const ToolGroupRun: React.FC<ToolGroupProps> = ({ startIndex, endIndex, children }) => {
  const context = useContext(ToolConfirmationContext);
  const snapshots = useToolGroupCalls(startIndex, endIndex);
  const pendingConfirmations = context?.pendingConfirmations;

  const calls: ToolGroupCall[] = useMemo(
    () =>
      snapshots.map(({ toolCallId, ...call }) =>
        toolCallId && pendingConfirmations?.has(toolCallId)
          ? { ...call, state: 'awaiting-approval' as const }
          : call
      ),
    [snapshots, pendingConfirmations]
  );

  // A group opens only for an approval the user has to give. Otherwise it
  // stays one line — the header names what is running, and counts what did
  // not finish. Once the user toggles it, their choice holds.
  const shouldAutoExpand = calls.some((call) => call.state === 'awaiting-approval');
  const [isExpanded, setIsExpanded] = useState(shouldAutoExpand);
  const hasUserToggled = useRef(false);

  useEffect(() => {
    if (!hasUserToggled.current) {
      setIsExpanded(shouldAutoExpand);
    }
  }, [shouldAutoExpand]);

  const handleOpenChange = (open: boolean) => {
    hasUserToggled.current = true;
    setIsExpanded(open);
  };

  return (
    <ToolGroupShell calls={calls} isExpanded={isExpanded} onOpenChange={handleOpenChange}>
      {children}
    </ToolGroupShell>
  );
};

// Calls that render as a card of their own, which must stay in view rather than
// fold away with the rest of a finished group.
const STANDALONE_TOOL_NAMES = new Set(['start_thread']);

// One character per part of the range, 's' for a standalone call: a string, so
// the selector's value is stable across streamed tokens.
function useStandaloneParts(startIndex: number, endIndex: number): string {
  return useAuiState((s) => {
    const parts = (s.optional.message?.parts ?? []) as readonly MessagePartLike[];
    let flags = '';
    for (let i = startIndex; i <= endIndex; i++) {
      const part = parts[i];
      flags +=
        part?.type === 'tool-call' && STANDALONE_TOOL_NAMES.has(part.toolName ?? '') ? 's' : '-';
    }
    return flags;
  });
}

const ToolGroup: React.FC<ToolGroupProps> = ({ startIndex, endIndex, children }) => {
  const standalone = useStandaloneParts(startIndex, endIndex);
  if (!standalone.includes('s')) {
    return (
      <ToolGroupRun startIndex={startIndex} endIndex={endIndex}>
        {children}
      </ToolGroupRun>
    );
  }
  // The rest of the range still groups, split around each standalone call.
  const items = React.Children.toArray(children);
  const rendered: React.ReactNode[] = [];
  let runStart: number | null = null;
  const closeRun = (end: number) => {
    if (runStart === null) {
      return;
    }
    rendered.push(
      <ToolGroupRun
        key={`run-${startIndex + runStart}`}
        startIndex={startIndex + runStart}
        endIndex={startIndex + end}
      >
        {items.slice(runStart, end + 1)}
      </ToolGroupRun>
    );
    runStart = null;
  };
  items.forEach((item, offset) => {
    if (standalone[offset] === 's') {
      closeRun(offset - 1);
      rendered.push(
        <div key={`standalone-${startIndex + offset}`} data-testid="standalone-tool-call">
          {item}
        </div>
      );
    } else if (runStart === null) {
      runStart = offset;
    }
  });
  closeRun(items.length - 1);
  return <>{rendered}</>;
};

export { ToolGroup };
