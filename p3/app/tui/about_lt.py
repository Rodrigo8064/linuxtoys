import requests
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Rule,
    Static,
    TabbedContent,
    TabPane,
)

from app import get_app_resource_path
from app.compat import get_system_compat_keys
from app.updater import __version__

from .helper import translations


class AboutScreen(ModalScreen[None]):
    BINDINGS = [
        Binding(
            "escape", "app.pop_screen", translations.get("script_runner_close")
        ),
    ]

    def compose(self) -> ComposeResult:
        compat_display = self._get_compat_display_string()

        with Vertical(id="about-container"):
            with TabbedContent(initial="about"):
                with TabPane(
                    translations.get("about_tab", "About"),
                    id="about",
                ):
                    with VerticalScroll(classes="about-scroll"):
                        # logo and info
                        with Vertical(classes="profile-text"):
                            yield Static(
                                "[bold]LinuxToys[/bold]",
                            )
                            yield Static(compat_display)
                            yield Static(
                                translations.get(
                                    "subtitle",
                                    "A collection of tools for Linux in a user-friendly way.",
                                ),
                            )

                        yield Rule()
                        # Autor
                        with Vertical(classes="profile-text"):
                            yield Static(
                                "[bold]Victor 'psygreg' Gregory[/bold]"
                            )
                            yield Static(
                                translations.get(
                                    "project_lead", "Project Lead"
                                )
                            )

                        yield Rule()

                        # contributors grid
                        with Vertical(classes="section-title"):
                            yield Static(
                                f"[bold]{translations.get('contributors_label', 'Contributors')}[/bold]",
                            )
                        yield Vertical(
                            id="contributors-list",
                            classes="contributors-grid",
                        )
                with TabPane(
                    translations.get("license_tab", "License"), id="license"
                ):
                    license_text = self.get_license_text()
                    with VerticalScroll(id="license-scroll"):
                        yield Static(
                            content=license_text, classes="license-text"
                        )

        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#contributors-list").mount(
            Static("Loading contributors....")
        )
        self._load_contributors()

    def _get_compat_display_string(self) -> str:
        compat_keys = get_system_compat_keys()
        os_keys = [
            "debian",
            "ubuntu",
            "zorin",
            "cachy",
            "arch",
            "fedora",
            "rhel",
            "suse",
            "solus",
            "manjaro",
            "ostree",
            "ublue",
        ]
        primary_os = None
        for key in os_keys:
            if key in compat_keys:
                primary_os = key
                break
        display_parts = [__version__]
        if primary_os:
            display_parts.append(primary_os.capitalize())
        return " | ".join(display_parts) if display_parts else "Unknown"

    @work(exclusive=True, thread=True)
    def _load_contributors(self) -> None:
        try:
            response = requests.get(
                "https://api.github.com/repos/psygreg/linuxtoys/contributors",
                timeout=10,
            )
            response.raise_for_status()
            contributors_data = response.json()
            # Filter out 'psygreg' (project lead), 'Script Update Bot', and 'Gitea Actions'
            excluded_users = {
                "psygreg",
                "script update bot",
                "gitea actions",
            }
            filtered_contributors = [
                c
                for c in contributors_data
                if c.get("login", "").lower() not in excluded_users
            ]
            contributors = filtered_contributors[:9]
        except Exception as exc:
            self.app.call_from_thread(self._show_contributors_error, exc)
            return

        self.app.call_from_thread(self._show_contributors, contributors)

    def _show_contributors(self, contributors: list[dict]) -> None:
        container = self.query_one("#contributors-list")
        container.remove_children()
        if not contributors:
            container.mount(Static("Unable to load contributors."))
            return
        for c in contributors:
            container.mount(Static(c.get("login", "?")))

    def _show_contributors_error(self, exc: Exception) -> None:
        container = self.query_one("#contributors-list")
        container.remove_children()
        container.mount(Static(f"Error loading contributors: {exc}"))

    def get_license_text(self) -> str:
        license_path = get_app_resource_path("../LICENSE")
        try:
            with open(license_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            return f"Error loading license. {e}"
