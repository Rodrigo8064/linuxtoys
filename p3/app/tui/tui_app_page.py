from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import (
    Button,
    Label,
    Rule,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

__all__ = ["AppPageWidget", "FeaturedCard", "show_app_page", "hide_app_page"]

# IDs da homescreen existente
MENU_PANEL = "#menu-panel"
BODY = "#body-home"
LIST_VIEW = "#home-menu"
LOGO = "#logo_lt"
TERMINAL = "#terminal-conteiner"

# Geometria do "Try These" (GUI: 280px mín. por card, até 5 colunas)
MIN_CARD_WIDTH = 30
CARD_HEIGHT = 5
MAX_COLUMNS = 5

_BLANK = Select.NULL


# --------------------------------------------------------------------------- #
# Helpers puros
# --------------------------------------------------------------------------- #
def _norm_lang(value: Any) -> str:
    value = str(value or "").strip().replace("_", "-")
    return value.split("-", 1)[0].split(".", 1)[0].casefold() if value else ""


_SPAN_STYLES = {
    "bold": "bold",
    "strong": "bold",
    "italic": "italic",
    "emphasis": "italic",
    "em": "italic",
    "code": "#e5c07b on #2a2a2a",
    "underline": "underline",
}


def _spans_to_text(spans: Sequence[Mapping[str, Any]] | str) -> Text:
    """Converte spans do AppStream ({'text', 'styles', 'url'}) em rich.Text."""
    if isinstance(spans, str):
        return Text(spans)
    out = Text()
    for span in spans or ():
        styles = [
            _SPAN_STYLES[s]
            for s in (span.get("styles") or ())
            if s in _SPAN_STYLES
        ]
        url = span.get("url")
        if url:
            styles.append(f"underline blue link {url}")
        out.append(str(span.get("text", "")), style=" ".join(styles) or None)
    return out


def blocks_to_text(blocks: Any) -> Text:
    """Renderiza blocos (paragraph / unordered_list / ordered_list) ou str."""
    if isinstance(blocks, str):
        return Text(blocks)
    out = Text()
    for block in blocks or ():
        if out.plain:
            out.append("\n\n" if block.get("type") == "paragraph" else "\n")
        kind = block.get("type")
        if kind == "paragraph":
            out.append_text(_spans_to_text(block.get("spans") or ()))
        elif kind in ("unordered_list", "ordered_list"):
            for number, item in enumerate(block.get("items") or (), start=1):
                if number > 1:
                    out.append("\n")
                out.append(
                    "  • " if kind == "unordered_list" else f"  {number}. "
                )
                out.append_text(_spans_to_text(item))
    return out


def _script_key(info: Mapping[str, Any]) -> str:
    return str(
        info.get("id") or info.get("appstream_id") or info.get("name") or ""
    )


def _fmt_price(option: Mapping[str, Any]) -> str:
    return (
        f"{option.get('currency_symbol') or '$'}{float(option['price']):.2f}"
    )


# --------------------------------------------------------------------------- #
# Card do "Try These"
# --------------------------------------------------------------------------- #
class FeaturedCard(Static, can_focus=True):
    BINDINGS = [Binding("enter", "select", "Open", show=False)]

    class Selected(Message):
        def __init__(self, script_info: Mapping[str, Any]) -> None:
            super().__init__()
            self.script_info = script_info

    def __init__(self, script_info: Mapping[str, Any]) -> None:
        desc = str(script_info.get("description") or "").strip()
        text = Text()
        text.append(str(script_info.get("name", "")), style="bold")
        if desc:
            text.append("\n" + desc, style="dim")
        super().__init__(text)
        self.script_info = script_info
        if desc:
            self.tooltip = desc

    def on_click(self) -> None:
        self.action_select()

    def action_select(self) -> None:
        self.post_message(self.Selected(self.script_info))


# --------------------------------------------------------------------------- #
# Página
# --------------------------------------------------------------------------- #
class AppPageWidget(Vertical):
    """Página de app. Recebe tudo que precisa no construtor."""

    # ---- mensagens para o host ------------------------------------------- #
    class BackRequested(Message):
        pass

    class InstallRequested(Message):
        def __init__(self, script_info: Mapping[str, Any]) -> None:
            super().__init__()
            self.script_info = script_info

    class OpenRequested(InstallRequested):
        pass

    class RevertRequested(InstallRequested):
        pass

    class RateRequested(Message):
        def __init__(self, stars: int, script_info: Mapping[str, Any]) -> None:
            super().__init__()
            self.stars = stars
            self.script_info = script_info

    class UrlRequested(Message):
        def __init__(self, url: str) -> None:
            super().__init__()
            self.url = url

    class TranslateRequested(Message):
        def __init__(self, blocks: Any, target_language: str) -> None:
            super().__init__()
            self.blocks = blocks
            self.target_language = target_language

    class SourceChanged(Message):
        def __init__(self, script_info: Mapping[str, Any]) -> None:
            super().__init__()
            self.script_info = script_info

    class FeaturedSelected(Message):
        def __init__(self, script_info: Mapping[str, Any]) -> None:
            super().__init__()
            self.script_info = script_info

    class UninstallRequested(InstallRequested):
        pass

    # ---- construção ------------------------------------------------------- #
    def __init__(
        self,
        script_info: Mapping[str, Any],
        *,
        translations: Mapping[str, str] | None = None,
        featured: Sequence[Mapping[str, Any]] | None = None,
        installed: bool = False,
        system_language: str | None = None,
        id: str | None = "app-page",
        **kwargs: Any,
    ) -> None:
        super().__init__(id=id, **kwargs)
        self.script_info = script_info
        self.translations = translations or {}
        self._installed = installed
        self._system_language = _norm_lang(
            system_language
            if system_language is not None
            else os.environ.get("LANG", "")
        )

        self._source_options = [
            o
            for o in (script_info.get("source_options") or ())
            if isinstance(o, Mapping)
        ]
        self._selected: Mapping[str, Any] = script_info
        if self._source_options:
            recommended = self._recommended_source()
            self._selected = next(
                (
                    o
                    for o in self._source_options
                    if self._source_key(o) == recommended
                ),
                self._source_options[0],
            )

        # Descrição (original / traduzida)
        self._blocks = (
            script_info.get("long_description_blocks")
            or script_info.get("long_description")
            or script_info.get("description")
            or ""
        )
        self._translated_blocks: Any = None
        self._showing_translation = False

        # Avaliação do usuário (None = ainda não avaliou)
        self._user_rating: int | None = script_info.get("user_rating")

        # Featured: mesma categoria primeiro, nunca o app atual
        current = _script_key(script_info)
        category = script_info.get("category")
        cands = [f for f in (featured or ()) if _script_key(f) != current]
        cands.sort(
            key=lambda f: (
                0 if category and f.get("category") == category else 1
            )
        )
        self._featured = cands
        self._featured_signature: tuple | None = None
        self._featured_timer = None
        self._purchase_options: dict[str, list] = {}
        self._previous_border_title: Any = None

    def _t(self, key: str, default: str) -> str:
        return self.translations.get(key, default)

    # ---- fontes ----------------------------------------------------------- #
    def _recommended_source(self) -> str:
        return str(
            self.script_info.get("recommended_source")
            or self.script_info.get("appstream_source")
            or ""
        )

    @staticmethod
    def _source_key(entry: Mapping[str, Any]) -> str:
        source = str(entry.get("appstream_source", "") or "").strip()
        if source != "flatpak":
            return source
        return "flatpak:{}:{}".format(
            str(entry.get("flatpak_scope", "") or "").strip(),
            str(entry.get("flatpak_installation", "") or "").strip(),
        )

    def _has_multiple_flatpak_scopes(self) -> bool:
        scopes = {
            str(o.get("flatpak_scope", "") or "").strip()
            for o in self._source_options
            if str(o.get("appstream_source", "") or "").strip() == "flatpak"
        }
        scopes.discard("")
        return len(scopes) > 1

    def _source_label(self, entry: Mapping[str, Any]) -> str:
        source = str(entry.get("appstream_source", "") or "").strip()
        if source == "flatpak":
            scope = str(entry.get("flatpak_scope", "") or "").strip()
            if scope == "system" and self._has_multiple_flatpak_scopes():
                return (
                    f"Flathub ({self._t('app_page_source_system', 'system')})"
                )
            return "Flathub"
        if source == "native":
            return self._t("app_page_source_native", "Native")
        if source == "snap":
            return "Snap"
        if source == "aur":
            return "AUR"
        return source.capitalize() or self._t(
            "app_page_source_native", "Native"
        )

    def _is_snap(self) -> bool:
        return (
            str(self._selected.get("appstream_source", "") or "").strip()
            == "snap"
        )

    # ---- textos do cabeçalho ---------------------------------------------- #
    def _name_text(self) -> Text:
        text = Text()
        text.append(str(self.script_info.get("name", "") or ""), style="bold")
        license_name = str(self.script_info.get("license", "") or "").strip()
        if license_name:
            text.append(" · " + license_name, style="dim")
        return text

    def _developer_text(self) -> Text | None:
        info = self.script_info
        developer = str(info.get("developer", "") or "").strip()
        if not developer:
            return None
        text = Text()
        if info.get("is_official") or info.get("is_verified"):
            text.append("✔ ", style="bold green")
        elif info.get("is_repo_entry") or info.get("is_script"):
            text.append("◆ ", style="bold cyan")
        elif info.get("is_appstream_entry"):
            text.append("◈ ", style="dim")
        text.append(developer)
        return text

    def _meta_text(self) -> Text | None:
        info = self.script_info
        text = Text()
        repo = str(info.get("repo", "") or "").strip()
        if repo:
            text.append(repo, style="dim")
        if info.get("is_appstream_entry"):
            try:
                rating = float(info.get("review_rating"))
                count = int(info.get("review_count"))
            except (TypeError, ValueError):
                rating, count = -1.0, 0
            if count > 0 and 0.0 <= rating <= 100.0:
                if text.plain:
                    text.append("   ")
                text.append(f"★ {rating / 20.0:.1f}", style="bold yellow")
                text.append(f" ({count})")
        return text if text.plain else None

    # ---- comércio --------------------------------------------------------- #
    def _commerce_label(
        self, kind: str, options: Sequence[Mapping[str, Any]]
    ) -> str:
        if kind == "purchase":
            plain_k, plain_d = "app_page_purchase", "Purchase"
            price_k, price_d = "app_page_purchase_price", "Purchase · ${price}"
        else:
            plain_k, plain_d = "app_page_subscribe", "Subscribe"
            price_k, price_d = (
                "app_page_subscribe_price",
                "Subscribe · ${price}",
            )
        if not options:
            return self._t(plain_k, plain_d).strip()
        lowest = min(options, key=lambda o: o["price"])
        price_text = _fmt_price(lowest)
        if len(options) > 1:
            price_text = self._t(
                "app_page_from_price", "from ${price}"
            ).replace("${price}", price_text)
        template = self._t(price_k, price_d)
        return template.replace("${price}", price_text).strip()

    def _option_label(self, kind: str, option: Mapping[str, Any]) -> str:
        pieces = []
        name = str(option.get("name") or "").strip()
        if name:
            pieces.append(name)
        months = option.get("months")
        if kind == "subscription" and isinstance(months, int) and months > 0:
            unit = (
                self._t("app_page_month", "month")
                if months == 1
                else self._t("app_page_months", "months")
            )
            pieces.append(f"{months} {unit}")
        pieces.append(_fmt_price(option))
        return " · ".join(pieces)

    def _commerce_widget(
        self, kind: str, options: list, fallback_url: str
    ) -> Widget:
        label = self._commerce_label(kind, options)
        if len(options) > 1:
            self._purchase_options[kind] = options
            return Select(
                [
                    (self._option_label(kind, o), i)
                    for i, o in enumerate(options)
                ],
                prompt=label,
                allow_blank=True,
                id=f"app-{kind}-select",
                classes="act-select",
            )
        url = options[0]["url"] if options else fallback_url
        button = Button(f"🛒 {label}", variant="primary", id=f"app-{kind}")
        button.url = url  # type: ignore[attr-defined]
        return button

    # ---- compose ---------------------------------------------------------- #
    def compose(self) -> ComposeResult:
        info = self.script_info

        with Horizontal(id="app-top"):
            yield Button("←", id="app-back", tooltip=self._t("back", "Back"))
            yield Static(self._name_text(), id="app-name")

        developer = self._developer_text()
        if developer is not None:
            yield Static(developer, id="app-developer")

        description = self._description_text()
        if description is not None:
            yield Static(description, id="app-description-short")
        meta = self._meta_text()
        if meta is not None:
            yield Static(meta, id="app-meta")

        yield Horizontal(
            *self._primary_actions(),
            *self._link_actions(),
            classes="app-row",
            id="app-actions",
        )
        yield self._rating_row()

        content = self._build_content()
        extensions = list(info.get("extensions") or ())
        if extensions:
            with TabbedContent(id="app-tabs"):
                with TabPane(
                    self._t("app_page_details", "Details"), id="tab-details"
                ):
                    yield content
                with TabPane(
                    self._t("app_page_extensions", "Extensions"),
                    id="tab-extensions",
                ):
                    yield self._build_extensions(extensions)
        else:
            yield content

    def _primary_actions(self) -> list[Widget]:
        install = Button(
            "⬇ " + self._t("skills_install_label", "Install").strip(),
            variant="success",
            id="app-install",
        )
        install.display = not self._installed
        remove = Button(
            "🗑 " + self._t("app_page_remove", "Remover").strip(),
            variant="error",
            id="app-remove",
        )
        remove.display = self._installed
        revert = Button(
            "↺ " + self._t("app_page_snap_revert", "Revert").strip(),
            id="app-revert",
        )
        revert.display = self._installed and self._has_snap_source()
        widgets: list[Widget] = [install, remove, revert]

        if len(self._source_options) >= 2:
            recommended = self._recommended_source()
            options = []
            for i, entry in enumerate(self._source_options):
                mark = "◆ " if self._source_key(entry) == recommended else ""
                options.append((mark + self._source_label(entry), i))
            current = self._source_options.index(self._selected)
            widgets.append(
                Select(
                    options,
                    value=current,
                    allow_blank=False,
                    id="app-source",
                    classes="act-select",
                )
            )
        return widgets

    def _link_actions(self) -> list[Widget]:
        info = self.script_info
        purchase_url = info.get("purchase_url") or ""
        purchase = list(info.get("purchase_options") or [])
        subscription = list(info.get("subscription_options") or [])
        if (
            not purchase
            and purchase_url
            and info.get("purchase_price") is not None
        ):
            purchase = [
                {
                    "name": "",
                    "price": info["purchase_price"],
                    "url": purchase_url,
                    "currency_symbol": info.get("purchase_currency_symbol")
                    or "$",
                }
            ]
        if (
            not subscription
            and purchase_url
            and info.get("subscription_price") is not None
        ):
            subscription = [
                {
                    "name": "",
                    "months": 1,
                    "price": info["subscription_price"],
                    "url": purchase_url,
                    "currency_symbol": info.get("subscription_currency_symbol")
                    or "$",
                }
            ]

        widgets: list[Widget] = []
        if purchase:
            widgets.append(
                self._commerce_widget("purchase", purchase, purchase_url)
            )
        if subscription:
            widgets.append(
                self._commerce_widget(
                    "subscription", subscription, purchase_url
                )
            )
        if purchase_url and not purchase and not subscription:
            widgets.append(self._commerce_widget("purchase", [], purchase_url))

        homepage = info.get("homepage_url") or ""
        if homepage:
            b = Button(
                "🌐 " + self._t("app_page_homepage", "Website").strip(),
                id="app-homepage",
            )
            b.url = homepage  # type: ignore[attr-defined]
            widgets.append(b)
        donate = info.get("donate_url") or ""
        if donate:
            only = not purchase_url and not purchase and not subscription
            b = Button(
                "♥ " + self._t("app_page_donate", "Donate").strip(),
                variant="primary" if only else "default",
                id="app-donate",
            )
            b.url = donate  # type: ignore[attr-defined]
            widgets.append(b)
        return widgets

    def _rating_row(self) -> Horizontal:
        row = Horizontal(
            Label(self._t("app_page_rate", "Rate:")),
            *[Button("☆", id=f"app-rate-{i}") for i in range(1, 6)],
            id="app-rating",
            classes="app-row",
        )
        row.display = False
        return row

    def _description_text(self) -> str | None:
        description = str(self.script_info.get("description") or "").strip()
        return description or None

    # ---- corpo ------------------------------------------------------------ #
    def _build_content(self) -> VerticalScroll:
        children: list[Widget] = []

        desc_parts: list[Widget] = []
        if self._needs_translation():
            btn = Button(
                "文/A",
                id="app-translate",
                tooltip=self._t("app_page_translate", "Translate"),
            )
            desc_parts.append(Horizontal(btn, id="app-translate-row"))
        desc_parts.append(
            Static(blocks_to_text(self._blocks), id="app-description-text")
        )
        children.append(Vertical(*desc_parts, id="app-description"))

        if self._featured:
            children.append(
                Vertical(
                    Rule(),
                    Label(
                        self._t("featured_scripts", "Try These"),
                        id="app-featured-title",
                    ),
                    Grid(id="featured-grid"),
                    id="app-featured",
                )
            )
            children[-1].display = False
        return VerticalScroll(*children, id="app-content")

    def _build_extensions(self, extensions: Sequence[Any]) -> VerticalScroll:
        items = []
        for ext in extensions:
            if isinstance(ext, Mapping):
                text = Text(
                    str(ext.get("name") or ext.get("id") or ""), style="bold"
                )
                summary = str(
                    ext.get("summary") or ext.get("description") or ""
                ).strip()
                if summary:
                    text.append("\n" + summary, style="dim")
            else:
                text = Text(str(ext), style="bold")
            items.append(Static(text, classes="ext-item"))
        return VerticalScroll(*items, id="app-extensions")

    def _needs_translation(self) -> bool:
        info = self.script_info
        if info.get("appstream_source") != "flatpak":
            return bool(info.get("is_appstream_entry", True)) and bool(
                self._blocks
            )
        source = _norm_lang(info.get("long_description_locale"))
        return bool(
            source
            and self._system_language
            and source != self._system_language
        )

    # ---- ciclo de vida ---------------------------------------------------- #
    def on_mount(self) -> None:
        self._refresh_rating()
        self.call_after_refresh(self._schedule_featured)
        self.call_after_refresh(self._focus_first)

    def _focus_first(self) -> None:
        try:
            self.query_one(
                "#app-install" if not self._installed else "#app-open"
            ).focus()
        except Exception:
            pass

    def on_resize(self) -> None:
        self._schedule_featured()

    @on(TabbedContent.TabActivated)
    def _tab_changed(self) -> None:
        self._featured_signature = None
        self._schedule_featured()

    # ---- API pública (host atualiza estado) ------------------------------ #
    def set_install_state(self, installed: bool) -> None:
        self._installed = installed
        self.query_one("#app-install").display = not installed
        self.query_one("#app-remove").display = installed
        self._refresh_rating()

    def set_snap_revert_visible(self, visible: bool) -> None:
        self.query_one("#app-revert").display = visible

    def set_user_rating(self, stars: int | None) -> None:
        self._user_rating = stars
        self._refresh_rating()

    def set_translated_blocks(self, blocks: Any) -> None:
        """Host chama com a tradução pronta (ou None em caso de falha)."""
        btn = self.query_one("#app-translate", Button)
        btn.disabled = False
        btn.label = "文/A"
        if blocks is None:
            btn.tooltip = self._t(
                "app_page_translate_failed", "Translation failed. Try again."
            )
            return
        self._translated_blocks = blocks
        self._show_description(translated=True)

    # ---- internos --------------------------------------------------------- #
    def _show_description(self, *, translated: bool) -> None:
        blocks = self._translated_blocks if translated else self._blocks
        self.query_one("#app-description-text", Static).update(
            blocks_to_text(blocks)
        )
        self._showing_translation = translated
        btn = self.query_one("#app-translate", Button)
        btn.tooltip = (
            self._t("app_page_show_original", "Show original")
            if translated
            else self._t("app_page_translate", "Translate")
        )
        self._featured_signature = None
        self.call_after_refresh(self._schedule_featured)

    def _refresh_rating(self) -> None:
        try:
            row = self.query_one("#app-rating")
        except Exception:
            return
        row.display = self._installed
        rated = self._user_rating is not None
        snap = self._is_snap()
        tooltip = self._t("app_page_rate_stars", "Rate {stars} stars")
        for i in range(1, 6):
            btn = self.query_one(f"#app-rate-{i}", Button)
            if snap:
                btn.display = i in (1, 5)
                btn.label = "👎" if i == 1 else "👍"
                btn.tooltip = "Do not recommend" if i == 1 else "Recommend"
            else:
                btn.display = True
                btn.label = (
                    "★" if rated and i <= (self._user_rating or 0) else "☆"
                )
                btn.tooltip = tooltip.format(stars=i)
            btn.disabled = not self._installed or rated
        if rated:
            row.tooltip = self._t(
                "app_page_rate_done", "You have already rated this app."
            )
        else:
            row.tooltip = None

    def _has_snap_source(self) -> bool:
        info = self.script_info
        if str(info.get("snap_name") or "").strip():
            return True
        for option in info.get("source_options") or ():
            if str(option.get("snap_name") or "").strip():
                return True
        return False

    # ---- featured (preenche só o espaço sobrando, como na GUI) ------------ #
    def _schedule_featured(self) -> None:
        if not self._featured or not self.is_mounted:
            return
        if self._featured_timer is not None:
            self._featured_timer.stop()
        self._featured_timer = self.set_timer(0.12, self._refresh_featured)

    def _refresh_featured(self) -> None:
        self._featured_timer = None
        try:
            box = self.query_one("#app-featured")
            scroller = self.query_one("#app-content", VerticalScroll)
            grid = self.query_one("#featured-grid", Grid)
        except Exception:
            return
        viewport_h = scroller.size.height
        width = scroller.size.width - (
            2 if scroller.show_vertical_scrollbar else 0
        )
        if viewport_h <= 1 or width <= 1:
            return

        used = 0
        for child in scroller.children:
            if child is not box:
                used += child.outer_size.height + child.styles.margin.height
        # Rule(1+margens) + título(1+2) = overhead fixo do bloco
        spare = viewport_h - used - 6
        rows = spare // CARD_HEIGHT
        if rows < 1:
            box.display = False
            self._featured_signature = None
            return

        gap = 1
        columns = max(
            1, min(MAX_COLUMNS, (width + gap) // (MIN_CARD_WIDTH + gap))
        )
        count = min(len(self._featured), rows * columns)
        signature = (columns, rows, count)
        if signature != self._featured_signature:
            self._featured_signature = signature
            grid.styles.grid_size_columns = columns
            grid.styles.grid_rows = str(CARD_HEIGHT)
            grid.styles.height = (
                (count + columns - 1) // columns
            ) * CARD_HEIGHT
            grid.remove_children()
            grid.mount(
                *[FeaturedCard(info) for info in self._featured[:count]]
            )
        box.display = True

    # ---- handlers --------------------------------------------------------- #
    def action_back(self) -> None:
        self.post_message(self.BackRequested())

    @on(FeaturedCard.Selected)
    def _featured_selected(self, event: FeaturedCard.Selected) -> None:
        event.stop()
        self.post_message(self.FeaturedSelected(event.script_info))

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        if bid == "app-back":
            self.post_message(self.BackRequested())
        elif bid == "app-install":
            event.button.disabled = True
            event.button.label = "..."
            self.post_message(self.InstallRequested(self._selected))
        elif bid == "app-open":
            self.post_message(self.OpenRequested(self._selected))
        elif bid == "app-remove":
            event.button.disabled = True
            event.button.label = "..."
            self.post_message(self.UninstallRequested(self._selected))
        elif bid == "app-revert":
            event.button.disabled = True
            event.button.label = "..."
            self.post_message(self.RevertRequested(self._selected))
        elif bid.startswith("app-rate-"):
            self.post_message(
                self.RateRequested(int(bid.rsplit("-", 1)[1]), self._selected)
            )
        elif bid in (
            "app-purchase",
            "app-subscription",
            "app-homepage",
            "app-donate",
        ):
            url = getattr(event.button, "url", "")
            if url:
                self.post_message(self.UrlRequested(url))
        elif bid == "app-translate":
            self._on_translate_pressed(event.button)

    def reset_install_button(self) -> None:
        try:
            button = self.query_one("#app-install", Button)
        except NoMatches:
            return
        button.disabled = False
        button.label = (
            "⬇ " + self._t("skills_install_label", "Install").strip()
        )

    def reset_remove_button(self) -> None:
        """Reabilita e restaura o label do botão Remover, usado quando a
        remoção falha ou é cancelada."""
        try:
            button = self.query_one("#app-remove", Button)
        except NoMatches:
            return
        button.disabled = False
        button.label = "🗑 " + self._t("app_page_remove", "Remover").strip()

    def reset_revert_button(self) -> None:
        try:
            button = self.query_one("#app-revert", Button)
        except NoMatches:
            return
        button.disabled = False
        button.label = "↺ " + self._t("app_page_snap_revert", "Revert").strip()

    def _on_translate_pressed(self, button: Button) -> None:
        if self._showing_translation:
            self._show_description(translated=False)
        elif self._translated_blocks is not None:
            self._show_description(translated=True)
        else:
            button.disabled = True
            button.label = "…"
            self.post_message(
                self.TranslateRequested(self._blocks, self._system_language)
            )

    @on(Select.Changed)
    def _select_changed(self, event: Select.Changed) -> None:
        event.stop()
        sid = event.select.id or ""
        if sid == "app-source":
            if event.value is _BLANK:
                return
            self._selected = self._source_options[int(event.value)]
            self._refresh_rating()
            self._featured_signature = None
            self._schedule_featured()
            self.post_message(self.SourceChanged(self._selected))
        elif sid in ("app-purchase-select", "app-subscription-select"):
            if event.value is _BLANK:
                return
            kind = (
                "purchase" if sid == "app-purchase-select" else "subscription"
            )
            url = self._purchase_options[kind][int(event.value)]["url"]
            event.select.clear()
            self.post_message(self.UrlRequested(url))


# --------------------------------------------------------------------------- #
# Troca de painel na homepage (mesmo padrão do _toggle_about_panel)
# --------------------------------------------------------------------------- #
async def show_app_page(
    host: Widget,
    script_info: Mapping[str, Any],
    *,
    translations: Mapping[str, str] | None = None,
    featured: Sequence[Mapping[str, Any]] | None = None,
    installed: bool = False,
) -> AppPageWidget:
    """Esconde logo/menu/terminal do #menu-panel e monta a app page no lugar."""
    panel = host.query_one(MENU_PANEL)

    # Fecha o painel "Sobre" e o terminal, se abertos (igual ao _toggle_about_panel)
    about_open = getattr(host, "_is_about_showing", False)
    if about_open and hasattr(host, "_toggle_about_panel"):
        host._toggle_about_panel(force_hide=True)
    if host.query_one(TERMINAL).display and hasattr(host, "_hide_terminal"):
        host._hide_terminal()

    # Título original da borda (preservado de uma página anterior, se houver)
    previous_title = "Menu" if about_open else panel.border_title
    for old in list(host.query(AppPageWidget)):
        previous_title = old._previous_border_title
        await old.remove()

    page = AppPageWidget(
        script_info,
        translations=translations,
        featured=featured,
        installed=installed,
    )
    page._previous_border_title = previous_title

    host.query_one(LOGO).display = False
    host.query_one(LIST_VIEW).display = False
    host.query_one(TERMINAL).display = False
    panel.border_title = str(script_info.get("name") or "")

    await panel.mount(page)
    page.display = True
    return page


async def hide_app_page(host: Widget) -> None:
    """Remove a app page e restaura logo e menu."""
    panel = host.query_one(MENU_PANEL)
    for page in list(host.query(AppPageWidget)):
        panel.border_title = page._previous_border_title
        await page.remove()
    host.query_one(LOGO).display = True
    host.query_one(LIST_VIEW).display = True
    try:
        host.query_one(LIST_VIEW).focus()
    except Exception:
        pass
