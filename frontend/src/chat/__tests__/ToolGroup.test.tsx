import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ToolGroup } from '../ToolGroup';
import type { ToolGroupCall } from '../toolGroupSummary';
import { ToolGroupShell } from '../ToolGroupShell';

describe('ToolGroup', () => {
  const mockChildren = (
    <div data-testid="tool-content">
      <div>Tool 1</div>
      <div>Tool 2</div>
    </div>
  );

  it('expands when clicked', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const content = screen.getByTestId('tool-group-content');

    expect(content).toHaveAttribute('data-state', 'closed');
    expect(trigger).toHaveAttribute('data-state', 'closed');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    await user.click(trigger);

    expect(content).toHaveAttribute('data-state', 'open');
    expect(trigger).toHaveAttribute('data-state', 'open');
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
  });

  it('collapses again when clicked while expanded', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const content = screen.getByTestId('tool-group-content');

    // First expand
    await user.click(trigger);
    expect(content).toHaveAttribute('data-state', 'open');

    // Then collapse again
    await user.click(trigger);
    expect(content).toHaveAttribute('data-state', 'closed');
  });

  it('supports keyboard navigation with Enter key', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const content = screen.getByTestId('tool-group-content');

    // Focus the trigger
    await user.tab();
    expect(trigger).toHaveFocus();

    // Initially collapsed
    expect(content).toHaveAttribute('data-state', 'closed');

    // Press Enter to expand
    await user.keyboard('{Enter}');
    expect(content).toHaveAttribute('data-state', 'open');
  });

  it('supports keyboard navigation with Space key', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const content = screen.getByTestId('tool-group-content');

    // Focus the trigger
    await user.tab();
    expect(trigger).toHaveFocus();

    // Initially collapsed
    expect(content).toHaveAttribute('data-state', 'closed');

    // Press Space to expand
    await user.keyboard(' ');
    expect(content).toHaveAttribute('data-state', 'open');
  });

  it('renders children content when expanded', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const content = screen.getByTestId('tool-group-content');

    // Initially collapsed - content should be unmounted
    expect(content).toHaveAttribute('hidden');
    expect(screen.queryByTestId('tool-content')).not.toBeInTheDocument();

    // Expand
    await user.click(trigger);

    // Content should now be visible
    expect(content).not.toHaveAttribute('hidden');
    expect(screen.getByTestId('tool-content')).toBeInTheDocument();
    expect(screen.getByText('Tool 1')).toBeInTheDocument();
    expect(screen.getByText('Tool 2')).toBeInTheDocument();
  });

  it('has proper ARIA attributes', () => {
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');

    expect(trigger).toHaveAttribute('type', 'button');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  describe('collapsed header', () => {
    function renderShell(calls: ToolGroupCall[]): HTMLElement {
      render(
        <ToolGroupShell calls={calls} isExpanded={false} onOpenChange={() => {}}>
          {mockChildren}
        </ToolGroupShell>
      );
      return screen.getByTestId('tool-group-trigger');
    }

    const note = (state: ToolGroupCall['state']): ToolGroupCall => ({
      toolName: 'add_or_update_note',
      display: { active: 'Saving note “Groceries”', done: 'Saved note “Groceries”', changes: true },
      state,
    });
    const search = (state: ToolGroupCall['state']): ToolGroupCall => ({
      toolName: 'search_documents',
      display: {
        active: 'Searching documents for “milk”',
        done: 'Searched documents for “milk”',
        changes: false,
      },
      state,
    });

    it('names the call in progress with a spinner while the group runs', () => {
      const trigger = renderShell([search('succeeded'), note('running')]);

      expect(trigger).toHaveTextContent('Saving note “Groceries”');
      expect(trigger).toHaveTextContent('1 done');
      expect(screen.getByTestId('tool-group-spinner')).toBeInTheDocument();
    });

    it('names the call that changed something once the group finishes', () => {
      const trigger = renderShell([note('succeeded'), search('succeeded')]);

      expect(trigger).toHaveTextContent('Saved note “Groceries” and 1 more');
      expect(screen.queryByTestId('tool-group-spinner')).not.toBeInTheDocument();
    });

    it('counts calls that did not succeed', () => {
      const trigger = renderShell([search('succeeded'), note('unsuccessful')]);

      expect(screen.getByTestId('tool-group-headline')).toHaveTextContent(
        'Searched documents for “milk” and 1 more'
      );
      expect(trigger).toHaveTextContent("1 didn't finish");
    });

    it('asks for approval ahead of anything else', () => {
      const trigger = renderShell([search('succeeded'), note('awaiting-approval')]);

      expect(trigger).toHaveTextContent('Needs your approval: Saving note “Groceries”');
    });
  });
});
