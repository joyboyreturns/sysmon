# sysmon

A live Linux terminal app with six tabs, usage bars and rolling history charts.
Python standard library only at runtime; Docker CLI is optional.

## Install

Requires Linux, Python 3.9+ with curses, and a UTF-8 terminal.

```sh
git clone https://github.com/joyboyreturns/sysmon.git
cd sysmon
mkdir -p ~/.local/bin
install -m 755 sysmon/app.py ~/.local/bin/sysmon
# Ensure ~/.local/bin is on PATH, then:
sysmon
```

Alternatively, install the package in your own virtual environment with `pip install .`.
Run without installing using `python3 -m sysmon` from this directory.

```sh
sysmon       # refresh every second
sysmon 2     # refresh every two seconds
sysmon -1    # one text snapshot
sysmon --help
```

## Tabs

1. **Overview** — hardware summary, uptime, load, CPU/RAM histories, disk and container memory totals.
2. **CPU** — every logical CPU with usage, physical core, socket, sibling threads, core type where the kernel reports it, current frequency, governor and sensor temperatures.
3. **Memory** — RAM, available/free memory, caches, kernel memory, swap, and CPU/memory/I/O pressure.
4. **Docker** — all containers returned by `docker stats --all`, CPU, actual RAM usage/limit, memory percentage, PIDs, cumulative network/block I/O and a detail view with image, state, status, published ports and creation time.
5. **Processes** — live interval CPU, resident RAM, PID and state; sortable and searchable by name.
6. **Storage/Net** — mounted device filesystems and per-interface receive/transmit rates and totals.

## Controls

| Key | Action |
| --- | --- |
| `1`–`6`, Tab / Shift-Tab, Left / Right | Change tabs |
| Up / Down, `j` / `k` | Scroll; select Docker container |
| Page Up / Page Down, Home / End | Navigate long lists |
| Enter | Docker detail view |
| `/`, then Enter | Filter Docker/process names |
| Esc | Clear filter / close details |
| `c`, `m`, `n` | Sort by CPU, memory, name |
| Space | Pause host sampling/display values; Docker collection continues |
| `+` / `-` | Faster / slower refresh |
| `?` | Help |
| `q`, Ctrl-C | Exit and restore terminal |

## Metric notes

- Logical CPU usage is 0–100%. Process and Docker CPU can exceed 100% (100% = one logical CPU); their bars saturate at 100% while the number retains the full value.
- RAM used is total minus available. Memory breakdown categories can overlap.
- Docker RAM is the Docker CLI working-set figure and its configured limit. Percentage is relative to that limit. Docker polling runs separately, typically every 3–4 seconds; the tab shows sample age. Stopped containers may report zero statistics.
- Intel hybrid core labels use Linux's reported topology type. Unsupported/unreported types are marked accordingly; sysmon does not infer core types from frequency.
- CPU topology, clock and temperatures depend on kernel/sysfs availability. No root needed for host monitoring. Docker needs existing daemon access.
- Network bars scale against the current peak interface rate; counters include virtual interfaces, so summing them may double-count traffic.
- Read-only monitoring: no process termination, container control, command-line/environment capture, or external telemetry.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Licensed under MIT.
