# mtdis: mac task dispatcher

Run background jobs on your Mac only when the conditions are right, and stop them when they aren't.

```bash
mtdis add backup --ac --ram 0.5 -- rsync -a ~/Pictures/ /Volumes/Backup/Pictures/
mtdis add scrape --internet --cwd ~/scraper -- python3 scrape.py
mtdis status
```

You add tasks with their requirements (internet, AC power, GPU, free RAM, CPU load). [Hammerspoon](https://www.hammerspoon.org) watches for network changes, power changes and wake from sleep, and also checks once a minute. Each check starts tasks that fit, stops tasks that no longer fit, and records tasks that have finished. Tasks run in parallel, each in its own `tmux` session.

Typical uses: backups that should only run on AC power, scrapers or API jobs that need internet, and heavy local jobs (model runs, video encodes, builds) that shouldn't start while the Mac is short on memory.

## Install

```bash
brew install dongkukim/tap/mtdis
```
```bash
brew install --cask hammerspoon
```
```bash
mtdis setup
```

Homebrew installs `tmux` and Python automatically. `mtdis setup` connects mtdis to Hammerspoon (it adds one line to `~/.hammerspoon/init.lua`) and opens Hammerspoon. Allow it Accessibility access when macOS asks. If Hammerspoon was already open, choose **Reload Config** from its menu bar icon.

To update: `brew upgrade mtdis`. To uninstall: `mtdis setup --remove`, then `brew uninstall mtdis`.

<details>
<summary>Without Homebrew</summary>

```bash
pipx install git+https://github.com/DongkuKim/mac-task-dispatcher.git
```
```bash
brew install tmux
```

Then install Hammerspoon and run `mtdis setup` as above.
</details>

## Commands

| Command | What it does |
| --- | --- |
| `mtdis` / `mtdis status` | Every task, its state, live CPU/RAM, last exit code, and what a waiting task is waiting for. |
| `mtdis add <id> [options] -- <command>` | Add a task. It runs in the directory you're in, unless you pass `--cwd`. |
| `mtdis list` | Each task's command, directory and requirements. |
| `mtdis remove <id>...` | Stop tasks (if running) and delete them. |
| `mtdis edit` | Open the queue file in `$EDITOR` to change tasks or settings. |
| `mtdis logs <id> [-f]` | A task's output (`-f` keeps following it). |
| `mtdis attach <id>` | Open a running task's terminal. Detach with `Ctrl-b d`. |
| `mtdis stop <id>...` | Stop tasks and hold them, so they don't restart. |
| `mtdis reset <id>...` / `--all` | Put done, failed or held tasks back in the queue. |
| `mtdis run` | Check conditions now instead of waiting for the next check. |
| `mtdis setup [--interval S]` / `--remove` | Connect to or disconnect from Hammerspoon. |

### Adding tasks

```bash
mtdis add <id> [options] -- <command and its arguments>
mtdis add <id> [options] "<command with | pipes, && or ;>"
```

| Option | Requirement |
| --- | --- |
| `--internet` | Only run while online. Stopped when the connection drops. |
| `--ac` | Only run on AC power. Stopped when you unplug. |
| `--gpu` | The Mac must have a Metal GPU (see [Limitations](#limitations)). |
| `--ram GB` | RAM the task needs. Checked before it starts, and reserved for it while it runs. |
| `--max-cpu PCT` | Start only while total CPU load is at or below `PCT`%. |
| `--cpu PCT` | CPU the task uses, as a share of the whole machine (100 = every core busy). Reserved like `--ram`. |
| `--cwd DIR` | Directory to run in. Default: the current directory. |
| `--terminal` | Open a terminal window for the task. See [Interactive tasks](#interactive-tasks). |
| `--replace` | Overwrite an existing task with the same id. |
| `--no-run` | Don't run a check right after adding. |

Adding a task runs a check right away, so it starts immediately if it can.

Task states: `pending` → `running` → `done` (exit 0) or `failed`. Tasks stopped because internet, power or memory ran out go back to `pending` and restart when conditions allow. `held` means you stopped it. `done` and `failed` tasks don't run again until you `mtdis reset` them.

## How it decides

```
 Hammerspoon                       mtdis run                                tmux
 ───────────                       ─────────                                ────
 network change ─┐                 1. record finished tasks (done/failed)
 power change   ─┼─▶ --internet ─▶ 2. stop tasks that lost internet/power ─▶ mtdis_<id>
 wake from sleep─┤    --ac         3. stop newest tasks if RAM runs low        (one session
 every 60 s     ─┘                 4. start pending tasks that fit             per task)
```

- **Parallel, with resource tracking.** Every check measures the CPU and RAM used by each running task's whole process tree. New tasks start only if they fit in what's left.
- **Room for tasks that grow.** A task's `--ram` stays reserved until the task actually uses that much. A job that grows slowly won't be crowded out by tasks started after it.
- **Memory protection.** If free RAM drops below the reserve (2 GB by default), the most recently started tasks are stopped until it recovers. They go back in the queue after a cooldown. CPU load alone never stops a running task: it slows things down but doesn't crash the Mac.
- **Low battery cost.** Hammerspoon only reacts to events; resources are sampled for about one second per check.

## Files and settings

| Path | Contents |
| --- | --- |
| `~/.config/mtdis/queue.json` | Your tasks and settings (`mtdis edit` opens it). |
| `~/.local/state/mtdis/logs/<id>.log` | Each task's output (`mtdis logs <id>`). |
| `~/.local/state/mtdis/dispatcher.log` | Every decision mtdis made. |

Settings go in the `settings` object of `queue.json`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `max_parallel` | `4` | Maximum tasks running at once. |
| `reserve_ram_gb` | `2.0` | RAM always kept free for you and macOS. Falling below it stops the newest tasks. |
| `restart_cooldown_s` | `300` | Wait before restarting a task that was stopped for low memory. |
| `stop_grace_s` | `10` | Time a task gets to exit after Ctrl-C before it is terminated. |
| `cpu_sample_s` | `1.0` | How long CPU is sampled each check. |
| `terminal_app` | `"Terminal"` | App used for `--terminal` tasks, for example `"iTerm"`. |

[`queue.example.json`](queue.example.json) shows the full file format.

## Interactive tasks

mtdis is built for unattended jobs. A command that waits for your input (a chat REPL like `ollama run llama3`, a prompt asking for confirmation) would otherwise sit in a hidden tmux session that nobody sees.

For tasks like that, use `--terminal`. When the task starts, a terminal window attached to it opens so you can use it. Closing the window doesn't stop the task: `mtdis attach <id>` reopens it. When the program exits, the window shows the exit code and waits for Enter.

The task can still be stopped when its conditions change (for example, unplugging stops an `--ac` task), which ends whatever you were doing. If you only need a background service, run it non-interactively instead, for example `ollama serve` rather than `ollama run`.

## Limitations

- **Stopping is not pausing.** A stopped task gets Ctrl-C (then SIGTERM/SIGKILL if it doesn't exit) and starts from the beginning next time. Tasks that should pick up where they left off need to handle that themselves (for example `rsync`, or a script that checks what it already did).
- **The GPU check only tells whether a GPU exists.** `--gpu` checks for a Metal-capable GPU, which nearly every recent Mac has. It does not measure GPU load: reading that on Apple Silicon needs `sudo powermetrics`, which a background tool shouldn't require.
- **Apple Silicon shares memory between CPU and GPU.** GPU memory comes out of the same RAM, so a model that needs 8 GB of VRAM should use `--ram 8`.
- **RAM per task is an upper estimate.** It adds up the resident memory of every process in the task, so memory shared between them is counted more than once. Mistakes lean toward caution.
- **Checks happen once a minute.** Memory that spikes faster than that can still run out before mtdis reacts. Keep the reserve large enough for the spikes you expect, or check more often with `mtdis setup --interval 30`.

## License

MIT
