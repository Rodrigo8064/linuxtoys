import os

from textual.containers import VerticalScroll

from app.easy_cli import (
    _cleanup_tmp_noram_dirs,
    _is_error_exit_code,
    _save_script_to_registry,
    _try_execute_auto_revert,
    create_temp_file,
    is_dev_mode_enabled,
    resolve_script_dir,
)
from app.library_loader import script_command
from app.registry_utils import parse_registry_file
from app.repo_parser import materialize_repo_script
from app.revert_helper import build_uninstall_script_entry
from app.updater.update_helper import UpdateHelper

from .dialog_screen import (
    CancelledDialog,
    ConfirmScriptScreen,
    ErrorDialog,
    RemoveScriptScreen,
    SuccessDialog,
    SudoPasswordScreen,
    UpdateAvailableDialog,
    UpdateCompleteDialog,
)
from .helper import (
    get_scripts_for_category_cached,
    make_widget_id,
    translations,
)
from .manifest_dialog import ManifestReportDialog
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
    _running_is_update: bool = False
    _manifest_queue: list[dict] = []
    _manifest_results: list[dict] = []
    _is_manifest_run: bool = False

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
            self.app.push_screen(
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
        items = get_scripts_for_category_cached(button.path)
        left_panel = self.query_one("#left-panel-home", VerticalScroll)
        await left_panel.remove_children()
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
                    item.get("revert", None),
                    item.get("reboot", "no"),
                    id=make_widget_id(item["path"]),
                )
            )

    def run_script(self, button: DescButton) -> None:
        self._running_button = button
        self._execute_script_info(
            {"name": str(button.label), "path": button.path}
        )

    def _execute_script_info(self, script_info: dict) -> None:
        """O que já era o corpo de run_script, só que recebendo o dict
        direto — usado tanto por clique normal quanto pela fila do manifesto."""
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

    def on_manifest_chosen(self, manifest_path: str | None) -> None:
        if manifest_path is None:
            return
        self.notify("Carregando manifesto...", timeout=3)
        self.run_worker(
            lambda: self._classify_manifest(manifest_path),
            thread=True,
            exclusive=True,
            name="manifest_classify",
        )

    def _classify_manifest(self, manifest_path: str) -> None:
        import asyncio

        from app.manifest_helper import (
            check_flatpaks_async,
            check_package_exists,
            find_script_by_name,
            load_manifest,
        )

        names = load_manifest(manifest_path)
        if not names:
            self.app.call_from_thread(
                self.notify, "Manifesto vazio ou inválido.", severity="warning"
            )
            return

        results = []
        potential_flatpaks = []
        items_to_check = []

        for name in names:
            script_info = find_script_by_name(name, translations)
            if script_info is not None:
                results.append(script_info)
            elif name.count(".") >= 2:
                potential_flatpaks.append(name)
            else:
                items_to_check.append(name)

        packages_to_install = []
        flatpaks_to_install = []

        if potential_flatpaks:
            exists_results = asyncio.run(
                check_flatpaks_async(potential_flatpaks)
            )
            for name, exists in zip(potential_flatpaks, exists_results):
                if exists:
                    flatpaks_to_install.append(name)
                elif check_package_exists(name):
                    packages_to_install.append(name)

        for name in items_to_check:
            if check_package_exists(name):
                packages_to_install.append(name)

        if packages_to_install or flatpaks_to_install:
            temp_path = self._build_packages_flatpaks_script(
                packages_to_install, flatpaks_to_install
            )
            results.append(
                {
                    "name": translations.get(
                        "packages_flatpaks", "Pacotes e Flatpaks"
                    ),
                    "path": temp_path,
                    "is_script": True,
                }
            )

        if not results:
            self.app.call_from_thread(
                self.notify,
                "Nenhum item válido encontrado no manifesto.",
                severity="warning",
            )
            return

        self.app.call_from_thread(self._start_manifest_queue, results)

    def _build_packages_flatpaks_script(
        self, packages: list[str], flatpaks: list[str]
    ) -> str:
        import shlex
        import tempfile

        script_dir = resolve_script_dir()
        lib_path = os.path.join(script_dir, "libs", "linuxtoys.bash")
        packages_str = " ".join(shlex.quote(p) for p in packages)
        flatpaks_str = " ".join(shlex.quote(f) for f in flatpaks)

        script_content = f"""#!/bin/bash
    source {shlex.quote(lib_path)}

    _packages=({packages_str})
    [ "${{#_packages[@]}}" -eq 0 ] || {{ sudo_rq; _install_; }}

    _flatpaks=({flatpaks_str})
    _flatpak_
    """
        tmp = tempfile.NamedTemporaryFile(delete=False, mode="w", suffix=".sh")
        tmp.write(script_content)
        tmp.close()
        os.chmod(tmp.name, 0o700)
        return tmp.name

    def _start_manifest_queue(self, items: list[dict]) -> None:
        self._manifest_queue = items
        self._manifest_results = []
        self._is_manifest_run = True
        self._run_next_manifest_item()

    def _run_next_manifest_item(self) -> None:
        if not self._manifest_queue:
            self._finish_manifest_run()
            return
        self._running_button = None
        script_info = self._manifest_queue.pop(0)
        self._execute_script_info(script_info)

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
        if self._running_is_update:
            self._running_is_update = False
            self._finish_update_run(message.exit_code)
            return

        if self._is_manifest_run:
            self._on_manifest_item_finished(message)
            return
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

    def _on_manifest_item_finished(self, message: ScriptFinished) -> None:
        script_info = self._running_script_info or {}
        script_name = script_info.get("name", "item desconhecido")
        exit_code = message.exit_code
        temp_path = self._running_temp_path
        dev_mode = self._running_dev_mode
        success = exit_code == 0

        if not dev_mode:
            if success:
                _save_script_to_registry(script_name, TRANSMAP_PATH)
                _cleanup_tmp_noram_dirs(TRANSMAP_PATH)
            self._remove_transmap()
        self._cleanup_temp_file(temp_path, dev_mode)

        self._manifest_results.append(
            {"name": script_name, "exit_code": exit_code, "success": success}
        )
        self._run_next_manifest_item()

    def _finish_manifest_run(self) -> None:
        self._hide_terminal()
        results = self._manifest_results
        self._is_manifest_run = False
        self._manifest_queue = []
        self._manifest_results = []
        self._running_script_info = None
        self._running_temp_path = None
        self.app.push_screen(ManifestReportDialog(results))

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

    def _finish_update_run(self, exit_code: int | None) -> None:
        if exit_code == 0:
            self.app.push_screen(
                UpdateCompleteDialog(), callback=self._restart_app
            )
        else:
            self._hide_terminal()
            self.notify(
                "A atualização falhou ou foi cancelada.", severity="error"
            )

    def _restart_app(self, _result: None = None) -> None:
        import sys

        self.app.exit()
        os.execv(sys.executable, [sys.executable] + sys.argv)

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
                self.app.call_from_thread(
                    self.notify,
                    f"Bug reportado com sucesso (issue #{issue_number}).",
                    severity="information",
                )
            else:
                self.app.call_from_thread(
                    self.notify,
                    "Não foi possível enviar o relatório de bug.",
                    severity="error",
                )
        except ConnectionError:
            self.app.call_from_thread(
                self.notify,
                "Sem conexão com a internet — não foi possível reportar o bug.",
                severity="error",
            )
        except Timeout:
            self.app.call_from_thread(
                self.notify,
                "Tempo esgotado ao tentar reportar o bug.",
                severity="error",
            )
        except Exception as exc:
            self.app.call_from_thread(
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

    def _check_for_update(self) -> None:
        """Roda em thread separada — urllib é bloqueante."""
        helper = UpdateHelper()
        available = helper._update_available()
        self.app.call_from_thread(self._on_update_checked, helper, available)

    def _on_update_checked(
        self, helper: UpdateHelper, available: bool
    ) -> None:
        if not available:
            self.notify("✓ It's already on the latest available version")
            return
        tag = helper._latest_ver.get("tag_name", "")
        body = helper._latest_ver.get("body", "Sem changelog disponível.")
        self.app.push_screen(
            UpdateAvailableDialog(tag, body), callback=self._on_update_decision
        )

    def _on_update_decision(self, wants_update: bool | None) -> None:
        if not wants_update:
            return

        self._running_button = None
        self._running_script_info = None
        self._running_temp_path = None
        self._running_dev_mode = False
        self._running_is_uninstall = False
        self._running_is_update = True
        self._show_terminal()
        terminal = self.query_one("#terminal", Terminal)
        terminal.run_script(
            ["sh", "-c", "curl -fsSL https://linux.toys/install.sh | bash"]
        )
