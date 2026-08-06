from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageGrab


CLIPBOARD_ROOT = Path(tempfile.gettempdir()) / "GitDrop"
CLIPBOARD_ROOT.mkdir(parents=True, exist_ok=True)
CLIPBOARD_DIR = Path(tempfile.mkdtemp(prefix="clipboard-", dir=CLIPBOARD_ROOT))


class ClipboardError(RuntimeError):
    pass


def save_clipboard_image(image: Image.Image) -> Path:
    CLIPBOARD_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = CLIPBOARD_DIR / f"clipboard-{timestamp}.png"
    image.save(path, format="PNG")
    return path


def _macos_file_paths() -> list[Path]:
    script = r'''
ObjC.import('AppKit');
const board = $.NSPasteboard.generalPasteboard;
const classes = $.NSArray.arrayWithObject($.NSURL);
const options = $.NSDictionary.dictionaryWithObjectForKey(
    true, $.NSPasteboardURLReadingFileURLsOnlyKey
);
const urls = board.readObjectsForClassesOptions(classes, options);
const paths = [];
for (let index = 0; index < urls.count; index++) {
    paths.push(ObjC.unwrap(urls.objectAtIndex(index).path));
}
paths.join('\n');
'''
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            text=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [Path(line) for line in result.stdout.splitlines() if Path(line).exists()]


def paste_paths() -> list[Path]:
    try:
        content = ImageGrab.grabclipboard()
    except (OSError, RuntimeError) as exc:
        raise ClipboardError(f"无法读取剪贴板：{exc}") from exc

    if isinstance(content, Image.Image):
        try:
            return [save_clipboard_image(content)]
        finally:
            content.close()
    if isinstance(content, list):
        return [Path(value) for value in content if Path(value).exists()]
    if sys.platform == "darwin":
        return _macos_file_paths()
    return []


def remove_clipboard_file(path: Path) -> None:
    try:
        if path.resolve().is_relative_to(CLIPBOARD_DIR.resolve()):
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def cleanup_clipboard_images() -> None:
    shutil.rmtree(CLIPBOARD_DIR, ignore_errors=True)
