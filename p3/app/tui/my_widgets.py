from functools import lru_cache

from rich_pixels import Pixels
from textual.widgets import Button, Label, ListItem


@lru_cache(maxsize=1)
def get_sidebar_logo():
    """Gera o Pixels do logo uma única vez (lru_cache guarda o resultado);
    chamadas seguintes reaproveitam o mesmo objeto, sem reler o arquivo."""
    return Pixels.from_image_path(
        "app/icons/linuxtoys_tui.png", resize=(40, 40)
    )


class DescButton(Button):
    def __init__(
        self,
        label: str,
        description: str,
        path: str,
        is_script: bool,
        **kwargs,
    ) -> None:
        super().__init__(label, tooltip=description, **kwargs)
        self.description = description
        self.path = path
        self.is_script = is_script


class HistoryListItem(ListItem):
    def __init__(self, script_name: str) -> None:
        super().__init__(Label(script_name))
        self.script_name = script_name
