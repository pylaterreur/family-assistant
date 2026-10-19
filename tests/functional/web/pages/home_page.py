"""Page object for Home and its threads in the chat."""

from playwright.async_api import Locator, expect

from .chat_page import ChatPage


class HomePage(ChatPage):
    """The chat with threads enabled: Home, thread cards and the grouped sidebar."""

    CONVERSATION_HEADING = '[data-testid="conversation-title"]'
    HOME_ITEM = '[data-testid="home-item"]'
    THREAD_BANNER = '[data-testid="thread-banner"]'
    THREAD_BREADCRUMB_HOME = '[data-testid="thread-breadcrumb-home"]'
    THREAD_ORIGIN = '[data-testid="thread-origin"]'
    THREAD_WORKING_INDICATOR = '[data-testid="thread-working-indicator"]'
    THREAD_UNREAD_BADGE = '[data-testid="thread-unread-badge"]'
    THREAD_CARD_STATE = '[data-testid="thread-card-state"]'
    THREAD_MARK_DONE = '[data-testid="thread-mark-done"]'
    THREAD_BANNER_STATE = '[data-testid="thread-banner-state"]'

    def echo_of(self, thread_conversation_id: str) -> Locator:
        """The Home message echoing a thread's outcome."""
        return self.page.locator(
            '[data-testid="assistant-message"]'
            f'[data-echoed-from-thread="{thread_conversation_id}"]'
        )

    def heading(self) -> Locator:
        return self.page.locator(self.CONVERSATION_HEADING)

    def thread_card(self, thread_conversation_id: str) -> Locator:
        return self.page.locator(
            f'[data-testid="thread-card"][data-conversation-id="{thread_conversation_id}"]'
        )

    def sidebar_thread(self, thread_conversation_id: str) -> Locator:
        return self.page.get_by_test_id(f"thread-item-{thread_conversation_id}")

    def sidebar_group(self, state: str) -> Locator:
        return self.page.get_by_test_id(f"thread-group-{state}")

    def thread_banner(self) -> Locator:
        return self.page.locator(self.THREAD_BANNER)

    async def wait_for_home(self, home_conversation_id: str) -> None:
        """Wait until Home is the open conversation."""
        await expect(self.heading()).to_have_text("Home", timeout=15000)
        await self.page.wait_for_url(
            f"**/chat?conversation_id={home_conversation_id}", timeout=15000
        )

    async def wait_for_card_state(
        self, thread_conversation_id: str, state: str, timeout: int = 15000
    ) -> None:
        await expect(self.thread_card(thread_conversation_id)).to_have_attribute(
            "data-thread-state", state, timeout=timeout
        )

    async def open_thread_from_card(self, thread_conversation_id: str) -> None:
        await self.thread_card(thread_conversation_id).click()
        await self.page.wait_for_url(
            f"**/chat?conversation_id={thread_conversation_id}", timeout=15000
        )
        await self.thread_banner().wait_for(state="visible", timeout=15000)

    async def back_to_home(self, home_conversation_id: str) -> None:
        await self.page.locator(self.THREAD_BREADCRUMB_HOME).click()
        await self.wait_for_home(home_conversation_id)
