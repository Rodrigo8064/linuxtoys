import asyncio

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

from app.easy_cli import (
    is_dev_mode_enabled,
)
from app.registry_utils import parse_registry_file

from . import logo
from .about_lt import AboutScreen
from .button_helper import ScriptRunnerMixin
from .dialog_screen import LanguageSelectorDialog, ReportBugDialog
from .helper import (
    get_categories,
    get_specials_root_item,
    invalidate_search_caches,
    is_search_ready,
    make_widget_id,
    search_scripts_fast,
    translations,
    warm_search_and_category_index,
)
from .manifest_dialog import ManifestDialog
from .my_widgets import (
    DescButton,
    FocusableLabel,
    Terminal,
)
from .registry_screen import RegistryOpenerMixin, RegistryScreen


class HomeScreen(ScriptRunnerMixin, RegistryOpenerMixin, Screen):
    """main screen for linuxtoys TUI"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._nav_stack: list[str] = []

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
                        yield self._make_desc_button(item, registry_data)
            # right panel widgets
            with Vertical(id="menu-panel"):
                yield Static(logo, id="logo")
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

    def _make_desc_button(self, item: dict, registry_data) -> DescButton:
        return DescButton(
            item["name"],
            item["description"],
            item["path"],
            item["is_script"],
            item.get("is_new", False),
            item["name"] in registry_data,
            item.get("is_repo_entry", False),
            item.get("is_appstream_entry", False),
            id=make_widget_id(item["path"]),
        )

    async def _render_items(self, items: list[dict]) -> None:
        left_panel = self.query_one("#left-panel-home", VerticalScroll)
        await left_panel.remove_children()
        registry_data = parse_registry_file()
        buttons = [
            self._make_desc_button(item, registry_data) for item in items
        ]
        await left_panel.mount_all(buttons)

    @on(FocusableLabel.Pressed, "#report-bug")
    def handle_report_bug(self) -> None:
        self.app.push_screen(ReportBugDialog())

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Check if the pressed button is a DescButton instance
        and forward it to handle_desc_button."""
        button = event.button
        if not isinstance(button, DescButton):
            return
        await self.handle_desc_button(button)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Get the selected item and push a screen based on its ID."""
        if event.item.id == "about":
            self.app.push_screen(AboutScreen())
        if event.item.id == "registry":
            self.app.push_screen(RegistryScreen())
        if event.item.id == "scripts_resync":
            self._start_scripts_resync()
        if event.item.id == "language":
            self.app.push_screen(
                LanguageSelectorDialog(), callback=self.apply_language_change
            )
        if event.item.id == "manifest":
            self.app.push_screen(
                ManifestDialog(), callback=self.on_manifest_chosen
            )
        if event.item.id == "update":
            self.notify("LinuxToys Update Checker...")
            self.run_worker(
                self._check_for_update, thread=True, exclusive=True
            )

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
        if not self._nav_stack:
            return  # já está na home
        self._nav_stack.pop()
        if self._nav_stack:
            await self._render_items(self._items_for_path(self._nav_stack[-1]))
        else:
            await self.action_reset_to_home()
        self._update_breadcrumb()

    async def action_reset_to_home(self) -> None:
        self._nav_stack.clear()
        await self._render_items(self._home_items())
        self._update_breadcrumb()

    def _start_scripts_resync(self) -> None:
        if is_dev_mode_enabled():
            self.notify(
                "Modo de desenvolvedor ativo — sincronização de scripts foi pulada.",
                severity="warning",
            )
            return

        self.notify("Sincronizando scripts...", timeout=3)
        self.run_worker(
            self._resync_scripts_worker,
            thread=True,
            exclusive=True,
            name="scripts_resync",
        )

    def _resync_scripts_worker(self) -> None:
        from app.git_scripts_manager import force_update_scripts

        def progress(key: str) -> None:
            message = translations.get(key, key)
            self.app.call_from_thread(self.notify, message, timeout=3)

        success = force_update_scripts(progress_callback=progress)
        self.app.call_from_thread(self._on_scripts_resync_done, success)

    def _on_scripts_resync_done(self, success: bool) -> None:
        if success:
            self.notify(
                "Scripts sincronizados com sucesso.", severity="information"
            )
            invalidate_search_caches()
            warm_search_and_category_index(translations)
            self.run_worker(self.action_reset_to_home())
        else:
            self.notify(
                "Não foi possível sincronizar os scripts agora.",
                severity="warning",
            )

    async def apply_language_change(
        self, new_language_code: str | None
    ) -> None:
        if new_language_code is None:
            return

        from app import lang_utils

        new_translations = lang_utils.load_translations(new_language_code)
        translations.clear()
        translations.update(new_translations)  # muta no lugar, não reatribui
        lang_utils.save_language(new_language_code)
        invalidate_search_caches()
        warm_search_and_category_index(translations)

        await self.action_reset_to_home()
        await self._refresh_fixed_ui_labels()

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
