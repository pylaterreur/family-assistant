import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it } from 'vitest';
import { TooltipProvider } from '@/components/ui/tooltip';
import AppShell, { AppTopBar } from '../AppShell';
import { findActiveNavItem, isNavItemActive, NAV_SECTIONS } from '../navigation';

describe('navigation matching', () => {
  const documents = NAV_SECTIONS.flatMap((section) => section.items).find(
    (item) => item.to === '/documents'
  )!;

  it.each([
    ['/documents', true],
    ['/documents/', true],
    ['/documents/upload', true],
    ['/documents-archive', false],
    ['/', false],
  ])('matches %s against Documents: %s', (pathname, expected) => {
    expect(isNavItemActive(documents, pathname)).toBe(expected);
  });

  it('matches Home only at the root', () => {
    expect(findActiveNavItem('/')?.title).toBe('Home');
    expect(findActiveNavItem('/chat')?.title).toBe('Chat');
    expect(findActiveNavItem('/no-such-page')).toBeUndefined();
  });
});

describe('MobileNav drawer', () => {
  it.each(['', '/app'])(
    'closes after navigation while preserving the %s router base',
    async (basename) => {
      const user = userEvent.setup();
      render(
        <MemoryRouter basename={basename} initialEntries={[`${basename}/context`]}>
          <TooltipProvider>
            <AppTopBar />
          </TooltipProvider>
          <Routes>
            <Route path="/context" element={<h1>Context page</h1>} />
            <Route path="/notes" element={<h1>Notes page</h1>} />
          </Routes>
        </MemoryRouter>
      );
      await user.click(screen.getByRole('button', { name: 'Open navigation menu' }));
      const drawer = screen.getByRole('dialog');
      expect(within(drawer).getByRole('link', { name: 'Context' })).toHaveAttribute(
        'aria-current',
        'page'
      );
      const notesLink = within(drawer).getByRole('link', { name: 'Notes' });
      expect(notesLink).toHaveAttribute('href', `${basename}/notes`);
      await user.click(notesLink);
      expect(await screen.findByRole('heading', { name: 'Notes page' })).toBeInTheDocument();
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    }
  );

  it('titles the bar after the current page', () => {
    render(
      <MemoryRouter initialEntries={['/settings/tokens']}>
        <AppTopBar />
      </MemoryRouter>
    );
    expect(screen.getByRole('banner')).toHaveTextContent('API tokens');
  });
});

describe('AppShell sidebar', () => {
  const originalMatchMedia = window.matchMedia;

  function setWide(wide: boolean) {
    window.matchMedia = ((query: string) => ({
      ...originalMatchMedia(query),
      matches: wide,
    })) as typeof window.matchMedia;
  }

  function renderShell() {
    return render(
      <MemoryRouter initialEntries={['/notes']}>
        <AppShell>
          <p>content</p>
        </AppShell>
      </MemoryRouter>
    );
  }

  afterEach(() => {
    window.matchMedia = originalMatchMedia;
    window.localStorage.removeItem('family-assistant-nav-collapsed');
  });

  it('collapses to an icon rail and remembers the choice', async () => {
    setWide(true);
    const user = userEvent.setup();
    const { unmount } = renderShell();
    const sidebar = screen.getByTestId('app-sidebar');
    expect(sidebar).toHaveAttribute('data-collapsed', 'false');
    expect(within(sidebar).getByText('Notes')).toBeInTheDocument();

    await user.click(within(sidebar).getByRole('button', { name: 'Collapse navigation' }));
    expect(sidebar).toHaveAttribute('data-collapsed', 'true');
    expect(within(sidebar).queryByText('Notes')).not.toBeInTheDocument();
    expect(within(sidebar).getByRole('link', { name: 'Notes' })).toHaveAttribute(
      'aria-current',
      'page'
    );
    unmount();

    renderShell();
    expect(screen.getByTestId('app-sidebar')).toHaveAttribute('data-collapsed', 'true');
  });

  it('is always the rail on tablet widths, without a toggle', () => {
    setWide(false);
    renderShell();
    const sidebar = screen.getByTestId('app-sidebar');
    expect(sidebar).toHaveAttribute('data-collapsed', 'true');
    expect(within(sidebar).queryByRole('button', { name: /navigation/ })).not.toBeInTheDocument();
    expect(
      within(sidebar).getByRole('link', { name: 'Family Assistant home' })
    ).toBeInTheDocument();
  });
});
