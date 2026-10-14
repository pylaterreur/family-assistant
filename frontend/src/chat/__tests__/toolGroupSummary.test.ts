import { describe, expect, it } from 'vitest';
import { summarizeToolGroup, type ToolGroupCall, toolCallPhrase } from '../toolGroupSummary';

function call(
  toolName: string,
  state: ToolGroupCall['state'],
  phrase?: { active: string; done: string; changes?: boolean }
): ToolGroupCall {
  return {
    toolName,
    state,
    display: phrase ? { changes: false, ...phrase } : undefined,
  };
}

const readNote = (state: ToolGroupCall['state']) =>
  call('get_note', state, { active: 'Reading note “A”', done: 'Read note “A”' });
const addEvent = (state: ToolGroupCall['state']) =>
  call('add_calendar_event', state, {
    active: 'Adding event “Swim”',
    done: 'Added event “Swim”',
    changes: true,
  });
const checkCalendar = (state: ToolGroupCall['state']) =>
  call('search_calendar_events', state, {
    active: 'Checking calendar',
    done: 'Checked calendar',
  });

describe('summarizeToolGroup', () => {
  it('names the latest call still running', () => {
    const summary = summarizeToolGroup([readNote('succeeded'), checkCalendar('running')]);

    expect(summary).toMatchObject({
      headline: 'Checking calendar',
      detail: '1 done',
      isRunning: true,
    });
  });

  it('prefers a call that changed something over a later read', () => {
    const summary = summarizeToolGroup([
      checkCalendar('succeeded'),
      addEvent('succeeded'),
      readNote('succeeded'),
    ]);

    expect(summary.headline).toBe('Added event “Swim” and 2 more');
    expect(summary.isRunning).toBe(false);
  });

  it('falls back to the latest successful call', () => {
    const summary = summarizeToolGroup([readNote('succeeded'), checkCalendar('succeeded')]);

    expect(summary.headline).toBe('Checked calendar and 1 more');
  });

  it('does not describe a change that failed as done', () => {
    const summary = summarizeToolGroup([addEvent('unsuccessful')]);

    expect(summary.headline).toBe('Adding event “Swim”');
    expect(summary.detail).toBe("1 didn't finish");
  });

  it('puts a pending approval first', () => {
    const summary = summarizeToolGroup([addEvent('awaiting-approval'), readNote('running')]);

    expect(summary.headline).toBe('Needs your approval: Adding event “Swim”');
    expect(summary.needsApproval).toBe(true);
  });
});

describe('toolCallPhrase', () => {
  it('reads a call the server did not describe as its name and key argument', () => {
    expect(
      toolCallPhrase(
        { toolName: 'query_trino', args: { sql: 'SELECT 1' }, state: 'succeeded' },
        'done'
      )
    ).toBe('Query trino · SELECT 1');
  });
});
