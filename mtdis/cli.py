"""The `mtdis` command line."""

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from . import __version__
from . import dispatcher as d

HS_DIR = os.path.expanduser("~/.hammerspoon")
HS_INIT = os.path.join(HS_DIR, "init.lua")
HS_MODULE = os.path.join(HS_DIR, "mtdis.lua")
# Left behind by the pre-0.2 install.sh; `mtdis setup` cleans them up.
LEGACY_MODULE = os.path.join(HS_DIR, "mac_task_dispatcher.lua")
LEGACY_MARKERS = ('require("mac_task_dispatcher")', "-- mac-task-dispatcher")


# ---------------------------------------------------------------- status

def cmd_status(args):
    _, tasks = d.load_queue()
    # Record tasks that finished since the last check, so they don't show as
    # running until the next one. Skipped if a dispatch pass holds the lock.
    with d.state_lock(wait=False, exit_if_busy=False) as locked:
        state = d.load_state()
        recs = state.setdefault("tasks", {})
        if locked:
            for task in tasks:
                recs.setdefault(task["id"], {"status": "pending", "runs": 0})
            if d.reconcile(tasks, recs, echo=False):
                d.save_state(state)
    running_ids = [t["id"] for t in tasks if recs.get(t["id"], {}).get("status") == "running"]
    snap = d.Snapshot(running_ids, 0.5)

    last = state.get("last_run")
    last_run = f"{d.fmt_time(last['at'])} ({last['reason']})" if last else "never"
    print(f"CPU {snap.cpu_percent:.0f}% | free RAM {snap.free_ram_gb:.1f}GB | last check {last_run}")
    if last and time.time() - last["at"] > 300:
        print("  No check in the last 5 minutes. Is Hammerspoon running? (`mtdis setup` connects it)")
    if not tasks:
        print("\nNo tasks yet. Add one with: mtdis add <id> -- <command>")
        return

    print(f"\n{'TASK':<24}{'STATUS':<10}{'RUNS':>5}  {'CPU':>5}  {'RAM':>7}  {'STARTED':<12}{'EXIT':>5}  NOTE")
    for task in tasks:
        tid = task["id"]
        rec = recs.get(tid, {"status": "pending", "runs": 0})
        status = rec["status"]
        live = status == "running"
        cpu = f"{snap.task_cpu.get(tid, 0):.0f}%" if live else "-"
        ram = f"{snap.task_ram_gb.get(tid, 0):.2f}G" if live else "-"
        code = rec.get("exit_code")
        note = ""
        if status == "pending":
            note = "waiting: " + rec["waiting_for"] if rec.get("waiting_for") else "waiting for next check"
            if rec.get("last_stop"):
                note += f" (last stopped: {rec['last_stop']['reason']})"
        elif status == "held":
            note = "held by `mtdis stop`; `mtdis reset` to release"
        elif status in ("done", "failed"):
            note = f"finished {d.fmt_time(rec.get('finished_at'))}"
        print(f"{tid:<24}{status:<10}{rec.get('runs', 0):>5}  {cpu:>5}  {ram:>7}  "
              f"{d.fmt_time(rec.get('started_at')):<12}{'-' if code is None else code:>5}  {note}")


# ---------------------------------------------------------------- queue editing

def describe_requirements(task):
    reqs = d.reqs_of(task)
    parts = []
    if reqs.get("needs_internet"):
        parts.append("internet")
    if reqs.get("needs_ac_power"):
        parts.append("AC power")
    if reqs.get("needs_gpu"):
        parts.append("GPU")
    if "min_ram_gb" in reqs:
        parts.append(f"{reqs['min_ram_gb']}GB RAM")
    if "max_cpu_percent" in reqs:
        parts.append(f"CPU <= {reqs['max_cpu_percent']}%")
    if "expected_cpu_percent" in reqs:
        parts.append(f"uses ~{reqs['expected_cpu_percent']}% CPU")
    return ", ".join(parts) or "none"


def cmd_add(args):
    if not d.valid_task_id(args.id):
        sys.exit(f"Task id {args.id!r} may only contain letters, digits, '_' and '-'.")
    # One argument is used as written (pipes, &&, etc. work if you quote it);
    # several arguments are re-quoted so they run exactly as you typed them.
    cmd = args.command[0] if len(args.command) == 1 else shlex.join(args.command)

    reqs = {}
    if args.internet:
        reqs["needs_internet"] = True
    if args.ac:
        reqs["needs_ac_power"] = True
    if args.gpu:
        reqs["needs_gpu"] = True
    if args.ram is not None:
        reqs["min_ram_gb"] = args.ram
    if args.max_cpu is not None:
        reqs["max_cpu_percent"] = args.max_cpu
    if args.cpu is not None:
        reqs["expected_cpu_percent"] = args.cpu

    task = {"id": args.id, "cmd": cmd, "cwd": os.path.abspath(os.path.expanduser(args.cwd))}
    if args.terminal:
        task["open_terminal"] = True
    if reqs:
        task["requirements"] = reqs

    with d.state_lock(wait=True):
        data = d.read_queue()
        existing = [i for i, t in enumerate(data["tasks"]) if t.get("id") == args.id]
        if existing and not args.replace:
            sys.exit(f"A task named {args.id!r} already exists. Use --replace to overwrite it.")
        if existing:
            data["tasks"][existing[0]] = task
        else:
            data["tasks"].append(task)
        d.write_queue(data)
        if existing:  # a replaced task starts over with the new definition
            state = d.load_state()
            rec = state.setdefault("tasks", {}).get(args.id)
            if rec and rec["status"] != "running":
                state["tasks"][args.id] = {"status": "pending", "runs": rec.get("runs", 0)}
                d.save_state(state)

    print(f"{'Replaced' if existing else 'Added'} {args.id}")
    print(f"  command:      {cmd}")
    print(f"  directory:    {task['cwd']}")
    print(f"  requirements: {describe_requirements(task)}")
    if existing and d.session_alive(args.id):
        print("  It's still running the old command; `mtdis stop` and `mtdis reset` it to restart.")
    if args.no_run:
        print("It will start at the next check.")
    else:
        d.dispatch(reason="add")


def cmd_remove(args):
    settings, _ = d.load_queue()
    with d.state_lock(wait=True):
        data = d.read_queue()
        state = d.load_state()
        recs = state.setdefault("tasks", {})
        for tid in args.ids:
            if not any(t.get("id") == tid for t in data["tasks"]):
                print(f"No task named {tid}")
                continue
            if d.session_alive(tid):
                print(f"Stopping {tid}...")
                d.stop_task(tid, settings)
            data["tasks"] = [t for t in data["tasks"] if t.get("id") != tid]
            recs.pop(tid, None)
            d.remove_run_files(tid, all_files=True)
            print(f"Removed {tid} (its log stays at {d.log_file(tid)})")
        d.write_queue(data)
        d.save_state(state)


def cmd_edit(args):
    d.read_queue()  # creates the file if needed
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        subprocess.run(["open", "-t", d.QUEUE_FILE])
        print(f"Opened {d.QUEUE_FILE} in your text editor.")
        return
    subprocess.run(shlex.split(editor) + [d.QUEUE_FILE])
    try:
        d.load_queue()
    except d.QueueError as e:
        sys.exit(f"Warning: {e}\nRun `mtdis edit` again to fix it.")


def cmd_list(args):
    _, tasks = d.load_queue()
    if not tasks:
        print("No tasks. Add one with: mtdis add <id> -- <command>")
    for task in tasks:
        print(f"{task['id']}")
        print(f"  command:      {task['cmd']}")
        print(f"  directory:    {task.get('cwd', '~')}")
        print(f"  requirements: {describe_requirements(task)}"
              + ("  [opens terminal]" if task.get("open_terminal") else ""))
    print(f"\nQueue file: {d.QUEUE_FILE}")


# ---------------------------------------------------------------- control

def cmd_run(args):
    d.dispatch(args.internet, args.ac, args.reason)


def cmd_stop(args):
    settings, tasks = d.load_queue()
    ids = {t["id"] for t in tasks}
    with d.state_lock(wait=True):
        state = d.load_state()
        recs = state.setdefault("tasks", {})
        for tid in args.ids:
            if tid not in ids:
                print(f"No task named {tid}")
                continue
            d.stop_task(tid, settings)
            rec = recs.setdefault(tid, {"runs": 0})
            rec.update(status="held", last_stop={"at": time.time(), "reason": "stopped by user"})
            print(f"Stopped {tid}. It won't start again until you run: mtdis reset {tid}")
        d.save_state(state)


def cmd_reset(args):
    _, tasks = d.load_queue()
    with d.state_lock(wait=True):
        state = d.load_state()
        recs = state.setdefault("tasks", {})
        ids = [t["id"] for t in tasks] if args.all else args.ids
        if not ids:
            sys.exit("Name one or more tasks, or pass --all.")
        for tid in ids:
            rec = recs.get(tid)
            if rec is None:
                if not args.all:
                    print(f"{tid} has no history to reset.")
                continue
            if rec["status"] == "running":
                if not args.all:
                    print(f"{tid} is running; `mtdis stop {tid}` first.")
                continue
            d.remove_run_files(tid)
            recs[tid] = {"status": "pending", "runs": rec.get("runs", 0)}
            print(f"{tid}: back in the queue")
        d.save_state(state)


def cmd_logs(args):
    path = d.log_file(args.id)
    if not os.path.exists(path):
        sys.exit(f"No log for {args.id} yet.")
    tail = ["tail", "-n", str(args.lines)] + (["-f"] if args.follow else []) + [path]
    os.execvp("tail", tail)


def cmd_attach(args):
    if not d.session_alive(args.id):
        sys.exit(f"{args.id} isn't running.")
    os.execvp("tmux", ["tmux", "attach", "-t", f"={d.session_name(args.id)}"])


# ---------------------------------------------------------------- setup

def stable_path(path):
    """Homebrew installs into a versioned Cellar folder that changes on every
    upgrade. Point at the unversioned /opt link instead when there is one."""
    m = re.match(r"^(.*)/Cellar/([^/]+)/[^/]+/(.*)$", path)
    if m:
        alt = f"{m[1]}/opt/{m[2]}/{m[3]}"
        if os.path.exists(alt):
            return alt
    return path


def mtdis_command():
    return shutil.which("mtdis") or stable_path(os.path.abspath(sys.argv[0]))


def strip_hook_lines(text):
    keep = [line for line in text.splitlines()
            if 'require("mtdis")' not in line and not any(m in line for m in LEGACY_MARKERS)
            and line.strip() != "-- mtdis"]
    return "\n".join(keep).rstrip() + "\n" if any(s.strip() for s in keep) else ""


def cmd_setup(args):
    init = open(HS_INIT).read() if os.path.exists(HS_INIT) else ""
    cleaned = strip_hook_lines(init)
    if os.path.islink(LEGACY_MODULE):
        os.remove(LEGACY_MODULE)

    if args.remove:
        if os.path.exists(HS_INIT):
            with open(HS_INIT, "w") as f:
                f.write(cleaned)
        if os.path.islink(HS_MODULE) or os.path.exists(HS_MODULE):
            os.remove(HS_MODULE)
        print("Disconnected mtdis from Hammerspoon. Reload Hammerspoon's config to apply.")
        print("Tasks already running keep going; stop them with `mtdis stop <id>`.")
        return

    if not shutil.which("tmux"):
        print("Warning: tmux isn't installed. Install it with: brew install tmux")

    os.makedirs(HS_DIR, exist_ok=True)
    lua = stable_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mtdis.lua"))
    if os.path.islink(HS_MODULE) or os.path.exists(HS_MODULE):
        os.remove(HS_MODULE)
    os.symlink(lua, HS_MODULE)

    cmd = mtdis_command()
    opts = f'command = "{cmd}"'
    if args.interval is not None:
        opts += f", interval = {args.interval}"
    hook = f'-- mtdis\nrequire("mtdis").start({{ {opts} }})\n'
    with open(HS_INIT, "w") as f:
        f.write((cleaned + "\n" if cleaned else "") + hook)

    d.read_queue()  # creates an empty queue on first setup
    print(f"Connected mtdis to Hammerspoon ({HS_INIT}).")
    print(f"Tasks are kept in {d.QUEUE_FILE}")

    if not os.path.isdir("/Applications/Hammerspoon.app"):
        print("\nHammerspoon isn't installed. Install it, then open it:\n  brew install --cask hammerspoon")
    elif subprocess.run(["pgrep", "-x", "Hammerspoon"], capture_output=True).returncode != 0:
        subprocess.run(["open", "-a", "Hammerspoon"])
        print("\nStarted Hammerspoon. If macOS asks, allow it Accessibility access.")
    else:
        print("\nHammerspoon is running: choose Reload Config from its menu bar icon to apply.")


# ---------------------------------------------------------------- main

def parse_bool(value):
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mtdis",
        description="Run background tasks on your Mac only when internet, power, CPU, RAM and GPU allow.",
        epilog="Run `mtdis <command> -h` for details on a command. With no command, shows status.",
    )
    parser.add_argument("--version", action="version", version=f"mtdis {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("status", help="show tasks, their state and live CPU/RAM").set_defaults(func=cmd_status)
    sub.add_parser("list", aliases=["ls"], help="show each task's command and requirements").set_defaults(func=cmd_list)

    add = sub.add_parser(
        "add", help="add a task",
        description="Add a task to the queue. Put the command after --, or quote it as one argument.",
        epilog='examples:\n'
               '  mtdis add backup --ac --ram 0.5 -- rsync -a ~/Pictures/ /Volumes/Backup/Pictures/\n'
               '  mtdis add scrape --internet --cwd ~/scraper "python3 scrape.py | tee out.txt"\n'
               '  mtdis add chat --ac --gpu --ram 8 --terminal -- ollama run llama3',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add.add_argument("id", help="task name: letters, digits, _ and -")
    add.add_argument("command", nargs="+", help="command to run")
    add.add_argument("--internet", action="store_true", help="only run while online")
    add.add_argument("--ac", action="store_true", help="only run on AC power")
    add.add_argument("--gpu", action="store_true", help="needs a Metal GPU")
    add.add_argument("--ram", type=float, metavar="GB", help="RAM the task needs")
    add.add_argument("--max-cpu", type=float, metavar="PCT", help="only start while CPU load is at most PCT%%")
    add.add_argument("--cpu", type=float, metavar="PCT", help="CPU the task uses, %% of the whole machine")
    add.add_argument("--cwd", default=os.getcwd(), metavar="DIR", help="directory to run in (default: here)")
    add.add_argument("--terminal", action="store_true", help="open a terminal window for interactive use")
    add.add_argument("--replace", action="store_true", help="overwrite a task with the same name")
    add.add_argument("--no-run", action="store_true", help="don't run a check right after adding")
    add.set_defaults(func=cmd_add)

    rm = sub.add_parser("remove", aliases=["rm"], help="stop and delete tasks")
    rm.add_argument("ids", nargs="+", metavar="id")
    rm.set_defaults(func=cmd_remove)

    sub.add_parser("edit", help="open the queue file in your editor").set_defaults(func=cmd_edit)

    run = sub.add_parser("run", help="check conditions now; start, stop and record tasks")
    run.add_argument("--internet", type=parse_bool, metavar="true|false", help="detected if omitted")
    run.add_argument("--ac", type=parse_bool, metavar="true|false", help="detected if omitted")
    run.add_argument("--reason", default="manual", help="label for the log")
    run.set_defaults(func=cmd_run)

    stop = sub.add_parser("stop", help="stop tasks and hold them until reset")
    stop.add_argument("ids", nargs="+", metavar="id")
    stop.set_defaults(func=cmd_stop)

    reset = sub.add_parser("reset", help="put done, failed or held tasks back in the queue")
    reset.add_argument("ids", nargs="*", metavar="id")
    reset.add_argument("--all", action="store_true", help="every task that isn't running")
    reset.set_defaults(func=cmd_reset)

    logs = sub.add_parser("logs", help="show a task's output")
    logs.add_argument("id")
    logs.add_argument("-f", "--follow", action="store_true", help="keep printing new output")
    logs.add_argument("-n", "--lines", type=int, default=50, help="lines to show (default 50)")
    logs.set_defaults(func=cmd_logs)

    attach = sub.add_parser("attach", help="open a running task's terminal (detach: Ctrl-b d)")
    attach.add_argument("id")
    attach.set_defaults(func=cmd_attach)

    setup = sub.add_parser("setup", help="connect mtdis to Hammerspoon")
    setup.add_argument("--interval", type=int, metavar="SECONDS", help="time between checks (default 60)")
    setup.add_argument("--remove", action="store_true", help="disconnect from Hammerspoon")
    setup.set_defaults(func=cmd_setup)
    return parser


def main():
    args = build_parser().parse_args()
    func = getattr(args, "func", cmd_status)
    try:
        func(args)
    except d.QueueError as e:
        sys.exit(f"Error: {e}\nFix it with `mtdis edit`.")
    except KeyboardInterrupt:
        sys.exit(130)
