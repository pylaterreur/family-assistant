import XCTest

/// Home and threads against the in-process mock backend with threads on
/// (`FAMILY_ASSISTANT_UITEST_THREADS=1`): Home is the landing screen, its
/// `start_thread` call is a card that opens the thread, and the switcher lists
/// Home, the threads by state and the ordinary conversations.
final class HomeThreadsUITests: XCTestCase {
    private var app: XCUIApplication!

    private static let readyTimeout: TimeInterval = 30

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchArguments = ["--ui-testing"]
        app.launchEnvironment = [
            "FAMILY_ASSISTANT_UITEST_INITIAL_PATH": "/chat",
            "FAMILY_ASSISTANT_UITEST_THREADS": "1",
        ]
        app.launch()
    }

    override func tearDown() {
        app = nil
        super.tearDown()
    }

    func testLandsOnHomeWithTheThreadCard() {
        XCTAssertTrue(element("home-title").waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(app.staticTexts["On it. I've started a thread for the quotes."].waitForExistence(timeout: 20))

        let card = quotesCard
        XCTAssertTrue(card.waitForExistence(timeout: 20))
        XCTAssertTrue(card.label.contains("Hot water system quotes"))
        XCTAssertTrue(
            card.label.contains("Waiting on you"),
            "The card shows the thread's state once the thread list has loaded."
        )

        let threadsButton = app.buttons["threads-button"]
        XCTAssertTrue(threadsButton.waitForExistence(timeout: 10))
        XCTAssertTrue(
            threadsButton.label.contains("1 waiting"),
            "The threads button badges the one thread waiting on the user."
        )
        attachScreenshot(named: "home-landing")
    }

    func testCardOpensTheThreadAndBackReturnsHome() {
        let card = quotesCard
        XCTAssertTrue(card.waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(waitForEnabled(card))
        card.tap()

        XCTAssertTrue(element("thread-title").waitForExistence(timeout: 20))
        XCTAssertTrue(element("thread-origin-banner").waitForExistence(timeout: 20))
        XCTAssertTrue(
            app.staticTexts["Gas or electric heat pump? Heat pumps qualify for a rebate."].waitForExistence(timeout: 20)
        )
        XCTAssertFalse(
            app.buttons["profile-picker"].exists,
            "A thread runs on its own profile, so it offers no profile switch."
        )
        attachScreenshot(named: "thread-from-card")

        app.navigationBars.buttons.element(boundBy: 0).tap()

        XCTAssertTrue(element("home-title").waitForExistence(timeout: 10))
        XCTAssertTrue(quotesCard.waitForExistence(timeout: 10))
    }

    func testSwitcherGroupsThreadsAndOpensOne() {
        let threadsButton = app.buttons["threads-button"]
        XCTAssertTrue(threadsButton.waitForExistence(timeout: Self.readyTimeout))
        threadsButton.tap()

        XCTAssertTrue(element("thread-switcher-home").waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["WAITING ON YOU"].waitForExistence(timeout: 10)
            || app.staticTexts["Waiting on you"].exists)
        XCTAssertTrue(element("thread-row-web_conv_thread_quotes").exists)
        XCTAssertTrue(element("thread-row-web_conv_thread_flights").exists)
        XCTAssertTrue(
            element("conversation-row-web_conv_seed").waitForExistence(timeout: 10),
            "Ordinary conversations are listed under Earlier."
        )
        XCTAssertFalse(
            element("conversation-row-web_conv_home").exists,
            "Home is its own row, not an Earlier conversation."
        )
        attachScreenshot(named: "thread-switcher")

        element("thread-row-web_conv_thread_flights").tap()

        XCTAssertTrue(element("thread-title").waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Checking Qantas first."].waitForExistence(timeout: 20))

        threadsButtonFromThread()
    }

    func testSwitcherOpensAnEarlierConversation() {
        let threadsButton = app.buttons["threads-button"]
        XCTAssertTrue(threadsButton.waitForExistence(timeout: Self.readyTimeout))
        threadsButton.tap()

        let row = element("conversation-row-web_conv_seed")
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.tap()

        XCTAssertTrue(app.staticTexts["Milk and apples."].waitForExistence(timeout: 20))
        XCTAssertFalse(element("thread-origin-banner").exists)
    }

    func testEchoedOutcomeOpensItsThread() {
        // Found by label, like the card: the bubble's identifier is applied
        // over its contents. The title appears once the thread list loads.
        let header = app.buttons["From thread: Gutter cleaning"]
        XCTAssertTrue(header.waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(app.staticTexts["Booked Sam's Gutters for Saturday at 9am."].exists)
        attachScreenshot(named: "home-echo-row")

        header.tap()

        XCTAssertTrue(element("thread-title").waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Sam's Gutters can come Saturday at 9am. Booked."].waitForExistence(timeout: 20))
    }

    func testMarkDoneInAThreadUpdatesItsCard() {
        let card = quotesCard
        XCTAssertTrue(card.waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(waitForEnabled(card))
        card.tap()

        let menu = element("thread-actions-menu")
        XCTAssertTrue(menu.waitForExistence(timeout: 20))
        menu.tap()
        let markDone = app.buttons["Mark Done"]
        XCTAssertTrue(markDone.waitForExistence(timeout: 10))
        markDone.tap()

        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(element("home-title").waitForExistence(timeout: 10))
        XCTAssertTrue(
            waitForLabel(of: quotesCard, containing: "Done, You marked this done"),
            "The card shows the thread closed by the user: \(quotesCard.label)"
        )
        let threadsButton = app.buttons["threads-button"]
        XCTAssertFalse(threadsButton.label.contains("waiting"), "Nothing is waiting on the user any more.")
        attachScreenshot(named: "home-card-marked-done")
    }

    func testOpeningAThreadClearsItsUnreadBadge() {
        let card = quotesCard
        XCTAssertTrue(card.waitForExistence(timeout: Self.readyTimeout))
        XCTAssertTrue(waitForLabel(of: card, containing: "1 unread"))
        XCTAssertTrue(waitForEnabled(card))
        card.tap()
        XCTAssertTrue(
            app.staticTexts["Gas or electric heat pump? Heat pumps qualify for a rebate."].waitForExistence(timeout: 20)
        )

        app.navigationBars.buttons.element(boundBy: 0).tap()

        XCTAssertTrue(element("home-title").waitForExistence(timeout: 10))
        XCTAssertTrue(
            waitForLabel(of: quotesCard, notContaining: "unread"),
            "Reading the thread clears its badge: \(quotesCard.label)"
        )
    }

    func testChatsSwitcherListsDoneThreads() {
        let threadsButton = app.buttons["threads-button"]
        XCTAssertTrue(threadsButton.waitForExistence(timeout: Self.readyTimeout))
        threadsButton.tap()

        XCTAssertTrue(app.navigationBars["Chats"].waitForExistence(timeout: 10))
        let row = element("thread-row-web_conv_thread_gutters")
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        XCTAssertTrue(row.label.contains("Done"))
        XCTAssertTrue(row.label.contains("Booked Sam's Gutters"), "An assistant-closed thread shows its outcome.")
    }

    /// Back from a thread opened via the switcher lands on Home.
    private func threadsButtonFromThread() {
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(element("home-title").waitForExistence(timeout: 10))
    }

    /// The card for the thread Home's `start_thread` call started. Found by its
    /// label: the message bubble's identifier is applied over its contents.
    private var quotesCard: XCUIElement {
        app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Hot water system quotes")).firstMatch
    }

    private func element(_ identifier: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: identifier).firstMatch
    }

    private func waitForLabel(
        of element: XCUIElement,
        containing text: String? = nil,
        notContaining excluded: String? = nil
    ) -> Bool {
        var conditions: [NSPredicate] = []
        if let text {
            conditions.append(NSPredicate(format: "label CONTAINS %@", text))
        }
        if let excluded {
            conditions.append(NSPredicate(format: "NOT (label CONTAINS %@)", excluded))
        }
        let predicate = NSCompoundPredicate(andPredicateWithSubpredicates: conditions)
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: element)
        return XCTWaiter.wait(for: [expectation], timeout: 20) == .completed
    }

    private func waitForEnabled(_ element: XCUIElement) -> Bool {
        let predicate = NSPredicate(format: "isEnabled == true")
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: element)
        return XCTWaiter.wait(for: [expectation], timeout: 20) == .completed
    }

    private func attachScreenshot(named name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
