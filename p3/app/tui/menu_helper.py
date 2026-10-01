from app.easy_cli import is_dev_mode_enabled
from app.updater.update_helper import UpdateHelper

from .about_lt import AboutWidget
from .dialog_screen import LanguageSelectorDialog, UpdateAvailableDialog
from .helper import (
    invalidate_search_caches,
    translations,
    warm_search_and_category_index,
)
from .manifest_dialog import ManifestDialog
from .my_widgets import Terminal
from .registry_screen import RegistryScreen


class MenuSelectionMixin:
    """
    Mixin para lidar com a lógica de seleção de itens no menu principal da HomeScreen.
    """

    _running_is_update: bool = False
    _is_about_showing: bool = False

    def on_list_view_selected(self, event) -> None:
        """
        Trata o item de menu selecionado e aciona a ação correspondente.
        """
        if event.item.id == "about":
            self._toggle_about_panel()
            return

        if self._is_about_showing:
            self._toggle_about_panel(force_hide=True)

        item_id = event.item.id
        action_map = {
            "registry": lambda: self.app.push_screen(RegistryScreen()),
            "scripts_resync": self._start_scripts_resync,
            "language": lambda: self.app.push_screen(
                LanguageSelectorDialog(), callback=self.apply_language_change
            ),
            "manifest": lambda: self.app.push_screen(
                ManifestDialog(), callback=self.on_manifest_chosen
            ),
            "update": self._trigger_update_check,
        }

        if item_id in action_map:
            action_map[item_id]()

    def _toggle_about_panel(self, force_hide: bool = False) -> None:
        """
        Mostra ou esconde o painel 'Sobre' no lugar do menu principal.
        """
        menu_panel = self.query_one("#menu-panel")
        logo = self.query_one("#logo")
        home_menu = self.query_one("#home-menu")
        terminal_container = self.query_one("#terminal-conteiner")

        # Esconde o terminal se estiver aberto
        if terminal_container.display:
            self._hide_terminal()

        should_show = not self._is_about_showing
        if force_hide:
            should_show = False

        self._is_about_showing = should_show
        logo.display = not should_show
        home_menu.display = not should_show

        if should_show:
            about_widget = AboutWidget(id="about-content")
            menu_panel.mount(about_widget)
            menu_panel.border_title = translations.get(
                "about_title", "About LinuxToys"
            )
            about_widget.scroll_visible()
        else:
            about_content = self.query("#about-content")
            if about_content:
                about_content.remove()

    async def action_go_back(self) -> None:
        if self._is_about_showing:
            self._toggle_about_panel(force_hide=True)
            return
        # O resto da função action_go_back original de main.py continua aqui
        # ... (código original)

    # Adicione esta função para garantir que o painel 'about' seja fechado ao ir para home
    async def action_reset_to_home(self) -> None:
        if self._is_about_showing:
            self._toggle_about_panel(force_hide=True)
        # O resto da função action_reset_to_home original de main.py continua aqui
        # ... (código original)

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

    def _start_scripts_resync(self) -> None:
        """Inicia a ressincronização dos scripts se o modo de desenvolvedor não estiver ativo."""
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
        """
        Força a atualização dos scripts e reporta o progresso.
        (Este método roda em uma thread de trabalho)
        """
        from app.git_scripts_manager import force_update_scripts

        def progress(key: str) -> None:
            message = translations.get(key, key)
            self.app.call_from_thread(self.notify, message, timeout=3)

        success = force_update_scripts(progress_callback=progress)
        self.app.call_from_thread(self._on_scripts_resync_done, success)

    def _on_scripts_resync_done(self, success: bool) -> None:
        """Callback executado após a tentativa de ressincronização."""
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
        """Aplica a mudança de idioma em toda a aplicação."""
        if new_language_code is None:
            return

        from app import lang_utils

        new_translations = lang_utils.load_translations(new_language_code)
        translations.clear()
        translations.update(new_translations)
        lang_utils.save_language(new_language_code)
        invalidate_search_caches()
        warm_search_and_category_index(translations)

        await self.action_reset_to_home()
        await self._refresh_fixed_ui_labels()
