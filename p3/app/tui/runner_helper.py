import asyncio
import os
import shlex
import tempfile
import threading
from collections.abc import Mapping
from functools import partial
from typing import Any

from textual.css.query import NoMatches

from app import homebrew_catalog
from app.compat import get_system_compat_keys
from app.easy_cli import (
    _cleanup_tmp_noram_dirs,
    _save_script_to_registry,
    _try_execute_auto_revert,
    create_temp_file,
    is_dev_mode_enabled,
    resolve_script_dir,
)
from app.library_loader import script_command
from app.manifest_helper import find_script_by_name
from app.parser import get_breadcrumb_path, script_requires_reboot
from app.registry_utils import parse_registry_file
from app.repo_parser import materialize_repo_script
from app.revert_helper import build_uninstall_script_entry
from app.updater.update_helper import UpdateHelper

from .appstream_executor import (
    AUR_SECURITY_BLOCKED,
    AUR_SECURITY_OK,
    build_aur_install_script,
    build_install_script,
    build_snap_revert_command,
    check_aur_package_security,
)
from .dialog_screen import (
    AurSecurityDialog,
    CancelledDialog,
    ConfirmScriptScreen,
    ErrorDialog,
    RebootDialog,
    RemoveScriptScreen,
    SuccessDialog,
    SudoPasswordScreen,
    UpdateAvailableDialog,
    UpdateCompleteDialog,
)
from .helper import (
    SPECIALS_ROOT,
    ScriptResult,
    _classify_exit_code,
    get_homebrew_items,
    get_scripts_for_category_cached,
    get_specials_items,
    is_homebrew_catalog_valid,
    is_homebrew_path,
    is_removable,
    is_specials_path,
    refresh_homebrew_catalog,
    translations,
)
from .manifest_dialog import ManifestPlanDialog, ManifestReportDialog
from .my_widgets import (
    InfoButton,
    PasswordPromptDetected,
    ScriptFinished,
    Terminal,
)
from .tui_app_page import AppPageWidget, show_app_page

TRANSMAP_PATH = "/tmp/linuxtoys/transmap"


class ScriptRunnerMixin:
    """Instalação/desinstalação de scripts.

    Também mantém o estado de execução comum e os helpers/handlers
    compartilhados (terminal, limpeza, ``on_script_finished``).
    """

    _running_button: InfoButton | None = None
    _running_script_info: dict | None = None
    _running_temp_path: str | None = None
    _running_dev_mode: bool = False
    _running_is_uninstall: bool = False

    async def handle_desc_button(self, button: InfoButton) -> None:
        """Call a function based on is script or not."""
        if button.is_repo_entry and button.has_app_page:
            pass
        elif button.is_script:
            self._handle_script_button(button)
        else:
            await self._navigate_to_category(button)

    def _handle_script_button(self, button: InfoButton) -> None:
        """Handle script button actions based on its execution state.

        Push the confirmation screen if it is the script's first run,
        otherwise push the installation reversal screen.
        """
        script_name = str(button.label)
        registry_data = parse_registry_file()
        is_first_run = script_name not in registry_data
        is_script_removable = is_removable(
            script_name,
            button.path,
            button.is_repo_entry,
            button.is_appstream_entry,
        )

        if not is_first_run and is_script_removable:
            self.app.push_screen(
                RemoveScriptScreen(script_name, button.description),
                callback=lambda confirmed: self._on_confirm_uninstall(
                    button, confirmed
                ),
            )
        else:
            self.app.push_screen(
                ConfirmScriptScreen(script_name, button.description),
                callback=lambda confirmed: self._on_confirm_install(
                    button, confirmed
                ),
            )

    def _on_confirm_install(self, button: InfoButton, confirmed: bool) -> None:
        if confirmed:
            self.run_script(button)

    def _on_confirm_uninstall(
        self, button: InfoButton, confirmed: bool
    ) -> None:
        if confirmed:
            self.run_uninstall(button)

    async def _navigate_to_category(self, button: InfoButton) -> None:
        if is_homebrew_path(button.path):
            await self._open_homebrew_category(button)
            return
        self._nav_stack.append((button.path, str(button.label)))
        await self._render_items(self._items_for_path(button.path))
        self._update_breadcrumb()

    def _items_for_path(self, path: str) -> list[dict]:
        if is_specials_path(path):
            return get_specials_items(path)
        if is_homebrew_path(path):
            return get_homebrew_items()
        return get_scripts_for_category_cached(path)

    def _homebrew_view_active(self) -> bool:
        return bool(self._nav_stack) and is_homebrew_path(
            self._nav_stack[-1][0]
        )

    async def _open_homebrew_category(self, button: InfoButton) -> None:
        if not homebrew_catalog.enabled():
            self.notify(
                translations.get(
                    "homebrew_unavailable", "Homebrew is not available."
                ),
                severity="warning",
            )
            await self._sync_homebrew_availability()
            return

        self._nav_stack.append((button.path, str(button.label)))
        self._update_breadcrumb()
        await self._render_items([])
        if self._homebrew_view_active():
            self._set_items_loading(True)

        if is_homebrew_catalog_valid():
            self._homebrew_awaiting_refresh = False
            self._load_homebrew_items()
        else:
            self._homebrew_awaiting_refresh = True
            self._request_homebrew_refresh()

    def _load_homebrew_items(self) -> None:
        self.run_worker(
            self._load_homebrew_items_async(),
            exclusive=True,
            group="homebrew-items",
        )

    async def _load_homebrew_items_async(self) -> None:
        unavailable = translations.get(
            "homebrew_catalog_unavailable",
            "Homebrew metadata is unavailable. Try again later.",
        )
        try:
            items = await asyncio.to_thread(get_homebrew_items)
        except Exception:  # noqa: BLE001
            if self._homebrew_view_active():
                await self._show_items_message(unavailable)
            return
        if not self._homebrew_view_active():
            return
        if not items:
            await self._show_items_message(
                translations.get(
                    "homebrew_no_items", "No Homebrew packages found."
                )
            )
            return
        await self._render_items(items, lazy=True)

    def _request_homebrew_refresh(self) -> None:
        if self._homebrew_refresh_running or not homebrew_catalog.enabled():
            return
        self._homebrew_refresh_running = True
        started = homebrew_catalog.availability_fingerprint()
        app = self.app

        def worker() -> None:
            result = refresh_homebrew_catalog()
            try:
                app.call_from_thread(
                    self._finish_homebrew_refresh, result, started
                )
            except Exception:
                pass

        threading.Thread(
            target=worker, daemon=True, name="linuxtoys-homebrew-source"
        ).start()

    async def _finish_homebrew_refresh(self, result: dict, started) -> None:
        """Roda no event loop. Revalida o estado: ele pode ter mudado."""
        self._homebrew_refresh_running = False

        if not homebrew_catalog.enabled():
            await self._sync_homebrew_availability()
            return
        if homebrew_catalog.availability_fingerprint() != started:
            self._request_homebrew_refresh()
            return
        await self._resolve_homebrew_view(result)

    async def _resolve_homebrew_view(self, result: dict) -> None:
        awaiting = self._homebrew_awaiting_refresh
        self._homebrew_awaiting_refresh = False
        if not self._homebrew_view_active():
            return

        from app import appstream_cache

        source = (
            appstream_cache.get_state().get("sources", {}).get("homebrew", {})
        )
        ok = bool(result.get("success") and source.get("complete"))
        unavailable = translations.get(
            "homebrew_catalog_unavailable",
            "Homebrew metadata is unavailable. Try again later.",
        )

        if awaiting:
            if ok:
                self._load_homebrew_items()
            else:
                await self._show_items_message(unavailable)
                self.notify(
                    unavailable, title="Homebrew", severity="error", timeout=8
                )
        elif ok and result.get("changed"):
            self._load_homebrew_items()
            self.notify(
                "Homebrew catalog updated.", title="Homebrew", timeout=3
            )
        elif not ok:
            self.notify(
                unavailable, title="Homebrew", severity="warning", timeout=6
            )

    async def _check_homebrew_source(self) -> None:
        fingerprint = homebrew_catalog.availability_fingerprint()
        if fingerprint == self._homebrew_source_fingerprint:
            return
        self._homebrew_source_fingerprint = fingerprint
        await self._sync_homebrew_availability()
        self._request_homebrew_refresh()

    async def _sync_homebrew_availability(self) -> None:
        """Reconcilia a UI com homebrew_catalog.enabled()."""
        if self._homebrew_view_active() and not homebrew_catalog.enabled():
            self._homebrew_awaiting_refresh = False
            self.notify(
                translations.get(
                    "homebrew_removed", "Homebrew is no longer available."
                ),
                severity="warning",
            )
            await self._show_home()
        elif not self._nav_stack:
            await self._show_home()

    def run_script(self, button: InfoButton) -> None:
        self._running_button = button
        self._execute_script_info(
            {
                "name": str(button.label),
                "path": button.path,
                "is_repo_entry": button.is_repo_entry,
                "is_appstream_entry": button.is_appstream_entry,
            }
        )

    def _execute_script_info(self, script_info: dict) -> None:
        if script_info.get("is_repo_entry"):
            try:
                script_info = materialize_repo_script(script_info)
            except (ValueError, NotImplementedError, OSError) as exc:
                self.notify(
                    "✗ Could not prepare repository entry "
                    f"'{script_info.get('name', 'unknown')}': {exc}",
                    severity="error",
                )
                self._running_button = None
                if self._is_manifest_run:
                    self._manifest_start_failed(
                        script_info.get("name", "unknown"), 1
                    )
                return
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
        self._running_is_appstream = False

        env = {
            "LINUXTOYS_SCRIPT_NAME": script_info.get("name", "unknown"),
            "DISABLE_ZENITY": "1",
            "CACHE_DIR": os.environ.get("SCRIPT_DIR", "") + "/scripts",
        }
        if self._is_manifest_run and not script_info.get("is_batch"):
            env.update(self._manifest_script_env(script_info))

        self._show_terminal()
        terminal = self.query_one("#terminal", Terminal)
        terminal.run_script(
            temp_path,
            env=env,
        )

    def run_uninstall(self, button: InfoButton) -> None:
        script_name = str(button.label)
        script_info = find_script_by_name(script_name, translations)
        # script_info = {"name": str(button.label), "path": button.path}
        uninstall_entry = build_uninstall_script_entry(
            script_info, translations
        )

        if not uninstall_entry:
            self.notify(
                f"✗ No removable registry entry found for '{script_name}'.",
                severity="warning",
            )
            return

        uninstall_path = uninstall_entry.get("path")
        cleanup_path = uninstall_entry.get("cleanup_path")

        self._running_button = button
        self._running_script_info = script_info
        self._running_temp_path = cleanup_path
        self._running_dev_mode = False
        self._running_is_uninstall = True
        self._running_is_appstream = False

        self._show_terminal()
        terminal = self.query_one("#terminal", Terminal)
        terminal.run_script(
            script_command(uninstall_path, resolve_script_dir())
        )

    def _show_terminal(self) -> None:
        logo = self.query_one("#logo_lt")
        menu = self.query_one("#home-menu")
        terminal_container = self.query_one("#terminal-conteiner")
        terminal = self.query_one("#terminal", Terminal)

        logo.display = False
        menu.display = False
        terminal_container.display = True
        terminal.focus()

    def _hide_terminal(self) -> None:
        logo = self.query_one("#logo_lt")
        menu = self.query_one("#home-menu")
        terminal_container = self.query_one("#terminal-conteiner")

        terminal_container.display = False
        logo.display = True
        menu.display = True

    async def on_script_finished(self, message: ScriptFinished) -> None:
        if self._running_is_update:
            self._running_is_update = False
            self._finish_update_run(message.exit_code)
            return

        if self._is_manifest_run:
            self._on_manifest_item_finished(message)
            return
        script_info = self._running_script_info or {}
        script_name = script_info.get("name", "Script")

        # verify if is reboot
        script_path = script_info.get("path", "")
        system_compat_keys = get_system_compat_keys()
        reboot = script_requires_reboot(script_path, system_compat_keys)

        temp_path = self._running_temp_path
        dev_mode = self._running_dev_mode
        is_uninstall = self._running_is_uninstall
        is_appstream = self._running_is_appstream
        exit_code = message.exit_code
        result = _classify_exit_code(exit_code)

        action_done = "removed" if is_uninstall else "installed"
        action_verb = "remove" if is_uninstall else "install"

        if result is ScriptResult.SUCCESS:
            if not dev_mode and not is_uninstall:
                _save_script_to_registry(script_name, TRANSMAP_PATH)
                _cleanup_tmp_noram_dirs(TRANSMAP_PATH)
                self._remove_transmap()
            self._cleanup_temp_file(temp_path, dev_mode)

            if is_appstream:
                page = self._current_app_page()
                if page is not None:
                    await show_app_page(
                        self,
                        script_info,
                        translations=translations,
                        featured=None,
                        installed=not is_uninstall,
                    )
            if reboot:
                self.app.push_screen(
                    RebootDialog(), callback=self._finish_script_run
                )
            else:
                self.app.push_screen(
                    SuccessDialog(script_name, action=action_done),
                    callback=self._finish_script_run,
                )

        elif result is ScriptResult.CANCELLED:
            if not dev_mode and not is_uninstall:
                self._remove_transmap()
            self._cleanup_temp_file(temp_path, dev_mode)
            if is_appstream:
                page = self._current_app_page()
                if page is not None:
                    action = self._running_appstream_action
                    if action == "uninstall":
                        page.reset_remove_button()
                    elif action == "revert":
                        page.reset_revert_button()
                    else:
                        page.reset_install_button()
            self.app.push_screen(
                CancelledDialog(script_name), callback=self._finish_script_run
            )

        elif result is ScriptResult.TERMINAL_CLOSED:
            self.notify(
                "O terminal encerrou inesperadamente; uma nova sessão foi "
                "iniciada automaticamente.",
                severity="warning",
            )
            self._cleanup_temp_file(temp_path, dev_mode)
            if is_appstream:
                page = self._current_app_page()
                if page is not None:
                    action = self._running_appstream_action
                    if action == "uninstall":
                        page.reset_remove_button()
                    elif action == "revert":
                        page.reset_revert_button()
                    else:
                        page.reset_install_button()
            self._finish_script_run()

        else:  # ScriptResult.ERROR
            if is_appstream and not is_uninstall:
                _save_script_to_registry(script_path, TRANSMAP_PATH)
                self._remove_transmap()
            elif not dev_mode and not is_uninstall:
                revert_info = {
                    "name": script_name,
                    "icon": "application-x-executable",
                    "repo": "",
                }
                reverted = _try_execute_auto_revert(revert_info, TRANSMAP_PATH)
                if reverted:
                    self.notify(
                        f"'{script_name}' failed, but automatic reversion  "
                        "completed successfully.",
                        severity="warning",
                    )
                else:
                    self.notify("✗ Automatic reversion failed with exit code")
                self._remove_transmap()

            if is_appstream:
                page = self._current_app_page()
                if page is not None:
                    action = self._running_appstream_action
                    if action == "uninstall":
                        page.reset_remove_button()
                    elif action == "revert":
                        page.reset_revert_button()
                    else:
                        page.reset_install_button()

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
        if not self._running_is_appstream:
            self._hide_terminal()
        self._running_button = None
        self._running_script_info = None
        self._running_temp_path = None
        self._running_is_uninstall = False
        self._running_is_appstream = False
        self._running_appstream_action = None

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

    def _update_breadcrumb(self) -> None:
        panel = self.query_one("#left-panel-home")
        base = "LinuxToys"
        if not self._nav_stack:
            panel.border_title = base
            return

        current_path, current_name = self._nav_stack[-1]
        if is_specials_path(current_path):
            specials_label = translations.get("specials", "Specials")
            if current_path == SPECIALS_ROOT:
                panel.border_title = specials_label
            else:
                panel.border_title = f"{specials_label} > {current_name}"
            return

        crumbs = get_breadcrumb_path(current_path, translations)
        names = " › ".join(
            translations.get(c["name"], c["name"]) for c in crumbs
        )
        panel.border_title = names if names else base

    def _current_app_page(self) -> AppPageWidget | None:
        try:
            return self.query_one(AppPageWidget)
        except NoMatches:
            return None


class AppstreamRunnerMixin:
    """Instalação, remoção e reversão de itens via AppStream."""

    _running_is_appstream: bool = False
    _running_appstream_action: str | None = None

    _running_button: InfoButton | None
    _running_script_info: dict | None
    _running_temp_path: str | None
    _running_dev_mode: bool
    _running_is_uninstall: bool

    def run_appstream_install(self, script_info: Mapping[str, Any]) -> None:
        entry = dict(script_info)
        raw_script_path = build_install_script(entry)

        resolve_script_dir()
        script_path = create_temp_file(raw_script_path)
        try:
            os.remove(raw_script_path)
        except OSError:
            pass

        try:
            with open(TRANSMAP_PATH, "w"):
                pass
        except (IOError, OSError):
            pass

        self._running_button = None
        self._running_script_info = entry
        self._running_temp_path = script_path
        self._running_dev_mode = False
        self._running_is_uninstall = False
        self._running_is_appstream = True
        self._running_appstream_action = "install"

        terminal = self.query_one("#terminal", Terminal)
        terminal.run_script(
            script_path,
            env={
                "LINUXTOYS_SCRIPT_NAME": entry.get("name", "unknown"),
                "DISABLE_ZENITY": "1",
                "CACHE_DIR": os.environ.get("SCRIPT_DIR", "") + "/scripts",
                "TRANSMAP_PATH": TRANSMAP_PATH,
            },
            pause_on_exit=False,
        )

    def run_appstream_snap_revert(
        self, script_info: Mapping[str, Any]
    ) -> None:
        entry = dict(script_info)
        try:
            argv = build_snap_revert_command(entry)
        except ValueError:
            self.notify(
                f"✗ '{entry.get('name', '')}' has no snap package to revert.",
                severity="warning",
            )
            return

        directory = "/tmp/linuxtoys/appstream-overrides"
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(
            prefix="snap-revert-", suffix=".sh", dir=directory, text=True
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("#!/usr/bin/env bash\n")
            handle.write(" ".join(shlex.quote(part) for part in argv))
            handle.write("\n")
        os.chmod(raw_path, 0o700)

        resolve_script_dir()
        script_path = create_temp_file(raw_path)
        try:
            os.remove(raw_path)
        except OSError:
            pass

        try:
            with open(TRANSMAP_PATH, "w"):
                pass
        except (IOError, OSError):
            pass

        self._running_button = None
        self._running_script_info = entry
        self._running_temp_path = script_path
        self._running_dev_mode = False
        self._running_is_uninstall = False
        self._running_is_appstream = True
        self._running_appstream_action = "revert"

        terminal = self.query_one("#terminal", Terminal)
        terminal.run_script(
            script_path,
            env={
                "LINUXTOYS_SCRIPT_NAME": entry.get("name", "unknown"),
                "DISABLE_ZENITY": "1",
                "CACHE_DIR": os.environ.get("SCRIPT_DIR", "") + "/scripts",
                "TRANSMAP_PATH": TRANSMAP_PATH,
            },
            pause_on_exit=False,
        )

    def run_appstream_uninstall(self, script_info: Mapping[str, Any]) -> None:
        entry = dict(script_info)
        name = entry.get("name", "")
        plain_script_info = {"name": name, "path": entry.get("path", "")}
        uninstall_entry = build_uninstall_script_entry(
            plain_script_info, translations
        )

        if not uninstall_entry:
            self.notify(
                f"✗ No removable registry entry found for '{name}'.",
                severity="warning",
            )
            page = self._current_app_page()
            if page is not None:
                page.reset_remove_button()
            return

        uninstall_path = uninstall_entry["path"]
        cleanup_path = uninstall_entry.get("cleanup_path")

        self._running_button = None
        self._running_script_info = entry
        self._running_temp_path = cleanup_path
        self._running_dev_mode = False
        self._running_is_uninstall = True
        self._running_is_appstream = True
        self._running_appstream_action = "uninstall"

        terminal = self.query_one("#terminal", Terminal)
        terminal.run_script(
            script_command(uninstall_path, resolve_script_dir()),
            pause_on_exit=False,
        )

    async def run_appstream_aur_install(
        self, script_info: Mapping[str, Any]
    ) -> None:
        entry = dict(script_info)
        name = entry.get("name", "")
        package = str(entry.get("package-name") or "").strip()

        if not package:
            self.notify(
                f"✗ '{name}' has no AUR package name.", severity="error"
            )
            page = self._current_app_page()
            if page is not None:
                page.reset_install_button()
            return

        status, detail = await asyncio.to_thread(
            check_aur_package_security, package
        )

        if status != AUR_SECURITY_OK:
            page = self._current_app_page()
            if page is not None:
                page.reset_install_button()
            self.app.push_screen(
                AurSecurityDialog(
                    name,
                    blocked=(status == AUR_SECURITY_BLOCKED),
                    detail=detail,
                )
            )
            return

        raw_path = build_aur_install_script(package)
        resolve_script_dir()
        script_path = create_temp_file(raw_path)
        try:
            os.remove(raw_path)
        except OSError:
            pass

        try:
            with open(TRANSMAP_PATH, "w"):
                pass
        except (IOError, OSError):
            pass

        self._running_button = None
        self._running_script_info = entry
        self._running_temp_path = script_path
        self._running_dev_mode = False
        self._running_is_uninstall = False
        self._running_is_appstream = True
        self._running_appstream_action = "install"

        terminal = self.query_one("#terminal", Terminal)
        terminal.run_script(
            script_path,
            env={
                "LINUXTOYS_SCRIPT_NAME": name or "unknown",
                "DISABLE_ZENITY": "1",
                "CACHE_DIR": os.environ.get("SCRIPT_DIR", "") + "/scripts",
                "TRANSMAP_PATH": TRANSMAP_PATH,
            },
            pause_on_exit=False,
        )


class ManifestRunnerMixin:
    """Instalação de scripts via manifesto."""

    _manifest_queue: list[dict] = []
    _manifest_results: list[dict] = []
    _manifest_temp_files: list[str] = []
    _manifest_bootstrap: dict | None = None
    _is_manifest_run: bool = False

    _MANIFEST_BOOTSTRAP_LABELS = {
        "flatpak": "Flatpak/Flathub",
        "homebrew": "Homebrew",
    }

    def on_manifest_chosen(self, manifest_path: str | None) -> None:
        if manifest_path is None:
            return
        # resolve_script_dir()
        if self._is_manifest_run:
            self.notify(
                "Já existe uma execução de manifesto em andamento.",
                severity="warning",
            )
            return
        self.notify("Carregando manifesto...", timeout=3)
        self._start_manifest_classify(manifest_path)

    def _start_manifest_classify(
        self,
        manifest_path: str,
        attempted: tuple[str, ...] = (),
        refresh_appstream: bool = False,
    ) -> None:
        self.run_worker(
            lambda: self._classify_manifest(
                manifest_path, attempted, refresh_appstream
            ),
            thread=True,
            exclusive=True,
            name="manifest_classify",
        )

    def _classify_manifest(
        self,
        manifest_path: str,
        attempted: tuple[str, ...] = (),
        refresh_appstream: bool = False,
    ) -> None:
        """Resolve the manifest off the UI thread, then hand over the plan."""
        from app.manifest_helper import load_manifest

        from .manifest_resolver import (
            refresh_appstream_after_flathub,
            resolve_manifest,
        )

        resolve_script_dir()

        if refresh_appstream:
            # Flathub was just enabled: make its AppStream entries visible.
            try:
                refresh_appstream_after_flathub()
            except Exception as exc:  # mirrors the CLI bootstrap helper
                self.app.call_from_thread(
                    self.notify,
                    f"Falha ao atualizar o AppStream: {exc}",
                    severity="error",
                )
                return

        try:
            names = load_manifest(manifest_path)
        except (OSError, ValueError):  # ValueError covers UnicodeDecodeError
            self.app.call_from_thread(
                self.notify,
                "Não foi possível ler o manifesto.",
                severity="error",
            )
            return

        if not names:
            self.app.call_from_thread(
                self.notify, "Manifesto vazio ou inválido.", severity="warning"
            )
            return

        try:
            plan = resolve_manifest(names, translations)
        except Exception as exc:  # worker boundary: never let it kill the TUI
            self.app.call_from_thread(
                self.notify,
                f"Erro ao validar o manifesto: {exc}",
                severity="error",
            )
            return
        self.app.call_from_thread(
            self._on_manifest_resolved, plan, manifest_path, attempted
        )

    def _on_manifest_resolved(
        self, plan, manifest_path: str, attempted: tuple[str, ...]
    ) -> None:
        if plan.bootstrap is None:
            self._review_manifest_plan(plan)
            return

        label = self._MANIFEST_BOOTSTRAP_LABELS[plan.bootstrap]
        if plan.bootstrap in attempted:
            self.notify(
                f"{label} continua indisponível após a configuração.",
                severity="error",
            )
        elif self._dry_run_enabled():
            self.notify(
                f"Pré-requisito ausente: {label}. "
                "O modo dev não instala pré-requisitos.",
                severity="warning",
            )
        else:
            self._offer_manifest_bootstrap(plan, manifest_path, attempted)

    @staticmethod
    def _dry_run_enabled() -> bool:
        from app.dev_mode import should_dry_run_scripts

        return should_dry_run_scripts()

    def _offer_manifest_bootstrap(
        self, plan, manifest_path: str, attempted: tuple[str, ...]
    ) -> None:
        label = self._MANIFEST_BOOTSTRAP_LABELS[plan.bootstrap]
        self.app.push_screen(
            ManifestPlanDialog(
                f"Este manifesto requer {label}",
                [
                    f"{label} não está configurado neste sistema.",
                    "",
                    "Configurar agora e continuar a validação do manifesto?",
                ],
                confirm_label="Configurar",
            ),
            partial(
                self._on_manifest_bootstrap_answer,
                plan,
                manifest_path,
                attempted,
            ),
        )

    def _on_manifest_bootstrap_answer(
        self,
        plan,
        manifest_path: str,
        attempted: tuple[str, ...],
        confirmed: bool | None,
    ) -> None:
        if not confirmed:
            self.notify("Operação cancelada.", timeout=3)
            return

        # Both run in the terminal: they may need sudo.
        self._manifest_temp_files = []
        if plan.bootstrap == "homebrew":
            item = plan.bootstrap_script
        else:
            try:
                item = self._batch_item("Flatpak/Flathub", ["pkg_flat"])
            except OSError as exc:
                self.notify(
                    f"Não foi possível preparar o script: {exc}",
                    severity="error",
                )
                return

        self._manifest_bootstrap = {
            "path": manifest_path,
            "kind": plan.bootstrap,
            "attempted": (*attempted, plan.bootstrap),
        }
        self._is_manifest_run = True  # routes ScriptFinished to our handler
        self._running_button = None
        self._execute_script_info(item)

    def _after_manifest_bootstrap(self, result, exit_code: int | None) -> None:
        state = self._manifest_bootstrap
        self._manifest_bootstrap = None
        self._is_manifest_run = False
        self._running_script_info = None
        self._running_temp_path = None
        self._hide_terminal()
        self._discard_manifest_temp_files()

        label = self._MANIFEST_BOOTSTRAP_LABELS[state["kind"]]
        if result is ScriptResult.SUCCESS:
            self.notify("Validando manifesto...", timeout=3)
            self._start_manifest_classify(
                state["path"],
                state["attempted"],
                refresh_appstream=state["kind"] == "flatpak",
            )
        elif result is ScriptResult.CANCELLED:
            self.notify(
                f"Configuração do {label} cancelada.", severity="warning"
            )
        elif result is ScriptResult.ERROR:
            self.notify(
                f"Falha ao configurar {label} (código {exit_code}).",
                severity="error",
            )

    def _review_manifest_plan(self, plan) -> None:
        """Mirror the CLI: any rejected item aborts, otherwise ask to go on."""
        if not plan.is_valid:
            self.app.push_screen(
                ManifestPlanDialog(
                    "Manifesto inválido. Nada será instalado.",
                    [f"✗ {error}" for error in plan.errors],
                    cancel_label="Fechar",
                )
            )
            return

        if plan.total == 0:
            if plan.warnings:
                self.app.push_screen(
                    ManifestPlanDialog(
                        "Nenhum item para executar",
                        [f"- {warning}" for warning in plan.warnings],
                        cancel_label="Fechar",
                    )
                )
            else:
                self.notify(
                    "Nenhum item compatível encontrado no manifesto.",
                    severity="warning",
                )
            return

        dry_run = self._dry_run_enabled()
        lines = self._manifest_plan_lines(plan)
        if dry_run:
            heading = f"DRY-RUN: validar {plan.total} item(ns)?"
            lines = [
                "Nada será instalado: scripts são validados; pacotes, "
                "snaps e flatpaks apenas simulados.",
                "",
                *lines,
            ]
            confirm_label = "Validar"
        else:
            heading = f"Instalar {plan.total} item(ns)?"
            confirm_label = "Continuar"

        self.app.push_screen(
            ManifestPlanDialog(heading, lines, confirm_label=confirm_label),
            partial(self._on_manifest_plan_answer, plan, dry_run),
        )

    @staticmethod
    def _manifest_plan_lines(plan) -> list[str]:
        # Same order in which the queue is executed.
        lines = [f"[PACOTE] {name}" for name in plan.packages]
        lines += [f"[SNAP] {name}" for name in plan.snaps]
        lines += [f"[FLATPAK] {name}" for name in plan.flatpaks]
        lines += [f"[SCRIPT] {script['name']}" for script in plan.scripts]
        if plan.warnings:
            lines += ["", "Ignorados:"]
            lines += [f"  - {warning}" for warning in plan.warnings]
        return lines

    def _on_manifest_plan_answer(
        self, plan, dry_run: bool, confirmed: bool | None
    ) -> None:
        if not confirmed:
            self.notify("Operação cancelada.", timeout=3)
            return
        if dry_run:
            self._start_manifest_dry_run(plan)
            return
        try:
            queue = self._build_manifest_queue(plan)
        except (OSError, ValueError) as exc:
            self._discard_manifest_temp_files()
            self.notify(
                f"Não foi possível preparar a execução: {exc}",
                severity="error",
            )
            return
        self._start_manifest_queue(queue)

    def _build_manifest_queue(self, plan) -> list[dict]:
        """Packages, snaps and flatpaks first (one item each), then scripts.

        One item per kind keeps exit codes meaningful: a single combined
        script would only report the status of its last command.
        """
        self._manifest_temp_files = []
        queue: list[dict] = []

        if plan.packages:
            values = self._shell_words(plan.packages)
            queue.append(
                self._batch_item(
                    f"Pacotes ({len(plan.packages)})",
                    ["askpass", f"pkg_install {values}"],
                )
            )
        if plan.snaps:
            values = self._shell_words(plan.snaps)
            queue.append(
                self._batch_item(
                    f"Snaps ({len(plan.snaps)})", [f"pkg_snap {values}"]
                )
            )
        if plan.flatpaks:
            values = self._shell_words(plan.flatpaks)
            queue.append(
                self._batch_item(
                    f"Flatpaks ({len(plan.flatpaks)})", [f"pkg_flat {values}"]
                )
            )

        queue.extend(self._prepare_queue_script(s) for s in plan.scripts)
        return queue

    def _prepare_queue_script(self, script: dict) -> dict:
        """Homebrew entries have a virtual path: materialize like run_script."""
        if script.get("appstream_source") != "homebrew":
            return script
        from app import homebrew_catalog

        materialized = homebrew_catalog.materialize_install(script)
        self._manifest_temp_files = [
            *self._manifest_temp_files,
            materialized["path"],
        ]
        return materialized

    @staticmethod
    def _shell_words(names: list[str]) -> str:
        return " ".join(shlex.quote(name) for name in names)

    def _batch_item(self, name: str, lines: list[str]) -> dict:
        return {
            "name": name,
            "path": self._write_batch_script(lines),
            "is_script": True,
            "is_batch": True,
        }

    def _write_batch_script(self, lines: list[str]) -> str:
        """Write a temporary bash script with the given library calls.

        ``create_temp_file`` prepends the preamble that sets SCRIPT_DIR and
        loads the core library, so nothing is sourced here.
        """
        content = "\n".join(["#!/bin/bash", "set -eo pipefail", *lines, ""])
        fd, path = tempfile.mkstemp(prefix="linuxtoys-manifest-", suffix=".sh")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(path, 0o700)
        # Rebinding (not appending) avoids mutating the shared class attribute.
        self._manifest_temp_files = [*self._manifest_temp_files, path]
        return path

    def _discard_manifest_temp_files(self) -> None:
        """Remove generated batch/Homebrew scripts.

        ``_running_temp_path`` is the copy made by ``create_temp_file``; the
        scripts written above must be removed separately.
        """
        dev_mode = getattr(self, "_running_dev_mode", False)
        for temp_path in self._manifest_temp_files:
            self._cleanup_temp_file(temp_path, dev_mode)
        self._manifest_temp_files = []

    def _manifest_script_env(self, script_info: dict) -> dict[str, str]:
        """Extra variables the CLI exports through ``script_environment``."""
        from app.manifest_helper import script_environment

        env = script_environment(
            script_info, {"SCRIPT_DIR": resolve_script_dir()}
        )
        # The terminal receives one `export` line: keep every value on it.
        return {
            key: str(value).replace("\r", " ").replace("\n", " ")
            for key, value in env.items()
        }

    def _manifest_start_failed(self, script_name: str) -> None:
        """An item could not even start (e.g. repo entry preparation)."""
        if self._manifest_bootstrap is not None:
            self._after_manifest_bootstrap(ScriptResult.ERROR, 1)
        else:
            self._advance_manifest(script_name, 1)

    def _on_manifest_item_finished(self, message) -> None:
        script_info = self._running_script_info or {}
        script_name = script_info.get("name", "item desconhecido")
        exit_code = message.exit_code
        result = _classify_exit_code(exit_code)
        temp_path = self._running_temp_path
        dev_mode = self._running_dev_mode
        is_batch = bool(script_info.get("is_batch"))

        if not dev_mode:
            # Synthetic batch items are not LinuxToys scripts: no registry.
            if result is ScriptResult.SUCCESS and not is_batch:
                _save_script_to_registry(script_name, TRANSMAP_PATH)
                _cleanup_tmp_noram_dirs(TRANSMAP_PATH)
            self._remove_transmap()
        self._cleanup_temp_file(temp_path, dev_mode)

        if result is ScriptResult.TERMINAL_CLOSED:
            self.notify(
                "O terminal encerrou inesperadamente; uma nova sessão foi "
                "iniciada automaticamente.",
                severity="warning",
            )

        if self._manifest_bootstrap is not None:
            self._after_manifest_bootstrap(result, exit_code)
            return
        self._advance_manifest(script_name, exit_code, is_batch)

    @staticmethod
    def _manifest_status(result) -> str:
        if result is ScriptResult.SUCCESS:
            return "success"
        if result is ScriptResult.CANCELLED:
            return "cancelled"
        if result is ScriptResult.TERMINAL_CLOSED:
            return "closed"
        return "error"

    def _advance_manifest(
        self, script_name: str, exit_code: int | None, is_batch: bool = False
    ) -> None:
        """Record a result, then decide how the queue goes on.

        Like the CLI: only a failed *script* asks whether to continue, while
        failed package/snap/flatpak batches just move on. Cancellation and a
        closed terminal stop the whole run.
        """
        result = _classify_exit_code(exit_code)
        self._manifest_results.append(
            {
                "name": script_name,
                "exit_code": exit_code,
                "success": result is ScriptResult.SUCCESS,
                "status": self._manifest_status(result),
            }
        )

        if result in (ScriptResult.CANCELLED, ScriptResult.TERMINAL_CLOSED):
            self._stop_manifest_run()
            return
        if (
            result is ScriptResult.ERROR
            and not is_batch
            and self._manifest_queue
        ):
            self._ask_continue_after_failure(script_name, exit_code)
            return
        self._run_next_manifest_item()

    def _ask_continue_after_failure(
        self, script_name: str, exit_code: int
    ) -> None:
        remaining = [item["name"] for item in self._manifest_queue]
        self.app.push_screen(
            ManifestPlanDialog(
                f"'{script_name}' falhou (código de saída {exit_code}).",
                ["Continuar com os itens restantes?", "", *remaining],
                confirm_label="Continuar",
                cancel_label="Parar",
            ),
            self._on_manifest_continue_answer,
        )

    def _on_manifest_continue_answer(self, proceed: bool | None) -> None:
        if proceed:
            self._run_next_manifest_item()
            return
        self._stop_manifest_run()

    def _stop_manifest_run(self) -> None:
        not_run = [item["name"] for item in self._manifest_queue]
        self._manifest_queue = []
        self._finish_manifest_run(not_run)

    def _finish_manifest_run(self, not_run: list[str] | None = None) -> None:
        self._hide_terminal()
        results = self._manifest_results
        self._discard_manifest_temp_files()

        self._is_manifest_run = False
        self._manifest_queue = []
        self._manifest_results = []
        self._running_script_info = None
        self._running_temp_path = None
        self.app.push_screen(ManifestReportDialog(results, not_run=not_run))

    def _start_manifest_dry_run(self, plan) -> None:
        self.notify("Validando itens (dry-run)...", timeout=3)
        self.run_worker(
            lambda: self._dry_run_manifest(plan),
            thread=True,
            exclusive=True,
            name="manifest_dry_run",
        )

    def _dry_run_manifest(self, plan) -> None:
        """Same order as a real run; only scripts are actually validated."""
        from app.dev_mode import dry_run_script

        resolve_script_dir()

        results = []
        for label, names in (
            ("Pacotes", plan.packages),
            ("Snaps", plan.snaps),
            ("Flatpaks", plan.flatpaks),
        ):
            if names:
                results.append(
                    {
                        "name": f"{label} ({len(names)})",
                        "exit_code": 0,
                        "success": True,
                        "status": "simulated",
                    }
                )
        for script in plan.scripts:
            results.append(self._dry_run_script_item(script, dry_run_script))
        self.app.call_from_thread(self._finish_manifest_dry_run, results)

    @staticmethod
    def _dry_run_script_item(script: dict, dry_run_script) -> dict:
        """Mirror the CLI: pass when syntax and dependencies are valid."""
        cleanup_path = None
        try:
            target = script
            if script.get("is_repo_entry"):
                target = materialize_repo_script(script)
            elif script.get("appstream_source") == "homebrew":
                from app import homebrew_catalog

                target = homebrew_catalog.materialize_install(script)
                cleanup_path = target["path"]
            outcome = dry_run_script(target["path"])
            ok = bool(
                outcome["syntax_valid"] and outcome["dependencies_valid"]
            )
        except Exception:  # worker boundary: a broken script is a failed item
            ok = False
        finally:
            if cleanup_path:
                try:
                    os.unlink(cleanup_path)
                except OSError:
                    pass
        return {
            "name": script.get("name", "item desconhecido"),
            "exit_code": 0 if ok else 1,
            "success": ok,
            "status": "success" if ok else "error",
        }

    def _finish_manifest_dry_run(self, results: list[dict]) -> None:
        self.app.push_screen(ManifestReportDialog(results, dry_run=True))

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


class UpdateRunnerMixin:
    """Atualização do projeto."""

    _running_is_update: bool = False

    def _trigger_update_check(self) -> None:
        """Inicia a verificação de atualização em uma thread de trabalho."""
        self.notify("LinuxToys Update Checker...")
        self.run_worker(self._check_for_update, thread=True, exclusive=True)

    def _check_for_update(self) -> None:
        """
        Executa a verificação de atualização e chama o callback na thread principal.
        (Este método roda em uma thread de trabalho)
        """
        helper = UpdateHelper()
        available = helper._update_available()
        self.app.call_from_thread(self._on_update_checked, helper, available)

    def _on_update_checked(
        self, helper: UpdateHelper, available: bool
    ) -> None:
        """
        Callback executado após a verificação de atualização.
        Abre o diálogo de confirmação se uma atualização estiver disponível.
        """
        if not available:
            self.notify("✓ It's already on the latest available version")
            return
        tag = helper._latest_ver.get("tag_name", "")
        body = helper._latest_ver.get("body", "Sem changelog disponível.")
        self.app.push_screen(
            UpdateAvailableDialog(tag, body), callback=self._on_update_decision
        )

    def _on_update_decision(self, wants_update: bool | None) -> None:
        """Inicia o processo de atualização se o usuário confirmar."""
        if not wants_update:
            return

        self._running_is_update = True
        self._show_terminal()
        terminal = self.query_one("#terminal", Terminal)
        terminal.run_script(
            ["sh", "-c", "curl -fsSL https://linux.toys/install.sh | bash"]
        )

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
