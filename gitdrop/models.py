from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UploadItem:
    source: Path
    relative_path: str

    @property
    def size(self) -> int:
        return self.source.stat().st_size


def collect_paths(paths: list[Path]) -> list[UploadItem]:
    """Expand selected files and folders while preserving folder names."""
    result: list[UploadItem] = []
    seen: set[tuple[Path, str]] = set()

    for selected in paths:
        selected = selected.resolve()
        if selected.is_file():
            item = UploadItem(selected, selected.name)
            key = (item.source, item.relative_path)
            if key not in seen:
                result.append(item)
                seen.add(key)
            continue

        if selected.is_dir():
            for source in sorted(p for p in selected.rglob("*") if p.is_file()):
                relative = (Path(selected.name) / source.relative_to(selected)).as_posix()
                item = UploadItem(source, relative)
                key = (item.source, item.relative_path)
                if key not in seen:
                    result.append(item)
                    seen.add(key)

    return result


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"

