import Foundation
import Observation

/// Home's threads and the ordinary conversations listed beside them, shared by
/// the thread cards in Home's transcript, the threads button's badge and the
/// thread switcher.
@MainActor
@Observable
final class HomeThreadsStore {
    let homeConversationID: String
    private(set) var threads: [ChatThreadSummary] = []
    private(set) var conversations: [ChatConversationSummary] = []
    private(set) var threadsRefreshFailed = false
    private(set) var conversationsRefreshFailed = false
    private(set) var isLoadingConversations = false
    /// Whether the ordinary conversations are on screen, so activity refreshes
    /// them too. Otherwise they are fetched when the switcher opens.
    @ObservationIgnored var listsConversations = false
    /// Opens a conversation over Home. Set by the view that owns navigation.
    @ObservationIgnored var openConversation: ((String) -> Void)?
    /// The conversation model pushed over Home, so a push hint reaches it.
    @ObservationIgnored weak var pushedModel: ChatViewModel?

    @ObservationIgnored private let apiClient: ChatAPIClient
    @ObservationIgnored private let errorReporter: ErrorReporter

    init(homeConversationID: String, apiClient: ChatAPIClient, errorReporter: ErrorReporter = .shared) {
        self.homeConversationID = homeConversationID
        self.apiClient = apiClient
        self.errorReporter = errorReporter
    }

    var waitingCount: Int {
        ThreadSwitcherSections.waitingCount(in: threads)
    }

    var sections: ThreadSwitcherSections {
        ThreadSwitcherSections(
            threads: threads,
            conversations: conversations,
            homeConversationID: homeConversationID
        )
    }

    func thread(forSourceToolCallID toolCallID: String) -> ChatThreadSummary? {
        threads.first { $0.sourceToolCallID == toolCallID }
    }

    func thread(conversationID: String?) -> ChatThreadSummary? {
        guard let conversationID else {
            return nil
        }
        return threads.first { $0.conversationID == conversationID }
    }

    func open(_ conversationID: String) {
        openConversation?(conversationID)
    }

    func refreshThreads() async {
        do {
            threads = try await apiClient.listHomeThreads().threads
            threadsRefreshFailed = false
        } catch {
            guard !Self.isCancellation(error) else {
                return
            }
            threadsRefreshFailed = true
            errorReporter.report(error, component: "Chat.homeThreads")
        }
    }

    func refreshConversations() async {
        isLoadingConversations = true
        defer { isLoadingConversations = false }
        do {
            conversations = try await apiClient.listOrdinaryConversations()
            conversationsRefreshFailed = false
        } catch {
            guard !Self.isCancellation(error) else {
                return
            }
            conversationsRefreshFailed = true
            errorReporter.report(error, component: "Chat.ordinaryConversations")
        }
    }

    /// Refresh on an activity ping: a thread run starting or ending, or any
    /// conversation of the caller's changing.
    func refreshForActivity() async {
        await refreshThreads()
        if listsConversations {
            await refreshConversations()
        }
    }

    func searchConversations(matching query: String) async throws -> [ChatConversationSummary] {
        do {
            return try await apiClient.searchConversations(query: query)
        } catch {
            if !Self.isCancellation(error) {
                errorReporter.report(error, component: "Chat.conversationSearch")
            }
            throw error
        }
    }

    private static func isCancellation(_ error: Error) -> Bool {
        error is CancellationError || (error as? URLError)?.code == .cancelled
    }
}

/// Whether this server gives the caller a Home, remembered between launches so
/// the chat tab opens straight onto the right screen. It is refreshed on every
/// launch; a Home that has gone away or changed is corrected in place.
enum HomeAvailability {
    enum State: Equatable {
        case unknown
        case disabled
        case enabled(homeConversationID: String)
    }

    static let serverKey = "chat.home.server"
    static let conversationKey = "chat.home.conversationID"

    static func cached(serverURL: String?, defaults: UserDefaults = .standard) -> State {
        guard let serverURL, defaults.string(forKey: serverKey) == serverURL,
              let stored = defaults.string(forKey: conversationKey)
        else {
            return .unknown
        }
        return stored.isEmpty ? .disabled : .enabled(homeConversationID: stored)
    }

    static func state(for response: ChatHomeResponse) -> State {
        guard response.enabled, let conversationID = response.conversationID, !conversationID.isEmpty else {
            return .disabled
        }
        return .enabled(homeConversationID: conversationID)
    }

    static func store(_ state: State, serverURL: String?, defaults: UserDefaults = .standard) {
        guard let serverURL else {
            return
        }
        switch state {
        case .unknown:
            clear(defaults: defaults)
            return
        case .disabled:
            defaults.set("", forKey: conversationKey)
        case let .enabled(homeConversationID):
            defaults.set(homeConversationID, forKey: conversationKey)
        }
        defaults.set(serverURL, forKey: serverKey)
    }

    static func clear(defaults: UserDefaults = .standard) {
        defaults.removeObject(forKey: serverKey)
        defaults.removeObject(forKey: conversationKey)
    }

    /// What to show once a fresh answer arrives, given what is showing now.
    /// Home that has gone or moved is replaced at once, since its routes no
    /// longer answer for it. A Home that appears while the conversation list is
    /// showing waits for the next launch rather than pulling the screen away
    /// from someone mid-chat.
    static func resolved(current: State, fetched: State) -> State {
        switch current {
        case .unknown, .enabled:
            return fetched
        case .disabled:
            return .disabled
        }
    }
}

/// What a chat route asks of the Home screen.
enum HomeRouteAction: Equatable {
    /// Show Home itself, popping anything opened over it.
    case showHome
    /// Open a conversation over Home.
    case open(conversationID: String)
    /// Open a new ordinary conversation over Home, sending `initialPrompt`.
    case newConversation(requestID: String, initialPrompt: String?)
    /// Send a prompt in Home, the way quick questions go.
    case sendInHome(prompt: String)

    static func resolve(_ route: ChatRoute, homeConversationID: String) -> HomeRouteAction {
        // A share hand-off lands in Home's composer: quick capture lives there.
        if route.sharedAttachmentBatchID != nil {
            return .showHome
        }
        if let requestID = route.newConversationRequestID {
            return .newConversation(requestID: requestID, initialPrompt: route.initialPrompt)
        }
        if let prompt = route.initialPrompt, !prompt.isEmpty {
            return .sendInHome(prompt: prompt)
        }
        if let conversationID = route.conversationID, conversationID != homeConversationID {
            return .open(conversationID: conversationID)
        }
        return .showHome
    }
}
