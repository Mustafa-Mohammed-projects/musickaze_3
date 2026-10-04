# -*- coding: utf-8 -*-
"""
File-based communication between the app and the playback engine.

Why files?  Broadcast intents between two processes are fragile (they depend on
receiver registration, flags, process state ...).  Plain files in the app's
private folder always work, so they are the PRIMARY channel; broadcasts are only
used on top of them for lower latency.

    <data_dir>/kaze_cmd/<seq>_<pid>.json   one file per command (app -> engine)
    <data_dir>/kaze_state.json             latest player state  (engine -> app)
    <data_dir>/kaze.log                    shared log of app and engine

No kivy imports here: the playback service uses this module too.
"""

import os
import json
import time
import glob


def cmd_dir(data_dir):
    return os.path.join(data_dir, 'kaze_cmd')


def state_path(data_dir):
    return os.path.join(data_dir, 'kaze_state.json')


def log_path(data_dir):
    return os.path.join(data_dir, 'kaze.log')


def write_json_atomic(path, obj):
    tmp = '{}.{}.tmp'.format(path, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------- commands ----------------
def send_command(data_dir, command):
    """command must contain an integer 'seq'."""
    directory = cmd_dir(data_dir)
    os.makedirs(directory, exist_ok=True)
    name = '{:020d}_{}.json'.format(int(command['seq']), os.getpid())
    write_json_atomic(os.path.join(directory, name), command)


def pending_commands(data_dir):
    """[(path, command_dict)] sorted by sequence number."""
    result = []
    for path in sorted(glob.glob(os.path.join(cmd_dir(data_dir), '*.json'))):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                result.append((path, json.load(f)))
        except (OSError, ValueError):
            continue            # half-written or already taken by someone else
    return result


# ---------------- state ----------------
def write_state(data_dir, state):
    write_json_atomic(state_path(data_dir), state)


def read_state(data_dir):
    try:
        with open(state_path(data_dir), 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------- log ----------------
def log(data_dir, role, *args):
    line = '{} [{}] {}'.format(time.strftime('%H:%M:%S'), role, ' '.join(str(a) for a in args))
    print(line, flush=True)
    try:
        path = log_path(data_dir)
        if os.path.exists(path) and os.path.getsize(path) > 200000:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                tail = f.readlines()[-200:]
            with open(path, 'w', encoding='utf-8') as f:
                f.writelines(tail)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def tail_log(data_dir, lines=25):
    try:
        with open(log_path(data_dir), 'r', encoding='utf-8', errors='replace') as f:
            return ''.join(f.readlines()[-lines:])
    except OSError:
        return '(no log yet)'
