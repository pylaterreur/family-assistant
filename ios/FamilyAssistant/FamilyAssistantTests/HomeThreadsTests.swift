import Foundation
import XCTest

@testable import FamilyAssistant

@MainActor
final class HomeThreadsTests: XCTestCase {
    private let serverURL = "https://assistant.example.test"
    private var models: [ChatViewModel] = []

    override func setUp() {
        super.setUp()
        resetStoredState()
        KeychainHelper.save(key: "fa_api_token", string: "home-threads-token")
        UserDefaults.standard.set(
            ISO8601DateFormatter().string(from: Date().addingTimeInterval(7200)),
            forKey: "fa_token_expiry"
        )
        URLProtocol.registerClass(ChatMockBackendURLProtocol.self)
        ChatMockBackendURLProtocol.reset()
    }

    override func tearDown() {
        for model in models {
            model.scenePhaseChanged(old: .active, new: .background)
            model.cancelStream()
        }
        models.removeAll()
        ChatMockBackendURLProtocol.reset()
        URLProtocol.unregisterClass(ChatMockBackendURLProtocol.self)
        resetStoredState()
        super.tearDown()
    }

    // MARK: - Decoding

    func testThreadListDecodesEveryStateAndToleratesNewOnes() throws {
        let json = """
        {
          "home_conversation_id": "web_conv_home",
          "threads": [
            \(threadJSON(id: "t1", state: "working")),
            \(threadJSON(id: "t2", state: "waiting", sourceToolCallID: "call-2")),
            \(threadJSON(id: "t3", state: "failed")),
            \(threadJSON(id: "t4", state: "archived_someday"))
          ]
        }
        """
        let response = try JSONDecoder.chatDecoder.decode(ChatHomeThreadsResponse.self, from: Data(json.utf8))

        XCTAssertEqual(response.homeConversationID, "web_conv_home")
        XCTAssertEqual(response.threads.map(\.state), [.working, .waiting, .failed, .unknown("archived_someday")])
        XCTAssertEqual(response.threads[1].sourceToolCallID, "call-2")
        XCTAssertNil(response.threads[0].sourceToolCallID)
    }

    func testMessagesResponseWithoutThreadFieldsStillDecodes() throws {
        let json = """
        {"conversation_id":"web_conv_a","messages":[],"count":0,"total_messages":0}
        """
        let response = try JSONDecoder.chatDecoder.decode(ChatConversationMessagesResponse.self, from: Data(json.utf8))

        XCTAssertNil(response.latestUserProfileID)
        XCTAssertNil(response.conversationKind)
        XCTAssertNil(response.thread)
    }

    func testMessagesResponseDecodesThreadMetadata() throws {
        let json = """
        {
          "conversation_id":"t1","messages":[],"count":0,"total_messages":0,
          "latest_user_profile_id":"browser_profile",
          "conversation_kind":"thread",
          "thread": \(threadJSON(id: "t1", state: "waiting", profileID: "browser_profile"))
        }
        """
        let response = try JSONDecoder.chatDecoder.decode(ChatConversationMessagesResponse.self, from: Data(json.utf8))

        XCTAssertEqual(response.latestUserProfileID, "browser_profile")
        XCTAssertEqual(response.conversationKind, .thread)
        XCTAssertEqual(response.thread?.profileID, "browser_profile")
    }

    func testConversationSummaryKindIsOptional() throws {
        let json = """
        {"conversations":[
          {"conversation_id":"a","last_message":"x","last_timestamp":"2026-06-08T12:00:00Z","message_count":1},
          {"conversation_id":"b","last_message":"y","last_timestamp":"2026-06-08T12:00:00Z","message_count":1,"kind":"thread"}
        ],"count":2}
        """
        let response = try JSONDecoder.chatDecoder.decode(ChatConversationListResponse.self, from: Data(json.utf8))

        XCTAssertEqual(response.conversations.map(\.kind), [nil, "thread"])
    }

    // MARK: - Switcher grouping

    func testSwitcherGroupsThreadsByStateInServerOrder() {
        let threads = [
            thread("w1", .working),
            thread("q1", .waiting),
            thread("f1", .failed),
            thread("q2", .waiting),
            thread("u1", .unknown("later")),
        ]

        let sections = ThreadSwitcherSections(threads: threads, conversations: [], homeConversationID: "home")

        XCTAssertEqual(sections.groups.map(\.title), ["Waiting on you", "Working", "Failed"])
        XCTAssertEqual(sections.groups[0].threads.map(\.conversationID), ["q1", "q2", "u1"])
        XCTAssertEqual(sections.groups[1].threads.map(\.conversationID), ["w1"])
        XCTAssertEqual(sections.groups[2].threads.map(\.conversationID), ["f1"])
        XCTAssertEqual(ThreadSwitcherSections.waitingCount(in: threads), 2)
    }

    func testSwitcherOmitsEmptyGroups() {
        let sections = ThreadSwitcherSections(
            threads: [thread("w1", .working)],
            conversations: [],
            homeConversationID: "home"
        )

        XCTAssertEqual(sections.groups.map(\.title), ["Working"])
    }

    func testEarlierListsOnlyOrdinaryConversations() {
        let conversations = [
            conversation("home", kind: "home"),
            conversation("t1", kind: nil),
            conversation("t2", kind: "thread"),
            conversation("old", kind: nil),
            conversation("new", kind: "conversation"),
        ]

        let sections = ThreadSwitcherSections(
            threads: [thread("t1", .waiting)],
            conversations: conversations,
            homeConversationID: "home"
        )

        XCTAssertEqual(sections.earlier.map(\.conversationID), ["old", "new"])
    }

    // MARK: - Thread cards

    func testStartThreadCallBecomesACardAndLeavesTheGroup() {
        let calls = [
            toolCall("call-search", name: "search_notes", status: .succeeded),
            toolCall("call-start", name: "start_thread", status: .succeeded),
        ]

        let split = ThreadCardSplit(toolCalls: calls, showsCards: true)

        XCTAssertEqual(split.threadCalls.map(\.id), ["call-start"])
        XCTAssertEqual(split.otherCalls.map(\.id), ["call-search"])
    }

    func testCardsAreOnlyDrawnInHome() {
        let calls = [toolCall("call-start", name: "start_thread", status: .succeeded)]

        let split = ThreadCardSplit(toolCalls: calls, showsCards: false)

        XCTAssertTrue(split.threadCalls.isEmpty)
        XCTAssertEqual(split.otherCalls.map(\.id), ["call-start"])
    }

    func testRefusedStartThreadStaysAToolRow() {
        let calls = [
            toolCall("call-failed", name: "start_thread", status: .failed),
            toolCall("call-refused", name: "start_thread", status: .succeeded, result: "Error: Threads are not enabled."),
            toolCall("call-running", name: "start_thread", status: .running),
        ]

        let split = ThreadCardSplit(toolCalls: calls, showsCards: true)

        XCTAssertEqual(split.threadCalls.map(\.id), ["call-running"])
        XCTAssertEqual(split.otherCalls.map(\.id), ["call-failed", "call-refused"])
    }

    func testKnownThreadIsACardWhateverTheCallRecorded() {
        let calls = [toolCall("call-start", name: "start_thread", status: .unknown)]

        let split = ThreadCardSplit(toolCalls: calls, showsCards: true, hasThread: { $0 == "call-start" })

        XCTAssertEqual(split.threadCalls.map(\.id), ["call-start"])
    }

    func testCardTitleFallsBackToTheRequestedTitle() {
        XCTAssertEqual(
            ChatThreadTool.requestedTitle(from: #"{"title":"  Hot water quotes ","request":"x"}"#),
            "Hot water quotes"
        )
        XCTAssertNil(ChatThreadTool.requestedTitle(from: #"{"request":"x"}"#))
        XCTAssertNil(ChatThreadTool.requestedTitle(from: "not json"))
    }

    func testPreviewLineFallsBackToTheSeedRequest() {
        XCTAssertEqual(thread("a", .working, preview: "Latest", seed: "Seed").previewLine, "Latest")
        XCTAssertEqual(thread("a", .working, preview: "  ", seed: "Seed").previewLine, "Seed")
        XCTAssertNil(thread("a", .working, preview: nil, seed: nil).previewLine)
    }

    func testStoreMatchesCardsBySourceToolCallID() async {
        ChatMockBackendURLProtocol.respond { request in
            guard request.url?.path == "/api/v1/chat/home/threads" else {
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
            return .json("""
            {"home_conversation_id":"home","threads":[
              \(self.threadJSON(id: "t1", state: "waiting", sourceToolCallID: "call-1")),
              \(self.threadJSON(id: "t2", state: "working"))
            ]}
            """)
        }
        let store = HomeThreadsStore(homeConversationID: "home", apiClient: ChatAPIClient(authManager: makeAuthManager()))

        await store.refreshThreads()

        XCTAssertEqual(store.thread(forSourceToolCallID: "call-1")?.conversationID, "t1")
        XCTAssertNil(store.thread(forSourceToolCallID: "call-2"))
        XCTAssertEqual(store.thread(conversationID: "t2")?.state, .working)
        XCTAssertEqual(store.waitingCount, 1)
        XCTAssertFalse(store.threadsRefreshFailed)
    }

    func testStoreKeepsItsThreadsWhenARefreshFails() async {
        let failures = LockedStrings()
        ChatMockBackendURLProtocol.respond { _ in
            if !failures.values.isEmpty {
                return .json(#"{"detail":"boom"}"#, statusCode: 500)
            }
            return .json("""
            {"home_conversation_id":"home","threads":[\(self.threadJSON(id: "t1", state: "waiting"))]}
            """)
        }
        let store = HomeThreadsStore(
            homeConversationID: "home",
            apiClient: ChatAPIClient(authManager: makeAuthManager()),
            errorReporter: ErrorReporter(spoolDirectory: nil)
        )
        await store.refreshThreads()

        failures.append("fail")
        await store.refreshThreads()

        XCTAssertEqual(store.threads.map(\.conversationID), ["t1"])
        XCTAssertTrue(store.threadsRefreshFailed)
    }

    // MARK: - Routes

    func testRoutesIntoHome() {
        let home = "web_conv_home"

        XCTAssertEqual(HomeRouteAction.resolve(ChatRoute(), homeConversationID: home), .showHome)
        XCTAssertEqual(HomeRouteAction.resolve(ChatRoute(conversationID: home), homeConversationID: home), .showHome)
        XCTAssertEqual(
            HomeRouteAction.resolve(ChatRoute(conversationID: "t1"), homeConversationID: home),
            .open(conversationID: "t1")
        )
        XCTAssertEqual(
            HomeRouteAction.resolve(ChatRoute(initialPrompt: "Buy milk"), homeConversationID: home),
            .sendInHome(prompt: "Buy milk")
        )
        XCTAssertEqual(
            HomeRouteAction.resolve(
                ChatRoute(initialPrompt: "Plan", newConversationRequestID: "r1"),
                homeConversationID: home
            ),
            .newConversation(requestID: "r1", initialPrompt: "Plan")
        )
        XCTAssertEqual(
            HomeRouteAction.resolve(
                ChatRoute(newConversationRequestID: "b1", sharedAttachmentBatchID: "b1"),
                homeConversationID: home
            ),
            .showHome
        )
    }

    // MARK: - Home availability

    func testHomeAvailabilityIsRememberedPerServer() throws {
        let defaults = try XCTUnwrap(UserDefaults(suiteName: "HomeThreadsTests.availability"))
        defaults.removePersistentDomain(forName: "HomeThreadsTests.availability")

        XCTAssertEqual(HomeAvailability.cached(serverURL: serverURL, defaults: defaults), .unknown)

        HomeAvailability.store(.enabled(homeConversationID: "home"), serverURL: serverURL, defaults: defaults)
        XCTAssertEqual(
            HomeAvailability.cached(serverURL: serverURL, defaults: defaults),
            .enabled(homeConversationID: "home")
        )
        XCTAssertEqual(HomeAvailability.cached(serverURL: "https://other.example.test", defaults: defaults), .unknown)

        HomeAvailability.store(.disabled, serverURL: serverURL, defaults: defaults)
        XCTAssertEqual(HomeAvailability.cached(serverURL: serverURL, defaults: defaults), .disabled)

        HomeAvailability.clear(defaults: defaults)
        XCTAssertEqual(HomeAvailability.cached(serverURL: serverURL, defaults: defaults), .unknown)
    }

    func testFreshAnswerReplacesAGoneOrMovedHomeButNotTheConversationList() {
        let home = HomeAvailability.State.enabled(homeConversationID: "home")
        let moved = HomeAvailability.State.enabled(homeConversationID: "moved")

        XCTAssertEqual(HomeAvailability.resolved(current: .unknown, fetched: home), home)
        XCTAssertEqual(HomeAvailability.resolved(current: .unknown, fetched: .disabled), .disabled)
        XCTAssertEqual(HomeAvailability.resolved(current: home, fetched: .disabled), .disabled)
        XCTAssertEqual(HomeAvailability.resolved(current: home, fetched: moved), moved)
        XCTAssertEqual(HomeAvailability.resolved(current: .disabled, fetched: home), .disabled)
        XCTAssertEqual(
            HomeAvailability.state(for: ChatHomeResponse(enabled: true, conversationID: nil)),
            .disabled
        )
    }

    // MARK: - API client

    func testGetHomeTreatsAMissingRouteAsThreadsOff() async throws {
        ChatMockBackendURLProtocol.respond { _ in .json(#"{"detail":"Not Found"}"#, statusCode: 404) }

        let home = try await ChatAPIClient(authManager: makeAuthManager()).getHome()

        XCTAssertEqual(home, ChatHomeResponse(enabled: false, conversationID: nil))
    }

    func testGetHomeDecodesTheHome() async throws {
        ChatMockBackendURLProtocol.respond { request in
            XCTAssertEqual(request.url?.path, "/api/v1/chat/home")
            return .json(#"{"enabled":true,"conversation_id":"web_conv_home"}"#)
        }

        let home = try await ChatAPIClient(authManager: makeAuthManager()).getHome()

        XCTAssertEqual(home, ChatHomeResponse(enabled: true, conversationID: "web_conv_home"))
    }

    func testOrdinaryConversationsAskForOrdinaryKind() async throws {
        let queries = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            queries.append(request.url?.query ?? "")
            return .json(#"{"conversations":[],"count":0}"#)
        }

        _ = try await ChatAPIClient(authManager: makeAuthManager()).listOrdinaryConversations()

        XCTAssertEqual(queries.values.count, 1)
        XCTAssertTrue(queries.values[0].contains("kind=ordinary"))
    }

    // MARK: - View model roles

    func testThreadAdoptsItsOwnProfileWithoutAUserMessage() async {
        respondWithConversation(
            id: "t1",
            messagesJSON: #"[{"internal_id":1,"role":"assistant","content":"Gas or electric?","timestamp":"2026-06-08T12:00:01Z","processing_profile_id":"browser_profile"}]"#,
            extraJSON: """
            ,"latest_user_profile_id":"browser_profile","conversation_kind":"thread",
            "thread": \(threadJSON(id: "t1", state: "waiting", profileID: "browser_profile"))
            """
        )
        let model = makeViewModel(conversationID: "t1", role: .pushed)

        await model.selectConversation("t1")

        XCTAssertEqual(model.selectedProfileID, "browser_profile")
        XCTAssertEqual(model.conversationKind, .thread)
        XCTAssertEqual(model.threadSummary?.title, "Thread t1")
        XCTAssertFalse(model.allowsProfileChange)
    }

    func testHomeWithoutHistoryRunsOnTheDefaultProfile() async {
        PreferredProfile.store("research")
        respondWithConversation(
            id: "home",
            messagesJSON: "[]",
            extraJSON: #","conversation_kind":"home""#
        )
        let model = makeViewModel(conversationID: "home", role: .home)

        await model.selectConversation("home")

        XCTAssertEqual(model.selectedProfileID, "default_assistant")
        XCTAssertEqual(model.conversationKind, .home)
        XCTAssertFalse(model.allowsProfileChange)
    }

    func testOrdinaryConversationOverHomeMayChangeProfile() async {
        respondWithConversation(
            id: "old",
            messagesJSON: #"[{"internal_id":1,"role":"user","content":"Hi","timestamp":"2026-06-08T12:00:00Z","processing_profile_id":"research"}]"#,
            extraJSON: #","latest_user_profile_id":"research","conversation_kind":"conversation""#
        )
        let model = makeViewModel(conversationID: "old", role: .pushed)

        await model.selectConversation("old")

        XCTAssertEqual(model.selectedProfileID, "research")
        XCTAssertTrue(model.allowsProfileChange)
    }

    func testOnlyHomeAndPushedLoadsAskForConversationProfile() async {
        let queries = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if path.hasSuffix("/messages") {
                queries.append(request.url?.query ?? "")
                return .json(#"{"conversation_id":"c","messages":[],"count":0,"total_messages":0}"#)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }

        await makeViewModel(conversationID: "c", role: .standalone).selectConversation("c")
        await makeViewModel(conversationID: "c", role: .pushed).selectConversation("c")

        XCTAssertEqual(queries.values.count, 2)
        XCTAssertFalse(queries.values[0].contains("include_conversation_profile"))
        XCTAssertTrue(queries.values[1].contains("include_conversation_profile=true"))
    }

    func testHomeKeepsNoConversationListAndForwardsActivityAndResync() async {
        let listRequests = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            if request.url?.path == "/api/v1/chat/conversations" {
                listRequests.append(request.url?.query ?? "")
            }
            return .json(#"{"conversations":[],"count":0}"#)
        }
        let model = makeViewModel(conversationID: "home", role: .home)
        var activitySignals = 0
        model.onListRefresh = { activitySignals += 1 }

        let refreshed = await model.refreshConversations()
        await model.activityStreamDidSignal(generation: model.syncCoordinator.activityGeneration)
        XCTAssertEqual(activitySignals, 1)

        // A resync catches up on pings the activity stream missed while it was down.
        let snapshotApplied = await model.applyListSnapshot()
        await model.applyRecentListSnapshot()

        XCTAssertTrue(refreshed)
        XCTAssertTrue(snapshotApplied)
        XCTAssertTrue(listRequests.values.isEmpty)
        XCTAssertEqual(activitySignals, 3)
    }

    func testHomeDoesNotBecomeTheRestoredConversation() {
        _ = makeViewModel(conversationID: "home", role: .home)
        _ = makeViewModel(conversationID: "t1", role: .pushed)

        XCTAssertNil(UserDefaults.standard.string(forKey: "lastConversationId"))
    }

    // MARK: - Helpers

    private func makeAuthManager() -> AuthManager {
        let authManager = AuthManager()
        authManager.serverURL = serverURL
        return authManager
    }

    private func makeViewModel(conversationID: String?, role: ChatViewModel.Role) -> ChatViewModel {
        let model = ChatViewModel(
            authManager: makeAuthManager(),
            conversationID: conversationID,
            role: role,
            errorReporter: ErrorReporter(spoolDirectory: nil),
            pathMonitor: StubPathMonitor(isSatisfied: true)
        )
        models.append(model)
        return model
    }

    private func respondWithConversation(id: String, messagesJSON: String, extraJSON: String) {
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path.hasSuffix("/messages") {
                return .json("""
                {"conversation_id":"\(id)","messages":\(messagesJSON),"count":0,"total_messages":0\(extraJSON)}
                """)
            }
            return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
        }
    }

    private nonisolated func threadJSON(
        id: String,
        state: String,
        sourceToolCallID: String? = nil,
        profileID: String = "research"
    ) -> String {
        let source = sourceToolCallID.map { "\"\($0)\"" } ?? "null"
        return """
        {"conversation_id":"\(id)","home_conversation_id":"web_conv_home","title":"Thread \(id)",
         "profile_id":"\(profileID)","state":"\(state)","source_tool_call_id":\(source),
         "seed_request":"Do \(id)","preview":null,
         "created_at":"2026-06-08T12:00:00Z","updated_at":"2026-06-08T12:05:00.123456+00:00"}
        """
    }

    private func thread(
        _ id: String,
        _ state: ChatThreadState,
        preview: String? = nil,
        seed: String? = nil
    ) -> ChatThreadSummary {
        ChatThreadSummary(
            conversationID: id,
            homeConversationID: "home",
            title: id,
            profileID: "research",
            state: state,
            sourceToolCallID: nil,
            seedRequest: seed,
            preview: preview,
            createdAt: Date(),
            updatedAt: Date()
        )
    }

    private func conversation(_ id: String, kind: String?) -> ChatConversationSummary {
        ChatConversationSummary(
            conversationID: id,
            lastMessage: "",
            lastTimestamp: Date(),
            messageCount: 1,
            kind: kind
        )
    }

    private func toolCall(
        _ id: String,
        name: String,
        status: ChatToolStatus,
        result: String? = nil
    ) -> ChatToolCall {
        ChatToolCall(id: id, name: name, argumentsText: "{}", resultText: result, attachments: [], status: status)
    }

    private func resetStoredState() {
        KeychainHelper.delete(key: "fa_api_token")
        KeychainHelper.delete(key: "fa_refresh_token")
        UserDefaults.standard.removeObject(forKey: "fa_token_expiry")
        UserDefaults.standard.removeObject(forKey: "fa_server_url")
        UserDefaults.standard.removeObject(forKey: "lastConversationId")
        UserDefaults.standard.removeObject(forKey: "lastConversationActiveAt")
        UserDefaults.standard.removeObject(forKey: "selectedProfileId")
        HomeAvailability.clear()
    }
}

private final class LockedStrings: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [String] = []

    var values: [String] {
        lock.withLock { stored }
    }

    func append(_ value: String) {
        lock.withLock { stored.append(value) }
    }
}
