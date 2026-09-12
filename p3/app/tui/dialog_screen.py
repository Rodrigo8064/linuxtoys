from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from app.antenna import antenna
from app.lang_utils import create_translator
from app.official_index import get_bug_report_entries

from .helper import translations


class ConfirmScriptScreen(ModalScreen[bool]):
    """Modal exibido antes de rodar um script, com a descrição e
    dois botões: Cancelar e Executar. Retorna True/False via dismiss()."""

    BINDINGS = [
        ("escape", "cancel", "Cancelar"),
        ("w", "app.pop_screen", "Voltar"),
    ]

    def __init__(self, name: str, description: str) -> None:
        super().__init__()
        self.script_name = name
        self.description = description

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(f"Executar '{self.script_name}'?", id="confirm-title")
            yield Static(self.description, id="confirm-description")
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancelar", id="cancel-btn", variant="error")
                yield Button("Executar", id="execute-btn", variant="success")

    def on_mount(self) -> None:
        self.query_one("#confirm-description").border_title = "Descrição"
        self.query_one("#execute-btn", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "execute-btn")

    def action_cancel(self) -> None:
        self.dismiss(False)


class RemoveScriptScreen(ModalScreen[bool]):
    """Modal exibido antes de rodar um script, com a descrição e
    dois botões: Cancelar e Executar. Retorna True/False via dismiss()."""

    BINDINGS = [
        ("escape", "cancel", "Cancelar"),
        ("w", "app.pop_screen", "Voltar"),
    ]

    def __init__(self, name: str, description: str) -> None:
        super().__init__()
        self.script_name = name
        self.description = description

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(f"Remover '{self.script_name}'?", id="confirm-title")
            yield Static(self.description, id="confirm-description")
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancelar", id="cancel-btn", variant="error")
                yield Button("Remover", id="execute-btn", variant="success")

    def on_mount(self) -> None:
        self.query_one("#confirm-description").border_title = "Descrição"
        self.query_one("#execute-btn", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "execute-btn")

    def action_cancel(self) -> None:
        self.dismiss(False)


class SuccessDialog(ModalScreen[None]):
    """Diálogo exibido quando um script termina com exit code 0."""

    BINDINGS = [
        ("escape", "cancel", "Fechar"),
        ("w", "app.pop_screen", "Voltar"),
    ]

    def __init__(self, script_name: str, action: str = "instalado") -> None:
        super().__init__()
        self.script_name = script_name
        self.action = action

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(f"'{self.script_name}' foi {self.action} com sucesso.")
            with Horizontal(id="confirm-buttons"):
                yield Button("Concluir", id="execute-btn", variant="success")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "execute-btn":
            self.dismiss()

    def action_cancel(self) -> None:
        self.dismiss()


class ErrorDialog(ModalScreen[bool]):
    """Diálogo exibido quando um script termina com um exit code de
    erro de verdade (não cancelamento nem Ctrl+C). Retorna True via
    dismiss() se o usuário pediu pra reportar o bug, False caso contrário."""

    BINDINGS = [
        ("escape", "cancel", "Fechar"),
        ("w", "app.pop_screen", "Voltar"),
    ]

    def __init__(
        self, script_name: str, exit_code: int | None, action: str = "instalar"
    ) -> None:
        super().__init__()
        self.script_name = script_name
        self.exit_code = exit_code
        self.action = action

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(
                f"Falha ao {self.action} '{self.script_name}' "
                f"(código de saída: {self.exit_code})."
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Concluir", id="execute-btn", variant="success")
                yield Button(
                    "Reportar Bug", id="report-bug", variant="warning"
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "report-bug")

    def action_cancel(self) -> None:
        self.dismiss(False)


class SudoPasswordScreen(ModalScreen[str | None]):
    """Modal pra coletar a senha sudo dentro do próprio TUI (nunca via
    /dev/tty, pra não brigar com o Textual pela leitura do terminal).
    Retorna a senha via dismiss(), ou None se cancelado."""

    BINDINGS = [
        ("escape", "cancel", "Cancelar"),
        ("w", "app.pop_screen", "Voltar"),
    ]

    def __init__(
        self, script_name: str, error_message: str | None = None
    ) -> None:
        super().__init__()
        self.script_name = script_name
        self.error_message = error_message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(
                f"'{self.script_name}' precisa de privilégios sudo",
                id="confirm-title",
            )
            if self.error_message:
                yield Static(self.error_message, id="sudo-error")
            yield Input(
                placeholder="Senha",
                password=True,
                id="sudo-password-input",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancelar", id="cancel-btn", variant="error")
                yield Button("Confirmar", id="confirm-btn", variant="success")

    def on_mount(self) -> None:
        self.query_one("#sudo-password-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "confirm-btn":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def _submit(self) -> None:
        password = self.query_one("#sudo-password-input", Input).value
        self.dismiss(password or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CancelledDialog(ModalScreen[None]):
    """Diálogo exibido quando a execução é cancelada (código 100 —
    convenção do LinuxToys pra cancelamento, seja injetado por nós ao
    cancelar o prompt de senha, seja gerado pelo próprio script)."""

    BINDINGS = [
        ("escape", "cancel", "Fechar"),
        ("w", "app.pop_screen", "Voltar"),
    ]

    def __init__(self, script_name: str) -> None:
        super().__init__()
        self.script_name = script_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(f"Execução de '{self.script_name}' cancelada.")
            with Horizontal(id="confirm-buttons"):
                yield Button("OK", id="execute-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "execute-btn":
            self.dismiss()

    def action_cancel(self) -> None:
        self.dismiss()


class ReportBugDialog(ModalScreen[bool]):
    BINDINGS = [
        ("escape", "cancel", "Fechar"),
        ("w", "app.pop_screen", "Voltar"),
    ]

    def compose(self) -> ComposeResult:
        _ = create_translator()
        bug_report_app = _("bug_report_app")
        bug_report_desc = _("bug_report_desc")
        system_info = _("system_info")
        system_context = antenna.get_system_context()

        raw_entries = get_bug_report_entries(translations=translations)
        options = [("LinuxToys", "LinuxToys")] + [
            (display_name, name) for name, display_name in raw_entries
        ]

        with Vertical(id="dialog-container"):
            with Horizontal(classes="form-row"):
                yield Static(bug_report_app, classes="label-inline")
                yield Select(
                    options,
                    value="LinuxToys",
                    id="select-app",
                    allow_blank=False,
                )
            yield Static(bug_report_desc, classes="field-label")
            yield TextArea(id="bug-description")
            yield Static(
                f"{system_info}: {system_context}", id="system-info-text"
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancelar", id="cancel-btn-bug", variant="error")
                yield Button("OK", id="execute-btn-bug", variant="primary")

    @on(Button.Pressed, "#cancel-btn-bug")
    def handle_cancel(self) -> None:
        self.dismiss()

    # 2. Trata o clique no botão OK / Enviar
    @on(Button.Pressed, "#execute-btn-bug")
    def handle_execute(self) -> None:
        _ = create_translator()
        bug_report_empty = _("bug_report_empty")
        # Resgata os valores diretamente dos widgets
        comment = self.query_one("#bug-description", TextArea).text.strip()
        selected_app = self.query_one("#select-app", Select).value

        # Validação simples para não enviar vazio
        if not comment:
            self.notify(
                bug_report_empty,
                severity="warning",
            )
            return

        # Desabilita o botão para evitar múltiplos cliques enquanto envia
        self.query_one("#execute-btn-bug", Button).disabled = True

        # Dispara o worker em segundo plano
        self._submit_bug_report(comment, selected_app)

    # 3. Worker assíncrono do Textual (substitui threading.Thread e GLib)
    @work(thread=True)
    def _submit_bug_report(self, comment: str, selected_app=None) -> None:
        """Envia o relatório de bug em uma thread de segundo plano."""
        _ = create_translator()
        bug_report_submitted = _("bug_report_submitted")
        bug_report_failed = _("bug_report_failed")
        try:
            system_context = antenna.get_system_context()
            antenna.submit_issue(
                title="User Report: Bug from TUI",
                logs=comment,
                context=system_context,
                is_footer_triggered=True,
                related_app=selected_app,
                infer_related_app=False,
            )

            # Notificação de Sucesso na thread principal
            self.app.call_from_thread(
                self.notify,
                bug_report_submitted,
                title="Sucesso",
                severity="information",
            )

            # Fecha a janela modal após envio
            self.app.call_from_thread(self.dismiss)

        except Exception as e:
            # Notificação de Erro na thread principal
            self.app.call_from_thread(
                self.notify,
                f"{bug_report_failed}: {e}",
                title="Erro",
                severity="error",
            )
            # Reabilita o botão para permitir nova tentativa
            self.app.call_from_thread(
                setattr,
                self.query_one("#execute-btn-bug", Button),
                "disabled",
                False,
            )
