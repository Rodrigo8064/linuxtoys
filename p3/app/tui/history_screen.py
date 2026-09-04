from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Footer,
    Header,
    Input,
    Link,
    ListView,
    Static,
)

from app.lang_utils import create_translator
from app.registry_utils import parse_registry_file

from .my_widgets import HistoryListItem


class HistoryScreen(Screen):
    BINDINGS = [
        ("escape", "close_history", "Fechar"),
        ("w", "app.pop_screen", "Voltar"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.registry_data = parse_registry_file()

    def compose(self) -> ComposeResult:
        _ = create_translator()
        credits = _("credits_label")
        suport = _("support_footer")
        yield Header(name="Histórico de execição")
        with Horizontal(id="body"):
            with Vertical(id="left-column"):
                yield Input(placeholder="Buscar", id="search-registry")
                with Vertical(id="left-panel"):
                    script_name = sorted(self.registry_data.keys())
                    yield ListView(
                        *[HistoryListItem(name) for name in script_name],
                        id="history-list",
                    )
            with Vertical(id="right-panel"):
                yield Static(
                    "Selecione um script à esquerda para ver os detalhes"
                    if self.registry_data
                    else "Nenhum script foi executado ainda.",
                    id="history-details",
                )
        with Horizontal(classes="home-links"):
            yield Link(" Wiki", url="https://linux.toys/knowledgebase.html")
            yield Link(f" {credits}", url="https://linux.toys/credits.html")
            yield Link(f" {suport}", url="https://ko-fi.com/psygreg")
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one("#left-panel").border_title = "Scripts"
        self.query_one("#right-panel").border_title = "Detalhes do registro"

        list_view = self.query_one("#history-list", ListView)
        list_view.focus()
        if self.registry_data:
            list_view.index = 0
            first_item = list_view.query(HistoryListItem).first()
            self._display_script_details(first_item.script_name)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if isinstance(item, HistoryListItem):
            self._display_script_details(item.script_name)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search-registry":
            return
        self._filter_history(event.value)

    def _filter_history(self, query: str) -> None:
        query = query.strip().lower()
        all_names = sorted(self.registry_data.keys())
        filtered_names = (
            [name for name in all_names if query in name.lower()]
            if query
            else all_names
        )
        list_view = self.query_one("#history-list", ListView)
        list_view.clear()
        for name in filtered_names:
            list_view.append(HistoryListItem(name))

        details = self.query_one("#history-details", Static)
        if filtered_names:
            list_view.index = 0
            self._display_script_details(filtered_names[0])
        else:
            details.update("Nenhum script encontrado.")

    def _display_script_details(self, script_name: str) -> None:
        executions = self.registry_data.get(script_name, [])
        lines = [f"Script: {script_name}\n", "=" * 60 + "\n\n"]

        for idx, (timestamp, operations) in enumerate(executions, 1):
            lines.append(f"Timestamp: {idx}\n")
            if timestamp:
                lines.append(f"Timestamp: {timestamp}\n")
            lines.append("\n")
            if operations:
                lines.append("Operações:\n")
                for op in operations:
                    lines.append(f"  • {op}\n")
            else:
                lines.append("Operações: (nenhuma)\n")
            lines.append("\n" + "-" * 60 + "\n\n")

        self.query_one("#history-details", Static).update("".join(lines))

    def action_close_history(self) -> None:
        self.app.pop_screen()


class HistoryOpenerMixin:
    def action_open_history(self) -> None:
        self.app.push_screen(HistoryScreen())
