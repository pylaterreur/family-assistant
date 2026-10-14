"""Tests for the app shell: the sidebar on wide screens and the drawer on narrow ones."""

import pytest
from playwright.async_api import expect

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/notes", "/chat", "/voice"])
async def test_desktop_sidebar_is_the_same_on_every_page(
    web_test_fixture_readonly: WebTestFixture, path: str
) -> None:
    """Chat and voice get the same sidebar as every other page, marking the current one."""
    page = web_test_fixture_readonly.page
    await page.set_viewport_size({"width": 1440, "height": 900})
    await page.goto(f"{web_test_fixture_readonly.base_url}{path}")

    nav = page.get_by_test_id("app-sidebar").get_by_role("navigation", name="Main")
    await expect(nav).to_be_visible()
    await expect(nav.locator("[aria-current='page']")).to_have_attribute("href", path)
    await expect(page.get_by_role("button", name="Open navigation menu")).to_be_hidden()

    await nav.get_by_role("link", name="Tools", exact=True).click()
    await expect(page).to_have_url(f"{web_test_fixture_readonly.base_url}/tools")
    await expect(nav.locator("[aria-current='page']")).to_have_attribute(
        "href", "/tools"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_nested_pages_highlight_their_section(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """A page below a section, such as the upload form, highlights that section."""
    page = web_test_fixture_readonly.page
    await page.set_viewport_size({"width": 1440, "height": 900})
    await page.goto(f"{web_test_fixture_readonly.base_url}/documents/upload")

    nav = page.get_by_test_id("app-sidebar").get_by_role("navigation", name="Main")
    await expect(nav.locator("[aria-current='page']")).to_have_count(1)
    await expect(nav.locator("[aria-current='page']")).to_have_attribute(
        "href", "/documents"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_sidebar_collapse_persists_across_reloads(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Collapsing to the icon rail sticks after a reload, and links stay usable."""
    page = web_test_fixture_readonly.page
    await page.set_viewport_size({"width": 1440, "height": 900})
    await page.goto(f"{web_test_fixture_readonly.base_url}/notes")

    sidebar = page.get_by_test_id("app-sidebar")
    await expect(sidebar).to_have_attribute("data-collapsed", "false")
    expanded_box = await sidebar.bounding_box()

    await page.get_by_role("button", name="Collapse navigation").click()
    await expect(sidebar).to_have_attribute("data-collapsed", "true")
    await page.reload()
    await expect(sidebar).to_have_attribute("data-collapsed", "true")
    await expect(page.get_by_role("button", name="Expand navigation")).to_be_visible()
    collapsed_box = await sidebar.bounding_box()
    assert expanded_box is not None
    assert collapsed_box is not None
    assert collapsed_box["width"] < expanded_box["width"] / 2

    await sidebar.get_by_role("link", name="Tools", exact=True).click()
    await expect(page).to_have_url(f"{web_test_fixture_readonly.base_url}/tools")


@pytest.mark.playwright
@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/notes", "/chat"])
async def test_mobile_drawer_is_the_same_on_every_page(
    web_test_fixture_readonly: WebTestFixture, path: str
) -> None:
    """Phones get one menu button, in the same place, opening the same drawer."""
    page = web_test_fixture_readonly.page
    await page.set_viewport_size({"width": 375, "height": 812})
    await page.goto(f"{web_test_fixture_readonly.base_url}{path}")

    await expect(page.get_by_test_id("app-sidebar")).to_be_hidden()
    menu_button = page.get_by_role("button", name="Open navigation menu")
    await expect(menu_button).to_have_count(1)
    box = await menu_button.bounding_box()
    assert box is not None
    assert box["x"] < 20
    assert box["y"] < 20

    await menu_button.click()
    drawer = page.get_by_role("dialog")
    await expect(drawer.get_by_role("navigation", name="Main")).to_be_visible()
    await expect(drawer.locator("[aria-current='page']")).to_have_attribute(
        "href", path
    )
