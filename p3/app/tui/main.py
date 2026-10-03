import asyncio
from functools import partial

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Link,
    ListItem,
    ListView,
    Static,
)

from app.lang_utils import detect_system_language
from app.registry_utils import parse_registry_file

from .about_helper import load_ansi_art
from .app_page_translate import (
    load_cached_translation,
    save_cached_translation,
    translate_description_blocks,
)
from .button_helper import ScriptRunnerMixin
from .dialog_screen import ReportBugDialog
from .helper import (
    get_categories,
    get_script_info,
    get_specials_root_item,
    is_search_ready,
    make_widget_id,
    search_scripts_fast,
    translations,
    warm_search_and_category_index,
)
from .menu_helper import MenuSelectionMixin
from .my_widgets import (
    FocusableLabel,
    InfoButton,
    Terminal,
)
from .registry_screen import RegistryOpenerMixin
from .tui_app_page import AppPageWidget, hide_app_page, show_app_page


class HomeScreen(
    MenuSelectionMixin, ScriptRunnerMixin, RegistryOpenerMixin, Screen
):
    """main screen for linuxtoys TUI"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._nav_stack: list[tuple[str, str]] = []

    CSS_PATH = "style.tcss"
    _search_timer: asyncio.TimerHandle | None = None

    BINDINGS = [
        Binding("q", "app.quit", "Sair"),
        Binding("tab", "app.focus_next", "Navegar"),
        Binding("shift+tab", "app.focus_previous", "Anterior"),
        Binding("h", "reset_to_home", "Home"),
        Binding("escape", "go_back", "Voltar"),
        Binding("f1", "noop", "🔴 Uninstall   🟡 New", key_display=" "),
    ]

    def action_noop(self) -> None:
        """Empty action for purely informational captions in the footer."""

    def compose(self) -> ComposeResult:
        linuxtoys_art = load_ansi_art("linuxtoystui.ansi")
        yield Header(icon="")

        with Horizontal(id="body-home"):
            # left panel widgets
            with Vertical(id="left-column-home"):
                yield Input(
                    placeholder=translations.get(
                        "search_placeholder", "Search features"
                    ),
                    id="search-input",
                )
                with VerticalScroll(id="left-panel-home"):
                    registry_data = parse_registry_file()
                    for item in self._home_items():
                        yield self._make_info_button(item, registry_data)
            # right panel widgets
            with Vertical(id="menu-panel"):
                yield Static(linuxtoys_art, id="logo_lt")
                yield ListView(
                    ListItem(
                        Label(
                            f" {
                                translations.get(
                                    'load_manifest', 'Load manifest'
                                )
                            }"
                        ),
                        id="manifest",
                    ),
                    ListItem(
                        Label(
                            f" {
                                translations.get(
                                    'select_language', 'Select language'
                                )
                            }"
                        ),
                        id="language",
                    ),
                    ListItem(
                        Label(f" {translations.get('about', 'About')}"),
                        id="about",
                    ),
                    ListItem(
                        Label(f"󰲃 {translations.get('action_registry')}"),
                        id="registry",
                    ),
                    ListItem(Label("󰚰 Update LinuxToys"), id="update"),
                    ListItem(
                        Label(
                            f" {
                                translations.get(
                                    'scripts_resync', 'Scripts resync'
                                )
                            }"
                        ),
                        id="scripts_resync",
                    ),
                    id="home-menu",
                )
                with Vertical(id="terminal-conteiner"):
                    yield Terminal(id="terminal")

        with Horizontal(classes="home-links"):
            yield Link(" Wiki", url="https://linux.toys/documentation.html")
            yield Static("│", classes="link-sep")
            yield FocusableLabel(
                f" [u]{translations.get('report_label', 'Report Bug')}[/u]",
                id="report-bug",
                classes="report-bug",
            )
            yield Static("│", classes="link-sep")
            yield Link(
                f" {translations.get('devportal_label', 'Credits')}",
                url="https://dev.linux.toys",
            )
            yield Static("│", classes="link-sep")
            yield Link(
                f" {translations.get('support_footer', 'Support this project')}",
                url="https://ko-fi.com/psygreg",
            )

        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#terminal-conteiner").display = False
        self.query_one("#left-panel-home").border_title = "Categorias/Scripts"
        self.query_one("#menu-panel").border_title = "Menu"

        left_panel = self.query_one("#left-panel-home", VerticalScroll)
        first_button = left_panel.query(InfoButton).first()
        if first_button is not None:
            first_button.focus()

    @property
    def _is_app_page_showing(self) -> bool:
        return bool(self.query(AppPageWidget))

    def _make_info_button(self, item: dict, registry_data) -> InfoButton:
        return InfoButton(
            item["name"],
            item["description"],
            item["path"],
            item["is_script"],
            item.get("is_new", False),
            item["name"] in registry_data,
            item.get("is_repo_entry", False),
            item.get("is_appstream_entry", False),
            item.get("has_app_page", False),
            id=make_widget_id(item["path"]),
        )

    async def _render_items(self, items: list[dict]) -> None:
        left_panel = self.query_one("#left-panel-home", VerticalScroll)
        await left_panel.remove_children()
        registry_data = parse_registry_file()
        buttons = [
            self._make_info_button(item, registry_data) for item in items
        ]
        await left_panel.mount_all(buttons)

    @on(FocusableLabel.Pressed, "#report-bug")
    def handle_report_bug(self) -> None:
        self.app.push_screen(ReportBugDialog())

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._is_about_showing:
            self._toggle_about_panel(force_hide=True)
            logo = self.query_one("#logo_lt")
            menu = self.query_one("#home-menu")
            menu_panel = self.query_one("#menu-panel")
            logo.display = True
            menu.display = True
            menu_panel.border_title = "Menu"
            self._is_about_showing = False

        button = event.button
        if not isinstance(button, InfoButton):
            return

        if button.is_repo_entry and button.has_app_page:
            script_name = str(button.label)
            info = get_script_info(script_name)
            if info is None:
                self.notify(
                    f"app_page: sem info | label={button.label!r}",
                    severity="warning",
                )
                return
            await show_app_page(
                self,
                info,
                translations=translations,
                featured=None,
                installed=False,
            )
            return

        if button.is_script and self._is_app_page_showing:
            await hide_app_page(self)
        await self.handle_desc_button(button)

    async def on_app_page_widget_back_requested(
        self, message: AppPageWidget.BackRequested
    ) -> None:
        await self.action_go_back()

    def on_app_page_widget_url_requested(
        self, message: AppPageWidget.UrlRequested
    ) -> None:
        self.app.open_url(message.url)

    def on_app_page_widget_translate_requested(
        self, message: AppPageWidget.TranslateRequested
    ) -> None:
        page = self.query_one(AppPageWidget)
        self.run_worker(
            partial(self._translate_description, page, message.blocks),
            thread=True,
            exclusive=True,
            group="translate",
        )

    def _translate_description(self, page: AppPageWidget, blocks) -> None:
        """Roda em thread: não bloqueia a interface durante as requisições."""
        target = (
            detect_system_language().split("-")[0].split("_")[0].casefold()
        )
        try:
            translated = load_cached_translation(
                page.script_info, blocks, target
            )
            if translated is None:
                translated = translate_description_blocks(blocks, target)
                save_cached_translation(
                    page.script_info, blocks, target, translated
                )
        except Exception:
            translated = None

        def deliver() -> None:
            if page.is_attached:
                page.set_translated_blocks(translated)

        self.app.call_from_thread(deliver)

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search-input":
            return

        query = event.value.strip()

        if self._search_timer:
            self._search_timer.cancel()

        if not query:
            await self.action_reset_to_home()
            return

        loop = asyncio.get_running_loop()
        self._search_timer = loop.call_later(
            0.2,
            lambda: self.run_worker(
                lambda: self._execute_search_worker(query),
                thread=True,
                exclusive=True,
                name="search_filter",
            ),
        )

    def _execute_search_worker(self, query: str) -> None:
        # If cache is still warming on first keystroke, this ensures it finishes
        if not is_search_ready():
            warm_search_and_category_index(translations)

        items = search_scripts_fast(query)
        # Safely post back to main UI thread
        self.app.call_from_thread(self._render_search_results, items, query)

    async def _render_search_results(
        self, items: list[dict], query: str
    ) -> None:
        # Guard against out-of-order race conditions when typing quickly
        if self.query_one("#search-input", Input).value.strip() != query:
            return
        await self._render_items(items)

    def _home_items(self) -> list[dict]:
        """Top-level items: Specials button first, then the cached categories."""
        return [
            get_specials_root_item(translations),
            *get_categories(translations),
        ]

    async def action_go_back(self) -> None:
        if self._is_app_page_showing:
            await hide_app_page(self)
            return

        if self._is_about_showing:
            self._toggle_about_panel(force_hide=True)
            logo = self.query_one("#logo_lt")
            menu = self.query_one("#home-menu")
            menu_panel = self.query_one("#menu-panel")
            logo.display = True
            menu.display = True
            menu_panel.border_title = "Menu"
            self._is_about_showing = False

        if not self._nav_stack:
            return
        self._nav_stack.pop()
        if self._nav_stack:
            current_path, _ = self._nav_stack[-1]
            await self._render_items(self._items_for_path(current_path))
        else:
            await self.action_reset_to_home()
            return
        self._update_breadcrumb()

    async def action_reset_to_home(self) -> None:
        self._nav_stack.clear()
        await self._render_items(self._home_items())
        self._update_breadcrumb()

    async def _refresh_fixed_ui_labels(self) -> None:
        self.query_one("#search-input", Input).placeholder = translations.get(
            "search_placeholder", "Search"
        )
        menu = self.query_one("#home-menu", ListView)
        await menu.remove_children()
        await menu.mount(
            ListItem(
                Label(
                    f" {translations.get('load_manifest', 'Load manifest')}"
                ),
                id="manifest",
            )
        )
        await menu.mount(
            ListItem(
                Label(
                    f" {translations.get('select_language', 'Select language')}"
                ),
                id="language",
            )
        )
        await menu.mount(
            ListItem(
                Label(f" {translations.get('about', 'About')}"),
                id="about",
            )
        )
        await menu.mount(
            ListItem(
                Label(f"󰲃 {translations.get('action_registry')}"),
                id="registry",
            )
        )
        await menu.mount(ListItem(Label("󰚰 Update LinuxToys"), id="update"))
        await menu.mount(
            ListItem(
                Label(
                    f" {translations.get('scripts_resync', 'Scripts resync')}"
                ),
                id="scripts_resync",
            )
        )

        links_container = self.query_one(".home-links", Horizontal)
        await links_container.remove_children()
        await links_container.mount(
            Link(" Wiki", url="https://linux.toys/documentation.html")
        )
        await links_container.mount(Static("│", classes="link-sep"))
        await links_container.mount(
            FocusableLabel(
                f" [u]{translations.get('report_label', 'Report Bug')}[/u]",
                id="report-bug",
                classes="report-bug",
            )
        )
        await links_container.mount(Static("│", classes="link-sep"))
        await links_container.mount(
            Link(
                f" {translations.get('devportal_label', 'Credits')}",
                url="https://dev.linux.toys",
            )
        )
        await links_container.mount(Static("│", classes="link-sep"))
        await links_container.mount(
            Link(
                f" {translations.get('support_footer', 'Support this project')}",
                url="https://ko-fi.com/psygreg",
            )
        )


class LinuxToys(App):
    """enter for linuxtoys TUI"""

    CSS_PATH = "style.tcss"

    def on_mount(self) -> None:
        warm_search_and_category_index(translations)
        self.push_screen(HomeScreen())


if __name__ == "__main__":
    app = LinuxToys()
    app.run()
