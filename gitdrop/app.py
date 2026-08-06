from __future__ import annotations

import queue
import platform
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

from .clipboard import (
    ClipboardError,
    cleanup_clipboard_images,
    paste_paths,
    remove_clipboard_file,
)
from .config import AppConfig, load_config, load_token, save_config, save_token
from .git_sync import GitSyncError, GitSyncResult, LocalGitTransport
from .github import GitHubClient, GitHubError
from .models import UploadItem, collect_paths, human_size


COLORS = {
    "bg": "#f6f7f9",
    "panel": "#ffffff",
    "text": "#1d2025",
    "muted": "#68707c",
    "border": "#d8dde3",
    "primary": "#1769d2",
    "primary_hover": "#125bb8",
}
UI_FONT = "SF Pro Text" if platform.system() == "Darwin" else "Segoe UI"
PROJECT_URL = "https://github.com/jiale-wangOwO/gitdrop"


class GitDropApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config = load_config()
        self.items: list[UploadItem] = []
        self.repository_url = ""
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()

        root.title("GitDrop")
        root.geometry("860x760")
        root.minsize(720, 650)
        root.configure(bg=COLORS["bg"])
        self._configure_styles()
        self._build_ui()
        self._load_values()
        root.bind_all("<Control-v>", self._paste_clipboard_content, add="+")
        root.bind_all("<Command-v>", self._paste_clipboard_content, add="+")
        root.protocol("WM_DELETE_WINDOW", self._close)
        if not self.config.owner or not self.token_var.get():
            root.after(0, self._show_settings)
        else:
            root.after(50, self._start_repository_check)
        root.after(100, self._poll_events)

    def _configure_styles(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkFixedFont"):
            tkfont.nametofont(name).configure(family=UI_FONT, size=10)
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("App.TFrame", background=COLORS["bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("Title.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=(UI_FONT, 20, "bold"))
        style.configure("Subtitle.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=(UI_FONT, 10))
        style.configure("Section.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=(UI_FONT, 10, "bold"))
        style.configure("Panel.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=(UI_FONT, 10))
        style.configure("Muted.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=(UI_FONT, 10))
        style.configure("Status.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=(UI_FONT, 10))
        style.configure("TButton", font=(UI_FONT, 10), padding=(12, 7))
        style.map("TButton", background=[("active", "#eef1f4")])
        style.configure("Primary.TButton", font=(UI_FONT, 10, "bold"), padding=(18, 9))
        style.map("Primary.TButton", background=[("active", COLORS["primary_hover"]), ("disabled", "#a9b6c5")])
        style.configure("Link.TButton", foreground=COLORS["primary"], background=COLORS["bg"], borderwidth=0)
        style.configure("TEntry", padding=8, fieldbackground="white", bordercolor=COLORS["border"])

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, style="App.TFrame", padding=(30, 24, 30, 22))
        container.pack(fill="both", expand=True)
        header = ttk.Frame(container, style="App.TFrame")
        header.pack(fill="x", pady=(0, 16))
        titles = ttk.Frame(header, style="App.TFrame")
        titles.pack(side="left")
        ttk.Label(titles, text="GitDrop", style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="把消息和本地内容直接送到你的 GitHub 仓库", style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Button(header, text="仓库设置", command=self._show_settings).pack(side="right", pady=8)

        self.content = ttk.Frame(container, style="App.TFrame")
        self.content.pack(fill="both", expand=True)
        self.send_page = self._build_send_page(self.content)
        self.settings_page = self._build_settings_page(self.content)
        self.send_page.pack(fill="both", expand=True)

    def _panel(self, parent: tk.Widget) -> ttk.Frame:
        outer = tk.Frame(parent, bg=COLORS["border"], padx=1, pady=1)
        panel = ttk.Frame(outer, style="Panel.TFrame", padding=(20, 17))
        panel.pack(fill="both", expand=True)
        panel.outer = outer  # type: ignore[attr-defined]
        return panel

    def _build_send_page(self, parent: tk.Widget) -> ttk.Frame:
        page = ttk.Frame(parent, style="App.TFrame")
        message_panel = self._panel(page)
        ttk.Label(message_panel, text="消息", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        self.message_edit = tk.Text(message_panel, height=5, wrap="word", undo=True, font=(UI_FONT, 10), relief="solid", borderwidth=1, highlightthickness=0, padx=10, pady=9)
        self.message_edit.pack(fill="x")
        message_panel.outer.pack(fill="x", pady=(0, 13))  # type: ignore[attr-defined]

        files_panel = self._panel(page)
        header = ttk.Frame(files_panel, style="Panel.TFrame")
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="附件", style="Section.TLabel").pack(side="left")
        ttk.Button(header, text="清空", command=self._clear_items).pack(side="right", padx=(6, 0))
        ttk.Button(header, text="选择文件夹", command=self._choose_folder).pack(side="right", padx=(6, 0))
        ttk.Button(header, text="选择文件", command=self._choose_files).pack(side="right")
        list_frame = ttk.Frame(files_panel, style="Panel.TFrame")
        list_frame.pack(fill="both", expand=True)
        self.file_list = tk.Listbox(list_frame, selectmode="extended", activestyle="none", font=(UI_FONT, 10), relief="solid", borderwidth=1, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_list.yview)
        self.file_list.configure(yscrollcommand=scrollbar.set)
        self.file_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.file_list.bind("<Double-Button-1>", self._remove_selected)
        self.file_list.bind("<Delete>", self._remove_selected)
        self.file_list.drop_target_register(DND_FILES)
        self.file_list.dnd_bind("<<DropEnter>>", self._drag_enter)
        self.file_list.dnd_bind("<<DropLeave>>", self._drag_leave)
        self.file_list.dnd_bind("<<Drop>>", self._drop_paths)
        shortcut = "Cmd+V" if platform.system() == "Darwin" else "Ctrl+V"
        self.file_hint = ttk.Label(
            files_panel,
            text=f"拖入文件或文件夹，也可按 {shortcut} 粘贴图片或文件；双击或 Delete 可移除",
            style="Muted.TLabel",
        )
        self.file_hint.pack(anchor="w", pady=(8, 0))
        files_panel.outer.pack(fill="both", expand=True)  # type: ignore[attr-defined]

        bottom = ttk.Frame(page, style="App.TFrame")
        bottom.pack(fill="x", pady=(14, 0))
        self.status_text = tk.StringVar(value="尚未发送")
        ttk.Label(bottom, textvariable=self.status_text, style="Status.TLabel").pack(side="left")
        self.send_button = ttk.Button(bottom, text="发送到 GitHub", style="Primary.TButton", command=self._start_sync)
        self.send_button.pack(side="right")
        self.repository_button = ttk.Button(bottom, text="查看仓库", style="Link.TButton", command=self._open_repository)
        return page

    def _build_settings_page(self, parent: tk.Widget) -> ttk.Frame:
        page = ttk.Frame(parent, style="App.TFrame")
        panel = self._panel(page)
        top = ttk.Frame(panel, style="Panel.TFrame")
        top.pack(fill="x", pady=(0, 16))
        ttk.Label(top, text="仓库设置", style="Section.TLabel").pack(side="left")
        ttk.Button(top, text="返回", command=self._show_send).pack(side="right")

        self.owner_var = tk.StringVar()
        self.repo_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.remember_var = tk.BooleanVar(value=True)
        fields = [
            ("GitHub 用户名", self.owner_var, ""),
            ("仓库名", self.repo_var, ""),
            ("访问令牌", self.token_var, "*"),
        ]
        form = ttk.Frame(panel, style="Panel.TFrame")
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        for row, (label, variable, mask) in enumerate(fields):
            ttk.Label(form, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 20), pady=7)
            ttk.Entry(form, textvariable=variable, show=mask).grid(row=row, column=1, sticky="ew", pady=7)
        tk.Checkbutton(
            panel,
            text="使用 Git 凭据管理器记住 Token",
            variable=self.remember_var,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            activebackground=COLORS["panel"],
            font=(UI_FONT, 10),
            borderwidth=0,
            highlightthickness=0,
        ).pack(anchor="w", pady=(13, 10))

        guide = ttk.Frame(panel, style="Panel.TFrame")
        guide.pack(fill="x", pady=(2, 0))
        ttk.Label(guide, text="首次配置", style="Section.TLabel").pack(anchor="w", pady=(0, 5))
        ttk.Label(
            guide,
            text="1. 点击下方按钮打开 GitHub\n2. 页面中保留已勾选的 repo 权限，点击 Generate token\n3. 复制生成的 Token，粘贴到上方“访问令牌”",
            style="Muted.TLabel",
            justify="left",
            wraplength=620,
        ).pack(anchor="w")
        ttk.Button(guide, text="打开 GitHub 创建 Token", command=self._open_token_page).pack(anchor="w", pady=(9, 0))
        ttk.Button(guide, text="项目主页", command=self._open_project_page).pack(anchor="w", pady=(7, 0))
        ttk.Label(
            guide,
            text="同名仓库存在时直接使用；不存在时会在首次发送时自动创建私有仓库。",
            style="Muted.TLabel",
            wraplength=620,
        ).pack(anchor="w", pady=(10, 0))
        actions = ttk.Frame(panel, style="Panel.TFrame")
        actions.pack(fill="x", pady=(20, 0))
        self.clear_repository_button = ttk.Button(
            actions,
            text="清空仓库内容",
            command=self._confirm_clear_repository,
        )
        self.clear_repository_button.pack(side="left")
        ttk.Button(actions, text="保存设置", style="Primary.TButton", command=self._save_and_back).pack(side="right")
        panel.outer.pack(fill="x")  # type: ignore[attr-defined]
        return page

    def _load_values(self) -> None:
        self.owner_var.set(self.config.owner)
        self.repo_var.set(self.config.repository or "gitdrop-inbox")
        self.remember_var.set(self.config.remember_token)
        if self.config.remember_token and self.config.owner:
            self.token_var.set(load_token(self.config.owner))

    def _show_settings(self) -> None:
        self.send_page.pack_forget()
        self.settings_page.pack(fill="both", expand=True)

    def _show_send(self) -> None:
        self.settings_page.pack_forget()
        self.send_page.pack(fill="both", expand=True)

    def _save_settings(self) -> bool:
        owner, repository, token = self.owner_var.get().strip(), self.repo_var.get().strip(), self.token_var.get().strip()
        if not owner or not repository or not token:
            messagebox.showwarning("设置不完整", "请填写 GitHub 用户名、仓库名和访问令牌。", parent=self.root)
            return False
        self.config = AppConfig(
            owner=owner,
            repository=repository,
            branch=self.config.branch or "main",
            remote_folder=self.config.remote_folder or "inbox",
            remember_token=self.remember_var.get(),
        )
        try:
            save_config(self.config)
            save_token(owner, token, self.config.remember_token)
        except (OSError, RuntimeError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return False
        return True

    def _save_and_back(self) -> None:
        if self._save_settings():
            self._show_send()
            self._start_repository_check()

    def _open_token_page(self) -> None:
        webbrowser.open("https://github.com/settings/tokens/new?description=GitDrop&scopes=repo")

    def _open_project_page(self) -> None:
        webbrowser.open(PROJECT_URL)

    def _start_repository_check(self) -> None:
        token = self.token_var.get().strip()
        if not token or not self.config.owner or not self.config.repository:
            return
        self.status_text.set("正在检查远端仓库…")
        client = GitHubClient(
            token,
            self.config.owner,
            self.config.repository,
            self.config.branch,
        )
        threading.Thread(target=self._repository_check_worker, args=(client,), daemon=True).start()

    def _repository_check_worker(self, client: GitHubClient) -> None:
        try:
            repository = client.inspect_repository()
            self.events.put(("repository_found" if repository else "repository_missing", repository))
        except (GitHubError, OSError) as exc:
            self.events.put(("repository_error", str(exc)))
        except Exception as exc:
            self.events.put(("repository_error", f"未预期的错误：{exc}"))

    def _show_repository_button(self) -> None:
        if not self.repository_button.winfo_manager():
            self.repository_button.pack(side="right", padx=(0, 10))

    def _choose_files(self) -> None:
        self._add_paths([Path(path) for path in filedialog.askopenfilenames(parent=self.root, title="选择文件")])

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, title="选择文件夹")
        if selected:
            self._add_paths([Path(selected)])

    def _paste_clipboard_content(self, _event=None) -> str | None:
        try:
            paths = paste_paths()
        except ClipboardError as exc:
            messagebox.showerror("粘贴失败", str(exc), parent=self.root)
            return "break"
        if not paths:
            # Let text fields keep their normal paste behavior for ordinary text.
            return None
        before = len(self.items)
        self._add_paths(paths)
        added = len(self.items) - before
        self.status_text.set(f"已从剪贴板添加 {added} 个文件")
        return "break"

    def _add_paths(self, paths: list[Path]) -> None:
        existing = {(item.source, item.relative_path) for item in self.items}
        self.items.extend(item for item in collect_paths(paths) if (item.source, item.relative_path) not in existing)
        self._refresh_items()

    def _drag_enter(self, event) -> str:
        self.file_list.configure(background="#eef5ff")
        return event.action

    def _drag_leave(self, event) -> str:
        self.file_list.configure(background="white")
        return event.action

    def _drop_paths(self, event) -> str:
        self.file_list.configure(background="white")
        paths = [Path(value) for value in self.root.tk.splitlist(event.data)]
        self._add_paths([path for path in paths if path.exists()])
        return event.action

    def _refresh_items(self) -> None:
        self.file_list.delete(0, "end")
        total = sum(item.size for item in self.items)
        for item in self.items:
            self.file_list.insert("end", f"  {item.relative_path}    {human_size(item.size)}")
        self.file_hint.configure(
            text=f"{len(self.items)} 个文件，共 {human_size(total)}；可继续拖入或粘贴"
            if self.items
            else f"拖入文件或文件夹，也可按 {'Cmd+V' if platform.system() == 'Darwin' else 'Ctrl+V'} 粘贴图片或文件；双击或 Delete 可移除"
        )

    def _remove_selected(self, _event=None) -> None:
        selected = set(self.file_list.curselection())
        for index in selected:
            remove_clipboard_file(self.items[index].source)
        self.items = [item for index, item in enumerate(self.items) if index not in selected]
        self._refresh_items()

    def _clear_items(self) -> None:
        for item in self.items:
            remove_clipboard_file(item.source)
        self.items.clear()
        self._refresh_items()

    def _close(self) -> None:
        cleanup_clipboard_images()
        self.root.destroy()

    def _start_sync(self) -> None:
        message = self.message_edit.get("1.0", "end-1c").strip()
        if not message and not self.items:
            messagebox.showinfo("没有内容", "请输入消息或选择要发送的内容。", parent=self.root)
            return
        if not self._save_settings():
            self._show_settings()
            return
        token = self.token_var.get().strip()
        self.send_button.state(["disabled"])
        self.status_text.set("准备发送…")
        threading.Thread(
            target=self._sync_worker,
            args=(token, message, list(self.items)),
            daemon=True,
        ).start()

    def _sync_worker(self, token: str, message: str, items: list[UploadItem]) -> None:
        try:
            if not self.repository_url:
                client = GitHubClient(
                    token, self.config.owner, self.config.repository, self.config.branch
                )
                client.ensure_repository(lambda text: self.events.put(("progress", text)))
                self.config.branch = client.branch
            transport = LocalGitTransport(
                token, self.config.owner, self.config.repository, self.config.branch
            )
            result = transport.sync(
                message,
                items,
                self.config.remote_folder,
                lambda text: self.events.put(("progress", text)),
            )
            self.events.put(("success", result))
        except (GitHubError, GitSyncError, OSError) as exc:
            self.events.put(("error", str(exc)))
        except Exception as exc:
            self.events.put(("error", f"未预期的错误：{exc}"))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self.status_text.set(str(payload))
                elif kind == "success":
                    result = payload
                    assert isinstance(result, GitSyncResult)
                    self.repository_url = f"https://github.com/{self.config.owner}/{self.config.repository}"
                    self.status_text.set(f"发送成功 · {result.uploaded_files} 项")
                    self._show_repository_button()
                    self.message_edit.delete("1.0", "end")
                    self._clear_items()
                    self.send_button.state(["!disabled"])
                elif kind == "error":
                    self.status_text.set("发送失败")
                    self.send_button.state(["!disabled"])
                    messagebox.showerror("同步失败", str(payload), parent=self.root)
                elif kind == "repository_found":
                    repository = payload
                    assert isinstance(repository, dict)
                    self.repository_url = str(repository["url"])
                    self.config.branch = str(repository["branch"])
                    self.status_text.set(f"已连接：{repository['full_name']}")
                    self._show_repository_button()
                elif kind == "repository_missing":
                    self.repository_url = ""
                    self.repository_button.pack_forget()
                    self.status_text.set("远端仓库不存在 · 首次发送时自动创建")
                elif kind == "repository_error":
                    self.repository_url = ""
                    self.repository_button.pack_forget()
                    self.status_text.set(f"仓库检查失败：{payload}")
                elif kind == "clear_success":
                    self.clear_repository_button.state(["!disabled"])
                    self.status_text.set("仓库内容已清空")
                    self._show_send()
                elif kind == "clear_error":
                    self.clear_repository_button.state(["!disabled"])
                    messagebox.showerror("清理失败", str(payload), parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _open_repository(self) -> None:
        url = self.repository_url or f"https://github.com/{self.config.owner}/{self.config.repository}"
        webbrowser.open(url)

    def _confirm_clear_repository(self) -> None:
        if not self._save_settings():
            return
        repository = self.config.repository
        confirmed = messagebox.askyesno(
            "确认清空仓库",
            f"将删除 {self.config.owner}/{repository} 当前分支中的所有文件。\n\n"
            "仓库本身和提交历史会保留，此操作会产生一个清理提交。确定继续吗？",
            icon="warning",
            parent=self.root,
        )
        if not confirmed:
            return
        self.clear_repository_button.state(["disabled"])
        self.status_text.set("准备清空仓库…")
        token = self.token_var.get().strip()
        threading.Thread(target=self._clear_repository_worker, args=(token,), daemon=True).start()

    def _clear_repository_worker(self, token: str) -> None:
        try:
            client = GitHubClient(
                token, self.config.owner, self.config.repository, self.config.branch
            )
            repository = client.inspect_repository()
            if repository is None:
                raise GitSyncError("远端仓库不存在")
            self.config.branch = repository["branch"]
            transport = LocalGitTransport(
                token, self.config.owner, self.config.repository, self.config.branch
            )
            transport.clear_repository(lambda text: self.events.put(("progress", text)))
            self.events.put(("clear_success", None))
        except (GitHubError, GitSyncError, OSError) as exc:
            self.events.put(("clear_error", str(exc)))
        except Exception as exc:
            self.events.put(("clear_error", f"未预期的错误：{exc}"))


def run() -> int:
    root = TkinterDnD.Tk()
    GitDropApp(root)
    root.mainloop()
    return 0
