from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table


def _open(output_file: str | None):
    if output_file is None:
        return sys.stdout, False
    return open(output_file, "w"), True


class Writer:
    def __init__(self, output_file: str | None, fmt: str):
        self.fmt = fmt
        self._f, self._owned = _open(output_file)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self._owned:
            self._f.close()

    def _write(self, text: str):
        print(text, file=self._f)

    def table(
        self,
        headers: list[str],
        rows: list[list[str]],
        empty_msg: str = "(no results)",
        footer: list[str] | None = None,
    ):
        if not rows:
            self._write(empty_msg)
            return
        t = Table(show_header=True, header_style="bold cyan", show_lines=True, show_footer=footer is not None)
        for i, h in enumerate(headers):
            t.add_column(h, footer=("[bold]" + footer[i] + "[/bold]") if footer else "")
        for row in rows:
            t.add_row(*row)
        console = Console(file=self._f)
        console.print(t)

    def json(self, data: Any):
        self._write(json.dumps(data, indent=2, default=str, ensure_ascii=False))

    def prometheus(self, lines: list[str]):
        for line in lines:
            self._write(line)

    def text(self, msg: str):
        self._write(msg)
