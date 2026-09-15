from collections.abc import Iterable
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Static

from .helper import translations


class FilteredDirectoryTree(DirectoryTree):
    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [path for path in paths if not path.name.startswith(".")]


class ManifestDialog(ModalScreen[str | None]):
    BINDINGS = [
        ("escape", "cancel", "Cancelar"),
        ("w", "app.pop_screen", "Voltar"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="manifest-container"):
            with Horizontal(id="manifest-header"):
                yield Button(
                    translations.get("cancel_btn_label", "Cancelar"),
                    id="cancel-btn-manifest",
                    variant="error",
                )
                yield Static(
                    translations.get(
                        "choose_manifest_title",
                        "Por favor, escolha seu Manifest",
                    ),
                    id="manifest-title",
                )
                yield Input(placeholder="Buscar...", id="manifest-search")
                yield Button(
                    translations.get("select_button", "Selecionar"),
                    id="execute-btn-manifest",
                    variant="primary",
                    disabled=True,
                )

            yield FilteredDirectoryTree("~", id="manifest-tree")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel-btn-manifest":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
