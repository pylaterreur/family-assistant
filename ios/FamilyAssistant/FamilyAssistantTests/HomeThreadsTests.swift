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
        XCTAssertEqual(ThreadSwitcherSections.waitingCount(in: threads), 3)
    }

    func testUnknownStateIsWaitingOnCardSectionAndBadge() {
        let unknown = thread("u1", .unknown("needs_review"))

        XCTAssertEqual(unknown.presentationState, .waiting)
        XCTAssertEqual(ThreadPresentationState.label(for: unknown.presentationState), "Waiting on you")
        let sections = ThreadSwitcherSections(threads: [unknown], conversations: [], homeConversationID: "home")
        XCTAssertEqual(sections.groups.map(\.title), ["Waiting on you"])
        XCTAssertEqual(ThreadSwitcherSections.waitingCount(in: [unknown]), 1)
        XCTAssertEqual(
            ThreadPresentationState.label(for: nil),
            "Starting",
            "Only a card whose thread has not loaded yet reads as starting."
        )
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

    func testFailedHomeLookupIsNotThreadsOffAndRetryRecovers() async throws {
        let suite = "HomeThreadsTests.discovery"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defaults.removePersistentDomain(forName: suite)
        var fails = true
        var lookups = 0
        let discovery = HomeDiscovery(
            serverURL: serverURL,
            defaults: defaults,
            lookUp: {
                lookups += 1
                if fails {
                    throw URLError(.notConnectedToInternet)
                }
                return ChatHomeResponse(enabled: true, conversationID: "web_conv_home")
            },
            reportFailure: { _ in }
        )
        XCTAssertEqual(discovery.state, .unknown)

        await discovery.refresh()

        XCTAssertEqual(discovery.state, .failed, "A failed lookup says nothing about whether threads are on.")
        XCTAssertEqual(
            HomeAvailability.cached(serverURL: serverURL, defaults: defaults),
            .unknown,
            "A failure is never remembered as threads off."
        )

        fails = false
        await discovery.retryIfFailed()

        XCTAssertEqual(discovery.state, .enabled(homeConversationID: "web_conv_home"))
        XCTAssertEqual(
            HomeAvailability.cached(serverURL: serverURL, defaults: defaults),
            .enabled(homeConversationID: "web_conv_home")
        )

        await discovery.retryIfFailed()
        XCTAssertEqual(lookups, 2, "Retry looks again only after a failure.")
    }

    func testRouteArrivingWhileLookupFailedReachesHomeAfterRetry() async throws {
        let suite = "HomeThreadsTests.heldRoute"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defaults.removePersistentDomain(forName: suite)
        let failures = LockedStrings()
        failures.append("fail")
        let discovery = HomeDiscovery(
            serverURL: serverURL,
            defaults: defaults,
            lookUp: {
                if !failures.values.isEmpty {
                    throw URLError(.notConnectedToInternet)
                }
                return ChatHomeResponse(enabled: true, conversationID: "web_conv_home")
            },
            reportFailure: { _ in }
        )
        await discovery.refresh()
        XCTAssertEqual(discovery.state, .failed)

        let prompt = ChatRoute(initialPrompt: "Buy milk")
        let share = ChatRoute(newConversationRequestID: "b1", sharedAttachmentBatchID: "b1")
        XCTAssertEqual(
            HomeDiscovery.deliverableRoute(prompt, state: discovery.state),
            ChatRoute(),
            "The provisional conversation list must not consume a routed prompt."
        )
        XCTAssertEqual(HomeDiscovery.deliverableRoute(share, state: discovery.state), ChatRoute())
        XCTAssertEqual(HomeDiscovery.deliverableRoute(prompt, state: .unknown), ChatRoute())

        failures.reset()
        await discovery.retryIfFailed()

        let delivered = HomeDiscovery.deliverableRoute(prompt, state: discovery.state)
        XCTAssertEqual(delivered, prompt)
        XCTAssertEqual(
            HomeRouteAction.resolve(delivered, homeConversationID: "web_conv_home"),
            .sendInHome(prompt: "Buy milk")
        )
        XCTAssertEqual(HomeDiscovery.deliverableRoute(share, state: .disabled), share)
    }

    func testSigningInAgainLooksHomeUpAfresh() async throws {
        let suite = "HomeThreadsTests.restart"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defaults.removePersistentDomain(forName: suite)
        HomeAvailability.store(.enabled(homeConversationID: "home-a"), serverURL: serverURL, defaults: defaults)
        let responses = LockedStrings()
        let discovery = HomeDiscovery(
            serverURL: serverURL,
            defaults: defaults,
            lookUp: {
                guard let next = responses.values.last else {
                    throw URLError(.notConnectedToInternet)
                }
                return ChatHomeResponse(enabled: true, conversationID: next)
            },
            reportFailure: { _ in }
        )
        XCTAssertEqual(discovery.state, .enabled(homeConversationID: "home-a"))

        // Signed in again as someone else: their Home replaces the previous one.
        responses.append("home-b")
        await discovery.restart()
        XCTAssertEqual(discovery.state, .enabled(homeConversationID: "home-b"))

        // A failed lookup after signing in again never falls back to the
        // previous account's Home.
        responses.reset()
        await discovery.restart()
        XCTAssertEqual(discovery.state, .failed)
    }

    func testHomeTakesOverTheFallbackListsDraft() {
        let fallback = makeViewModel(conversationID: "web_conv_old", role: .standalone)
        let attachment = ChatAttachment(
            id: "draft-file",
            attachmentID: "66666666-6666-6666-6666-666666666666",
            type: .document,
            name: "quote.pdf",
            contentURL: "/api/attachments/66666666-6666-6666-6666-666666666666",
            mimeType: "application/pdf",
            size: 10,
            localFileURL: nil,
            uploadState: .uploaded,
            errorMessage: nil
        )
        fallback.draftText = "Typed before Retry"
        fallback.draftAttachments = [attachment]

        let home = makeViewModel(conversationID: "home", role: .home)
        home.inheritDraft(fallback.currentDraft)

        XCTAssertEqual(home.currentDraft, ChatDraft(text: "Typed before Retry", attachments: [attachment]))

        home.draftText = "Already here"
        home.inheritDraft(ChatDraft(text: "Other", attachments: []))
        XCTAssertEqual(home.draftText, "Already here", "A draft being written is not overwritten.")
    }

    func testFailedLookupKeepsARememberedHome() async throws {
        let suite = "HomeThreadsTests.discoveryRemembered"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defaults.removePersistentDomain(forName: suite)
        HomeAvailability.store(.enabled(homeConversationID: "home"), serverURL: serverURL, defaults: defaults)
        let discovery = HomeDiscovery(
            serverURL: serverURL,
            defaults: defaults,
            lookUp: { throw URLError(.timedOut) },
            reportFailure: { _ in }
        )

        await discovery.refresh()

        XCTAssertEqual(discovery.state, .enabled(homeConversationID: "home"))
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

    func testPushedThreadStaysUnsendableUntilItsLoadSucceeds() async {
        PreferredProfile.store("research")
        let loads = LockedStrings()
        let turnPosts = LockedStrings()
        let threadJSON = threadJSON(id: "t1", state: "waiting", profileID: "browser_profile")
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            if request.httpMethod == "POST", path == "/api/v1/chat/turns" {
                turnPosts.append(path)
                return .json(#"{"detail":"unexpected"}"#, statusCode: 500)
            }
            guard request.httpMethod == "GET", path.hasSuffix("/messages") else {
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
            loads.append(path)
            if loads.values.count == 1 {
                return .json(#"{"detail":"upstream unavailable"}"#, statusCode: 503)
            }
            return .json("""
            {"conversation_id":"t1","messages":[],"count":0,"total_messages":0,
             "latest_user_profile_id":"browser_profile","conversation_kind":"thread","thread":\(threadJSON)}
            """)
        }
        let model = makeViewModel(conversationID: "t1", role: .pushed)

        await model.selectConversation("t1")
        model.draftText = "Either, but flag the rebate"

        XCTAssertTrue(model.conversationMetadataLoadFailed)
        XCTAssertFalse(model.canSendDraft, "A thread whose description failed to load must not send.")
        await model.sendDraft()
        XCTAssertTrue(turnPosts.values.isEmpty, "Nothing is posted under a guessed profile.")

        await model.retryConversationLoad()

        XCTAssertFalse(model.conversationMetadataLoadFailed)
        XCTAssertEqual(model.selectedProfileID, "browser_profile")
        XCTAssertTrue(model.canSendDraft)
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

    func testPromptRoutedMidBootstrapSendsOnceOnHomesProfile() async throws {
        PreferredProfile.store("research")
        let messagesRequested = expectation(description: "Home's history requested")
        let releaseHistory = DispatchSemaphore(value: 0)
        let historyLoads = LockedStrings()
        let turnProfiles = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "GET", path) {
            case ("GET", "/api/v1/profiles"):
                return .json("""
                {"profiles":[
                  {"id":"default_assistant","description":"d","available_tools":[],"enabled_mcp_servers":[],"delegation_only":false},
                  {"id":"research","description":"r","available_tools":[],"enabled_mcp_servers":[],"delegation_only":false}
                ],"default_profile_id":"default_assistant"}
                """)
            case ("GET", "/api/v1/chat/conversations/home/messages"):
                historyLoads.append(request.url?.query ?? "")
                if historyLoads.values.count == 1 {
                    // Hold the bootstrap load open until the route has arrived.
                    messagesRequested.fulfill()
                    releaseHistory.wait()
                }
                return .json(
                    #"{"conversation_id":"home","messages":[],"count":0,"total_messages":0,"conversation_kind":"home"}"#
                )
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(
                    JSONSerialization.jsonObject(with: Self.body(of: request)) as? [String: Any]
                )
                turnProfiles.append(payload["profile_id"] as? String ?? "<nil>")
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(#"{"turn_id":"\#(turnID)","conversation_id":"home","first_seq":0}"#)
            case ("GET", "/api/v1/chat/conversations/home/stream"):
                return .droppedStream("")
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text("")
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }
        let model = makeViewModel(conversationID: "home", role: .home)

        let bootstrap = Task { await model.bootstrap() }
        await fulfillment(of: [messagesRequested], timeout: 10)

        // The route lands (twice, as a re-applied route does) while Home is
        // still loading: nothing may be sent yet.
        await model.sendPromptInPlace("Buy milk")
        await model.sendPromptInPlace("Buy milk")
        XCTAssertTrue(turnProfiles.values.isEmpty)

        releaseHistory.signal()
        await bootstrap.value
        try await waitUntil { !turnProfiles.values.isEmpty }

        XCTAssertEqual(turnProfiles.values, ["default_assistant"])
        await model.sendPromptInPlace("Buy milk")
        XCTAssertEqual(turnProfiles.values.count, 1, "A prompt already sent is not sent again.")
    }

    func testRoutedPromptLeavesTheDraftInPlace() async throws {
        let turnPayloads = LockedPayloads()
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/conversations/home/messages"):
                return .json(
                    #"{"conversation_id":"home","messages":[],"count":0,"total_messages":0,"conversation_kind":"home"}"#
                )
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(
                    JSONSerialization.jsonObject(with: Self.body(of: request)) as? [String: Any]
                )
                turnPayloads.append(payload)
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(#"{"turn_id":"\#(turnID)","conversation_id":"home","first_seq":0}"#)
            case ("GET", "/api/v1/chat/conversations/home/stream"):
                return .droppedStream("")
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text("")
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }
        let model = makeViewModel(conversationID: "home", role: .home)
        await model.bootstrap()
        let draftAttachment = ChatAttachment(
            id: "draft-photo",
            attachmentID: "55555555-5555-5555-5555-555555555555",
            type: .image,
            name: "photo.jpg",
            contentURL: "/api/attachments/55555555-5555-5555-5555-555555555555",
            mimeType: "image/jpeg",
            size: 10,
            localFileURL: nil,
            uploadState: .uploaded,
            errorMessage: nil
        )
        model.draftText = "Half typed"
        model.draftAttachments = [draftAttachment]

        await model.sendPromptInPlace("Buy milk")
        try await waitUntil { !turnPayloads.values.isEmpty }

        let payload = try XCTUnwrap(turnPayloads.values.first)
        XCTAssertEqual(payload["prompt"] as? String, "Buy milk")
        XCTAssertTrue(
            (payload["attachments"] as? [Any])?.isEmpty ?? true,
            "The routed prompt goes alone, without the draft's attachments."
        )
        XCTAssertEqual(model.draftText, "Half typed")
        XCTAssertEqual(model.draftAttachments, [draftAttachment])
    }

    func testPromptHeldByAForeignTurnSendsWhenThatTurnEnds() async throws {
        let historyLoads = LockedStrings()
        let turnPrompts = LockedStrings()
        ChatMockBackendURLProtocol.respond { request in
            switch (request.httpMethod ?? "GET", request.url?.path ?? "") {
            case ("GET", "/api/v1/profiles"):
                return .json(#"{"profiles":[],"default_profile_id":"default_assistant"}"#)
            case ("GET", "/api/v1/chat/conversations/home/messages"):
                historyLoads.append(request.url?.query ?? "")
                // Home's first load finds a turn running that another device
                // started; by any later load it has finished.
                let activeTurns = historyLoads.values.count == 1
                    ? #"[{"turn_id":"turn-remote","started_at":"2026-06-08T12:00:00Z","latest_seq":3,"status":"running"}]"#
                    : "[]"
                return .json("""
                {"conversation_id":"home","messages":[],"count":0,"total_messages":0,
                 "conversation_kind":"home","active_turns":\(activeTurns)}
                """)
            case ("POST", "/api/v1/chat/turns"):
                let payload = try XCTUnwrap(
                    JSONSerialization.jsonObject(with: Self.body(of: request)) as? [String: Any]
                )
                turnPrompts.append(payload["prompt"] as? String ?? "<nil>")
                let turnID = try XCTUnwrap(payload["turn_id"] as? String)
                return .json(#"{"turn_id":"\#(turnID)","conversation_id":"home","first_seq":0}"#)
            case ("GET", "/api/v1/chat/conversations/home/stream"):
                return .droppedStream("")
            case ("GET", "/api/v1/chat/confirmations/pending"):
                return .json(#"{"confirmations":[]}"#)
            case ("GET", "/api/v1/chat/activity/stream"):
                return .text("")
            default:
                return .json(#"{"detail":"unexpected"}"#, statusCode: 404)
            }
        }
        let model = makeViewModel(conversationID: "home", role: .home)
        await model.bootstrap()

        await model.sendPromptInPlace("Buy milk")

        XCTAssertFalse(model.isReadyForRoutedPrompt, "A turn running on another device holds the prompt.")
        XCTAssertEqual(model.heldRoutedPrompts, ["Buy milk"], "A held prompt is visible, not silent.")
        XCTAssertTrue(turnPrompts.values.isEmpty)

        // The other device's turn ends, seen on the follow stream.
        let turnEnded = ChatStreamEvent(
            type: .turnEnded,
            turnID: "turn-remote",
            seq: 9,
            text: nil,
            toolCall: nil,
            toolCallID: nil,
            toolResult: nil,
            attachments: [],
            attachmentSource: .response,
            confirmation: nil,
            confirmationResult: nil,
            errorMessage: nil,
            status: "complete"
        )
        _ = await model.handleFollowEvent(
            turnEnded,
            conversationID: "home",
            generation: model.syncCoordinator.followGeneration
        )
        try await waitUntil { !turnPrompts.values.isEmpty }

        XCTAssertEqual(turnPrompts.values, ["Buy milk"])
        XCTAssertTrue(model.heldRoutedPrompts.isEmpty)
    }

    func testOrdinaryControlsWaitUntilTheConversationIsKnownToBeOrdinary() async {
        respondWithConversation(id: "t1", messagesJSON: "[]", extraJSON: #","conversation_kind":"thread""#)
        let linked = makeViewModel(conversationID: "t1", role: .pushed)
        XCTAssertFalse(
            linked.allowsProfileChange,
            "A conversation opened by a link is not known to be ordinary before its history loads."
        )
        await linked.selectConversation("t1")
        XCTAssertFalse(linked.allowsProfileChange)

        let fresh = ChatViewModel(
            authManager: makeAuthManager(),
            startsNewConversation: true,
            role: .pushed,
            errorReporter: ErrorReporter(spoolDirectory: nil),
            pathMonitor: StubPathMonitor(isSatisfied: true)
        )
        models.append(fresh)
        XCTAssertTrue(fresh.allowsProfileChange, "A conversation started here is ordinary.")
        fresh.startNewConversation()
        XCTAssertTrue(fresh.allowsProfileChange)
    }

    func testHomeDoesNotBecomeTheRestoredConversation() {
        _ = makeViewModel(conversationID: "home", role: .home)
        _ = makeViewModel(conversationID: "t1", role: .pushed)

        XCTAssertNil(UserDefaults.standard.string(forKey: "lastConversationId"))
    }

    // MARK: - Helpers

    private nonisolated static func body(of request: URLRequest) -> Data {
        if let body = request.httpBody {
            return body
        }
        guard let stream = request.httpBodyStream else {
            return Data()
        }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return data
    }

    private func waitUntil(timeout: TimeInterval = 10, _ condition: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            guard Date() < deadline else {
                XCTFail("Timed out waiting for condition")
                return
            }
            await Task.yield()
            try await Task.sleep(for: .milliseconds(20))
        }
    }

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

private final class LockedPayloads: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [[String: Any]] = []

    var values: [[String: Any]] {
        lock.withLock { stored }
    }

    func append(_ value: [String: Any]) {
        lock.withLock { stored.append(value) }
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

    func reset() {
        lock.withLock { stored.removeAll() }
    }
}
