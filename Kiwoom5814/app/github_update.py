"""Desktop prompts for the Rust release launcher."""
import json
import os
import queue
import subprocess
from threading import Thread
from tkinter import messagebox


def check(window, *, automatic=False):
    launcher = os.environ.get('DISTRIBUTION_LAUNCHER')
    if not launcher or getattr(window, '_release_update_busy', False):
        return
    window._release_update_busy = True
    result = queue.Queue()

    def run(arguments):
        try:
            completed = subprocess.run([launcher, *arguments], capture_output=True, text=True,
                                       check=True, timeout=600,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result.put((True, completed.stdout))
        except (OSError, subprocess.SubprocessError):
            result.put((False, ''))

    def poll(installing=False):
        try:
            success, output = result.get_nowait()
        except queue.Empty:
            window.after(200, lambda: poll(installing))
            return
        window._release_update_busy = False
        if not success:
            if installing or not automatic:
                messagebox.showerror('업데이트', '업데이트를 완료할 수 없습니다. GitHub 연결을 확인해 주세요.', parent=window)
            return
        if installing:
            messagebox.showinfo('업데이트', '설치를 마쳤습니다. 프로그램을 닫고 다시 열면 새 버전이 적용됩니다.', parent=window)
            return
        release = json.loads(output)
        if release is None:
            if not automatic:
                messagebox.showinfo('업데이트', '현재 설치된 버전이 최신입니다.', parent=window)
            return
        version = release['version']
        notes = release.get('notes', '').strip()[:800]
        if messagebox.askyesno('업데이트', f'새 버전 {version}을 설치할까요?\n\n{notes}', parent=window):
            window._release_update_busy = True
            Thread(target=run, args=(['--apply-update', version],), daemon=True).start()
            poll(installing=True)

    Thread(target=run, args=(['--check-update'],), daemon=True).start()
    poll()
