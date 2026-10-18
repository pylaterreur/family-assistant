import SwiftUI

private struct HomeThreadsKey: EnvironmentKey {
    static let defaultValue: HomeThreadsStore? = nil
}

extension EnvironmentValues {
    /// Home's threads, present inside Home's navigation stack. Message bubbles
    /// read it to draw a `start_thread` call as the thread's card.
    var homeThreads: HomeThreadsStore? {
        get { self[HomeThreadsKey.self] }
        set { self[HomeThreadsKey.self] = newValue }
    }
}

/// The Chat tab's root: Home when the server gives the caller one, otherwise
/// the conversation list exactly as before threads.
struct ChatEntryView: View {
    let authManager: AuthManager
    let route: ChatRoute

    @State private var home: HomeAvailability.State
    @State private var hasRefreshed = false

    init(authManager: AuthManager, route: ChatRoute) {
        self.authManager = authManager
        self.route = route
        _home = State(
            initialValue: HomeAvailability.cached(serverURL: authManager.validatedServerURL()?.absoluteString)
        )
    }

    var body: some View {
        Group {
            switch home {
            case .unknown:
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .disabled:
                ChatRootView(authManager: authManager, route: route)
            case let .enabled(homeConversationID):
                HomeRootView(
                    authManager: authManager,
                    route: route,
                    homeConversationID: homeConversationID
                )
                .id(homeConversationID)
            }
        }
        .task {
            guard !hasRefreshed else {
                return
            }
            hasRefreshed = true
            await refresh()
        }
    }

    private func refresh() async {
        let serverURL = authManager.validatedServerURL()?.absoluteString
        do {
            let fetched = HomeAvailability.state(for: try await ChatAPIClient(authManager: authManager).getHome())
            HomeAvailability.store(fetched, serverURL: serverURL)
            home = HomeAvailability.resolved(current: home, fetched: fetched)
        } catch {
            ErrorReporter.shared.report(error, component: "Chat.home")
            // Without an answer, a first launch keeps the conversation list it
            // has always had; a remembered Home stays until the server says
            // otherwise.
            if home == .unknown {
                home = .disabled
            }
        }
    }
}

/// Where a conversation opened over Home comes from.
enum HomeDestination: Hashable {
    case conversation(String)
    case newConversation(requestID: String, initialPrompt: String?)
}

/// Home: the single landing stream, with threads and other conversations
/// pushed over it. Home stays mounted underneath, so going back returns to it
/// where the reader left it.
struct HomeRootView: View {
    @Environment(SharedAttachmentInbox.self) private var sharedAttachmentInbox
    @Environment(NotificationManager.self) private var notificationManager
    @State private var viewModel: ChatViewModel
    @State private var store: HomeThreadsStore
    @State private var path: [HomeDestination]
    @State private var showsSwitcher = false
    let authManager: AuthManager
    let route: ChatRoute

    init(authManager: AuthManager, route: ChatRoute, homeConversationID: String) {
        self.authManager = authManager
        self.route = route
        _viewModel = State(initialValue: ChatViewModel(
            authManager: authManager,
            conversationID: homeConversationID,
            role: .home
        ))
        _store = State(initialValue: HomeThreadsStore(
            homeConversationID: homeConversationID,
            apiClient: ChatAPIClient(authManager: authManager)
        ))
        switch HomeRouteAction.resolve(route, homeConversationID: homeConversationID) {
        case let .open(conversationID):
            _path = State(initialValue: [.conversation(conversationID)])
        case let .newConversation(requestID, initialPrompt):
            _path = State(initialValue: [.newConversation(requestID: requestID, initialPrompt: initialPrompt)])
        case .showHome, .sendInHome:
            _path = State(initialValue: [])
        }
    }

    var body: some View {
        NavigationStack(path: $path) {
            ChatThreadView(viewModel: viewModel, keepsScrollPositionOnReappear: true)
                .navigationTitle("Home")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { homeToolbar }
                .navigationDestination(for: HomeDestination.self) { destination in
                    PushedConversationView(
                        destination: destination,
                        authManager: authManager,
                        store: store
                    )
                }
        }
        .environment(\.homeThreads, store)
        .sheet(isPresented: $showsSwitcher) {
            ThreadSwitcherView(
                store: store,
                homePreview: homePreview,
                onSelect: { conversationID in
                    showsSwitcher = false
                    open(conversationID)
                },
                onNewChat: {
                    showsSwitcher = false
                    path = [.newConversation(requestID: UUID().uuidString, initialPrompt: nil)]
                }
            )
        }
        .task {
            // Only the path binding and Home's id are captured: the store holds
            // this callback, so capturing the view would keep the store and
            // Home's model alive after Home is removed.
            let path = $path
            let homeConversationID = store.homeConversationID
            store.openConversation = { conversationID in
                Self.open(conversationID, homeConversationID: homeConversationID, path: path)
            }
            viewModel.onListRefresh = { [weak store] in
                await store?.refreshForActivity()
            }
            async let threads: Void = store.refreshThreads()
            await viewModel.bootstrap()
            await threads
            await applyInHome(route)
            consumePendingPushHint()
        }
        .onChange(of: route) { _, newRoute in
            apply(newRoute)
        }
        .onChange(of: notificationManager.pendingPushHint) { _, _ in
            consumePendingPushHint()
        }
        .alert(
            "Chat Error",
            isPresented: Binding(
                get: { viewModel.errorMessage != nil },
                set: { if !$0 { viewModel.errorMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(viewModel.errorMessage ?? "")
        }
    }

    @ToolbarContentBuilder
    private var homeToolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .topBarLeading) {
            Menu {
                Button {
                    path = [.newConversation(requestID: UUID().uuidString, initialPrompt: nil)]
                } label: {
                    Label("New Chat", systemImage: "square.and.pencil")
                }
                Button {
                    showsSwitcher = true
                } label: {
                    Label("Threads", systemImage: "list.bullet.rectangle")
                }
            } label: {
                Label("Menu", systemImage: "line.3.horizontal")
            }
            .accessibilityIdentifier("home-menu")
            if viewModel.syncPresentation != .live {
                LiveUpdatesIndicator(viewModel: viewModel)
            }
        }
        ToolbarItem(placement: .principal) {
            HomeTitlePill()
        }
        ToolbarItemGroup(placement: .topBarTrailing) {
            IntelligencePickerView(viewModel: viewModel)
            ThreadsButton(waitingCount: store.waitingCount) {
                showsSwitcher = true
            }
        }
    }

    private var homePreview: String? {
        viewModel.messages.last { !$0.text.isEmpty }.map { String($0.text.prefix(200)) }
    }

    private func open(_ conversationID: String) {
        Self.open(conversationID, homeConversationID: store.homeConversationID, path: $path)
    }

    private static func open(
        _ conversationID: String,
        homeConversationID: String,
        path: Binding<[HomeDestination]>
    ) {
        if conversationID == homeConversationID {
            path.wrappedValue = []
        } else if path.wrappedValue.last != .conversation(conversationID) {
            path.wrappedValue = [.conversation(conversationID)]
        }
    }

    private func apply(_ route: ChatRoute) {
        switch HomeRouteAction.resolve(route, homeConversationID: store.homeConversationID) {
        case .showHome, .sendInHome:
            path = []
            Task { await applyInHome(route) }
        case let .open(conversationID):
            open(conversationID)
        case let .newConversation(requestID, initialPrompt):
            path = [.newConversation(requestID: requestID, initialPrompt: initialPrompt)]
        }
    }

    /// The parts of a route that act on Home itself: a shared hand-off goes
    /// into Home's composer, and a prompt is sent there. Safe while Home is
    /// still bootstrapping: the model holds a routed prompt until Home has
    /// loaded and adopted its profile, then sends it once. The other route
    /// kinds only change the navigation path, which needs no loaded Home.
    private func applyInHome(_ route: ChatRoute) async {
        if let batchID = route.sharedAttachmentBatchID,
           let batch = sharedAttachmentInbox.consume(batchID: batchID)
        {
            await viewModel.addSharedAttachments(batch)
        }
        if case let .sendInHome(prompt) = HomeRouteAction.resolve(route, homeConversationID: store.homeConversationID) {
            await viewModel.sendPromptInPlace(prompt)
        }
    }

    private func consumePendingPushHint() {
        guard let hint = notificationManager.pendingPushHint else {
            return
        }
        viewModel.pushHintReceived(conversationID: hint.conversationID)
        if let pushed = store.pushedModel, pushed.conversationID == hint.conversationID {
            pushed.pushHintReceived(conversationID: hint.conversationID)
        }
        notificationManager.clearPendingPushHint()
        Task { await store.refreshThreads() }
    }
}

private struct HomeTitlePill: View {
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "house.fill")
                .font(.footnote)
                .frame(width: 26, height: 26)
                .background(Color(.tertiarySystemFill), in: Circle())
            VStack(alignment: .leading, spacing: 0) {
                Text("Family Assistant")
                    .font(.subheadline.weight(.semibold))
                Text("Home")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Family Assistant, Home")
        .accessibilityIdentifier("home-title")
    }
}

private struct ThreadsButton: View {
    let waitingCount: Int
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            // The count sits beside the icon rather than over its corner: a
            // toolbar button clips anything drawn outside its own bounds.
            HStack(spacing: 4) {
                Image(systemName: "list.bullet.rectangle")
                if waitingCount > 0 {
                    Text(waitingCount > 99 ? "99+" : "\(waitingCount)")
                        .font(.caption2.bold())
                        .foregroundStyle(.white)
                        .padding(.horizontal, 5)
                        .frame(minWidth: 18, minHeight: 18)
                        .background(ThreadStateStyle.waitingColor, in: Capsule())
                        .accessibilityIdentifier("threads-waiting-badge")
                }
            }
        }
        .accessibilityIdentifier("threads-button")
        .accessibilityLabel(waitingCount > 0 ? "Threads, \(waitingCount) waiting on you" : "Threads")
    }
}

/// A conversation pushed over Home: a thread, or an ordinary conversation.
private struct PushedConversationView: View {
    @State private var viewModel: ChatViewModel
    let store: HomeThreadsStore
    let authManager: AuthManager
    private let initialPrompt: String?

    init(destination: HomeDestination, authManager: AuthManager, store: HomeThreadsStore) {
        self.store = store
        self.authManager = authManager
        switch destination {
        case let .conversation(conversationID):
            initialPrompt = nil
            _viewModel = State(initialValue: ChatViewModel(
                authManager: authManager,
                conversationID: conversationID,
                role: .pushed
            ))
        case let .newConversation(_, prompt):
            initialPrompt = prompt
            _viewModel = State(initialValue: ChatViewModel(
                authManager: authManager,
                startsNewConversation: true,
                role: .pushed
            ))
        }
    }

    private var thread: ChatThreadSummary? {
        viewModel.threadSummary ?? store.thread(conversationID: viewModel.conversationID)
    }

    private var isThread: Bool {
        thread != nil || viewModel.conversationKind == .thread
    }

    var body: some View {
        ChatThreadView(viewModel: viewModel)
            .navigationTitle(thread?.title ?? "Chat")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if let thread {
                    ToolbarItem(placement: .principal) {
                        ThreadNavigationTitle(thread: thread)
                    }
                }
                if viewModel.syncPresentation != .live {
                    ToolbarItem(placement: .topBarLeading) {
                        LiveUpdatesIndicator(viewModel: viewModel)
                    }
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    if !isThread {
                        ConversationShareToolbar(
                            authManager: authManager,
                            conversationID: viewModel.shareableConversationID
                        )
                        if viewModel.allowsProfileChange {
                            ProfilePickerView(viewModel: viewModel)
                        }
                    }
                    IntelligencePickerView(viewModel: viewModel)
                }
            }
            .task {
                store.pushedModel = viewModel
                await viewModel.bootstrap(initialPrompt: initialPrompt)
            }
            .alert(
                "Chat Error",
                isPresented: Binding(
                    get: { viewModel.errorMessage != nil },
                    set: { if !$0 { viewModel.errorMessage = nil } }
                )
            ) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(viewModel.errorMessage ?? "")
            }
    }
}

private struct ThreadNavigationTitle: View {
    let thread: ChatThreadSummary

    var body: some View {
        VStack(spacing: 0) {
            Text(thread.title)
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)
            Text("Thread · from Home \(thread.createdAt.formatted(date: .omitted, time: .shortened))")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("thread-title")
    }
}

/// Atop a thread: where it came from and what the assistant asked of it. Drawn
/// from the thread's record, never part of the thread's own history.
struct ThreadOriginBanner: View {
    var viewModel: ChatViewModel

    private static let seedPreviewLimit = 400

    var body: some View {
        if let thread = viewModel.threadSummary {
            VStack(alignment: .leading, spacing: 4) {
                Label("Started from Home", systemImage: "arrow.turn.down.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                if let seed = thread.seedRequest, !seed.isEmpty {
                    Text("Assistant asked: \(String(seed.prefix(Self.seedPreviewLimit)))")
                        .font(.footnote)
                        .lineLimit(4)
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay {
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(Color(.separator), style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("thread-origin-banner")
        }
    }
}

enum ThreadStateStyle {
    static let waitingColor = Color(red: 0.49, green: 0.36, blue: 1.0)
    static let workingColor = Color(red: 0.96, green: 0.72, blue: 0.24)

    static func color(for state: ChatThreadState?) -> Color {
        switch state {
        case .waiting: waitingColor
        case .working: workingColor
        case .failed: .red
        case .done: .green
        case .unknown, nil: .secondary
        }
    }

    static func label(for state: ChatThreadState?) -> String {
        switch state {
        case .waiting: "Waiting on you"
        case .working: "Working"
        case .failed: "Failed"
        case .done: "Done"
        case .unknown, nil: "Starting"
        }
    }
}

struct ThreadStateDot: View {
    let state: ChatThreadState?

    var body: some View {
        Group {
            if state == .done {
                Image(systemName: "checkmark.circle.fill")
                    .font(.caption)
                    .foregroundStyle(.green)
            } else {
                Circle()
                    .fill(ThreadStateStyle.color(for: state))
                    .frame(width: 10, height: 10)
            }
        }
        .frame(width: 14, height: 14)
        .accessibilityHidden(true)
    }
}

/// A thread in Home's transcript, where the assistant started it: its state,
/// title and latest line, updated in place. Tapping opens the thread.
struct ThreadCardView: View {
    let call: ChatToolCall
    let store: HomeThreadsStore

    private static let previewLimit = 300

    var body: some View {
        let thread = store.thread(forSourceToolCallID: call.id)
        let title = thread?.title ?? ChatThreadTool.requestedTitle(from: call.argumentsText) ?? "New thread"
        Button {
            if let thread {
                store.open(thread.conversationID)
            }
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    ThreadStateDot(state: thread?.state)
                    Text(title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Text(ThreadStateStyle.label(for: thread?.state))
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(ThreadStateStyle.color(for: thread?.state))
                        .lineLimit(1)
                        .fixedSize()
                }
                if let line = thread?.previewLine {
                    Text(String(line.prefix(Self.previewLimit)))
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .padding(.leading, 22)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(.systemBackground), in: RoundedRectangle(cornerRadius: 14))
            .overlay {
                RoundedRectangle(cornerRadius: 14)
                    .strokeBorder(Color(.separator))
            }
        }
        .buttonStyle(.plain)
        .disabled(thread == nil)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title), \(ThreadStateStyle.label(for: thread?.state))")
        .accessibilityIdentifier("thread-card-\(call.id)")
    }
}

/// The conversation navigator when threads are on: Home, Home's threads by what
/// they need, then everything else under "Earlier".
struct ThreadSwitcherView: View {
    var store: HomeThreadsStore
    let homePreview: String?
    let onSelect: (String) -> Void
    let onNewChat: () -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var searchText = ""
    @State private var searchResults: (query: String, conversations: [ChatConversationSummary])?
    @State private var searchFailed = false

    private static let searchDebounce: Duration = .milliseconds(300)

    private var searchQuery: String {
        searchText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        NavigationStack {
            List {
                if searchQuery.isEmpty {
                    browseSections
                } else {
                    searchSection
                }
            }
            .navigationTitle("Threads")
            .navigationBarTitleDisplayMode(.large)
            .searchable(text: $searchText, prompt: "Search chats")
            .task(id: searchQuery) {
                await runSearch(for: searchQuery)
            }
            .refreshable {
                async let threads: Void = store.refreshThreads()
                await store.refreshConversations()
                await threads
            }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button(action: onNewChat) {
                        Label("New Chat", systemImage: "square.and.pencil")
                    }
                    .accessibilityIdentifier("thread-switcher-new-chat")
                }
            }
        }
        .task {
            store.listsConversations = true
            async let threads: Void = store.refreshThreads()
            await store.refreshConversations()
            await threads
        }
        .onDisappear {
            store.listsConversations = false
        }
    }

    @ViewBuilder
    private var browseSections: some View {
        let sections = store.sections
        if store.threadsRefreshFailed || store.conversationsRefreshFailed {
            Label("Couldn’t refresh. Pull to try again.", systemImage: "exclamationmark.triangle")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("thread-switcher-refresh-failed")
        }
        Section {
            Button {
                onSelect(store.homeConversationID)
            } label: {
                HStack(spacing: 11) {
                    Image(systemName: "house.fill")
                        .font(.footnote)
                        .foregroundStyle(.white)
                        .frame(width: 30, height: 30)
                        .background(Color.accentColor, in: RoundedRectangle(cornerRadius: 9))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Home")
                            .font(.body.weight(.semibold))
                        if let homePreview {
                            Text(homePreview)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                }
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("thread-switcher-home")
        }
        ForEach(sections.groups) { group in
            Section(group.title) {
                ForEach(group.threads) { thread in
                    Button {
                        onSelect(thread.conversationID)
                    } label: {
                        ThreadSwitcherRow(thread: thread)
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("thread-row-\(thread.conversationID)")
                }
            }
        }
        if !sections.earlier.isEmpty {
            Section("Earlier") {
                ForEach(sections.earlier) { conversation in
                    conversationButton(conversation)
                }
            }
        } else if store.isLoadingConversations {
            Section("Earlier") {
                ProgressView()
            }
        }
    }

    @ViewBuilder
    private var searchSection: some View {
        if searchFailed {
            Label("Couldn’t search chats. Edit the search to try again.", systemImage: "exclamationmark.triangle")
                .font(.footnote)
                .foregroundStyle(.secondary)
        } else if let searchResults, searchResults.query == searchQuery {
            if searchResults.conversations.isEmpty {
                ContentUnavailableView.search(text: searchQuery)
            } else {
                ForEach(searchResults.conversations) { conversation in
                    conversationButton(conversation)
                }
            }
        } else {
            ProgressView()
        }
    }

    private func conversationButton(_ conversation: ChatConversationSummary) -> some View {
        Button {
            onSelect(conversation.conversationID)
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(conversation.conversationID == store.homeConversationID ? "Home" : conversation.displayTitle)
                    .font(.body)
                    .lineLimit(2)
                if let matchExcerpt = conversation.matchExcerpt, !matchExcerpt.isEmpty {
                    Text(matchExcerpt)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                Text(conversation.lastTimestamp, style: .date)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("conversation-row-\(conversation.conversationID)")
    }

    private func runSearch(for query: String) async {
        searchFailed = false
        guard !query.isEmpty else {
            searchResults = nil
            return
        }
        do {
            try await Task.sleep(for: Self.searchDebounce)
            let conversations = try await store.searchConversations(matching: query)
            searchResults = (query, conversations)
        } catch {
            if !Task.isCancelled {
                searchFailed = true
            }
        }
    }
}

private struct ThreadSwitcherRow: View {
    let thread: ChatThreadSummary

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            ThreadStateDot(state: thread.state)
                .padding(.top, 3)
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline) {
                    Text(thread.title)
                        .font(.body.weight(.semibold))
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Text(thread.updatedAt.formatted(.relative(presentation: .named)))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .fixedSize()
                }
                if let line = thread.previewLine {
                    Text(String(line.prefix(200)))
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }
}
