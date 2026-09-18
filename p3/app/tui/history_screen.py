from textual.app import ComposeResult
from textual.binding import Binding
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

from app.registry_utils import parse_registry_file, search_registry_entries

from .helper import translations
from .my_widgets import FocusableLabel, HistoryListItem


class HistoryScreen(Screen):
    BINDINGS = [
        Binding(
            "escape", "close_history", translations.get("script_runner_close")
        ),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.registry_data = parse_registry_file()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="left-column"):
                yield Input(
                    placeholder=translations.get("search", "Search"),
                    id="search-registry",
                )
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
            yield FocusableLabel(
                f" [u]{translations.get('report_label', 'Report Bug')}[/u]",
                id="report-bug",
                classes="report-bug",
            )
            yield Link(
                f" {translations.get('credits_label', 'Credits')}",
                url="https://linux.toys/credits.html",
            )
            yield Link(
                f" {translations.get('support_footer', 'Support this project')}",
                url="https://ko-fi.com/psygreg",
            )
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one("#left-panel").border_title = translations.get(
            "scripts_label"
        )
        self.query_one("#right-panel").border_title = translations.get(
            "registry_details_label"
        )

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

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search-registry":
            return
        await self._filter_history(event.value)

    async def _filter_history(self, query: str) -> None:
        filtered_data = search_registry_entries(self.registry_data, query)
        filtered_names = sorted(filtered_data.keys())
        list_view = self.query_one("#history-list", ListView)
        await list_view.clear()
        for name in filtered_names:
            await list_view.append(HistoryListItem(name))
        details = self.query_one("#history-details", Static)
        if filtered_names:
            list_view.index = 0
            self._display_script_details(filtered_names[0])
        else:
            details.update("No script found.")  # criar tradução

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
