"""Runs CLI commands (seed / discover / crawl) in the background for the admin page.

Why a separate process instead of calling the functions directly?
  - It's exactly the same code path as typing the command yourself, so nothing new to learn.
  - A long crawl can't freeze or crash the website.
  - We get the printed output line by line, and show it live on the admin page.

Only ONE task runs at a time (two crawls writing the same DB would just slow each other down).
Everything here lives in memory: restarting the server forgets the history (the data stays in SQLite).
"""

import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # the folder with pyproject.toml and data/
MAX_LOG_LINES = 2000
MAX_HISTORY = 20


@dataclass
class Task:
    id: int
    title: str  # e.g. "Crawl"
    commands: list[list[str]]  # each one is CLI args, e.g. ["crawl", "--limit", "20"]
    status: str = "queued"  # queued -> running -> done / failed / stopped
    log: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "commands": [" ".join(c) for c in self.commands],
            "status": self.status,
            "log": self.log[-400:],  # the page only needs the recent part
            "started_at": self.started_at.isoformat(timespec="seconds") if self.started_at else None,
            "finished_at": self.finished_at.isoformat(timespec="seconds") if self.finished_at else None,
        }


class Runner:
    def __init__(self):
        self.current: Task | None = None
        self.history: list[Task] = []
        self.process: asyncio.subprocess.Process | None = None
        self.next_id = 1

    @property
    def busy(self) -> bool:
        return self.current is not None and self.current.status in ("queued", "running")

    def start(self, title: str, commands: list[list[str]]) -> Task:
        if self.busy:
            raise RuntimeError("Another task is already running. Wait for it or stop it first.")
        task = Task(self.next_id, title, commands)
        self.next_id += 1
        self.current = task
        asyncio.get_running_loop().create_task(self._run(task))
        return task

    async def stop(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            if self.current:
                self.current.status = "stopped"

    async def _run(self, task: Task) -> None:
        task.status, task.started_at = "running", datetime.now()
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONWARNINGS": "ignore"}
        try:
            for args in task.commands:
                task.log.append(f"$ sort-sherr {' '.join(args)}")
                # exec, not a shell: arguments are passed as a list, so nothing typed in a form can
                # be interpreted as a shell command
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "sort_sherr.cli", *args,
                    cwd=PROJECT_ROOT, env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                async for raw in self.process.stdout:
                    task.log.append(raw.decode(errors="replace").rstrip())
                    del task.log[:-MAX_LOG_LINES]
                code = await self.process.wait()
                if task.status == "stopped":
                    task.log.append("■ stopped by admin")
                    break
                if code != 0:
                    task.status = "failed"
                    task.log.append(f"✗ command exited with code {code}")
                    break
            else:
                task.status = "done"
                task.log.append("✓ finished")
        except Exception as e:  # never let a background task die silently
            task.status = "failed"
            task.log.append(f"✗ {type(e).__name__}: {e}")
        finally:
            task.finished_at = datetime.now()
            self.process = None
            self.history.insert(0, task)
            del self.history[MAX_HISTORY:]


runner = Runner()  # one shared runner for the whole app
