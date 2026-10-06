import json
import os
import shutil
import subprocess
from functools import partial

from textual import on
from textual.app import ComposeResult
from textual.containers import Grid, Vertical
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

from .helper import translations

# --- Reusable Business Logic ---


def _run_fetcher(command: str, *args: str) -> str | None:
    """Runs the skills_fetcher.py script to get data."""
    # This path assumes skills_fetcher.py is at the project root.
    fetcher_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "../..", "skills_fetcher.py")
    )
    if not os.path.exists(fetcher_path):
        return None
    python = shutil.which("python3") or shutil.which("python")
    if not python:
        return None
    cmd = [python, fetcher_path, command, *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, check=False
        )
        return result.stdout if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        # Log the error for debugging, but don't crash.
        print(f"Error running fetcher: {e}", flush=True)
        return None


def _run_npx_command(*args: str) -> tuple[bool, str]:
    """Runs an npx command and returns (success, output)."""
    npx = shutil.which("npx")
    if not npx:
        return False, "Error: 'npx' is not installed or not in PATH."
    cmd = [npx, *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, check=False
        )
        output = result.stdout.strip() or result.stderr.strip()
        return result.returncode == 0, output
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, f"Error executing npx command: {e}"


def _detect_agents() -> list[str]:
    """
    Detects installed AI agents based on directory presence.
    Reused directly from skills_view.py logic.
    """
    home = os.path.expanduser("~")
    agents = []
    checks = [
        (os.path.join(home, ".claude"), "claude-code"),
        (os.path.join(home, ".codex"), "codex"),
        (os.path.join(home, ".config", "opencode"), "opencode"),
        (os.path.join(home, ".cursor"), "cursor"),
        (os.path.join(home, ".windsurf"), "windsurf"),
        (os.path.join(home, ".gemini"), "gemini-cli"),
        (os.path.join(home, ".agents"), "cline"),
        (os.path.join(home, ".roo"), "roo"),
        (os.path.join(home, ".trae"), "trae"),
        (os.path.join(home, ".kilocode"), "kilo"),
        (os.path.join(home, ".factory"), "droid"),
        (os.path.join(home, ".copilot"), "github-copilot"),
    ]
    for path, name in checks:
        if os.path.isdir(path):
            agents.append(name)
    return agents


def _format_installs(count: int) -> str:
    """Formats install counts (e.g., 1200 -> 1.2K)."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        return "0"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


# --- TUI Modal Screens ---


class ActionDialog(ModalScreen[str]):
    """A modal dialog to ask for skill actions."""

    def __init__(self, skill_name: str):
        super().__init__()
        self.skill_name = skill_name

    def compose(self) -> ComposeResult:
        with Grid(id="dialog"):
            yield Label(f"Action for [b]{self.skill_name}[/b]")
            yield Button(
                translations.get("skills_install_label", "Install"),
                variant="success",
                id="install",
            )
            yield Button(
                translations.get("skills_detail_label", "View Details"),
                id="details",
                disabled=True,
            )
            yield Button(
                translations.get("skills_back_label", "Back"),
                variant="primary",
                id="back",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)


class AgentChoiceDialog(ModalScreen[str]):
    """A modal to choose which agent to install a skill for."""

    def __init__(self, agents: list[str]):
        super().__init__()
        self.agents = agents

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(
                translations.get("skills_choose_agent_title", "Choose Agent")
            )
            for agent in self.agents:
                yield Button(agent, id=agent, classes="agent_button")
            yield Button(
                translations.get("skills_back_label", "Cancel"),
                id="cancel",
                variant="error",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)


# --- TUI Main View and Components ---


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


class SkillsSeekerView(Static):
    """The main view for browsing and managing skills."""

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="discover") as tc:
            with TabPane("Discover", id="discover"):
                yield LoadingIndicator()
                yield ListView(id="discover_list", classes="hidden")
                yield Label(
                    "Could not load skills.",
                    id="discover_error",
                    classes="hidden",
                )
            with TabPane("Installed", id="installed"):
                yield LoadingIndicator()
                yield ListView(id="installed_list", classes="hidden")
                yield Label(
                    "Could not load installed skills.",
                    id="installed_error",
                    classes="hidden",
                )
        tc.border_title = "Skills Seeker"

    def on_mount(self) -> None:
        self.load_popular()
        self.load_installed()

    @on(TabbedContent.TabActivated)
    def on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Reload content when a tab is activated, if needed."""
        if event.pane.id == "installed":
            self.load_installed()

    @on(ListView.Selected, "#discover_list")
    async def on_discover_skill_selected(
        self, event: ListView.Selected
    ) -> None:
        card = event.item
        if not isinstance(card, SkillCard):
            return

        action = await self.app.push_screen_wait(ActionDialog(card.name))
        if action == "install":
            self.run_install_flow(card.skill)

    def run_install_flow(self, skill: dict) -> None:
        """Full flow to install a skill for a chosen agent."""
        agents = _detect_agents()
        if not agents:
            self.notify(
                "No supported AI agents found on your system.",
                severity="error",
                timeout=5,
            )
            return

        async def on_agent_chosen(agent: str):
            if agent != "cancel":
                self.notify(
                    f"Installing '{skill['name']}' for agent '{agent}'..."
                )
                self.parent.add_class("installing")
                self.run_worker(
                    partial(
                        _run_npx_command,
                        "skills",
                        "add",
                        skill["source"],
                        "--skill",
                        skill["skillId"],
                        "-a",
                        agent,
                        "-g",
                        "-y",
                    ),
                    partial(self._on_install_done, skill["name"]),
                    thread=True,
                )

        self.app.push_screen(AgentChoiceDialog(agents), on_agent_chosen)

    def _on_install_done(
        self, skill_name: str, result: tuple[bool, str]
    ) -> None:
        self.parent.remove_class("installing")
        success, output = result
        if success:
            self.notify(f"Skill '{skill_name}' installed successfully.")
            self.load_installed()
        else:
            self.notify(
                f"Failed to install '{skill_name}': {output}",
                severity="error",
                timeout=10,
            )

    def _handle_remove_skill(self, skill: dict) -> None:
        """Initiates the skill removal process."""
        skill_name = skill.get("name", "Unknown")
        self.notify(f"Removing '{skill_name}'...")
        self.parent.add_class("installing")  # Reuse loading style
        self.run_worker(
            partial(
                _run_npx_command, "skills", "remove", skill_name, "-g", "-y"
            ),
            partial(self._on_remove_done, skill_name),
            thread=True,
        )

    def _on_remove_done(
        self, skill_name: str, result: tuple[bool, str]
    ) -> None:
        self.parent.remove_class("installing")
        success, output = result
        if success:
            self.notify(f"Skill '{skill_name}' removed successfully.")
            self.load_installed()
        else:
            self.notify(
                f"Failed to remove '{skill_name}': {output}",
                severity="error",
                timeout=10,
            )

    # --- Data Loading Methods ---

    def _set_list_state(
        self, list_id: str, state: str, data: list | None = None
    ):
        """Helper to manage UI states: loading, error, success."""
        list_view = self.query_one(f"#{list_id}_list", ListView)
        loading = self.query_one(f"#{list_id} LoadingIndicator")
        error_label = self.query_one(f"#{list_id}_error", Label)

        loading.display = state == "loading"
        error_label.display = state == "error"
        list_view.display = state == "success"

        if state == "success":
            list_view.clear()
            if not data:
                # Can't append to listview, so we show the error label with a different message
                error_label.display = True
                error_label.update("No skills found.")
                list_view.display = False
            elif list_id == "discover":
                for skill in data:
                    list_view.append(SkillCard(skill))
            elif list_id == "installed":
                for skill in data:
                    list_view.append(
                        InstalledCard(skill, self._handle_remove_skill)
                    )

    def load_popular(self) -> None:
        self._set_list_state("discover", "loading")
        self.run_worker(
            partial(_run_fetcher, "popular", "50"),
            self._on_popular_loaded,
            thread=True,
        )

    def _on_popular_loaded(self, result: str | None) -> None:
        if result:
            try:
                data = json.loads(result)
                self._set_list_state("discover", "success", data.get("skills"))
            except json.JSONDecodeError:
                self._set_list_state("discover", "error")
        else:
            self._set_list_state("discover", "error")

    def load_installed(self) -> None:
        self._set_list_state("installed", "loading")
        self.run_worker(
            partial(_run_npx_command, "skills", "list", "--json", "-g"),
            self._on_installed_loaded,
            thread=True,
        )

    def _on_installed_loaded(self, result: tuple[bool, str]) -> None:
        success, output = result
        if success:
            try:
                skills = json.loads(output or "[]")
                self._set_list_state("installed", "success", skills)
            except json.JSONDecodeError:
                self._set_list_state("installed", "error")
        else:
            self._set_list_state("installed", "error")

    def do_search(self, query: str):
        """Public method to trigger a search from the parent screen."""
        self.query_one(TabbedContent).active = "discover"
        self._set_list_state("discover", "loading")
        self.run_worker(
            partial(_run_fetcher, "search", query),
            self._on_popular_loaded,
            thread=True,
        )
