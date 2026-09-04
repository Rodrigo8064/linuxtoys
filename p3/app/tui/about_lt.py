import requests
from rich_pixels import Pixels
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Link,
    Rule,
    Static,
)

from app import get_app_resource_path
from app.compat import get_system_compat_keys
from app.lang_utils import create_translator
from app.updater import __version__


class AboutScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Voltar"),
        ("w", "app.pop_screen", "Voltar"),
    ]

    def compose(self) -> ComposeResult:
        _ = create_translator()
        project_lead = _("project_lead")
        subtitle = _("subtitle")
        compat_display = self._get_compat_display_string()
        contributors_label = _("contributors")
        license = _("license_tab")
        credits = _("credits_label")
        suport = _("support_footer")

        yield Header()
        with Vertical(classes="home"):
            yield Static(
                Pixels.from_image_path(
                    "app/icons/linuxtoys_tui.png", resize=(40, 40)
                ),
                id="logo",
            )
            yield Static("[bold]LinuxToys[/bold]")
            yield Static(compat_display)
            yield Static(subtitle)
            yield Rule()
            yield Static("[bold]Victor 'psygreg' Gregory[/bold]")
            yield Static(project_lead)
            yield Rule()
            yield Static(f"[bold]{contributors_label}[/bold]")
            yield Vertical(id="contributors-list", classes="contributors-grid")
            yield Button(label=license, id="license")

        with Horizontal(classes="home-links"):
            yield Link(" Wiki", url="https://linux.toys/knowledgebase.html")
            yield Link(f" {credits}", url="https://linux.toys/credits.html")
            yield Link(f" {suport}", url="https://ko-fi.com/psygreg")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#contributors-list").mount(
            Static("Carregando colaboradores....")
        )
        self._load_contributors()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "license":
            self.app.push_screen(LicenseScreen())

    def _get_compat_display_string(self):
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
            container.mount(Static("Nenhum colaborador encontrado."))
            return
        for c in contributors:
            container.mount(Static(c.get("login", "?")))

    def _show_contributors_error(self, exc: Exception) -> None:
        container = self.query_one("#contributors-list")
        container.remove_children()
        container.mount(
            Static(f"Não foi possível carregar colaboradores: {exc}")
        )


class LicenseScreen(ModalScreen[bool]):
    BINDINGS = [
        ("escape", "app.pop_screen", "Voltar"),
        ("w", "app.pop_screen", "Voltar"),
    ]

    def get_license_text(self):
        license_path = get_app_resource_path("../LICENSE")
        try:
            with open(license_path, "r", encoding="utf-8") as f:
                license_text = f.read()
            return license_text
        except Exception as e:
            print(f"Error loading license. {e}")

    def compose(self) -> ComposeResult:
        license_text = self.get_license_text()
        yield Header()
        with Horizontal(id="home"):
            with VerticalScroll(id="hello"):
                yield Static(content=license_text, classes="hello")
        yield Footer()
