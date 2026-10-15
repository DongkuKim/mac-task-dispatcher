# mac-task-dispatcher

Run background jobs on your Mac only when the conditions are right, and stop them when they aren't.

You list tasks in `queue.json` with their requirements (internet, AC power, GPU, free RAM, CPU load). [Hammerspoon](https://www.hammerspoon.org) watches for network changes, power changes and wake from sleep, and also checks once a minute. Each check runs `dispatcher.py`, which starts tasks that fit, stops tasks that no longer fit, and records tasks that have finished. Tasks run in parallel, each in its own `tmux` session.

Typical uses: backups that should only run on AC power, scrapers or API jobs that need internet, and heavy local jobs (model runs, video encodes, builds) that shouldn't start while the Mac is short on memory.

## How it works

```
 Hammerspoon                         dispatcher.py run                      tmux
 ───────────                         ─────────────────                      ────
 network change ─┐                   1. record finished tasks (done/failed)
 power change   ─┼─▶ --internet ──▶  2. stop tasks that lost internet/power  ──▶ mtd_<task id>
 wake from sleep─┤    --ac           3. stop newest tasks if RAM runs low         (one session
 every 60 s     ─┘                   4. start pending tasks that fit              per task)
```

- **Parallel, with resource tracking.** Every run measures the CPU and RAM used by each running task's whole process tree. New tasks start only if they fit in what's left.
- **Room for tasks that grow.** A task's `min_ram_gb` stays reserved until the task actually uses that much. A job that grows slowly won't be crowded out by tasks started after it.
- **Memory protection.** If free RAM drops below `reserve_ram_gb`, the most recently started tasks are stopped until it recovers. They go back in the queue and wait `restart_cooldown_s` before restarting. CPU load alone never stops a running task: it slows things down but doesn't crash the Mac.
- **Finished tasks are tracked.** Each task's exit code is recorded. A task that exits 0 is `done`, anything else is `failed`, and neither starts again until you `reset` it.
- **Low battery cost.** Hammerspoon only reacts to events; resources are sampled for about one second per run.

## Requirements

- macOS
- [Homebrew](https://brew.sh), then:

```bash
brew install --cask hammerspoon
```
```bash
brew install tmux
```

- Python 3.9 or newer (`python3`)

## Installation

```bash
git clone https://github.com/DongkuKim/mac-task-dispatcher.git
```
```bash
cd mac-task-dispatcher && ./install.sh
```

`install.sh`:

1. creates `.venv` and installs `psutil` into it,
2. copies `queue.example.json` to `queue.json` (if you don't have one yet),
3. links `hammerspoon/mac_task_dispatcher.lua` into `~/.hammerspoon/`,
4. adds this line to `~/.hammerspoon/init.lua`:

```lua
require("mac_task_dispatcher").start({ repo = "/path/to/mac-task-dispatcher" })
```

Then open Hammerspoon (grant it Accessibility access when asked) and choose **Reload Config** from its menu bar icon. The dispatcher runs right away and then on every event.

Optional `start` settings: `interval` (seconds between timer checks, default `60`, `0` turns the timer off) and `python` (path to the interpreter, default `<repo>/.venv/bin/python`).

## The task queue

`queue.json` has global `settings` and a list of `tasks`. Tasks are considered in order. If one can't start, later ones that fit still can.

```json
{
  "settings": { "max_parallel": 3, "reserve_ram_gb": 2.0 },
  "tasks": [
    {
      "id": "photo_backup",
      "cmd": "rsync -a ~/Pictures/ /Volumes/Backup/Pictures/",
      "requirements": { "needs_ac_power": true, "min_ram_gb": 0.5, "max_cpu_percent": 70 }
    },
    {
      "id": "api_scraper",
      "cmd": "python3 scraper.py",
      "cwd": "~/projects/scraper",
      "requirements": { "needs_internet": true, "min_ram_gb": 1.0, "expected_cpu_percent": 10 }
    }
  ]
}
```

### Task fields

| Field | Meaning |
| --- | --- |
| `id` | Unique name: letters, digits, `_` and `-`. The tmux session is `mtd_<id>`. |
| `cmd` | Shell command to run. |
| `cwd` | Directory to run it in. Default: the folder containing `queue.json`. |
| `open_terminal` | `true` to open a terminal window attached to the task when it starts. See [Interactive tasks](#interactive-tasks). |
| `requirements` | Conditions below. Leave any out to ignore it. |

### Requirements

| Requirement | Checked | Meaning |
| --- | --- | --- |
| `needs_internet` | always | Only run while online. Stopped when the connection drops. |
| `needs_ac_power` | always | Only run on AC power. Stopped when you unplug. |
| `needs_gpu` | before start | The Mac must have a Metal GPU (see [Limitations](#limitations)). |
| `min_ram_gb` | before start | RAM the task needs. It must fit in free RAM minus the reserve minus what running tasks still have reserved. |
| `max_cpu_percent` | before start | Start only while total CPU load (including expected load from running tasks) is at or below this. |
| `expected_cpu_percent` | before start | CPU this task will use, as a share of the whole machine (100 = every core busy). Reserved like `min_ram_gb`, so tasks started in the same run don't overload the CPU together. |

### Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `max_parallel` | `4` | Maximum tasks running at once. |
| `reserve_ram_gb` | `2.0` | RAM always kept free for you and macOS. Falling below it stops the newest tasks. |
| `restart_cooldown_s` | `300` | Wait before restarting a task that was stopped for low memory. |
| `stop_grace_s` | `10` | Time a task gets to exit after Ctrl-C before it is terminated. |
| `cpu_sample_s` | `1.0` | How long CPU is sampled each run. |
| `terminal_app` | `"Terminal"` | App used for `open_terminal` tasks, for example `"iTerm"`. |

## Commands

Run these with the repo's Python, `.venv/bin/python dispatcher.py <command>`:

| Command | What it does |
| --- | --- |
| `status` | Shows every task, its state, live CPU/RAM, last exit code and what a pending task is waiting for. |
| `run [--internet true\|false] [--ac true\|false]` | One dispatch pass. Hammerspoon calls this; any value left out is detected. |
| `stop <id>...` | Stops tasks and holds them, so they don't restart. |
| `reset <id>...` / `reset --all` | Puts `done`, `failed` or held tasks back in the queue. |

Task states: `pending` → `running` → `done` / `failed`. Tasks stopped for missing internet/power or low memory go back to `pending`. `held` means stopped by you.

To watch a running task live: `tmux attach -t mtd_<id>` (detach with `Ctrl-b d`).

Logs:

- `state/logs/<id>.log`: each task's output, with start and exit lines
- `state/dispatcher.log`: every decision the dispatcher made

## Interactive tasks

The dispatcher is built for unattended jobs. A command that waits for your input (a chat REPL like `ollama run llama3`, a prompt asking for confirmation) would otherwise sit in a hidden tmux session that nobody sees.

For tasks like that, set `"open_terminal": true`. When the task starts, a terminal window attached to its tmux session opens so you can use it. Closing the window doesn't stop the task. Reattach with `tmux attach -t mtd_<id>`. When the program exits, the window shows the exit code and waits for Enter.

Keep in mind that the task can still be stopped when its conditions change (for example, unplugging stops a `needs_ac_power` task), which ends whatever you were doing. If you only need a background service, run it non-interactively instead, for example `ollama serve` rather than `ollama run`.

## Limitations

- **Stopping is not pausing.** A stopped task gets Ctrl-C (then SIGTERM/SIGKILL if it doesn't exit) and starts from the beginning next time. Tasks that should pick up where they left off need to handle that themselves (for example `rsync`, or a script that checks what it already did).
- **The GPU check only tells whether a GPU exists.** `needs_gpu` checks for a Metal-capable GPU, which nearly every recent Mac has. It does not measure GPU load: reading that on Apple Silicon needs `sudo powermetrics`, which a background tool shouldn't require.
- **Apple Silicon shares memory between CPU and GPU.** GPU memory comes out of the same RAM, so a model that needs 8 GB of VRAM should use `"min_ram_gb": 8`.
- **RAM per task is an upper estimate.** It adds up the resident memory of every process in the task, so memory shared between them is counted more than once. Mistakes lean toward caution.
- **Checks happen every `interval` seconds.** Memory that spikes faster than that can still run out before the dispatcher reacts. Keep `reserve_ram_gb` large enough for the spikes you expect.

## Uninstall

1. Remove the `mac_task_dispatcher` line from `~/.hammerspoon/init.lua` and delete `~/.hammerspoon/mac_task_dispatcher.lua`.
2. Stop running tasks: `tmux ls`, then `tmux kill-session -t mtd_<id>`.
3. Delete the repo folder.

## License

MIT
