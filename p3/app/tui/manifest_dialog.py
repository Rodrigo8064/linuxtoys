from collections.abc import Iterable
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, DirectoryTree, Footer, Input, Static

from .helper import translations


class FilteredDirectoryTree(DirectoryTree):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.search_query: str = ""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        _IGNORED_DIRS = {
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            ".git",
        }
        paths = [
            p
            for p in paths
            if not p.name.startswith(".")
            and p.name not in _IGNORED_DIRS
            and (p.is_dir() or p.name.lower().endswith(".txt"))
        ]
        query = self.search_query.strip().lower()
        if not query:
            return [
                p
                for p in paths
                if p.is_file() or self._dir_contains_match(p, "")
            ]
        return [p for p in paths if self._matches(p, query)]

    def _matches(self, path: Path, query: str) -> bool:
        if query in path.name.lower():
            return True
        return path.is_dir() and self._dir_contains_match(path, query)

    def _dir_contains_match(
        self, directory: Path, query: str, max_depth: int = 3
    ) -> bool:
        """Checa recursivamente (com limite de profundidade, por
        segurança/performance) se algo dentro de `directory` bate com
        a busca."""
        if max_depth <= 0:
            return False
        try:
            entries = directory.iterdir()
        except (PermissionError, OSError):
            return False
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if query in entry.name.lower():
                return True
            if entry.is_dir() and self._dir_contains_match(
                entry, query, max_depth - 1
            ):
                return True
        return False


class ManifestDialog(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", translations.get("script_runner_close"))
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
                yield Input(
                    placeholder=translations.get("search", "Search"),
                    id="manifest-search",
                )
                yield Button(
                    translations.get("select_button", "Selecionar"),
                    id="execute-btn-manifest",
                    variant="primary",
                    disabled=True,
                )

            yield FilteredDirectoryTree(
                Path("~").expanduser(), id="manifest-tree"
            )
        yield Footer()

    def on_mount(self):
        self._search_timer: Timer | None = None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel-btn-manifest":
            self.dismiss(None)
        elif event.button.id == "execute-btn-manifest":
            self.dismiss(getattr(self, "selected_path", None))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "manifest-search":
            return
        query = event.value
        if self._search_timer is not None:
            self._search_timer.stop()
        self._search_timer = self.set_timer(
            0.3, lambda: self._apply_search(query)
        )

    def _apply_search(self, query: str) -> None:
        tree = self.query_one("#manifest-tree", FilteredDirectoryTree)
        tree.search_query = query
        tree.reload()

    def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected
    ) -> None:
        if event.path.is_file() and event.path.suffix.lower() == ".txt":
            self.selected_path = str(event.path)
            self.query_one("#execute-btn-manifest", Button).disabled = False
        else:
            self.query_one("#execute-btn-manifest", Button).disabled = True

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ) -> None:
        self.query_one("#execute-btn-manifest", Button).disabled = True


class ManifestReportDialog(ModalScreen[None]):
    def __init__(self, results: list[dict]) -> None:
        super().__init__()
        self.results = results

    def compose(self) -> ComposeResult:
        total = len(self.results)
        successes = [r for r in self.results if r["success"]]
        failures = [r for r in self.results if not r["success"]]

        with Vertical(id="confirm-dialog"):
            yield Static(
                f"Manifesto concluído: {len(successes)}/{total} instalados com sucesso."
            )
            if failures:
                yield Static("Itens com erro:", classes="field-label")
                with VerticalScroll(id="manifest-failures"):
                    for item in failures:
                        yield Static(
                            f"• {item['name']} — código de saída: {item['exit_code']}"
                        )
            with Horizontal(id="confirm-buttons"):
                yield Button("OK", id="execute-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "execute-btn":
            self.dismiss()
