import os

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
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
    _cleanup_tmp_noram_dirs,
    _is_error_exit_code,
    _save_script_to_registry,
    _try_execute_auto_revert,
    create_temp_file,
    is_dev_mode_enabled,
    resolve_script_dir,
)
from app.lang_utils import create_translator
from app.library_loader import script_command
from app.parser import get_scripts_for_category
from app.registry_utils import parse_registry_file
from app.repo_parser import materialize_repo_script
from app.revert_helper import build_uninstall_script_entry

from . import logo
from .about_lt import AboutScreen
from .dialog_screen import (
    CancelledDialog,
    ConfirmScriptScreen,
    ErrorDialog,
    RemoveScriptScreen,
    SuccessDialog,
    SudoPasswordScreen,
)
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
    PasswordPromptDetected,
    ScriptFinished,
    Terminal,
)

TRANSMAP_PATH = "/tmp/linuxtoys/transmap"


class ScriptRunnerMixin:
    _running_button: DescButton | None = None
    _running_script_info: dict | None = None
    _running_temp_path: str | None = None
    _running_dev_mode: bool = False
    _running_is_uninstall: bool = False

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
                callback=lambda confirmed: self._on_confirm_install(
                    button, confirmed
                ),
            )
        else:
            self.push_screen(
                RemoveScriptScreen(script_name, button.description),
                callback=lambda confirmed: self._on_confirm_uninstall(
                    button, confirmed
                ),
            )

    def _on_confirm_install(self, button: DescButton, confirmed: bool) -> None:
        if confirmed:
            self.run_script(button)

    def _on_confirm_uninstall(
        self, button: DescButton, confirmed: bool
    ) -> None:
        if confirmed:
            self.run_uninstall(button)

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
                    item.get("is_new", False),
                    id=slugify(item["name"]),
                )
            )

    def run_script(self, button: DescButton) -> None:
        script_info = {"name": str(button.label), "path": button.path}
        script_info = materialize_repo_script(script_info)

        dev_mode = is_dev_mode_enabled()

        if dev_mode:
            temp_path = script_info["path"]
        else:
            resolve_script_dir()
            temp_path = create_temp_file(script_info["path"])
            try:
                with open(TRANSMAP_PATH, "w"):
                    pass
            except (IOError, OSError):
                pass

        self._running_button = button
        self._running_script_info = script_info
        self._running_temp_path = temp_path
        self._running_dev_mode = dev_mode
        self._running_is_uninstall = False

        self._show_terminal()
        terminal = self.query_one("#terminal", Terminal)
        terminal.run_script(
            temp_path,
            env={
                "LINUXTOYS_SCRIPT_NAME": script_info.get("name", "unknown"),
                "DISABLE_ZENITY": "1",
                "CACHE_DIR": os.environ.get("SCRIPT_DIR", "") + "/scripts",
            },
        )

    def run_uninstall(self, button: DescButton) -> None:
        script_info = {"name": str(button.label), "path": button.path}
        uninstall_entry = build_uninstall_script_entry(
            script_info, translations
        )

        if not uninstall_entry:
            self.notify(
                f"Nenhum registro removível encontrado para "
                f"'{script_info['name']}'.",
                severity="warning",
            )
            return

        uninstall_path = uninstall_entry["path"]
        cleanup_path = uninstall_entry.get("cleanup_path")

        self._running_button = button
        self._running_script_info = script_info
        self._running_temp_path = cleanup_path
        self._running_dev_mode = False
        self._running_is_uninstall = True

        self._show_terminal()
        terminal = self.query_one("#terminal", Terminal)
        terminal.run_script(
            script_command(uninstall_path, resolve_script_dir())
        )

    def _show_terminal(self) -> None:
        logo = self.query_one("#logo")
        menu = self.query_one("#home-menu")
        terminal_container = self.query_one("#terminal-conteiner")
        terminal = self.query_one("#terminal", Terminal)

        logo.display = False
        menu.display = False
        terminal_container.display = True
        terminal.focus()

    def _hide_terminal(self) -> None:
        logo = self.query_one("#logo")
        menu = self.query_one("#home-menu")
        terminal_container = self.query_one("#terminal-conteiner")

        terminal_container.display = False
        logo.display = True
        menu.display = True

    def on_script_finished(self, message: ScriptFinished) -> None:
        script_info = self._running_script_info or {}
        script_name = script_info.get("name", "o script")
        temp_path = self._running_temp_path
        dev_mode = self._running_dev_mode
        is_uninstall = self._running_is_uninstall
        exit_code = message.exit_code

        action_done = "removido" if is_uninstall else "instalado"
        action_verb = "remover" if is_uninstall else "instalar"

        if exit_code == 0:
            if not dev_mode and not is_uninstall:
                _save_script_to_registry(script_name, TRANSMAP_PATH)
                _cleanup_tmp_noram_dirs(TRANSMAP_PATH)
                self._remove_transmap()
            self._cleanup_temp_file(temp_path, dev_mode)
            self.app.push_screen(
                SuccessDialog(script_name, action=action_done),
                callback=self._finish_script_run,
            )

        elif exit_code == 100:
            if not dev_mode and not is_uninstall:
                self._remove_transmap()
            self._cleanup_temp_file(temp_path, dev_mode)
            self.app.push_screen(
                CancelledDialog(script_name), callback=self._finish_script_run
            )

        elif exit_code is None:
            self.notify(
                "O terminal encerrou inesperadamente; uma nova sessão foi "
                "iniciada automaticamente.",
                severity="warning",
            )
            self._cleanup_temp_file(temp_path, dev_mode)
            self._finish_script_run()

        elif not _is_error_exit_code(exit_code):
            # cancelamento (100) ou terminação por sinal (128-192) —
            # não é um erro de verdade, não precisa de diálogo
            self._cleanup_temp_file(temp_path, dev_mode)
            self._finish_script_run()

        else:
            # erro de verdade
            if not dev_mode and not is_uninstall:
                revert_info = {
                    "name": script_name,
                    "icon": "application-x-executable",
                    "repo": "",
                }
                # Bloqueante, sem PTY — limitação conhecida e aceita por
                # enquanto (ver conversa sobre o item 3). Se o revert
                # precisar de sudo_rq, hoje isso não vai funcionar.
                reverted = _try_execute_auto_revert(revert_info, TRANSMAP_PATH)
                if reverted:
                    self.notify(
                        f"'{script_name}' falhou, mas as mudanças foram "
                        "revertidas automaticamente.",
                        severity="warning",
                    )
                self._remove_transmap()

            terminal = self.query_one("#terminal", Terminal)
            terminal_text = terminal.get_full_text()
            self._cleanup_temp_file(temp_path, dev_mode)
            self.app.push_screen(
                ErrorDialog(script_name, exit_code, action=action_verb),
                callback=lambda wants_report: self._on_error_dialog_closed(
                    wants_report, script_name, exit_code, terminal_text
                ),
            )

    def _remove_transmap(self) -> None:
        try:
            if os.path.exists(TRANSMAP_PATH):
                os.remove(TRANSMAP_PATH)
        except (IOError, OSError):
            pass

    def _cleanup_temp_file(
        self, temp_path: str | None, dev_mode: bool
    ) -> None:
        if dev_mode or not temp_path:
            return
        try:
            os.remove(temp_path)
        except OSError:
            pass

    def _finish_script_run(self, _result=None) -> None:
        self._hide_terminal()
        self._running_button = None
        self._running_script_info = None
        self._running_temp_path = None
        self._running_is_uninstall = False

    def _on_error_dialog_closed(
        self,
        wants_report: bool,
        script_name: str,
        exit_code: int,
        terminal_text: str,
    ) -> None:
        self._finish_script_run()
        if wants_report:
            self.run_worker(
                lambda: self._submit_bug_report(
                    script_name, exit_code, terminal_text
                ),
                thread=True,
                exclusive=False,
            )

    def _submit_bug_report(
        self, script_name: str, exit_code: int, terminal_text: str
    ) -> None:
        from requests.exceptions import ConnectionError, Timeout

        from app.antenna import antenna

        try:
            context_parts = [f"Script: {script_name} | exit code: {exit_code}"]
            system_context = antenna.get_system_context()
            if system_context:
                context_parts.append(system_context)
            history_context = antenna.get_history_context()
            if history_context:
                context_parts.append(history_context)
            context = " | ".join(context_parts)

            result = antenna.submit_issue(
                title="Bug Report from LinuxToys (TUI)",
                logs=terminal_text,
                context=context,
            )
            if result:
                issue_number = result.get("issue_number", "")
                self.call_from_thread(
                    self.notify,
                    f"Bug reportado com sucesso (issue #{issue_number}).",
                    severity="information",
                )
            else:
                self.call_from_thread(
                    self.notify,
                    "Não foi possível enviar o relatório de bug.",
                    severity="error",
                )
        except ConnectionError:
            self.call_from_thread(
                self.notify,
                "Sem conexão com a internet — não foi possível reportar o bug.",
                severity="error",
            )
        except Timeout:
            self.call_from_thread(
                self.notify,
                "Tempo esgotado ao tentar reportar o bug.",
                severity="error",
            )
        except Exception as exc:
            self.call_from_thread(
                self.notify,
                f"Erro ao reportar bug: {exc}",
                severity="error",
            )

    def on_password_prompt_detected(
        self, message: PasswordPromptDetected
    ) -> None:
        """O terminal detectou algo parecido com um prompt de senha —
        mostra o diálogo em vez de deixar o usuário digitar no terminal cru."""
        script_name = (
            str(self._running_button.label)
            if self._running_button
            else "o script"
        )
        self.app.push_screen(
            SudoPasswordScreen(script_name),
            callback=self._on_password_submitted,
        )

    def _on_password_submitted(self, password: str | None) -> None:
        terminal = self.query_one("#terminal", Terminal)
        if password is None:
            terminal.cancel_password_prompt()
            terminal.send_interrupt()
        else:
            terminal.send_password(password)
        terminal.focus()


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
        credits = _("credits_label")
        suport = _("support_footer")
        about = _("about_title")
        registry = _("action_registry")
        scripts_resync = _("scripts_resync")
        search_placeholder = _("search_placeholder")

        yield Header(icon="")

        with Horizontal(id="body"):
            # left panel widgets
            with Vertical(id="left-column"):
                yield Input(placeholder=search_placeholder, id="search-input")
                with VerticalScroll(id="left-panel"):
                    for item in categories:
                        yield DescButton(
                            item["name"],
                            item["description"],
                            item["path"],
                            item["is_script"],
                            item.get("is_new", False),
                            id=slugify(item["name"]),
                        )
            # right panel widgets
            with Vertical(id="menu-panel"):
                yield Static(logo, id="logo")
                yield ListView(
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

        for item in items:
            await left_panel.mount(
                DescButton(
                    item["name"],
                    item["description"],
                    item["path"],
                    item["is_script"],
                    item.get("is_new", False),
                    id=slugify(item["name"]),
                )
            )

    async def action_reset_to_home(self) -> None:
        categories = load_categories(translations)
        left_panel = self.query_one("#left-panel", VerticalScroll)
        await left_panel.remove_children()
        for item in categories:
            await left_panel.mount(
                DescButton(
                    item["name"],
                    item["description"],
                    item["path"],
                    item["is_script"],
                    item.get("is_new", False),
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
