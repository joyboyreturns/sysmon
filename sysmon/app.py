#!/usr/bin/env python3
"""Live, dependency-free Linux dashboard. sysmon [seconds=1] | sysmon -1."""
import argparse
import glob
import platform
import re
import struct
import collections
import curses
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time


def read_cpu():
    result = {}
    with open('/proc/stat') as f:
        for line in f:
            fields = line.split()
            if fields and fields[0].startswith('cpu'):
                v = list(map(int, fields[1:9]))
                result[fields[0]] = (sum(v), v[3] + v[4])
    return result


def memory():
    with open('/proc/meminfo') as f:
        m = {p[0].rstrip(':'): int(p[1]) for p in map(str.split, f)}
    return m


def processes(previous, elapsed):
    current, rows = {}, []
    hz = os.sysconf('SC_CLK_TCK')
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open('/proc/' + pid + '/stat') as f:
                raw = f.read()
            end = raw.rindex(')')
            v = raw[end + 2:].split()
            ticks, rss = int(v[11]) + int(v[12]), int(v[21]) * os.sysconf('SC_PAGE_SIZE')
            key = (pid, v[19])
            current[key] = ticks
            pct = max(0, ticks - previous.get(key, ticks)) / hz / elapsed * 100
            rows.append((pct, rss, raw[raw.index('(') + 1:end], int(pid), v[0]))
        except (OSError, ValueError, IndexError):
            pass
    return current, sorted(rows, reverse=True)


class Monitor:
    def __init__(self):
        self.hardware = hardware()
        self.net_prev = network()
        self.net = {}
        self.prev = read_cpu()
        self.procs, _ = processes({}, 1)
        self.last = time.monotonic()
        self.history = collections.deque(maxlen=240)
        self.ram_history = collections.deque(maxlen=240)
        self.containers = []
        self.docker_status = 'Sampling containers…'
        self.docker_updated = None
        self.docker_metadata = {}
        self.docker_metadata_updated = None
        if shutil.which('docker'):
            threading.Thread(target=self.docker_loop, daemon=True).start()
        else:
            self.docker_status = 'Docker is not installed'

    def docker_error(self, message):
        previous = '; showing last sample' if self.docker_updated is not None else ''
        self.docker_status = message + previous + '; retrying…'

    def docker_loop(self):
        while True:
            try:
                self.poll_docker_stats()
            except subprocess.TimeoutExpired:
                self.docker_error('Docker stats timed out (30s)')
            except OSError:
                self.docker_error('Cannot run Docker')
            except (ValueError, KeyError, TypeError):
                self.docker_error('Invalid Docker stats response')
            time.sleep(2)

    def merge_docker_metadata(self, rows):
        for row in rows:
            details = self.docker_metadata.get(row['Name'], {})
            for field in ('Image', 'State', 'Status', 'Ports', 'CreatedAt'):
                row[field] = details.get(field, 'unavailable')

    def poll_docker_stats(self):
        p = subprocess.run(['docker', 'stats', '--all', '--no-stream', '--format', '{{json .}}'],
                           capture_output=True, text=True, timeout=30)
        if p.returncode:
            error = p.stderr.strip().splitlines()
            self.docker_error((error[0][:100] if error else 'Docker stats failed'))
            return
        rows = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
        for row in rows:
            if not isinstance(row, dict) or not row.get('Name'):
                raise ValueError('Docker stats row has no name')
            for field in ('CPUPerc', 'MemPerc', 'MemUsage', 'NetIO', 'BlockIO', 'PIDs'):
                if not isinstance(row.get(field), str):
                    row[field] = '--'
        self.merge_docker_metadata(rows)
        # Publish metrics before fetching optional details. Slow/failed `ps`
        # requests must never discard a successful stats sample.
        self.containers = sorted(rows, key=lambda r: percent(r['MemPerc']), reverse=True)
        self.docker_updated = time.monotonic()
        self.docker_status = '%d containers (including stopped)' % len(rows)
        if self.docker_metadata_updated is not None and time.monotonic() - self.docker_metadata_updated < 15:
            return
        try:
            meta = subprocess.run(['docker', 'ps', '--all', '--no-trunc', '--format', '{{json .}}'],
                                  capture_output=True, text=True, timeout=5)
            if meta.returncode:
                self.docker_status += ' · details delayed'
                return
            details = [json.loads(line) for line in meta.stdout.splitlines() if line.strip()]
            self.docker_metadata = {item['Names']: item for item in details}
            self.docker_metadata_updated = time.monotonic()
            self.merge_docker_metadata(rows)
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            self.docker_status += ' · details delayed'

    def sample(self):
        now = time.monotonic()
        current = read_cpu()
        self.cpu = {}
        for name, (total, idle) in current.items():
            pt, pi = self.prev.get(name, (total, idle))
            dt = total - pt
            self.cpu[name] = max(0, min(100, 100 * (dt - idle + pi) / dt)) if dt else 0
        self.prev = current
        self.mem = memory()
        self.ram = 100 * (1 - self.mem['MemAvailable'] / self.mem['MemTotal'])
        self.history.append(self.cpu.get('cpu', 0))
        self.ram_history.append(self.ram)
        self.procs, self.top = processes(self.procs, max(.01, now - self.last))
        self.last = now
        self.disk = shutil.disk_usage('/')
        net = network()
        self.net = {k: (max(0, v[0] - self.net_prev.get(k, v)[0]) / max(.01, now - self.last_net),
                        max(0, v[1] - self.net_prev.get(k, v)[1]) / max(.01, now - self.last_net), *v)
                    for k, v in net.items()} if hasattr(self, 'last_net') else {}
        self.net_prev, self.last_net = net, now


def human(kib):
    return '%.1f GiB' % (kib / 1048576)


def read(path, fallback='unknown'):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return fallback


def cpu_in_list(cpu, listing):
    """Match a logical CPU against the kernel's comma-separated CPU ranges."""
    for part in listing.split(','):
        try:
            ends = list(map(int, part.strip().split('-')))
            if len(ends) == 1 and cpu == ends[0]:
                return True
            if len(ends) == 2 and ends[0] <= cpu <= ends[1]:
                return True
        except ValueError:
            continue
    return False


# ARM implementer/part identifiers, as published in util-linux lscpu-arm.c.
ARM_VENDORS = {0x41: 'Arm', 0x42: 'Broadcom', 0x43: 'Cavium', 0x46: 'Fujitsu',
               0x48: 'HiSilicon', 0x4e: 'NVIDIA', 0x50: 'APM', 0x51: 'Qualcomm',
               0x53: 'Samsung', 0x61: 'Apple', 0x69: 'Intel', 0xc0: 'Ampere'}
ARM_PARTS = {0xd03: 'Cortex-A53', 0xd04: 'Cortex-A35', 0xd05: 'Cortex-A55',
             0xd07: 'Cortex-A57', 0xd08: 'Cortex-A72', 0xd09: 'Cortex-A73',
             0xd0a: 'Cortex-A75', 0xd0b: 'Cortex-A76', 0xd0c: 'Neoverse-N1',
             0xd0d: 'Cortex-A77', 0xd40: 'Neoverse-V1', 0xd41: 'Cortex-A78',
             0xd44: 'Cortex-X1', 0xd46: 'Cortex-A510', 0xd47: 'Cortex-A710',
             0xd48: 'Cortex-X2', 0xd49: 'Neoverse-N2', 0xd4d: 'Cortex-A715',
             0xd4e: 'Cortex-X3', 0xd4f: 'Neoverse-V2', 0xd80: 'Cortex-A520',
             0xd81: 'Cortex-A720', 0xd82: 'Cortex-X4', 0xd84: 'Neoverse-V3',
             0xd85: 'Cortex-X925', 0xd87: 'Cortex-A725'}


def number(value, default=-1):
    try:
        return int(value, 0)
    except (ValueError, TypeError):
        return default


def cpuid(cpu, leaf):
    """Optional read-only CPUID, pinned to the requested logical CPU.

    Uses the kernel device when already accessible, or the optional cpuid and
    taskset utilities. No executable memory, privilege changes or dependencies.
    """
    try:
        with open('/dev/cpu/%s/cpuid' % cpu, 'rb', buffering=0) as f:
            return struct.unpack('<IIII', os.pread(f.fileno(), 16, leaf))
    except (OSError, ValueError, struct.error):
        pass
    if not shutil.which('cpuid') or not shutil.which('taskset'):
        return None
    try:
        proc = subprocess.run(['taskset', '-c', str(cpu), 'cpuid', '-1', '-r',
                               '-l', hex(leaf), '-s', '0'], capture_output=True,
                              text=True, timeout=1)
        if proc.returncode:
            return None
        regs = dict(re.findall(r'\b(eax|ebx|ecx|edx)=0x([0-9a-fA-F]+)', proc.stdout))
        return tuple(int(regs[r], 16) for r in ('eax', 'ebx', 'ecx', 'edx'))
    except (OSError, subprocess.TimeoutExpired, KeyError, ValueError):
        return None


def core_kind(info, cpu):
    vendor = info.get('vendor_id', '')
    if vendor == 'GenuineIntel':
        for pmu, label in [('cpu_core', 'Performance core (P-core)'),
                           ('cpu_atom', 'Efficiency core (E-core)')]:
            listing = read('/sys/bus/event_source/devices/' + pmu + '/cpus', '')
            if cpu_in_list(int(cpu), listing):
                return label
        if info.get('cpu family') == '6' and info.get('model') in ('165', '166'):
            return 'Standard Core (Comet Lake; no P/E split)'
        maximum = cpuid(cpu, 0)
        if maximum and maximum[0] >= 0x1a:
            regs = cpuid(cpu, 0x1a)
            if regs:
                label = {0x40: 'Performance core (P-core)',
                         0x20: 'Efficiency core (E-core)'}.get(regs[0] >> 24)
                if label:
                    return label
        return 'Intel core (class unreported)'
    if vendor in ('AuthenticAMD', 'HygonGenuine'):
        maximum = cpuid(cpu, 0x80000000)
        if maximum and maximum[0] >= 0x80000026:
            regs = cpuid(cpu, 0x80000026)
            if regs and (regs[1] & 0xffff) and (regs[0] & (1 << 30)):
                code = regs[1] >> 28
                return {0: 'Performance core (AMD)', 1: 'Efficiency / compact core (AMD)',
                        2: 'Low-power core (AMD)'}.get(code, 'AMD core (reported class %d)' % code)
        return ('AMD' if vendor == 'AuthenticAMD' else 'Hygon') + ' core (class unreported)'
    if 'CPU implementer' in info:
        impl, part = number(info['CPU implementer']), number(info.get('CPU part'))
        vendor = ARM_VENDORS.get(impl, 'ARM implementer ' + info['CPU implementer'])
        name = ARM_PARTS.get(part) if impl == 0x41 else None
        return '%s %s' % (vendor, name or 'core (part %s)' % info.get('CPU part', 'unreported'))
    if 'uarch' in info:
        return info['uarch'] + ' (RISC-V)'
    return 'Unknown (kernel does not report core class)'


def cpu_identity(info):
    vendor = info.get('vendor_id') or info.get('vendor')
    if not vendor and 'CPU implementer' in info:
        vendor = ARM_VENDORS.get(number(info['CPU implementer']), info['CPU implementer'])
    model = (info.get('model name') or info.get('Processor') or info.get('cpu model')
             or info.get('cpu') or info.get('uarch') or info.get('isa') or platform.machine())
    return vendor or 'unreported', model


def hardware():
    info = []
    for block in read('/proc/cpuinfo', '').split('\n\n'):
        d = dict(line.split(':', 1) for line in block.splitlines() if ':' in line)
        d = {k.strip(): v.strip() for k, v in d.items()}
        if 'processor' not in d:
            continue
        n = d['processor']
        base = '/sys/devices/system/cpu/cpu' + n
        kind = core_kind(d, n)
        vendor, model = cpu_identity(d)
        info.append(dict(id=n, model=model,
                         family=d.get('cpu family', d.get('CPU architecture', 'unreported')),
                         model_id=d.get('model', d.get('CPU part', 'unreported')),
                         capacity=read(base + '/cpu_capacity', 'unreported'),
                         vendor=vendor, core=read(base + '/topology/core_id'),
                         socket=read(base + '/topology/physical_package_id'),
                         siblings=read(base + '/topology/thread_siblings_list'), kind=kind,
                         cache=d.get('cache size', 'unknown'), base=base))
    return info


def network():
    result = {}
    for line in read('/proc/net/dev', '').splitlines()[2:]:
        if ':' in line:
            name, data = line.split(':', 1)
            v = data.split()
            result[name.strip()] = (int(v[0]), int(v[8]))
    return result


def bytesize(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(n) < 1024 or unit == 'TiB':
            return '%.1f %s' % (n, unit)
        n /= 1024


def percent(value):
    try:
        return float(value.rstrip('%'))
    except (ValueError, AttributeError):
        return 0


def memory_bytes(value):
    """Docker's human formatted used/limit values, used for numeric sorting."""
    import re
    match = re.match(r'([\d.]+)\s*([A-Za-z]+)', value)
    if not match:
        return 0
    units = {'B': 1, 'kB': 1000, 'KB': 1000, 'MB': 1000**2, 'GB': 1000**3,
             'TB': 1000**4, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3, 'TiB': 1024**4}
    return float(match[1]) * units.get(match[2], 1)


TABS = ['Overview', 'CPU', 'Memory', 'Docker', 'Processes', 'Storage/Net']


class App:
    def __init__(self, screen, interval):
        self.s, self.interval = screen, interval
        self.m = Monitor()
        self.tab, self.pos, self.offset = 0, 0, 0
        self.paused = self.help = self.detail = self.searching = False
        self.query = ''
        self.sort = 'memory'
        self.selected = None
        self.rows = []
        self.next_sample = time.monotonic() + .25

    def put(self, y, x, text, attr=0):
        h, w = self.s.getmaxyx()
        if 0 <= y < h and 0 <= x < w - 1:
            try:
                self.s.addnstr(y, x, str(text), w - x - 1, attr)
            except curses.error:
                pass

    def heading(self, text):
        self.rows.append(('heading', text))

    def text(self, text=''):
        self.rows.append(('text', text))

    def bar(self, label, value, extra=''):
        self.rows.append(('bar', label, value, extra))

    def chart(self, label, values):
        self.rows.append(('chart', label, list(values)))

    def build(self):
        m = self.m
        self.rows = []
        if self.help:
            self.heading('KEYBOARD')
            for line in ['1–6: switch tab | Tab/Shift-Tab or Left/Right: next/previous tab',
                         'Up/Down or j/k: select/scroll | PgUp/PgDn: scroll a page',
                         'Home/End: first/last row | Enter: toggle Docker detail view',
                         '/: filter Docker/process names; Enter: apply; Esc: clear/cancel',
                         'c: sort CPU | m: sort memory | n: sort name',
                         'Space: pause/resume display (Docker continues collecting)',
                         '+/-: faster/slower refresh (0.1–3600 seconds)',
                         '?: help | q: quit | Ctrl-C: quit', '',
                         'CPU/thread bars use 0–100%. Process/container CPU: 100% = one logical CPU.',
                         'Container memory % is relative to its limit, not total host RAM.',
                         'Docker uses its reported working-set usage (cache accounting varies by platform).',
                         'No container stop/restart or process kill actions are performed.']:
                self.text(line)
            return
        if not hasattr(m, 'cpu'):
            self.text('Sampling…')
            return
        if self.tab == 0:
            self.heading('SYSTEM')
            self.text(m.hardware[0]['model'] if m.hardware else platform.machine())
            physical = len({(r['socket'], r['core']) for r in m.hardware})
            self.text('%s physical cores · %s logical CPUs · %s' % (physical, len(m.hardware), platform.release()))
            self.text('Uptime: %.1f hours · load: %.2f / %.2f / %.2f' % (float(read('/proc/uptime').split()[0]) / 3600, *os.getloadavg()))
            self.heading('LIVE USAGE')
            self.bar('CPU', m.cpu.get('cpu', 0))
            self.chart('CPU history', m.history)
            self.bar('RAM', m.ram, '%s / %s' % (bytesize((m.mem['MemTotal'] - m.mem['MemAvailable']) * 1024), bytesize(m.mem['MemTotal'] * 1024)))
            self.chart('RAM history', m.ram_history)
            self.bar('Root disk', m.disk.used / m.disk.total * 100, '%s / %s' % (bytesize(m.disk.used), bytesize(m.disk.total)))
            self.heading('DOCKER')
            self.text(m.docker_status)
            self.text('Container working set total: ' + bytesize(sum(memory_bytes(r['MemUsage']) for r in m.containers)))
            self.text('Open [4] Docker to inspect every container; [2] CPU to inspect all cores.')
        elif self.tab == 1:
            self.heading('CPU HARDWARE')
            if m.hardware:
                r = m.hardware[0]
                self.text(r['model'])
                self.text('Vendor: %s · architecture: %s · reported cache: %s' % (r['vendor'], platform.machine(), r['cache']))
            classes = collections.Counter()
            seen = set()
            for core in m.hardware:
                key = (core['socket'], core['core'])
                if key not in seen:
                    classes[core['kind']] += 1
                    seen.add(key)
            for kind, count in classes.items():
                self.text('%d physical cores: %s' % (count, kind))
            self.text('Each CPU below is a logical thread. Siblings share a physical core.')
            self.text('Unreported classes may be uniform, hidden by a VM, or unsupported by the kernel.')
            self.chart('Total history', m.history)
            for r in m.hardware:
                n = r['id']
                self.heading('CPU %s · physical core %s · socket %s · sibling CPUs %s' % (n, r['core'], r['socket'], r['siblings']))
                self.bar('CPU ' + n, m.cpu.get('cpu' + n, 0))
                freq = read(r['base'] + '/cpufreq/scaling_cur_freq', '')
                freq = '%.0f MHz' % (float(freq) / 1000) if freq.isdigit() else 'unavailable'
                self.text('Type: ' + r['kind'])
                self.text('Family: %s · model/part: %s · capacity: %s' % (r['family'], r['model_id'], r['capacity']))
                self.text('Frequency: %s · governor: %s' % (freq, read(r['base'] + '/cpufreq/scaling_governor')))
            self.heading('TEMPERATURES')
            for path in glob.glob('/sys/class/hwmon/hwmon*/temp*_input'):
                v = read(path, '')
                if v.lstrip('-').isdigit():
                    self.text('%s / %s: %.1f °C' % (read(os.path.dirname(path) + '/name'), read(path.replace('_input', '_label'), os.path.basename(path)), float(v) / 1000))
        elif self.tab == 2:
            self.heading('MEMORY')
            self.bar('RAM', m.ram, human(m.mem['MemTotal'] - m.mem['MemAvailable']) + ' / ' + human(m.mem['MemTotal']))
            self.chart('RAM history', m.ram_history)
            total = m.mem['MemTotal']
            for key in ['MemAvailable', 'MemFree', 'Buffers', 'Cached', 'SReclaimable', 'Shmem', 'Dirty', 'Writeback', 'Slab', 'PageTables', 'KernelStack']:
                if key in m.mem:
                    self.bar(key, m.mem[key] / total * 100, bytesize(m.mem[key] * 1024))
            self.heading('SWAP')
            total = m.mem['SwapTotal']
            self.bar('Swap', (total - m.mem['SwapFree']) / total * 100 if total else 0, '%s / %s' % (bytesize((total - m.mem['SwapFree']) * 1024), bytesize(total * 1024)))
            self.text('RAM used = total − available. Memory categories may overlap.')
            for name in ['memory', 'io', 'cpu']:
                self.heading(name.upper() + ' PRESSURE')
                for line in read('/proc/pressure/' + name, 'unavailable').splitlines():
                    self.text(line)
        elif self.tab == 3:
            self.heading('DOCKER · ' + m.docker_status)
            age = 'waiting' if m.docker_updated is None else '%.0fs ago' % (time.monotonic() - m.docker_updated)
            self.text('Last sample: %s · sort: %s · filter: %s' % (age, self.sort, self.query or 'none'))
            containers = [r for r in list(m.containers) if self.query.lower() in r['Name'].lower()]
            containers.sort(key=lambda r: percent(r['CPUPerc']) if self.sort == 'cpu' else memory_bytes(r['MemUsage']) if self.sort == 'memory' else r['Name'].lower(), reverse=self.sort != 'name')
            self.pos = min(self.pos, max(0, len(containers) - 1))
            self.selected = containers[self.pos] if containers else None
            if not containers:
                self.text('No matching containers. Docker daemon access is required.')
            if self.detail and self.selected:
                r = self.selected
                self.heading(r['Name'])
                for k in ['ID', 'Container', 'Name', 'CPUPerc', 'MemUsage', 'MemPerc', 'NetIO', 'BlockIO', 'PIDs', 'Image', 'State', 'Status', 'Ports', 'CreatedAt']:
                    if k in r:
                        self.text('%-14s %s' % (k, r[k]))
                self.bar('CPU', percent(r['CPUPerc']))
                self.bar('Memory', percent(r['MemPerc']), r['MemUsage'])
                self.text('Network and block I/O are cumulative counters, not rates.')
                self.text('Enter returns to all containers. Up/Down changes selected container.')
            else:
                for i, r in enumerate(containers):
                    self.heading(('▶ ' if i == self.pos else '  ') + r['Name'])
                    self.bar('CPU', percent(r['CPUPerc']), 'PIDs: ' + r.get('PIDs', '?'))
                    self.bar('RAM', percent(r['MemPerc']), r['MemUsage'])
                    self.text('%s · network: %s · disk I/O: %s' % (r.get('State', ''), r['NetIO'], r['BlockIO']))
                self.offset = self.pos * 4 if self.pos else 0
        elif self.tab == 4:
            self.heading('PROCESSES · sort: %s · filter: %s' % (self.sort, self.query or 'none'))
            self.text('CPU 100% = one logical CPU · memory = resident set (RSS)')
            rows = [r for r in m.top if self.query.lower() in r[2].lower()]
            rows.sort(key=lambda r: r[0] if self.sort == 'cpu' else r[1] if self.sort == 'memory' else r[2].lower(), reverse=self.sort != 'name')
            for cpu, rss, name, pid, state in rows:
                self.heading('%s · PID %s · state %s' % (name, pid, state))
                self.bar('CPU', cpu)
                self.bar('RAM', rss / 1024 / m.mem['MemTotal'] * 100, bytesize(rss))
        else:
            self.heading('FILESYSTEMS')
            seen = set()
            for line in read('/proc/mounts', '').splitlines():
                device, mount, fs = line.split()[:3]
                mount = mount.replace('\\040', ' ')
                if not device.startswith('/dev/') or (device, mount) in seen:
                    continue
                seen.add((device, mount))
                try:
                    usage = shutil.disk_usage(mount)
                except OSError:
                    continue
                self.text('%s → %s (%s)' % (device, mount, fs))
                self.bar('Used', usage.used / usage.total * 100 if usage.total else 0, '%s / %s' % (bytesize(usage.used), bytesize(usage.total)))
            self.heading('NETWORK · rates and cumulative totals')
            peak = max([max(v[:2]) for v in m.net.values()] + [1])
            for name, (rx, tx, total_rx, total_tx) in sorted(m.net.items()):
                self.heading(name)
                self.bar('RX', rx / peak * 100, bytesize(rx) + '/s · total ' + bytesize(total_rx))
                self.bar('TX', tx / peak * 100, bytesize(tx) + '/s · total ' + bytesize(total_tx))
            self.text('Network bars share an auto-scaled peak across visible interfaces.')

    def render(self):
        self.build()
        s = self.s
        h, w = s.getmaxyx()
        s.erase()
        self.put(0, 1, 'SYSMON · %s · %s · %.1fs%s' % (os.uname().nodename, time.strftime('%H:%M:%S'), self.interval, ' · PAUSED' if self.paused else ''), curses.A_BOLD | curses.color_pair(4))
        x = 1
        for i, name in enumerate(TABS):
            label = ' %d %s ' % (i + 1, name)
            self.put(2, x, label, curses.A_REVERSE if i == self.tab else curses.color_pair(4))
            x += len(label) + 1
        body = max(1, h - 6)
        self.offset = min(max(0, self.offset), max(0, len(self.rows) - body))
        for y, row in enumerate(self.rows[self.offset:self.offset + body], 4):
            kind = row[0]
            if kind == 'heading':
                self.put(y, 1, row[1], curses.A_BOLD | curses.color_pair(4))
            elif kind == 'text':
                self.put(y, 2, row[1])
            elif kind == 'bar':
                _, label, pct, extra = row
                bw = max(2, w - 29 - len(extra))
                filled = round(max(0, min(100, pct)) * bw / 100)
                self.put(y, 2, label[:14])
                self.put(y, 17, '█' * filled + '░' * (bw - filled), curses.color_pair(3 if pct >= 85 else 2 if pct >= 60 else 1))
                self.put(y, 18 + bw, '%6.1f%% %s' % (pct, extra))
            else:
                _, label, values = row
                self.put(y, 2, label)
                self.put(y, 17, ''.join('▁▂▃▄▅▆▇█'[min(7, max(0, int(v * 7 / 100)))] for v in values[-max(1, w - 20):]), curses.color_pair(4))
        status = '/' + self.query + '█  Enter apply · Esc clear' if self.searching else 'Tab/1–6 tabs · ↑↓ scroll · / filter · c/m/n sort · Space pause · ? help · q quit'
        self.put(h - 1, 1, status, curses.A_REVERSE)
        self.put(h - 2, 1, 'Rows %d–%d / %d' % (min(self.offset + 1, len(self.rows)), min(self.offset + body, len(self.rows)), len(self.rows)), curses.A_DIM)
        s.refresh()

    def key(self, key):
        if self.searching:
            if key in (10, 13):
                self.searching = False
            elif key == 27:
                self.query, self.searching = '', False
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.query = self.query[:-1]
            elif 32 <= key <= 126:
                self.query += chr(key)
            self.pos = self.offset = 0
            return True
        if key in (ord('q'), ord('Q')):
            return False
        if key in (9, curses.KEY_RIGHT, curses.KEY_BTAB, curses.KEY_LEFT) or ord('1') <= key <= ord('6'):
            self.tab = key - ord('1') if ord('1') <= key <= ord('6') else (self.tab + (-1 if key in (curses.KEY_LEFT, curses.KEY_BTAB) else 1)) % len(TABS)
            self.offset = self.pos = 0
            self.detail = self.help = False
        elif key == ord('?'):
            self.help = not self.help
            self.offset = 0
        elif key == ord(' '):
            self.paused = not self.paused
        elif key in (ord('+'), ord('='), ord('-')):
            self.interval = min(3600, max(.1, self.interval * (2 if key == ord('-') else .5)))
            self.next_sample = time.monotonic()
        elif key == ord('/'):
            self.searching = True
        elif key == 27:
            self.query, self.detail = '', False
            self.offset = self.pos = 0
        elif key in (ord('c'), ord('m'), ord('n')):
            self.sort = {ord('c'): 'cpu', ord('m'): 'memory', ord('n'): 'name'}[key]
            self.offset = self.pos = 0
        elif key in (10, 13) and self.tab == 3:
            self.detail = not self.detail
            self.offset = 0
        elif key in (curses.KEY_DOWN, ord('j'), curses.KEY_UP, ord('k'), curses.KEY_NPAGE, curses.KEY_PPAGE, curses.KEY_HOME, curses.KEY_END):
            step = -1 if key in (curses.KEY_UP, ord('k'), curses.KEY_PPAGE) else 1
            if self.tab == 3 and not self.help:
                self.pos = max(0, self.pos + (10 if key in (curses.KEY_NPAGE, curses.KEY_PPAGE) else 1) * step)
                if key == curses.KEY_HOME: self.pos = 0
                if key == curses.KEY_END: self.pos = max(0, len(self.m.containers) - 1)
            else:
                self.offset += step * (max(1, self.s.getmaxyx()[0] - 7) if key in (curses.KEY_NPAGE, curses.KEY_PPAGE) else 1)
                if key == curses.KEY_HOME: self.offset = 0
                if key == curses.KEY_END: self.offset = len(self.rows)
        return True

    def run(self):
        while True:
            if time.monotonic() >= self.next_sample and not self.paused:
                self.m.sample()
                self.next_sample = time.monotonic() + self.interval
            self.render()
            if not self.key(self.s.getch()):
                break


def run(screen, interval):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        for n, color in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_CYAN), 1):
            curses.init_pair(n, color, -1)
    screen.keypad(True)
    screen.timeout(100)
    App(screen, interval).run()


def main():
    parser = argparse.ArgumentParser(description='Interactive Linux system monitor with six live tabs.')
    parser.add_argument('interval', nargs='?', type=float, default=1, help='refresh seconds (default 1); -1 for snapshot')
    parser.add_argument('--version', action='version', version='sysmon 1.0.1')
    args = parser.parse_args()
    if args.interval == -1:
        m = Monitor()
        time.sleep(1)
        m.sample()
        print('CPU %.1f%% · RAM %.1f%% · Disk %.1f%%' % (m.cpu['cpu'], m.ram, m.disk.used / m.disk.total * 100))
        return
    if not .1 <= args.interval <= 3600:
        parser.error('interval must be between 0.1 and 3600 seconds')
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        parser.exit(1, 'Run sysmon in a terminal or use sysmon -1 for a snapshot.\n')
    try:
        curses.wrapper(run, args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
