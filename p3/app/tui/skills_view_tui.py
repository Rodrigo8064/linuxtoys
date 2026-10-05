import json
import os
import subprocess
from functools import partial

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Label,
    ListItem,
    ListView,
    LoadingIndicator,
    Static,
    TabbedContent,
    TabPane,
)


# --- Reusable Business Logic ---
# This function is a direct adaptation from skills_view.py, ensuring
# the same business logic is used.
def _run_fetcher(command: str, *args: str) -> str | None:
    """Runs the skills_fetcher.py script to get data."""
    fetcher_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "../../skills_fetcher.py"
    )
    python = os.environ.get("PYTHON", "python3")
    cmd = [python, fetcher_path, command, *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, check=False
        )
        return result.stdout if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _format_installs(count: int) -> str:
    """Formats install counts (e.g., 1200 -> 1.2K)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


# --- TUI Components ---


class SkillCard(ListItem):
    """A widget to display a skill in the 'Discover' list."""

    def __init__(self, skill: dict):
        super().__init__()
        self.skill = skill
        self.name = skill.get("name", "Unknown")
        self.installs = skill.get("installs", 0)

    def compose(self) -> ComposeResult:
        installs_str = _format_installs(self.installs)
        yield Label(f"{self.name}\n[dim] {installs_str}[/dim]")


class InstalledCard(ListItem):
    """A widget to display an installed skill."""

    def __init__(self, skill: dict, on_remove):
        super().__init__()
        self.skill = skill
        self.on_remove = on_remove
        self.name = skill.get("name", "Unknown")
        self.agents = ", ".join(skill.get("agents", []))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{self.name}[/b]")
            if self.agents:
                yield Label(f"[dim]Agents: {self.agents}[/dim]")
            yield Button("Remove", variant="error", classes="remove_button")

    @on(Button.Pressed, ".remove_button")
    def handle_remove_button(self, event: Button.Pressed) -> None:
        event.stop()
        self.on_remove(self.skill)


class ActionDialog(ModalScreen):
    """A modal dialog to ask for skill actions."""

    def __init__(self, skill_name: str):
        super().__init__()
        self.skill_name = skill_name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Action for [b]{self.skill_name}[/b]")
            yield Button("Install", variant="success", id="install")
            yield Button("View Details", id="details")
            yield Button("Back", variant="primary", id="back")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)


class SkillsSeekerView(Static):
    """The main view for browsing and managing skills."""

    def __init__(self, translations: dict):
        super().__init__()
        self.translations = translations

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="discover") as tc:
            with TabPane("Discover", id="discover"):
                yield LoadingIndicator()
                yield ListView(id="discover_list")
            with TabPane("Installed", id="installed"):
                yield LoadingIndicator()
                yield ListView(id="installed_list")
        tc.border_title = "Skills Seeker"

    def on_mount(self) -> None:
        """Load initial data when the widget is mounted."""
        self.load_popular()
        self.load_installed()

    @on(ListView.Selected, "#discover_list")
    async def on_discover_skill_selected(
        self, event: ListView.Selected
    ) -> None:
        """Handle selection of a skill in the discover list."""
        card = event.item
        if not isinstance(card, SkillCard):
            return

        def handle_dialog(result: str):
            if result == "install":
                self.notify(
                    f"Install action for '{card.name}' (not implemented)."
                )
            elif result == "details":
                self.notify(
                    f"Details action for '{card.name}' (not implemented)."
                )

        self.app.push_screen(ActionDialog(card.name), handle_dialog)

    def _handle_remove_skill(self, skill: dict) -> None:
        self.notify(
            f"Remove action for '{skill.get('name')}' (not implemented)."
        )
        # In a real scenario, this would call _run_fetcher('remove', ...)
        # and refresh the list upon completion.

    # --- Data Loading Methods ---

    def load_popular(self) -> None:
        """Load popular skills in a background thread."""
        self.query_one("#discover_list").display = False
        self.query_one("#discover LoadingIndicator").display = "block"
        self.run_worker(
            partial(_run_fetcher, "popular", "50"),
            self._on_popular_loaded,
            thread=True,
        )

    def _on_popular_loaded(self, result: str | None) -> None:
        """Callback to populate the list with popular skills."""
        list_view = self.query_one("#discover_list", ListView)
        list_view.clear()
        if result:
            try:
                data = json.loads(result)
                skills = data.get("skills", [])
                for skill in skills:
                    list_view.append(SkillCard(skill))
            except json.JSONDecodeError:
                self.notify(
                    "Failed to parse popular skills data.", severity="error"
                )
        else:
            self.notify("Could not load popular skills.", severity="error")

        self.query_one("#discover LoadingIndicator").display = "none"
        list_view.display = True

    def load_installed(self) -> None:
        """Load installed skills in a background thread."""
        self.query_one("#installed_list").display = False
        self.query_one("#installed LoadingIndicator").display = "block"
        self.run_worker(
            partial(_run_fetcher, "list", "--json", "-g"),
            self._on_installed_loaded,
            thread=True,
        )

    def _on_installed_loaded(self, result: str | None) -> None:
        """Callback to populate the list with installed skills."""
        list_view = self.query_one("#installed_list", ListView)
        list_view.clear()
        if result:
            try:
                skills = json.loads(result or "[]")
                for skill in skills:
                    list_view.append(
                        InstalledCard(skill, self._handle_remove_skill)
                    )
            except json.JSONDecodeError:
                self.notify(
                    "Failed to parse installed skills data.", severity="error"
                )
        else:
            self.notify("Could not load installed skills.", severity="error")

        self.query_one("#installed LoadingIndicator").display = "none"
        list_view.display = True
