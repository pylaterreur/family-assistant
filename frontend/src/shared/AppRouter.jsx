import React, { lazy, Suspense, useEffect } from 'react';
import { BrowserRouter, Outlet, Route, Routes, useMatch } from 'react-router-dom';
import { EMBEDDED_PAGE_BASE, isEmbeddedPage } from './embeddedNavigation';

// Lazy load all route components for code splitting
const AppShell = lazy(() => import('./AppShell.tsx'));
const ChatPage = lazy(() => import('../chat/ChatApp'));
const SharedConversationPage = lazy(() => import('../chat/SharedConversationPage'));
const ToolsApp = lazy(() => import('../tools/ToolsApp'));
const ErrorsApp = lazy(() => import('../errors/ErrorsApp'));
const ContextPage = lazy(() => import('./ContextPage.tsx'));
const NotesApp = lazy(() => import('../notes/NotesApp'));
const TasksApp = lazy(() => import('../tasks/TasksApp'));

const ArtifactReviewPage = lazy(() => import('../pages/Artifacts/ArtifactReviewPage'));
const ScriptsPage = lazy(() => import('../pages/Scripts/ScriptsPage'));
const AutomationsApp = lazy(() => import('../pages/Automations/AutomationsApp'));
const EventsApp = lazy(() => import('../pages/Events/EventsApp'));
const HistoryApp = lazy(() => import('../pages/History/HistoryApp'));
const DocumentationApp = lazy(() => import('../pages/Documentation/DocumentationApp'));
const TokenManagement = lazy(() => import('../pages/Settings/TokenManagement'));
const ConnectedAccounts = lazy(() => import('../pages/Settings/ConnectedAccounts'));
const DocumentsPage = lazy(() => import('../pages/Documents/DocumentsPage'));
const VectorSearchPage = lazy(() => import('../pages/VectorSearch/VectorSearchPage'));
const VoicePage = lazy(() => import('../voice/VoicePage'));
const LandingPage = lazy(() => import('../pages/LandingPage'));
const AboutPage = lazy(() => import('../pages/About/AboutPage'));

// Loading component for Suspense fallback
const LoadingSpinner = () => (
  <div
    data-loading-indicator="true"
    role="status"
    className="flex min-h-[200px] items-center justify-center text-muted-foreground"
  >
    Loading...
  </div>
);

const FallbackRedirect = () => {
  useEffect(() => {
    window.location.href = window.location.pathname;
  }, []);

  return (
    <div>
      <p>Page not yet converted to React. Redirecting...</p>
    </div>
  );
};

// One shell instance spans every page inside it, so moving between pages keeps
// the sidebar mounted (and its scroll position) instead of rebuilding it.
const ShellLayout = () => {
  const fullBleed = useMatch('/chat') !== null;
  return (
    <Suspense fallback={<LoadingSpinner />}>
      <AppShell fullBleed={fullBleed}>
        <Suspense fallback={<LoadingSpinner />}>
          <Outlet />
        </Suspense>
      </AppShell>
    </Suspense>
  );
};

const AppRouter = () => {
  return (
    <BrowserRouter basename={isEmbeddedPage() ? EMBEDDED_PAGE_BASE : undefined}>
      <Routes>
        {/* Shared transcripts are read-only views without the app navigation */}
        <Route
          path="/shared/conversations/:token"
          element={
            <Suspense fallback={<LoadingSpinner />}>
              <SharedConversationPage />
            </Suspense>
          }
        />

        <Route element={<ShellLayout />}>
          <Route path="/" element={<LandingPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/voice" element={<VoicePage />} />
          <Route path="/history/*" element={<HistoryApp />} />

          <Route path="/notes/*" element={<NotesApp />} />
          <Route path="/documents/*" element={<DocumentsPage />} />
          <Route path="/vector-search/*" element={<VectorSearchPage />} />
          <Route path="/context" element={<ContextPage />} />

          <Route path="/automations/*" element={<AutomationsApp />} />
          <Route path="/events/*" element={<EventsApp />} />
          <Route path="/scripts" element={<ScriptsPage />} />
          <Route path="/artifacts" element={<ArtifactReviewPage />} />

          <Route path="/tools" element={<ToolsApp />} />
          <Route path="/tasks/*" element={<TasksApp />} />
          <Route path="/errors/*" element={<ErrorsApp />} />

          <Route path="/settings/accounts" element={<ConnectedAccounts />} />
          <Route path="/settings/tokens" element={<TokenManagement />} />

          <Route path="/docs/*" element={<DocumentationApp />} />
          <Route path="/about" element={<AboutPage />} />
        </Route>

        {/* Catch-all for unmatched routes - redirect to external pages for now */}
        <Route path="*" element={<FallbackRedirect />} />
      </Routes>
    </BrowserRouter>
  );
};

export default AppRouter;
