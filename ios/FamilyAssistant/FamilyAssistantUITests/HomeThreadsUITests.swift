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
