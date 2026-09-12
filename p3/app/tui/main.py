from textual import events, on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
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
from app.lang_utils import create_translator
from app.registry_utils import parse_registry_file

from . import logo
from .about_lt import AboutScreen
from .button_helper import ScriptRunnerMixin
from .dialog_screen import ReportBugDialog
from .helper import (
    get_search_index,
    load_categories,
    search_scripts,
    slugify,
    translations,
)
from .history_screen import HistoryOpenerMixin, HistoryScreen
from .my_widgets import (
    DescButton,
    FocusableLabel,
    Terminal,
)


class LinuxToys(ScriptRunnerMixin, HistoryOpenerMixin, App):
    """main screen for linuxtoys TUI"""

    CSS_PATH = "style.tcss"

    BINDINGS = [
        ("q", "quit", "Sair"),
        ("tab", "focus_next", "Navegar"),
        ("shift+tab", "focus_previous", "Anterior"),
        ("h", "reset_to_home", "Home"),
    ]

    def compose(self) -> ComposeResult:
        categories = load_categories(translations)

        _ = create_translator()
        report_label = _("report_label")
        credits = _("credits_label")
        suport = _("support_footer")
        about = _("about_title")
        registry = _("action_registry")
        scripts_resync = _("scripts_resync")
        search_placeholder = _("search_placeholder")
        load_manifest = _("load_manifest")
        select_language = _("select_language")

        yield Header(icon="")

        with Horizontal(id="body"):
            # left panel widgets
            with Vertical(id="left-column"):
                yield Input(placeholder=search_placeholder, id="search-input")
                with VerticalScroll(id="left-panel"):
                    registry_data = parse_registry_file()
                    for item in categories:
                        yield DescButton(
                            item["name"],
                            item["description"],
                            item["path"],
                            item["is_script"],
                            item.get("is_new", False),
                            item["name"] in registry_data,
                            id=slugify(item["name"]),
                        )
            # right panel widgets
            with Vertical(id="menu-panel"):
                yield Static(logo, id="logo")
                yield ListView(
                    ListItem(Label(f" {load_manifest}"), id="manifest"),
                    ListItem(Label(f" {select_language}"), id="language"),
                    ListItem(Label(f" {about}"), id="about"),
                    ListItem(Label(f"󰲃 {registry}"), id="registry"),
                    ListItem(
                        Label(f" {scripts_resync}"), id="scripts_resync"
                    ),
                    id="home-menu",
                )
                with Vertical(id="terminal-conteiner"):
                    yield Terminal(id="terminal")

        with Horizontal(classes="home-links"):
            yield Link(" Wiki", url="https://linux.toys/knowledgebase.html")
            yield FocusableLabel(
                f" [u]{report_label}[/u]",
                id="report-bug",
                classes="report-bug",
            )
            yield Link(f" {credits}", url="https://linux.toys/credits.html")
            yield Link(f" {suport}", url="https://ko-fi.com/psygreg")

        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#terminal-conteiner").display = False
        self.query_one("#left-panel").border_title = "Categorias/Scripts"
        self.query_one("#menu-panel").border_title = "Menu"
        self.run_worker(
            get_search_index,
            thread=True,
            exclusive=False,
            name="warm_search_index",
        )

    @on(FocusableLabel.Pressed, "#report-bug")
    def handle_report_bug(self) -> None:
        self.push_screen(ReportBugDialog())

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
            self.app.push_screen(HistoryScreen())
        if event.item.id == "scripts_resync":
            self._start_scripts_resync()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search-input":
            return
        await self._filter_scripts(event.value)

    async def _filter_scripts(self, query: str) -> None:
        categories = load_categories(translations)
        query = query.strip().lower()
        left_panel = self.query_one("#left-panel", VerticalScroll)
        await left_panel.remove_children()
        items = categories if not query else search_scripts(query)
        registry_data = parse_registry_file()

        for item in items:
            await left_panel.mount(
                DescButton(
                    item["name"],
                    item["description"],
                    item["path"],
                    item["is_script"],
                    item.get("is_new", False),
                    item["name"] in registry_data,
                    id=slugify(item["name"]),
                )
            )

    async def action_reset_to_home(self) -> None:
        categories = load_categories(translations)
        left_panel = self.query_one("#left-panel", VerticalScroll)
        await left_panel.remove_children()
        registry_data = parse_registry_file()
        for item in categories:
            await left_panel.mount(
                DescButton(
                    item["name"],
                    item["description"],
                    item["path"],
                    item["is_script"],
                    item.get("is_new", False),
                    item["name"] in registry_data,
                    id=slugify(item["name"]),
                )
            )

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
            self.call_from_thread(self.notify, message, timeout=3)

        success = force_update_scripts(progress_callback=progress)
        self.call_from_thread(self._on_scripts_resync_done, success)

    def _on_scripts_resync_done(self, success: bool) -> None:
        if success:
            self.notify(
                "Scripts sincronizados com sucesso.", severity="information"
            )
            # invalida o cache de busca (senão a busca continuaria vendo
            # os scripts antigos, mesmo depois do pull ter trazido novos)
            import app.tui.helper as helper_module

            helper_module._search_index_cache = None
            # e recarrega a home, já que categorias podem ter mudado
            self.run_worker(self.action_reset_to_home())
        else:
            self.notify(
                "Não foi possível sincronizar os scripts agora.",
                severity="warning",
            )


if __name__ == "__main__":
    app = LinuxToys()
    app.run()
