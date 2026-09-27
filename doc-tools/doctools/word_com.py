"""Работа с установленным Microsoft Word через COM (только Windows).

WordSession запускает ОТДЕЛЬНЫЙ невидимый экземпляр Word (DispatchEx — окна
пользователя с открытыми документами не трогаются), а при выходе закрывает его
в finally. Если Word не закрылся сам (завис на диалоге), принудительно
завершаются только процессы WINWORD.EXE, появившиеся при запуске этой сессии.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import DocToolsError

WD_FORMAT_PDF = 17
WD_FORMAT_DOCX = 16
WD_ALERTS_NONE = 0
WD_DO_NOT_SAVE = 0


def winword_pids() -> set[int]:
    """PID всех запущенных WINWORD.EXE."""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WINWORD.EXE", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=30).stdout.decode("cp866", errors="ignore")
    except (OSError, subprocess.SubprocessError):
        return set()
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("winword") and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


class WordSession:
    """with WordSession() as w: w.to_pdf(src, dst)"""

    def __init__(self) -> None:
        self.app: Any = None
        self.own_pids: set[int] = set()
        self.killed: set[int] = set()
        self._com_ready = False

    def __enter__(self) -> "WordSession":
        if sys.platform != "win32":
            raise DocToolsError("Конвертация через Word доступна только в Windows с установленным Microsoft Word")
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise DocToolsError("Не установлен pywin32 (pip install pywin32)") from exc
        pythoncom.CoInitialize()
        self._com_ready = True
        before = winword_pids()
        try:
            self.app = win32com.client.DispatchEx("Word.Application")
        except Exception as exc:
            self._shutdown()
            raise DocToolsError(f"Не удалось запустить Microsoft Word: {exc}") from exc
        self.own_pids = winword_pids() - before
        try:
            self.app.Visible = False
            self.app.DisplayAlerts = WD_ALERTS_NONE
        except Exception:
            pass
        return self

    def __exit__(self, *exc: Any) -> None:
        self._shutdown()

    def _shutdown(self) -> None:
        try:
            if self.app is not None:
                try:
                    for i in range(self.app.Documents.Count, 0, -1):
                        self.app.Documents(i).Close(WD_DO_NOT_SAVE)
                except Exception:
                    pass
                try:
                    self.app.Quit(WD_DO_NOT_SAVE)
                except Exception:
                    pass
        finally:
            self.app = None
            if self._com_ready:
                import pythoncom

                pythoncom.CoUninitialize()
                self._com_ready = False
            self._kill_leftovers()

    def _kill_leftovers(self, wait: float = 8.0) -> None:
        """Ждём, пока наш Word выйдет сам; не вышел — завершаем только свои PID."""
        if not self.own_pids:
            return
        deadline = time.monotonic() + wait
        alive = self.own_pids & winword_pids()
        while alive and time.monotonic() < deadline:
            time.sleep(0.3)
            alive = self.own_pids & winword_pids()
        for pid in alive:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30)
            self.killed.add(pid)

    def _open(self, src: Path) -> Any:
        if self.app is None:
            raise DocToolsError("Word не запущен")
        src = Path(src).resolve()
        if not src.exists():
            raise DocToolsError(f"Файл не найден: {src}")
        try:
            return self.app.Documents.Open(str(src), ConfirmConversions=False, ReadOnly=True,
                                           AddToRecentFiles=False, Visible=False)
        except Exception as exc:
            raise DocToolsError(f"Word не смог открыть {src.name}: {exc}") from exc

    def to_pdf(self, src: Path, dst: Path) -> Path:
        dst = Path(dst).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
        doc = self._open(src)
        try:
            doc.ExportAsFixedFormat(str(dst), WD_FORMAT_PDF, OpenAfterExport=False)
        except Exception as exc:
            raise DocToolsError(f"Word не смог сохранить PDF {dst.name}: {exc}") from exc
        finally:
            doc.Close(WD_DO_NOT_SAVE)
        return dst

    def to_docx(self, src: Path, dst: Path) -> Path:
        dst = Path(dst).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
        doc = self._open(src)
        try:
            doc.SaveAs2(str(dst), FileFormat=WD_FORMAT_DOCX)
        except Exception as exc:
            raise DocToolsError(f"Word не смог сохранить {dst.name}: {exc}") from exc
        finally:
            doc.Close(WD_DO_NOT_SAVE)
        return dst
