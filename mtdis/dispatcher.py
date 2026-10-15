"""Core of mtdis: queue and state files, system checks, starting and stopping
tasks in tmux, and the dispatch pass that Hammerspoon triggers.

A dispatch pass:
  1. records tasks that finished since the last pass (done / failed),
  2. stops tasks whose network or power requirements no longer hold,
  3. stops the newest tasks if free RAM falls below the safety reserve,
  4. starts pending tasks while their requirements fit in the remaining headroom.
"""

import fcntl
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime

import psutil

HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("MTDIS_CONFIG_DIR", os.path.join(HOME, ".config", "mtdis"))
QUEUE_FILE = os.path.abspath(os.environ.get("MTDIS_QUEUE", os.path.join(CONFIG_DIR, "queue.json")))
STATE_DIR = os.path.abspath(os.environ.get("MTDIS_STATE_DIR", os.path.join(HOME, ".local", "state", "mtdis")))
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOCK_FILE = os.path.join(STATE_DIR, ".lock")
LOG_DIR = os.path.join(STATE_DIR, "logs")
RUN_DIR = os.path.join(STATE_DIR, "run")
DISPATCH_LOG = os.path.join(STATE_DIR, "dispatcher.log")
SESSION_PREFIX = "mtdis_"
GB = 1024 ** 3

# Hammerspoon starts processes with a minimal PATH; make Homebrew tools (tmux) findable.
os.environ["PATH"] = os.pathsep.join(
    ["/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")]
)

DEFAULT_SETTINGS = {
    "max_parallel": 4,           # most tasks allowed to run at once
    "reserve_ram_gb": 2.0,       # RAM always kept free for you and macOS
    "restart_cooldown_s": 300,   # wait this long before restarting a task stopped for low memory
    "stop_grace_s": 10,          # time a task gets to exit after Ctrl-C before it is terminated
    "cpu_sample_s": 1.0,         # how long CPU usage is sampled
    "terminal_app": "Terminal",  # app opened for tasks with "open_terminal": true
}


class QueueError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(DISPATCH_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "-"


def valid_task_id(task_id):
    return bool(task_id) and all(c.isascii() and (c.isalnum() or c in "_-") for c in task_id)


def read_queue():
    """The queue file as stored: {"settings": {...}, "tasks": [...]}. Created empty if missing."""
    if not os.path.exists(QUEUE_FILE):
        write_queue({"settings": {}, "tasks": []})
    with open(QUEUE_FILE) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise QueueError(f"{QUEUE_FILE} is not valid JSON: {e}")
    if isinstance(data, list):  # a bare list of tasks is also accepted
        data = {"tasks": data}
    data.setdefault("settings", {})
    data.setdefault("tasks", [])
    return data


def write_queue(data):
    os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, QUEUE_FILE)


def load_queue():
    """Validated (settings, tasks) for dispatching."""
    data = read_queue()
    settings = {**DEFAULT_SETTINGS, **data["settings"]}
    seen = set()
    for task in data["tasks"]:
        if "id" not in task or "cmd" not in task:
            raise QueueError(f"Every task needs an 'id' and a 'cmd': {task}")
        if not valid_task_id(task["id"]):
            raise QueueError(f"Task id {task['id']!r} may only contain letters, digits, '_' and '-'.")
        if task["id"] in seen:
            raise QueueError(f"Duplicate task id: {task['id']}")
        seen.add(task["id"])
    return settings, data["tasks"]


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"tasks": {}}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


@contextmanager
def state_lock(wait):
    """Only one mtdis process may change the queue or state at a time. Dispatch
    passes that overlap simply skip: the next pass does whatever they would have."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            print("Another mtdis run is in progress; skipping.")
            sys.exit(0)
        yield


def exit_file(task_id):
    return os.path.join(RUN_DIR, f"{task_id}.exit")


def log_file(task_id):
    return os.path.join(LOG_DIR, f"{task_id}.log")


def remove_run_files(task_id, all_files=False):
    names = [f"{task_id}.exit"]
    if all_files:
        names += [f"{task_id}.sh", f"{task_id}.attach.command"]
    for name in names:
        try:
            os.remove(os.path.join(RUN_DIR, name))
        except FileNotFoundError:
            pass


def reqs_of(task):
    return task.get("requirements", {})


# ---------------------------------------------------------------- tmux

def tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def session_name(task_id):
    return SESSION_PREFIX + task_id


def session_alive(task_id):
    return tmux("has-session", "-t", f"={session_name(task_id)}").returncode == 0


def task_processes(task_id):
    """Every process belonging to the task: the tmux pane's shell and its descendants."""
    result = tmux("list-panes", "-t", f"={session_name(task_id)}:", "-F", "#{pane_pid}")
    procs = []
    for pid in result.stdout.split():
        try:
            root = psutil.Process(int(pid))
            procs.append(root)
            procs.extend(root.children(recursive=True))
        except (psutil.Error, ValueError):
            pass
    return procs


# ---------------------------------------------------------------- system state

def detect_internet():
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
        return True
    except OSError:
        return False


def detect_ac_power():
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
    return "AC Power" in out


def has_metal_gpu(state):
    """Whether the Mac has a Metal-capable GPU. Hardware does not change, so the
    (slow) system_profiler call runs once and the answer is cached in the state."""
    if "has_metal_gpu" not in state:
        try:
            out = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"], capture_output=True, text=True, timeout=60
            ).stdout
            state["has_metal_gpu"] = "Metal" in out
        except (OSError, subprocess.SubprocessError):
            return False
    return state["has_metal_gpu"]


class Snapshot:
    """System CPU and free RAM, plus the CPU and RAM used by each running task."""

    def __init__(self, running_ids, sample_s):
        task_procs = {tid: task_processes(tid) for tid in running_ids}
        for procs in task_procs.values():
            for p in procs:
                try:
                    p.cpu_percent(None)  # start per-process measurement
                except psutil.Error:
                    pass
        self.cpu_percent = psutil.cpu_percent(interval=sample_s)
        cores = psutil.cpu_count() or 1
        self.task_cpu = {}     # share of the whole machine, same scale as cpu_percent
        self.task_ram_gb = {}
        for tid, procs in task_procs.items():
            cpu = ram = 0.0
            for p in procs:
                try:
                    cpu += p.cpu_percent(None)
                    ram += p.memory_info().rss
                except psutil.Error:
                    pass
            self.task_cpu[tid] = cpu / cores
            self.task_ram_gb[tid] = ram / GB
        self.free_ram_gb = psutil.virtual_memory().available / GB


def env_blockers(task, env, gpu):
    reqs = reqs_of(task)
    blockers = []
    if reqs.get("needs_internet") and not env["internet"]:
        blockers.append("no internet")
    if reqs.get("needs_ac_power") and not env["ac_power"]:
        blockers.append("on battery")
    if reqs.get("needs_gpu") and not gpu:
        blockers.append("no Metal GPU")
    return blockers


# ---------------------------------------------------------------- start / stop

def start_task(task, settings):
    tid = task["id"]
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    if session_alive(tid):  # e.g. a finished interactive task still waiting for Enter
        tmux("kill-session", "-t", f"={session_name(tid)}")
    remove_run_files(tid)

    q = shlex.quote
    logpath = log_file(tid)
    cwd = os.path.expanduser(task.get("cwd", "~"))
    interactive = task.get("open_terminal", False)

    lines = [
        "#!/bin/bash",
        f"cd {q(cwd)} || {{ echo 1 > {q(exit_file(tid))}; exit 1; }}",
        f'echo "=== $(date) start {tid}" >> {q(logpath)}',
    ]
    if interactive:
        # Output goes straight to the terminal so the program keeps a real TTY.
        lines += [task["cmd"], "code=$?"]
    else:
        lines += [f"( {task['cmd']}\n) 2>&1 | tee -a {q(logpath)}", "code=${PIPESTATUS[0]}"]
    lines += [
        f'echo "=== $(date) exit $code" >> {q(logpath)}',
        f'echo "$code" > {q(exit_file(tid))}',
    ]
    if interactive:
        lines += ['echo; echo "[mtdis] Task finished (exit $code). Press Enter to close."; read -r _']

    script = os.path.join(RUN_DIR, f"{tid}.sh")
    with open(script, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(script, 0o755)

    result = tmux("new-session", "-d", "-s", session_name(tid), "/bin/bash " + q(script))
    if result.returncode != 0:
        log(f"  ! could not start {tid}: {result.stderr.strip()}")
        return False
    if interactive:
        open_terminal(tid, settings)
    return True


def open_terminal(task_id, settings):
    """Open a terminal window attached to the task so you can interact with it.
    A .command file is used because `open` needs no AppleScript permission."""
    attach = os.path.join(RUN_DIR, f"{task_id}.attach.command")
    with open(attach, "w") as f:
        f.write(f"#!/bin/bash\nexport PATH={shlex.quote(os.environ['PATH'])}\n"
                f"exec tmux attach -t ={session_name(task_id)}\n")
    os.chmod(attach, 0o755)
    subprocess.run(["open", "-a", settings["terminal_app"], attach])


def stop_task(task_id, settings):
    """Send Ctrl-C, give the task stop_grace_s to exit, then terminate what's left."""
    if not session_alive(task_id):
        return
    procs = task_processes(task_id)
    tmux("send-keys", "-t", f"={session_name(task_id)}:", "C-c")
    _, alive = psutil.wait_procs(procs, timeout=settings["stop_grace_s"])
    for p in alive:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(alive, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    tmux("kill-session", "-t", f"={session_name(task_id)}")
    remove_run_files(task_id)


# ---------------------------------------------------------------- dispatch

def reconcile(tasks, recs):
    """Record tasks that finished since the last pass, and adopt orphaned sessions."""
    now = time.time()
    for task in tasks:
        tid, rec = task["id"], recs[task["id"]]
        has_exit = os.path.exists(exit_file(tid))
        if rec["status"] == "running":
            if has_exit:
                try:
                    with open(exit_file(tid)) as f:
                        code = int(f.read().strip())
                except ValueError:
                    code = None
                rec.update(status="done" if code == 0 else "failed", exit_code=code, finished_at=now)
                log(f"  {tid} finished: {rec['status']} (exit {code})")
            elif not session_alive(tid):
                rec.update(status="failed", exit_code=None, finished_at=now)
                log(f"  {tid} disappeared without an exit code: marked failed")
        elif rec["status"] == "pending" and not has_exit and session_alive(tid):
            rec.update(status="running", started_at=rec.get("started_at") or now)
            log(f"  {tid} found running in tmux: adopted")


def mark_stopped(rec, reason, cooldown_s=0):
    now = time.time()
    rec.update(status="pending", last_stop={"at": now, "reason": reason})
    rec["cooldown_until"] = now + cooldown_s if cooldown_s else None


def dispatch(internet=None, ac_power=None, reason="manual"):
    with state_lock(wait=False):
        settings, tasks = load_queue()
        state = load_state()
        recs = state.setdefault("tasks", {})
        for task in tasks:
            recs.setdefault(task["id"], {"status": "pending", "runs": 0})

        env = {
            "internet": detect_internet() if internet is None else internet,
            "ac_power": detect_ac_power() if ac_power is None else ac_power,
        }
        gpu = has_metal_gpu(state)
        reconcile(tasks, recs)

        running = [t for t in tasks if recs[t["id"]]["status"] == "running"]
        snap = Snapshot([t["id"] for t in running], settings["cpu_sample_s"])
        free_ram = snap.free_ram_gb
        cpu = snap.cpu_percent
        log(f"[{reason}] internet={env['internet']} ac={env['ac_power']} gpu={gpu} "
            f"cpu={cpu:.0f}% free_ram={free_ram:.1f}GB running={len(running)}")

        def release(task):
            nonlocal free_ram, cpu
            running.remove(task)
            free_ram += snap.task_ram_gb.get(task["id"], 0)
            cpu = max(0.0, cpu - snap.task_cpu.get(task["id"], 0))

        # 1. Network and power requirements are hard: stop tasks that lost them.
        for task in list(running):
            blockers = env_blockers(task, env, gpu)
            if blockers:
                log(f"  stopping {task['id']}: {', '.join(blockers)}")
                stop_task(task["id"], settings)
                mark_stopped(recs[task["id"]], ", ".join(blockers))
                release(task)

        # 2. Low memory can freeze or crash the Mac: stop the newest tasks until
        #    free RAM is back above the reserve. CPU load only slows things down,
        #    so running tasks are never stopped for it.
        reserve = settings["reserve_ram_gb"]
        if free_ram < reserve:
            for task in sorted(running, key=lambda t: recs[t["id"]].get("started_at", 0), reverse=True):
                if free_ram >= reserve:
                    break
                log(f"  stopping {task['id']}: free RAM {free_ram:.1f}GB is below the {reserve}GB reserve")
                stop_task(task["id"], settings)
                mark_stopped(recs[task["id"]], "low memory", settings["restart_cooldown_s"])
                release(task)

        # 3. Start pending tasks in queue order while they fit. A running task that
        #    hasn't yet grown to its declared size keeps the difference reserved,
        #    so tasks that ramp up slowly can't be crowded out by new ones.
        ram_headroom = free_ram - reserve - sum(
            max(0.0, reqs_of(t).get("min_ram_gb", 0) - snap.task_ram_gb.get(t["id"], 0)) for t in running
        )
        projected_cpu = cpu + sum(
            max(0.0, reqs_of(t).get("expected_cpu_percent", 0) - snap.task_cpu.get(t["id"], 0)) for t in running
        )
        now = time.time()
        for task in tasks:
            tid, rec, reqs = task["id"], recs[task["id"]], reqs_of(task)
            if rec["status"] != "pending":
                continue
            if len(running) >= settings["max_parallel"]:
                rec["waiting_for"] = f"no free slot (max_parallel={settings['max_parallel']})"
                continue
            if (rec.get("cooldown_until") or 0) > now:
                rec["waiting_for"] = f"cooldown after low memory until {fmt_time(rec['cooldown_until'])}"
                continue
            blockers = env_blockers(task, env, gpu)
            if blockers:
                rec["waiting_for"] = ", ".join(blockers)
                continue
            need_ram = reqs.get("min_ram_gb", 0)
            if need_ram > ram_headroom:
                rec["waiting_for"] = f"RAM: needs {need_ram}GB, {max(ram_headroom, 0):.1f}GB available"
                continue
            max_cpu = reqs.get("max_cpu_percent")
            if max_cpu is not None and projected_cpu > max_cpu:
                rec["waiting_for"] = f"CPU: at {projected_cpu:.0f}%, limit {max_cpu}%"
                continue

            log(f"  starting {tid}")
            if not start_task(task, settings):
                rec["waiting_for"] = "tmux failed to start the task (see dispatcher.log)"
                continue
            rec.update(status="running", started_at=time.time(), runs=rec.get("runs", 0) + 1,
                       exit_code=None, finished_at=None, cooldown_until=None)
            rec.pop("waiting_for", None)
            running.append(task)
            ram_headroom -= need_ram
            projected_cpu += reqs.get("expected_cpu_percent", 0)

        state["last_run"] = {"at": time.time(), "reason": reason}
        save_state(state)
