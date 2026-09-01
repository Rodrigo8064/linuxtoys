import contextlib
import io
import os

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.types import DuplicateID
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Link,
    ListItem,
    ListView,
    RichLog,
    Static,
)

from app.easy_cli import easy_cli_run_script
from app.lang_utils import create_translator
from app.parser import get_scripts_for_category
from app.registry_utils import parse_registry_file

from .about_lt import AboutScreen
from .dialog_screen import (
    ConfirmScriptScreen,
    RemoveScriptScreen,
    SudoPasswordScreen,
)
from .helper import (
    get_search_index,
    load_categories,
    prime_sudo,
    script_requires_sudo,
    search_scripts,
    slugify,
    sudo_is_cached,
    translations,
)
from .history_screen import HistoryOpenerMixin, HistoryScreen
from .my_widgets import DescButton, get_sidebar_logo


class ScriptRunnerMixin:
    async def handle_desc_button(self, button: DescButton) -> None:
        """Call a function based on is script or not."""
        if button.is_script:
            self._handle_script_button(button)
        else:
            await self._navigate_to_category(button)

    def _handle_script_button(self, button: DescButton) -> None:
        """Handle script button actions based on its execution state.

        Push the confirmation screen if it is the script's first run,
        otherwise push the installation reversal screen.
        """
        script_name = str(button.label)
        registry_data = parse_registry_file()
        is_first_run = script_name not in registry_data

        if is_first_run:
            self.app.push_screen(
                ConfirmScriptScreen(script_name, button.description),
                callback=lambda confirmed: self._on_confirm(button, confirmed),
            )
        else:
            self.push_screen(
                RemoveScriptScreen(script_name, button.description),
                callback=lambda confirmed: self._on_confirm(button, confirmed),
            )

    def _on_confirm(self, button: DescButton, confirmed: bool) -> None:
        if confirmed:
            self._run_with_sudo_check(button)

    def _run_with_sudo_check(
        self, button: DescButton, attempt: int = 1
    ) -> None:
        """Execute the script directly or push the sudo password screen if elevated privileges are needed."""
        if not script_requires_sudo(button.path):
            self.run_script(button)
            return
        if sudo_is_cached():
            self.run_script(button)
            return

        error_message = (
            f"Senha incorreta. Tentativa {attempt - 1}/3."
            if attempt > 1
            else None
        )
        self.app.push_screen(
            SudoPasswordScreen(str(button.label), error_message=error_message),
            callback=lambda password: self._on_sudo_password(
                button, password, attempt
            ),
        )

    def _on_sudo_password(
        self, button: DescButton, password: str | None, attempt: int
    ) -> None:
        """Launch an exclusive thread worker to validate the provided sudo password."""
        if password is None:
            return
        self.run_worker(
            lambda: self._validate_sudo_password(button, password, attempt),
            thread=True,
            exclusive=True,
        )

    def _validate_sudo_password(
        self, button: DescButton, password: str, attempt: int
    ) -> None:
        success = prime_sudo(password)
        self.call_from_thread(
            self._after_sudo_validation, button, success, attempt
        )

    def _after_sudo_validation(
        self, button: DescButton, success: bool, attempt: int
    ) -> None:
        """Run the script if sudo validation succeeds,
        or cancel execution after three failed attempts."""
        if success:
            self.run_script(button)
            return
        if attempt >= 3:
            log = self.screen.query_one("#log-box", RichLog)
            log.write(
                "[bold red]✘ Senha incorreta 3x. Execução cancelada.[/bold red]"
            )
            return
        self._run_with_sudo_check(button, attempt=attempt + 1)

    async def _navigate_to_category(self, button: DescButton) -> None:
        items = get_scripts_for_category(
            button.path, translations=translations
        )
        left_panel = self.query_one("#left-panel", VerticalScroll)
        await left_panel.remove_children()
        for item in items:
            await left_panel.mount(
                DescButton(
                    item["name"],
                    item["description"],
                    item["path"],
                    item["is_script"],
                    id=slugify(item["name"]),
                )
            )

    def run_script(self, button: DescButton) -> None:
        script_info = {"name": str(button.label), "path": button.path}
        log = self.screen.query_one("#log-box", RichLog)
        log.write(f"[bold cyan]▶ Iniciando:[/bold cyan] {script_info['name']}")

        self.run_worker(
            lambda: self._execute_script(script_info),
            thread=True,
            exclusive=True,
        )

    def _execute_script(self, script_info: dict) -> None:
        previous_easy_cli = os.environ.get("EASY_CLI")
        os.environ["EASY_CLI"] = "0"

        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                exit_code = easy_cli_run_script(script_info)
        except Exception as exc:
            exit_code = 1
            buffer.write(f"Erro inesperado ao executar o script: {exc}\n")
        finally:
            if previous_easy_cli is None:
                os.environ.pop("EASY_CLI", None)
            else:
                os.environ["EASY_CLI"] = previous_easy_cli

        output = buffer.getvalue()
        self.call_from_thread(
            self._show_script_result, script_info, exit_code, output
        )

    def _show_script_result(
        self, script_info: dict, exit_code: int, output: str
    ) -> None:
        """Write script output and final status message to the screen log box.

        Args:
            script_info (dict): Dictionary containing script metadata.
            exit_code (int): The process exit code (0 for success).
            output (str): The raw output generated by the script.
        """
        log = self.screen.query_one("#log-box", RichLog)
        if output:
            log.write(output)

        if exit_code == 0:
            log.write(
                f"[bold green]✔ '{script_info['name']}' concluído com sucesso.[/bold green]"
            )
        else:
            log.write(
                f"[bold red]✘ '{script_info['name']}' falhou "
                f"(código {exit_code}).[/bold red]"
            )


class LinuxToys(ScriptRunnerMixin, HistoryOpenerMixin, App):
    """main screen for linuxtoys TUI"""

    CSS_PATH = "style.tcss"

    BINDINGS = [
        ("escape", "quit", "Sair"),
        ("Tab Shit+Tab", "move", "Proximo/Anterior"),
        ("h", "reset_to_home", "Home"),
    ]

    def compose(self) -> ComposeResult:
        categories = load_categories(translations)

        _ = create_translator()
        credits = _("credits_label")
        suport = _("support_footer")
        about = _("about_title")
        registry = _("action_registry")
        scripts_resync = _("scripts_resync")

        yield Header(icon="")

        with Horizontal(id="body"):
            # left panel widgets
            with Vertical(id="left-column"):
                yield Input(placeholder="Buscar Script", id="search-input")
                with VerticalScroll(id="left-panel"):
                    for item in categories:
                        yield DescButton(
                            item["name"],
                            item["description"],
                            item["path"],
                            item["is_script"],
                            id=slugify(item["name"]),
                        )
            # right panel widgets
            with Vertical(id="menu-panel"):
                yield Static(get_sidebar_logo(), id="logo")
                yield ListView(
                    ListItem(Label(about), id="about"),
                    ListItem(Label(registry), id="registry"),
                    ListItem(Label(scripts_resync), id="scripts_resync"),
                    id="home-menu",
                )

        with Horizontal(classes="home-links"):
            yield Link(" Wiki", url="https://linux.toys/knowledgebase.html")
            yield Link(f" {credits}", url="https://linux.toys/credits.html")
            yield Link(f" {suport}", url="https://ko-fi.com/psygreg")

        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#left-panel").border_title = "Categorias/Scripts"
        self.query_one("#menu-panel").border_title = "Menu"
        self.run_worker(
            get_search_index,
            thread=True,
            exclusive=False,
            name="warm_search_index",
        )

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

        for item in items:
            await left_panel.mount(
                DescButton(
                    item["name"],
                    item["description"],
                    item["path"],
                    item["is_script"],
                    id=slugify(item["name"]),
                )
            )

    async def action_reset_to_home(self) -> None:
        categories = load_categories(translations)
        left_panel = self.query_one("#left-panel", VerticalScroll)
        await left_panel.remove_children()
        for item in categories:
            left_panel.mount(
                DescButton(
                    item["name"],
                    item["description"],
                    item["path"],
                    item["is_script"],
                    id=slugify(item["name"]),
                )
            )


if __name__ == "__main__":
    app = LinuxToys()
    app.run()
