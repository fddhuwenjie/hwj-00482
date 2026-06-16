#!/usr/bin/env python3
import os
import sys
import json
import time
import hashlib
import shutil
import argparse
import fnmatch
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

FSTIMELINE_DIR = ".fstimeline"
BLOBS_DIR = os.path.join(FSTIMELINE_DIR, "blobs")
CHUNKS_DIR = os.path.join(FSTIMELINE_DIR, "chunks")
EVENTS_FILE = os.path.join(FSTIMELINE_DIR, "events.jsonl")
SNAPSHOTS_DIR = os.path.join(FSTIMELINE_DIR, "snapshots")
IGNORE_FILE = ".fstignore"
STATE_FILE = os.path.join(FSTIMELINE_DIR, "state.json")
REFS_DIR = os.path.join(FSTIMELINE_DIR, "refs")
HEADS_DIR = os.path.join(REFS_DIR, "heads")
TAGS_DIR = os.path.join(REFS_DIR, "tags")
HEAD_FILE = os.path.join(FSTIMELINE_DIR, "HEAD")
REMOTES_FILE = os.path.join(FSTIMELINE_DIR, "remotes.json")
HOOKS_DIR = os.path.join(FSTIMELINE_DIR, "hooks")
HOOKS_LOG_FILE = os.path.join(FSTIMELINE_DIR, "hooks_log.jsonl")
MANIFESTS_DIR = os.path.join(FSTIMELINE_DIR, "manifests")

CHUNK_SIZE = 1024 * 1024  # 1MB
LARGE_FILE_THRESHOLD = 1024 * 1024  # 1MB

HOOK_NAMES = ["pre-snapshot", "post-snapshot", "pre-checkout", "post-checkout", "on-change"]


def sha256_file(filepath):
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except (IOError, OSError):
        return None
    return h.hexdigest()


def load_ignore_patterns(root):
    patterns = [".fstimeline", ".fstimeline/**", ".git", ".git/**"]
    ignore_path = os.path.join(root, IGNORE_FILE)
    if os.path.exists(ignore_path):
        with open(ignore_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns


def is_ignored(rel_path, patterns):
    rel = rel_path.replace(os.sep, "/")
    for pat in patterns:
        p = pat.rstrip("/")
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/**"):
            return True
        parts = rel.split("/")
        for i in range(len(parts)):
            if fnmatch.fnmatch("/".join(parts[: i + 1]), p):
                return True
    return False


def scan_directory(root, patterns):
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        dirnames[:] = [
            d for d in dirnames
            if not is_ignored(os.path.join(rel_dir, d) if rel_dir else d, patterns)
        ]
        for fn in filenames:
            rel = os.path.join(rel_dir, fn) if rel_dir else fn
            if is_ignored(rel, patterns):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
                result[rel] = {
                    "path": rel,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "hash": sha256_file(full),
                }
            except (IOError, OSError):
                continue
    return result


def ensure_storage(root):
    for d in [FSTIMELINE_DIR, BLOBS_DIR, SNAPSHOTS_DIR]:
        p = os.path.join(root, d)
        os.makedirs(p, exist_ok=True)


def store_blob(root, filepath, file_hash):
    if not file_hash:
        return False
    blob_path = os.path.join(root, BLOBS_DIR, file_hash)
    if not os.path.exists(blob_path):
        try:
            shutil.copy2(filepath, blob_path)
            return True
        except (IOError, OSError):
            return False
    return True


def sha256_chunk(chunk_data):
    return hashlib.sha256(chunk_data).hexdigest()


def chunk_file(filepath, chunk_size=CHUNK_SIZE):
    chunks = []
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                chunks.append(chunk)
    except (IOError, OSError):
        return None
    return chunks


def store_chunk(root, chunk_hash, chunk_data):
    chunk_path = os.path.join(root, CHUNKS_DIR, chunk_hash)
    if not os.path.exists(chunk_path):
        try:
            with open(chunk_path, "wb") as f:
                f.write(chunk_data)
            return True
        except (IOError, OSError):
            return False
    return True


def load_chunk(root, chunk_hash):
    chunk_path = os.path.join(root, CHUNKS_DIR, chunk_hash)
    if not os.path.exists(chunk_path):
        return None
    try:
        with open(chunk_path, "rb") as f:
            return f.read()
    except (IOError, OSError):
        return None


def store_large_file(root, filepath):
    size = os.path.getsize(filepath)
    if size < LARGE_FILE_THRESHOLD:
        file_hash = sha256_file(filepath)
        store_blob(root, filepath, file_hash)
        return {"hash": file_hash, "size": size, "chunked": False}

    chunks = chunk_file(filepath)
    if chunks is None:
        return None

    chunk_hashes = []
    h = hashlib.sha256()
    for chunk in chunks:
        ch = sha256_chunk(chunk)
        chunk_hashes.append(ch)
        h.update(chunk)
        store_chunk(root, ch, chunk)

    file_hash = h.hexdigest()
    blob_path = os.path.join(root, BLOBS_DIR, file_hash)
    if not os.path.exists(blob_path):
        try:
            shutil.copy2(filepath, blob_path)
        except (IOError, OSError):
            pass

    return {"hash": file_hash, "size": size, "chunked": True, "chunks": chunk_hashes}


def reconstruct_file_from_chunks(root, chunks_list, dest_path):
    parent = os.path.dirname(dest_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        with open(dest_path, "wb") as f:
            for ch in chunks_list:
                chunk_data = load_chunk(root, ch)
                if chunk_data is None:
                    return False
                f.write(chunk_data)
        return True
    except (IOError, OSError):
        return False


def append_event(root, event):
    events_path = os.path.join(root, EVENTS_FILE)
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_events(root):
    events = []
    events_path = os.path.join(root, EVENTS_FILE)
    if not os.path.exists(events_path):
        return events
    with open(events_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def get_snapshot_list(root):
    snaps = []
    snap_dir = os.path.join(root, SNAPSHOTS_DIR)
    if not os.path.exists(snap_dir):
        return snaps
    for fn in os.listdir(snap_dir):
        if fn.endswith(".json"):
            try:
                ts = float(fn[:-5])
                snaps.append(ts)
            except ValueError:
                continue
    snaps.sort()
    return snaps


def load_snapshot(root, ts):
    path = os.path.join(root, SNAPSHOTS_DIR, f"{ts}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(root, ts, data):
    path = os.path.join(root, SNAPSHOTS_DIR, f"{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_full_state_from_snapshots(root, up_to_ts=None):
    snaps = get_snapshot_list(root)
    state = {}
    for ts in snaps:
        if up_to_ts is not None and ts > up_to_ts:
            break
        snap = load_snapshot(root, ts)
        if not snap:
            continue
        if snap.get("full", True):
            state = {f["path"]: f for f in snap.get("files", [])}
        else:
            for f in snap.get("files", []):
                status = f.get("status")
                path = f.get("path")
                if status == "added" or status == "modified":
                    state[path] = {k: v for k, v in f.items() if k != "status"}
                elif status == "deleted":
                    if path in state:
                        del state[path]
    return state


def reconstruct_state_at(root, target_ts):
    snaps = get_snapshot_list(root)
    if not snaps:
        return {}

    state = build_full_state_from_snapshots(root, target_ts)

    last_snap_ts = None
    for s in snaps:
        if s <= target_ts:
            last_snap_ts = s
        else:
            break

    if last_snap_ts is None:
        return state

    events = read_events(root)
    for ev in events:
        ev_ts = ev.get("timestamp", 0)
        if ev_ts <= last_snap_ts:
            continue
        if ev_ts > target_ts:
            break
        etype = ev.get("event")
        path = ev.get("path")
        if etype == "create" or etype == "modify":
            state[path] = {
                "path": path,
                "size": ev.get("size"),
                "mtime": ev_ts,
                "hash": ev.get("hash"),
            }
        elif etype == "delete":
            if path in state:
                del state[path]
        elif etype == "rename":
            old = ev.get("old_path")
            new = ev.get("path")
            if old in state:
                info = state.pop(old)
                info["path"] = new
                state[new] = info
    return state


def find_closest_snapshot(root, target_ts):
    snaps = get_snapshot_list(root)
    if not snaps:
        return None
    best = None
    for s in snaps:
        if s <= target_ts:
            best = s
    if best is None:
        best = snaps[0]
    return best


def ensure_branch_storage(root):
    for d in [HEADS_DIR, TAGS_DIR, MANIFESTS_DIR, CHUNKS_DIR]:
        p = os.path.join(root, d)
        os.makedirs(p, exist_ok=True)


def get_head(root):
    head_path = os.path.join(root, HEAD_FILE)
    if not os.path.exists(head_path):
        return None
    with open(head_path, "r", encoding="utf-8") as f:
        return f.read().strip()


def set_head(root, ref):
    head_path = os.path.join(root, HEAD_FILE)
    with open(head_path, "w", encoding="utf-8") as f:
        f.write(ref + "\n")


def get_branch_list(root):
    heads_dir = os.path.join(root, HEADS_DIR)
    if not os.path.exists(heads_dir):
        return []
    branches = []
    for fn in os.listdir(heads_dir):
        if os.path.isfile(os.path.join(heads_dir, fn)):
            branches.append(fn)
    branches.sort()
    return branches


def get_branch_snapshot(root, branch):
    head_path = os.path.join(root, HEADS_DIR, branch)
    if not os.path.exists(head_path):
        return None
    with open(head_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        try:
            return float(content)
        except ValueError:
            return content


def set_branch_snapshot(root, branch, ts):
    heads_dir = os.path.join(root, HEADS_DIR)
    os.makedirs(heads_dir, exist_ok=True)
    head_path = os.path.join(heads_dir, branch)
    with open(head_path, "w", encoding="utf-8") as f:
        f.write(str(ts) + "\n")


def get_tag_list(root):
    tags_dir = os.path.join(root, TAGS_DIR)
    if not os.path.exists(tags_dir):
        return []
    tags = []
    for fn in os.listdir(tags_dir):
        if os.path.isfile(os.path.join(tags_dir, fn)):
            tags.append(fn)
    tags.sort()
    return tags


def get_tag_snapshot(root, tag):
    tag_path = os.path.join(root, TAGS_DIR, tag)
    if not os.path.exists(tag_path):
        return None
    with open(tag_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        try:
            return float(content)
        except ValueError:
            try:
                data = json.loads(content)
                return data.get("timestamp")
            except json.JSONDecodeError:
                return None


def set_tag_snapshot(root, tag, ts, message=None):
    tags_dir = os.path.join(root, TAGS_DIR)
    os.makedirs(tags_dir, exist_ok=True)
    tag_path = os.path.join(tags_dir, tag)
    data = {
        "name": tag,
        "timestamp": ts,
        "message": message or "",
        "created": time.time(),
    }
    with open(tag_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def resolve_ref(root, ref_str):
    ts = parse_timestamp(ref_str)
    if ts is not None:
        return ts

    tag_ts = get_tag_snapshot(root, ref_str)
    if tag_ts is not None:
        return tag_ts

    branch_ts = get_branch_snapshot(root, ref_str)
    if branch_ts is not None and isinstance(branch_ts, float):
        return branch_ts

    return None


def get_current_branch(root):
    head = get_head(root)
    if head and head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    return None


def get_current_snapshot_ts(root):
    branch = get_current_branch(root)
    if branch:
        return get_branch_snapshot(root, branch)
    snaps = get_snapshot_list(root)
    if snaps:
        return snaps[-1]
    return None


def ensure_main_branch(root):
    if not get_head(root):
        set_head(root, "ref: refs/heads/main")
        snaps = get_snapshot_list(root)
        if snaps:
            set_branch_snapshot(root, "main", snaps[-1])
        else:
            if not os.path.exists(os.path.join(root, HEADS_DIR, "main")):
                set_branch_snapshot(root, "main", 0)


def ensure_hooks_dir(root):
    hooks_dir = os.path.join(root, HOOKS_DIR)
    os.makedirs(hooks_dir, exist_ok=True)
    return hooks_dir


def get_hook_path(root, hook_name):
    return os.path.join(root, HOOKS_DIR, hook_name)


def hook_exists(root, hook_name):
    path = get_hook_path(root, hook_name)
    return os.path.exists(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def run_hook(root, hook_name, env_vars=None):
    if not hook_exists(root, hook_name):
        return None

    import subprocess

    hook_path = get_hook_path(root, hook_name)
    env = os.environ.copy()
    env["EVENT_TYPE"] = hook_name
    env["FSTIMELINE_ROOT"] = root
    if env_vars:
        for k, v in env_vars.items():
            env[k] = str(v)

    start_time = time.time()
    try:
        result = subprocess.run(
            [hook_path],
            env=env,
            capture_output=True,
            text=True,
            cwd=root,
            timeout=300,
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired:
        exit_code = -1
        stdout = ""
        stderr = "Hook execution timed out"
    except Exception as e:
        exit_code = -2
        stdout = ""
        stderr = str(e)

    duration = time.time() - start_time

    log_entry = {
        "timestamp": time.time(),
        "hook": hook_name,
        "exit_code": exit_code,
        "duration": duration,
        "stdout": stdout[:1000],
        "stderr": stderr[:1000],
        "env_vars": {k: v for k, v in (env_vars or {}).items() if k in ["FILE_PATH", "TIMESTAMP", "SNAPSHOT_TS"]},
    }

    log_path = os.path.join(root, HOOKS_LOG_FILE)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    return exit_code


def list_hooks(root):
    hooks_dir = ensure_hooks_dir(root)
    installed = []
    for name in HOOK_NAMES:
        path = os.path.join(hooks_dir, name)
        exists = os.path.exists(path)
        executable = exists and os.access(path, os.X_OK)
        installed.append({
            "name": name,
            "exists": exists,
            "executable": executable,
        })
    return installed


def get_hook_logs(root, limit=20):
    log_path = os.path.join(root, HOOKS_LOG_FILE)
    if not os.path.exists(log_path):
        return []

    logs = []
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        for line in reversed(lines[-limit:]):
            line = line.strip()
            if line:
                try:
                    logs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return logs[:limit]


def cmd_watch(args):
    root = os.path.abspath(args.dir)
    os.chdir(root)
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)
    ensure_hooks_dir(root)
    patterns = load_ignore_patterns(root)
    interval = args.interval
    debounce = args.debounce

    print(f"[fstimeline] 开始监控目录: {root}")
    print(f"[fstimeline] 扫描间隔: {interval}秒")
    if debounce > 0:
        print(f"[fstimeline] 防抖窗口: {debounce}秒")
    print(f"[fstimeline] 按 Ctrl+C 停止")

    state = scan_directory(root, patterns)
    for rel, info in state.items():
        store_blob(root, os.path.join(root, rel), info["hash"])

    pending_changes = {}

    try:
        while True:
            time.sleep(interval)
            new_state = scan_directory(root, patterns)
            now = time.time()

            old_paths = set(state.keys())
            new_paths = set(new_state.keys())

            changes = []

            for p in new_paths - old_paths:
                info = new_state[p]
                store_blob(root, os.path.join(root, p), info["hash"])
                changes.append({"event": "create", "path": p, "info": info})

            for p in old_paths - new_paths:
                old_info = state[p]
                changes.append({"event": "delete", "path": p, "info": old_info})

            for p in old_paths & new_paths:
                old_info = state[p]
                new_info = new_state[p]
                if old_info["hash"] != new_info["hash"] or old_info["size"] != new_info["size"]:
                    store_blob(root, os.path.join(root, p), new_info["hash"])
                    changes.append({"event": "modify", "path": p, "info": new_info, "old_info": old_info})

            if debounce > 0 and changes:
                for ch in changes:
                    pending_changes[ch["path"]] = (now, ch)

                ready_paths = [p for p, (t, _) in pending_changes.items() if now - t >= debounce]
                for p in ready_paths:
                    if p in pending_changes:
                        _, ch = pending_changes.pop(p)
                        _emit_change_event(root, ch, now)
                        print(f"  {_event_label(ch['event'])}: {p}")
                        run_hook(root, "on-change", {
                            "FILE_PATH": p,
                            "EVENT_TYPE": ch["event"],
                            "TIMESTAMP": str(now),
                        })
            else:
                for ch in changes:
                    _emit_change_event(root, ch, now)
                    print(f"  {_event_label(ch['event'])}: {ch['path']}")
                    run_hook(root, "on-change", {
                        "FILE_PATH": ch["path"],
                        "EVENT_TYPE": ch["event"],
                        "TIMESTAMP": str(now),
                    })

            state = new_state
    except KeyboardInterrupt:
        print("\n[fstimeline] 监控已停止")


def _event_label(etype):
    labels = {"create": "创建", "delete": "删除", "modify": "修改"}
    return labels.get(etype, etype)


def _emit_change_event(root, ch, now):
    event_type = ch["event"]
    path = ch["path"]
    info = ch["info"]

    if event_type == "create":
        append_event(root, {
            "timestamp": now,
            "event": "create",
            "path": path,
            "hash": info["hash"],
            "size": info["size"],
        })
    elif event_type == "delete":
        append_event(root, {
            "timestamp": now,
            "event": "delete",
            "path": path,
            "hash": info["hash"],
            "size": info["size"],
        })
    elif event_type == "modify":
        old_info = ch.get("old_info", info)
        append_event(root, {
            "timestamp": now,
            "event": "modify",
            "path": path,
            "hash": info["hash"],
            "size": info["size"],
            "old_hash": old_info["hash"],
            "old_size": old_info["size"],
        })


def cmd_snapshot(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)
    ensure_hooks_dir(root)
    patterns = load_ignore_patterns(root)
    branch = get_current_branch(root) or "main"

    run_hook(root, "pre-snapshot", {"BRANCH": branch})

    current = scan_directory(root, patterns)
    for rel, info in current.items():
        store_blob(root, os.path.join(root, rel), info["hash"])

    snaps = get_snapshot_list(root)
    now = time.time()

    full = True
    files = []
    last_ts = None
    last_branch_ts = get_branch_snapshot(root, branch)

    if snaps and last_branch_ts and last_branch_ts > 0 and not args.full:
        last_ts = last_branch_ts
        last_snap = load_snapshot(root, last_ts)
        if last_snap:
            last_state = build_full_state_from_snapshots(root, last_ts)
            full = False
            for p, info in current.items():
                if p not in last_state or last_state[p]["hash"] != info["hash"]:
                    files.append({**info, "status": "modified" if p in last_state else "added"})
            for p in last_state:
                if p not in current:
                    files.append({"path": p, "status": "deleted", "hash": None, "size": 0, "mtime": now})

    if full:
        files = list(current.values())

    snap_data = {
        "timestamp": now,
        "full": full,
        "branch": branch,
        "file_count": len(current),
        "total_size": sum(f["size"] for f in current.values()),
        "files": files,
        "message": args.message or "",
    }
    if last_ts:
        last_snap = load_snapshot(root, last_ts)
        if last_snap:
            snap_data["prev_snapshot"] = last_ts
            snap_data["prev_file_count"] = last_snap.get("file_count", 0)
            snap_data["prev_total_size"] = last_snap.get("total_size", 0)
            snap_data["size_delta"] = snap_data["total_size"] - snap_data["prev_total_size"]
    else:
        snap_data["prev_file_count"] = 0
        snap_data["prev_total_size"] = 0
        snap_data["size_delta"] = snap_data["total_size"]

    save_snapshot(root, now, snap_data)
    set_branch_snapshot(root, branch, now)

    run_hook(root, "post-snapshot", {"BRANCH": branch, "SNAPSHOT_TS": str(now)})

    dt = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[fstimeline] 快照已创建: {dt}")
    print(f"  分支: {branch}")
    print(f"  文件数: {snap_data['file_count']} (变更: {snap_data['file_count'] - snap_data['prev_file_count']:+d})")
    print(f"  总大小: {format_size(snap_data['total_size'])} (变更: {format_size(snap_data['size_delta'], True)})")
    print(f"  类型: {'完整' if full else '增量'}")


def cmd_list_snapshots(args):
    root = os.path.abspath(".")
    ensure_branch_storage(root)
    ensure_main_branch(root)
    snaps = get_snapshot_list(root)
    if not snaps:
        print("[fstimeline] 暂无快照")
        return

    current_branch = get_current_branch(root)
    branch_ts = get_branch_snapshot(root, current_branch) if current_branch else None

    print(f"{'时间':<20} {'分支':<10} {'文件数':>10} {'总大小':>12} {'大小变化':>12} {'类型':>6}")
    print("-" * 72)
    for ts in snaps:
        s = load_snapshot(root, ts)
        if not s:
            continue
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        fc = s.get("file_count", 0)
        ts_ = s.get("total_size", 0)
        delta = s.get("size_delta", 0)
        ftype = "完整" if s.get("full", True) else "增量"
        branch = s.get("branch", "")
        marker = " *" if branch_ts and abs(ts - branch_ts) < 0.001 and branch == current_branch else ""
        print(f"{dt:<20} {branch:<10} {fc:>10} {format_size(ts_):>12} {format_size(delta, True):>12} {ftype:>6}{marker}")


def cmd_checkout(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)
    ensure_hooks_dir(root)

    target = args.timestamp

    if target in get_branch_list(root):
        branch_ts = get_branch_snapshot(root, target)
        if branch_ts and isinstance(branch_ts, float) and branch_ts > 0:
            target_ts = branch_ts
            set_head(root, f"ref: refs/heads/{target}")
            is_branch_checkout = True
        else:
            print(f"[fstimeline] 错误: 分支 {target} 没有快照", file=sys.stderr)
            sys.exit(1)
    else:
        target_ts = resolve_ref(root, target)
        if target_ts is None:
            print("[fstimeline] 错误: 无效的时间戳、标签或分支名", file=sys.stderr)
            sys.exit(1)
        is_branch_checkout = False
        if get_current_branch(root):
            pass

    run_hook(root, "pre-checkout", {
        "TARGET_TS": str(target_ts),
        "TARGET_REF": target,
        "IS_BRANCH": str(is_branch_checkout),
    })

    patterns = load_ignore_patterns(root)
    target_state = reconstruct_state_at(root, target_ts)
    current = scan_directory(root, patterns)

    print(f"[fstimeline] 恢复到: {datetime.fromtimestamp(target_ts).strftime('%Y-%m-%d %H:%M:%S')}")
    if is_branch_checkout:
        print(f"  分支: {target}")

    for p in set(current.keys()) - set(target_state.keys()):
        full = os.path.join(root, p)
        if os.path.exists(full):
            try:
                os.remove(full)
                print(f"  删除: {p}")
            except OSError as e:
                print(f"  删除失败 {p}: {e}")

    for p, info in target_state.items():
        full = os.path.join(root, p)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        blob_path = os.path.join(root, BLOBS_DIR, info["hash"]) if info.get("hash") else None
        if blob_path and os.path.exists(blob_path):
            shutil.copy2(blob_path, full)
            print(f"  恢复: {p}")
        elif os.path.exists(full):
            pass
        else:
            print(f"  警告: 无法恢复 {p} (找不到blob)")

    run_hook(root, "post-checkout", {
        "TARGET_TS": str(target_ts),
        "TARGET_REF": target,
        "IS_BRANCH": str(is_branch_checkout),
    })


def cmd_restore(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)
    target_ts = resolve_ref(root, args.timestamp)
    if target_ts is None:
        print("[fstimeline] 错误: 无效的时间戳、标签或分支名", file=sys.stderr)
        sys.exit(1)

    path = args.path
    target_state = reconstruct_state_at(root, target_ts)
    if path not in target_state:
        print(f"[fstimeline] 错误: 在指定时间点找不到文件 {path}", file=sys.stderr)
        sys.exit(1)

    info = target_state[path]
    full = os.path.join(root, path)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    blob_path = os.path.join(root, BLOBS_DIR, info["hash"]) if info.get("hash") else None
    if blob_path and os.path.exists(blob_path):
        shutil.copy2(blob_path, full)
        print(f"[fstimeline] 已恢复 {path} 到 {datetime.fromtimestamp(target_ts).strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print(f"[fstimeline] 错误: 找不到文件内容blob", file=sys.stderr)
        sys.exit(1)


def is_text_file(blob_path, max_check=8192):
    if not os.path.exists(blob_path):
        return False
    try:
        with open(blob_path, "rb") as f:
            chunk = f.read(max_check)
        if b"\x00" in chunk:
            return False
        text_chars = bytes(range(32, 127)) + b"\n\r\t\b\f"
        if not chunk:
            return True
        non_text = sum(1 for b in chunk if b not in text_chars)
        return non_text / len(chunk) < 0.3
    except (IOError, OSError):
        return False


def generate_unified_diff(root, path, old_hash, new_hash, context=3):
    old_blob = os.path.join(root, BLOBS_DIR, old_hash) if old_hash else None
    new_blob = os.path.join(root, BLOBS_DIR, new_hash) if new_hash else None

    old_lines = []
    new_lines = []

    if old_blob and os.path.exists(old_blob):
        try:
            with open(old_blob, "r", encoding="utf-8", errors="replace") as f:
                old_lines = f.readlines()
        except (IOError, OSError):
            old_lines = []

    if new_blob and os.path.exists(new_blob):
        try:
            with open(new_blob, "r", encoding="utf-8", errors="replace") as f:
                new_lines = f.readlines()
        except (IOError, OSError):
            new_lines = []

    import difflib
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
    )
    return list(diff)


def cmd_diff(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)

    def get_state(ref_str):
        ts = resolve_ref(root, ref_str)
        if ts is None:
            return None, None
        return ts, reconstruct_state_at(root, ts)

    ts1, s1 = get_state(args.snap1)
    ts2, s2 = get_state(args.snap2)
    if s1 is None or s2 is None:
        print("[fstimeline] 错误: 无效的时间戳、标签或分支名", file=sys.stderr)
        sys.exit(1)

    show_unified = args.unified

    print(f"对比 {datetime.fromtimestamp(ts1).strftime('%Y-%m-%d %H:%M:%S')} -> {datetime.fromtimestamp(ts2).strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 60)

    p1, p2 = set(s1.keys()), set(s2.keys())
    added = p2 - p1
    deleted = p1 - p2
    modified = []
    for p in p1 & p2:
        if s1[p]["hash"] != s2[p]["hash"]:
            modified.append(p)

    if added:
        print("\n新增文件:")
        for p in sorted(added):
            print(f"  + {p} ({format_size(s2[p]['size'])})")
    if deleted:
        print("\n删除文件:")
        for p in sorted(deleted):
            print(f"  - {p} ({format_size(s1[p]['size'])})")
    if modified:
        print("\n修改文件:")
        for p in sorted(modified):
            old_s = s1[p]["size"]
            new_s = s2[p]["size"]
            print(f"  ~ {p} ({format_size(old_s)} -> {format_size(new_s)}, {format_size(new_s - old_s, True)})")

    print(f"\n总计: 新增 {len(added)}, 删除 {len(deleted)}, 修改 {len(modified)}")

    if show_unified and modified:
        print("\n" + "=" * 60)
        print(" 内容差异 (Unified Diff)")
        print("=" * 60)
        for p in sorted(modified):
            old_hash = s1[p]["hash"]
            new_hash = s2[p]["hash"]
            old_blob = os.path.join(root, BLOBS_DIR, old_hash) if old_hash else None
            new_blob = os.path.join(root, BLOBS_DIR, new_hash) if new_hash else None

            is_text = is_text_file(old_blob) or is_text_file(new_blob)
            if not is_text:
                print(f"\n--- {p} (二进制文件，跳过)")
                continue

            diff_lines = generate_unified_diff(root, p, old_hash, new_hash)
            if diff_lines:
                print(f"\n{'─' * 60}")
                print(f" 文件: {p}")
                print(f"{'─' * 60}")
                for line in diff_lines:
                    if line.startswith("+") and not line.startswith("+++"):
                        print(f"\033[32m{line.rstrip()}\033[0m")
                    elif line.startswith("-") and not line.startswith("---"):
                        print(f"\033[31m{line.rstrip()}\033[0m")
                    elif line.startswith("@@"):
                        print(f"\033[36m{line.rstrip()}\033[0m")
                    else:
                        print(line.rstrip())
            else:
                print(f"\n--- {p} (无行级差异)")


def cmd_history(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    path = args.path

    events = read_events(root)
    file_events = []
    for ev in events:
        if ev.get("path") == path or ev.get("old_path") == path:
            file_events.append(ev)

    snaps = get_snapshot_list(root)
    for ts in snaps:
        s = load_snapshot(root, ts)
        if not s:
            continue
        for f in s.get("files", []):
            if f.get("path") == path:
                file_events.append({
                    "timestamp": ts,
                    "event": "snapshot",
                    "path": path,
                    "hash": f.get("hash"),
                    "size": f.get("size"),
                })
                break

    file_events.sort(key=lambda e: e.get("timestamp", 0))

    if not file_events:
        print(f"[fstimeline] 无 {path} 的变更记录")
        return

    print(f"文件 {path} 的变更历史:")
    print(f"{'时间':<20} {'类型':<8} {'大小':>12} {'Hash':<20}")
    print("-" * 62)
    for ev in file_events:
        dt = datetime.fromtimestamp(ev["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        etype = ev.get("event", "?")
        size = format_size(ev.get("size", 0))
        h = (ev.get("hash") or "")[:16]
        print(f"{dt:<20} {etype:<8} {size:>12} {h:<20}")


def cmd_cat(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)
    target_ts = resolve_ref(root, args.timestamp)
    if target_ts is None:
        print("[fstimeline] 错误: 无效的时间戳、标签或分支名", file=sys.stderr)
        sys.exit(1)

    path = args.path
    target_state = reconstruct_state_at(root, target_ts)
    if path not in target_state:
        print(f"[fstimeline] 错误: 在指定时间点找不到文件 {path}", file=sys.stderr)
        sys.exit(1)

    info = target_state[path]
    blob_path = os.path.join(root, BLOBS_DIR, info["hash"]) if info.get("hash") else None
    if not blob_path or not os.path.exists(blob_path):
        print(f"[fstimeline] 错误: 找不到文件内容blob", file=sys.stderr)
        sys.exit(1)

    with open(blob_path, "rb") as f:
        sys.stdout.buffer.write(f.read())


def cmd_status(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)

    patterns = load_ignore_patterns(root)
    current = scan_directory(root, patterns)
    snaps = get_snapshot_list(root)
    events = read_events(root)

    blobs_dir = os.path.join(root, BLOBS_DIR)
    total_blobs_size = 0
    blob_count = 0
    if os.path.exists(blobs_dir):
        for fn in os.listdir(blobs_dir):
            fp = os.path.join(blobs_dir, fn)
            if os.path.isfile(fp):
                total_blobs_size += os.path.getsize(fp)
                blob_count += 1

    chunks_dir = os.path.join(root, CHUNKS_DIR)
    total_chunks_size = 0
    chunk_count = 0
    if os.path.exists(chunks_dir):
        for fn in os.listdir(chunks_dir):
            fp = os.path.join(chunks_dir, fn)
            if os.path.isfile(fp):
                total_chunks_size += os.path.getsize(fp)
                chunk_count += 1

    fst_size = 0
    fst_dir = os.path.join(root, FSTIMELINE_DIR)
    for dp, _, fns in os.walk(fst_dir):
        for fn in fns:
            fst_size += os.path.getsize(os.path.join(dp, fn))

    current_branch = get_current_branch(root)
    branches = get_branch_list(root)
    tags = get_tag_list(root)
    remotes = load_remotes(root)

    print("=" * 55)
    print(" fstimeline 状态报告")
    print("=" * 55)
    print(f"  监控目录:       {root}")
    if current_branch:
        print(f"  当前分支:       {current_branch}")
    print(f"  当前文件数:     {len(current)}")
    print(f"  当前总大小:     {format_size(sum(f['size'] for f in current.values()))}")
    print(f"  快照数量:       {len(snaps)}")
    print(f"  事件记录数:     {len(events)}")
    print(f"  Blob 文件数:    {blob_count}")
    print(f"  Blob 总大小:    {format_size(total_blobs_size)}")
    if chunk_count > 0:
        print(f"  Chunk 文件数:   {chunk_count}")
        print(f"  Chunk 总大小:   {format_size(total_chunks_size)}")
    print(f"  .fstimeline大小:{format_size(fst_size)}")
    if branches:
        print(f"  分支数量:       {len(branches)} ({', '.join(branches)})")
    if tags:
        print(f"  标签数量:       {len(tags)} ({', '.join(tags)})")
    if remotes:
        print(f"  远程仓库:       {len(remotes)} 个")
        for name, info in remotes.items():
            print(f"    {name}: {info.get('path', '')}")
    if snaps:
        print(f"  最早快照:       {datetime.fromtimestamp(snaps[0]).strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  最新快照:       {datetime.fromtimestamp(snaps[-1]).strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)


def cmd_prune(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    days = args.days
    cutoff = time.time() - days * 86400

    snaps = get_snapshot_list(root)
    to_delete = [s for s in snaps if s < cutoff]

    if not to_delete:
        print(f"[fstimeline] 没有超过 {days} 天的快照")
        return

    print(f"[fstimeline] 将删除 {len(to_delete)} 个超过 {days} 天的快照")
    kept_hashes = set()
    kept_snaps = [s for s in snaps if s >= cutoff]
    for ts in kept_snaps:
        s = load_snapshot(root, ts)
        if s:
            for f in s.get("files", []):
                if f.get("hash"):
                    kept_hashes.add(f["hash"])

    events = read_events(root)
    kept_events = [e for e in events if e.get("timestamp", 0) >= cutoff]
    for e in kept_events:
        if e.get("hash"):
            kept_hashes.add(e.get("hash"))

    for ts in to_delete:
        fp = os.path.join(root, SNAPSHOTS_DIR, f"{ts}.json")
        if os.path.exists(fp):
            os.remove(fp)
            print(f"  删除快照: {datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')}")

    events_path = os.path.join(root, EVENTS_FILE)
    with open(events_path, "w", encoding="utf-8") as f:
        for e in kept_events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    blobs_dir = os.path.join(root, BLOBS_DIR)
    removed_blobs = 0
    freed = 0
    for fn in os.listdir(blobs_dir):
        if fn not in kept_hashes:
            fp = os.path.join(blobs_dir, fn)
            try:
                freed += os.path.getsize(fp)
                os.remove(fp)
                removed_blobs += 1
            except OSError:
                pass

    print(f"[fstimeline] 清理完成: 删除快照 {len(to_delete)}, 清理blob {removed_blobs}, 释放空间 {format_size(freed)}")


def cmd_compact(args):
    root = os.path.abspath(".")
    ensure_storage(root)

    snaps = get_snapshot_list(root)
    events = read_events(root)
    used_hashes = set()

    for ts in snaps:
        s = load_snapshot(root, ts)
        if s:
            for f in s.get("files", []):
                if f.get("hash"):
                    used_hashes.add(f.get("hash"))
    for e in events:
        if e.get("hash"):
            used_hashes.add(e.get("hash"))

    blobs_dir = os.path.join(root, BLOBS_DIR)
    removed = 0
    freed = 0
    for fn in os.listdir(blobs_dir):
        if fn not in used_hashes:
            fp = os.path.join(blobs_dir, fn)
            try:
                freed += os.path.getsize(fp)
                os.remove(fp)
                removed += 1
            except OSError:
                pass

    print(f"[fstimeline] 压缩完成: 移除未引用blob {removed}, 释放空间 {format_size(freed)}")


def cmd_report(args):
    root = os.path.abspath(".")
    ensure_storage(root)

    since = parse_timestamp(args.since) if args.since else 0
    until = parse_timestamp(args.until) if args.until else time.time()
    if since is None or until is None:
        print("[fstimeline] 错误: 无效的时间格式", file=sys.stderr)
        sys.exit(1)

    events = [e for e in read_events(root) if since <= e.get("timestamp", 0) <= until]

    created = sum(1 for e in events if e.get("event") == "create")
    modified = sum(1 for e in events if e.get("event") == "modify")
    deleted = sum(1 for e in events if e.get("event") == "delete")
    renamed = sum(1 for e in events if e.get("event") == "rename")

    hour_counts = defaultdict(int)
    file_counts = defaultdict(int)
    for e in events:
        dt = datetime.fromtimestamp(e.get("timestamp", 0))
        hour_counts[dt.hour] += 1
        file_counts[e.get("path", "")] += 1

    print("=" * 60)
    print(f" 变更报告: {datetime.fromtimestamp(since).strftime('%Y-%m-%d %H:%M:%S')} ~ {datetime.fromtimestamp(until).strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print(f"  总事件数:    {len(events)}")
    print(f"  新增文件:    {created}")
    print(f"  修改文件:    {modified}")
    print(f"  删除文件:    {deleted}")
    if renamed:
        print(f"  重命名文件:  {renamed}")

    if hour_counts:
        peak_hour = max(hour_counts.items(), key=lambda x: x[1])
        print(f"  最活跃时段:  {peak_hour[0]:02d}:00 - {peak_hour[0] + 1:02d}:00 ({peak_hour[1]} 次变更)")

    if file_counts:
        top = sorted(file_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        print(f"\n  最频繁变更的文件 (Top {len(top)}):")
        for i, (p, c) in enumerate(top, 1):
            print(f"    {i:>2}. {c:>4}次  {p}")
    print("=" * 60)


def cmd_timeline(args):
    root = os.path.abspath(".")
    ensure_storage(root)

    events = read_events(root)
    snaps = get_snapshot_list(root)
    if not events and not snaps:
        print("[fstimeline] 暂无历史数据")
        return

    all_ts = [e.get("timestamp", 0) for e in events] + list(snaps)
    t_min, t_max = min(all_ts), max(all_ts)
    if t_max - t_min < 1:
        t_max = t_min + 1

    width = args.width
    span = t_max - t_min
    buckets = [0] * width
    snap_buckets = [0] * width

    for e in events:
        idx = min(width - 1, int((e.get("timestamp", 0) - t_min) / span * width))
        buckets[idx] += 1
    for ts in snaps:
        idx = min(width - 1, int((ts - t_min) / span * width))
        snap_buckets[idx] += 1

    max_c = max(max(buckets), 1)
    blocks = " ▁▂▃▄▅▆▇█"

    print()
    print(" 变更时间轴")
    print(f" 起始: {datetime.fromtimestamp(t_min).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" 结束: {datetime.fromtimestamp(t_max).strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    bar = ""
    for i in range(width):
        c = buckets[i]
        if c == 0:
            ch = "·"
        else:
            lvl = min(len(blocks) - 1, int(c / max_c * (len(blocks) - 1)))
            ch = blocks[lvl]
        if snap_buckets[i] > 0:
            ch = "◆"
        bar += ch

    print(" " + bar)
    print()

    start_label = datetime.fromtimestamp(t_min).strftime("%m-%d")
    end_label = datetime.fromtimestamp(t_max).strftime("%m-%d")
    mid_ts = (t_min + t_max) / 2
    mid_label = datetime.fromtimestamp(mid_ts).strftime("%m-%d")
    pad = width - len(start_label) - len(end_label) - len(mid_label)
    if pad > 2:
        left_pad = pad // 2 - len(mid_label) // 2
        right_pad = pad - left_pad
        labels = start_label + " " * max(0, left_pad) + mid_label + " " * max(0, right_pad) + end_label
        print(" " + labels[:width])
    else:
        print(f" {start_label}{' ' * (width - len(start_label) - len(end_label))}{end_label}")
    print()

    total_events = len(events)
    total_snaps = len(snaps)
    print(f" 总计: {total_events} 事件, {total_snaps} 快照")
    print(f" 峰值密度: {max(buckets)} 事件/格")
    print()


def cmd_branch(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)

    branch_name = getattr(args, "name", None)
    delete_name = getattr(args, "delete", None)

    if delete_name:
        if delete_name == "main":
            print("[fstimeline] 错误: 不能删除 main 分支", file=sys.stderr)
            sys.exit(1)
        current = get_current_branch(root)
        if current == delete_name:
            print("[fstimeline] 错误: 不能删除当前所在分支", file=sys.stderr)
            sys.exit(1)
        branch_path = os.path.join(root, HEADS_DIR, delete_name)
        if not os.path.exists(branch_path):
            print(f"[fstimeline] 错误: 分支 {delete_name} 不存在", file=sys.stderr)
            sys.exit(1)
        os.remove(branch_path)
        print(f"[fstimeline] 已删除分支 {delete_name}")
        return

    if branch_name:
        current_branch = get_current_branch(root) or "main"
        current_ts = get_branch_snapshot(root, current_branch)
        if current_ts is None or (isinstance(current_ts, float) and current_ts == 0):
            snaps = get_snapshot_list(root)
            if snaps:
                current_ts = snaps[-1]
            else:
                current_ts = 0

        existing = get_branch_snapshot(root, branch_name)
        if existing is not None:
            print(f"[fstimeline] 错误: 分支 {branch_name} 已存在", file=sys.stderr)
            sys.exit(1)

        set_branch_snapshot(root, branch_name, current_ts)
        print(f"[fstimeline] 已创建分支 {branch_name}")
        if current_ts and current_ts > 0:
            print(f"  基于快照: {datetime.fromtimestamp(current_ts).strftime('%Y-%m-%d %H:%M:%S')}")
        return

    branches = get_branch_list(root)
    current = get_current_branch(root)
    if not branches:
        print("[fstimeline] 暂无分支")
        return

    print("[fstimeline] 分支列表:")
    for b in branches:
        marker = " *" if b == current else "  "
        ts = get_branch_snapshot(root, b)
        ts_str = ""
        if ts and isinstance(ts, float) and ts > 0:
            ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        else:
            ts_str = "(无快照)"
        print(f"{marker} {b:<20} {ts_str}")


def cmd_tag(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)

    tag_name = getattr(args, "name", None)
    target_ref = getattr(args, "target", None)
    delete_name = getattr(args, "delete", None)

    if delete_name:
        tag_path = os.path.join(root, TAGS_DIR, delete_name)
        if not os.path.exists(tag_path):
            print(f"[fstimeline] 错误: 标签 {delete_name} 不存在", file=sys.stderr)
            sys.exit(1)
        os.remove(tag_path)
        print(f"[fstimeline] 已删除标签 {delete_name}")
        return

    if tag_name:
        if target_ref:
            target_ts = resolve_ref(root, target_ref)
            if target_ts is None:
                print("[fstimeline] 错误: 无效的目标引用", file=sys.stderr)
                sys.exit(1)
        else:
            current_branch = get_current_branch(root)
            target_ts = None
            if current_branch:
                target_ts = get_branch_snapshot(root, current_branch)
            if target_ts is None or not isinstance(target_ts, float) or target_ts == 0:
                snaps = get_snapshot_list(root)
                if snaps:
                    target_ts = snaps[-1]
                else:
                    print("[fstimeline] 错误: 没有快照可以打标签", file=sys.stderr)
                    sys.exit(1)

        existing = get_tag_snapshot(root, tag_name)
        if existing is not None:
            print(f"[fstimeline] 错误: 标签 {tag_name} 已存在", file=sys.stderr)
            sys.exit(1)

        message = getattr(args, "message", "") or ""
        set_tag_snapshot(root, tag_name, target_ts, message)
        print(f"[fstimeline] 已创建标签 {tag_name}")
        print(f"  快照时间: {datetime.fromtimestamp(target_ts).strftime('%Y-%m-%d %H:%M:%S')}")
        if message:
            print(f"  备注: {message}")
        return

    tags = get_tag_list(root)
    if not tags:
        print("[fstimeline] 暂无标签")
        return

    print("[fstimeline] 标签列表:")
    for t in tags:
        ts = get_tag_snapshot(root, t)
        ts_str = ""
        if ts:
            ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        tag_path = os.path.join(root, TAGS_DIR, t)
        msg = ""
        if os.path.exists(tag_path):
            try:
                with open(tag_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    msg = data.get("message", "")
            except (json.JSONDecodeError, IOError):
                pass
        print(f"  {t:<20} {ts_str} {msg}")


def cmd_merge(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)

    source_branch = args.branch
    current_branch = get_current_branch(root) or "main"

    if source_branch not in get_branch_list(root):
        print(f"[fstimeline] 错误: 分支 {source_branch} 不存在", file=sys.stderr)
        sys.exit(1)

    if source_branch == current_branch:
        print("[fstimeline] 错误: 不能合并当前分支到自身", file=sys.stderr)
        sys.exit(1)

    source_ts = get_branch_snapshot(root, source_branch)
    if not source_ts or not isinstance(source_ts, float) or source_ts == 0:
        print(f"[fstimeline] 错误: 源分支 {source_branch} 没有快照", file=sys.stderr)
        sys.exit(1)

    current_ts = get_branch_snapshot(root, current_branch)
    if not current_ts or not isinstance(current_ts, float) or current_ts == 0:
        target_state = {}
    else:
        target_state = build_full_state_from_snapshots(root, current_ts)

    source_state = build_full_state_from_snapshots(root, source_ts)

    conflicts = []
    merged_files = {}
    added = []
    modified = []
    deleted = []

    for path, info in source_state.items():
        if path not in target_state:
            merged_files[path] = info
            added.append(path)
        elif target_state[path]["hash"] != info["hash"]:
            conflicts.append(path)
            merged_files[path] = info
        else:
            merged_files[path] = info

    for path in target_state:
        if path not in source_state:
            deleted.append(path)

    print(f"[fstimeline] 合并分支 {source_branch} 到 {current_branch}")
    print(f"  源分支快照: {datetime.fromtimestamp(source_ts).strftime('%Y-%m-%d %H:%M:%S')}")
    if current_ts and current_ts > 0:
        print(f"  当前分支快照: {datetime.fromtimestamp(current_ts).strftime('%Y-%m-%d %H:%M:%S')}")

    print(f"\n  新增文件: {len(added)}")
    print(f"  删除文件: {len(deleted)}")
    print(f"  冲突文件: {len(conflicts)}")

    if conflicts:
        print(f"\n[fstimeline] 检测到 {len(conflicts)} 个冲突文件，生成冲突标记...")
        patterns = load_ignore_patterns(root)
        current_state = scan_directory(root, patterns)

        for path in conflicts:
            print(f"  冲突: {path}")
            full_path = os.path.join(root, path)
            parent = os.path.dirname(full_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            src_hash = source_state[path]["hash"]
            dst_hash = target_state[path]["hash"] if path in target_state else None

            src_blob = os.path.join(root, BLOBS_DIR, src_hash) if src_hash else None
            dst_blob = os.path.join(root, BLOBS_DIR, dst_hash) if dst_hash else None

            src_is_text = is_text_file(src_blob)
            dst_is_text = is_text_file(dst_blob)

            if src_is_text and dst_is_text:
                src_lines = []
                dst_lines = []
                if src_blob and os.path.exists(src_blob):
                    with open(src_blob, "r", encoding="utf-8", errors="replace") as f:
                        src_lines = f.readlines()
                if dst_blob and os.path.exists(dst_blob):
                    with open(dst_blob, "r", encoding="utf-8", errors="replace") as f:
                        dst_lines = f.readlines()

                import difflib
                merged = []
                matcher = difflib.SequenceMatcher(None, dst_lines, src_lines)
                for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                    if tag == "equal":
                        merged.extend(dst_lines[i1:i2])
                    elif tag == "delete":
                        merged.append("<<<<<<< CURRENT\n")
                        merged.extend(dst_lines[i1:i2])
                        merged.append(f"=======\n")
                        merged.append(f">>>>>>> {source_branch}\n")
                    elif tag == "insert":
                        merged.append("<<<<<<< CURRENT\n")
                        merged.append("=======\n")
                        merged.extend(src_lines[j1:j2])
                        merged.append(f">>>>>>> {source_branch}\n")
                    elif tag == "replace":
                        merged.append("<<<<<<< CURRENT\n")
                        merged.extend(dst_lines[i1:i2])
                        merged.append("=======\n")
                        merged.extend(src_lines[j1:j2])
                        merged.append(f">>>>>>> {source_branch}\n")

                with open(full_path, "w", encoding="utf-8") as f:
                    f.writelines(merged)

                conflict_file = full_path + ".fstimeline-conflict"
                with open(conflict_file, "w", encoding="utf-8") as f:
                    f.write(f"冲突文件: {path}\n")
                    f.write(f"当前分支: {current_branch} (hash: {dst_hash[:12] if dst_hash else 'none'})\n")
                    f.write(f"源分支: {source_branch} (hash: {src_hash[:12] if src_hash else 'none'})\n")
                    f.write(f"\n请手动解决冲突后删除此文件，然后提交快照。\n")
            else:
                conflict_file = full_path + ".fstimeline-conflict"
                with open(conflict_file, "w", encoding="utf-8") as f:
                    f.write(f"二进制文件冲突: {path}\n")
                    f.write(f"当前分支: {current_branch} (hash: {dst_hash[:12] if dst_hash else 'none'})\n")
                    f.write(f"源分支: {source_branch} (hash: {src_hash[:12] if src_hash else 'none'})\n")
                    f.write(f"\n请手动选择保留哪个版本，然后删除此文件并提交快照。\n")

                src_blob = os.path.join(root, BLOBS_DIR, src_hash) if src_hash else None
                if src_blob and os.path.exists(src_blob):
                    shutil.copy2(src_blob, full_path + f".{source_branch}")
                if dst_blob and os.path.exists(dst_blob):
                    shutil.copy2(dst_blob, full_path + f".{current_branch}")

        print(f"\n[fstimeline] 请手动解决 {len(conflicts)} 个冲突，然后创建快照完成合并。")
        return

    for path in added:
        info = source_state[path]
        full = os.path.join(root, path)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        blob_path = os.path.join(root, BLOBS_DIR, info["hash"]) if info.get("hash") else None
        if blob_path and os.path.exists(blob_path):
            shutil.copy2(blob_path, full)

    for path in deleted:
        full = os.path.join(root, path)
        if os.path.exists(full):
            try:
                os.remove(full)
            except OSError:
                pass

    now = time.time()
    snap_files = []
    current_snap_files = {}
    if current_ts and current_ts > 0:
        current_snap = load_snapshot(root, current_ts)
        if current_snap and current_snap.get("full"):
            current_snap_files = {f["path"]: f for f in current_snap.get("files", [])}

    all_paths = set(list(target_state.keys()) + list(source_state.keys()))
    for p in all_paths:
        if p in source_state:
            info = source_state[p]
            status = "modified" if (p in target_state and target_state[p]["hash"] != info["hash"]) else "added"
            snap_files.append({**info, "status": status})
        else:
            snap_files.append({"path": p, "status": "deleted", "hash": None, "size": 0, "mtime": now})

    merge_snap = {
        "timestamp": now,
        "full": False,
        "branch": current_branch,
        "file_count": len(source_state),
        "total_size": sum(f.get("size", 0) for f in source_state.values()),
        "files": snap_files,
        "message": f"Merge branch '{source_branch}' into {current_branch}",
        "merge_source": source_branch,
        "merge_source_ts": source_ts,
    }
    save_snapshot(root, now, merge_snap)
    set_branch_snapshot(root, current_branch, now)

    print(f"\n[fstimeline] 合并完成，新快照: {datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S')}")


def load_remotes(root):
    remotes_path = os.path.join(root, REMOTES_FILE)
    if not os.path.exists(remotes_path):
        return {}
    try:
        with open(remotes_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_remotes(root, remotes):
    remotes_path = os.path.join(root, REMOTES_FILE)
    with open(remotes_path, "w", encoding="utf-8") as f:
        json.dump(remotes, f, ensure_ascii=False, indent=2)


def cmd_remote(args):
    root = os.path.abspath(".")
    ensure_storage(root)

    action = getattr(args, "action", "list")

    if action == "add":
        name = args.name
        path = args.path
        remotes = load_remotes(root)
        if name in remotes:
            print(f"[fstimeline] 错误: 远程 {name} 已存在", file=sys.stderr)
            sys.exit(1)
        if not os.path.exists(path):
            print(f"[fstimeline] 警告: 远程路径不存在: {path}")
        remotes[name] = {"path": os.path.abspath(path)}
        save_remotes(root, remotes)
        print(f"[fstimeline] 已添加远程 {name}: {path}")
        return

    if action == "remove":
        name = args.name
        remotes = load_remotes(root)
        if name not in remotes:
            print(f"[fstimeline] 错误: 远程 {name} 不存在", file=sys.stderr)
            sys.exit(1)
        del remotes[name]
        save_remotes(root, remotes)
        print(f"[fstimeline] 已移除远程 {name}")
        return

    if action == "set-url":
        name = args.name
        path = args.path
        remotes = load_remotes(root)
        if name not in remotes:
            print(f"[fstimeline] 错误: 远程 {name} 不存在", file=sys.stderr)
            sys.exit(1)
        remotes[name]["path"] = os.path.abspath(path)
        save_remotes(root, remotes)
        print(f"[fstimeline] 已更新远程 {name}: {path}")
        return

    remotes = load_remotes(root)
    if not remotes:
        print("[fstimeline] 暂无远程仓库")
        return
    print("[fstimeline] 远程仓库列表:")
    for name, info in remotes.items():
        print(f"  {name:<15} {info.get('path', '')}")


def _collect_snapshot_blobs(root, snap_ts):
    state = build_full_state_from_snapshots(root, snap_ts)
    hashes = set()
    for info in state.values():
        if info.get("hash"):
            hashes.add(info["hash"])
    return hashes


def _sync_snapshots_and_blobs(src_root, dst_root, dry_run=False):
    src_snaps = set(get_snapshot_list(src_root))
    dst_snaps = set(get_snapshot_list(dst_root))
    missing_snaps = src_snaps - dst_snaps

    snap_dir_src = os.path.join(src_root, SNAPSHOTS_DIR)
    snap_dir_dst = os.path.join(dst_root, SNAPSHOTS_DIR)
    os.makedirs(snap_dir_dst, exist_ok=True)

    blobs_to_copy = set()
    for ts in sorted(missing_snaps):
        snap = load_snapshot(src_root, ts)
        if snap:
            snap_blobs = _collect_snapshot_blobs(src_root, ts)
            blobs_to_copy.update(snap_blobs)

    src_blobs_dir = os.path.join(src_root, BLOBS_DIR)
    dst_blobs_dir = os.path.join(dst_root, BLOBS_DIR)
    os.makedirs(dst_blobs_dir, exist_ok=True)

    existing_dst_blobs = set()
    if os.path.exists(dst_blobs_dir):
        existing_dst_blobs = set(os.listdir(dst_blobs_dir))

    new_blobs = blobs_to_copy - existing_dst_blobs

    total_blob_size = 0
    for h in new_blobs:
        src_path = os.path.join(src_blobs_dir, h)
        if os.path.exists(src_path):
            total_blob_size += os.path.getsize(src_path)

    if dry_run:
        print(f"  将传输 {len(missing_snaps)} 个快照")
        print(f"  将传输 {len(new_blobs)} 个新 blob")
        print(f"  总数据量: {format_size(total_blob_size)}")
        return len(missing_snaps), len(new_blobs), total_blob_size

    for ts in sorted(missing_snaps):
        src_path = os.path.join(snap_dir_src, f"{ts}.json")
        dst_path = os.path.join(snap_dir_dst, f"{ts}.json")
        if os.path.exists(src_path) and not os.path.exists(dst_path):
            shutil.copy2(src_path, dst_path)

    for h in new_blobs:
        src_path = os.path.join(src_blobs_dir, h)
        dst_path = os.path.join(dst_blobs_dir, h)
        if os.path.exists(src_path) and not os.path.exists(dst_path):
            shutil.copy2(src_path, dst_path)

    return len(missing_snaps), len(new_blobs), total_blob_size


def cmd_push(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)

    remote_name = args.remote
    remotes = load_remotes(root)
    if remote_name not in remotes:
        print(f"[fstimeline] 错误: 远程 {remote_name} 不存在", file=sys.stderr)
        sys.exit(1)

    remote_path = remotes[remote_name]["path"]
    if not os.path.exists(remote_path):
        print(f"[fstimeline] 错误: 远程路径不存在: {remote_path}", file=sys.stderr)
        sys.exit(1)

    remote_fst = os.path.join(remote_path, FSTIMELINE_DIR)
    if not os.path.exists(remote_fst):
        if args.dry_run:
            print(f"[fstimeline] 将初始化远程仓库: {remote_path}")
        else:
            os.makedirs(remote_fst, exist_ok=True)

    print(f"[fstimeline] 推送到远程 {remote_name} ({remote_path})")
    if args.dry_run:
        print("  (dry-run 模式，仅预览)")

    snaps_count, blobs_count, total_size = _sync_snapshots_and_blobs(root, remote_path, args.dry_run)

    if not args.dry_run:
        print(f"[fstimeline] 推送完成: {snaps_count} 个快照, {blobs_count} 个 blob, {format_size(total_size)}")
    else:
        print(f"[fstimeline] dry-run 完成")


def cmd_pull(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)

    remote_name = args.remote
    remotes = load_remotes(root)
    if remote_name not in remotes:
        print(f"[fstimeline] 错误: 远程 {remote_name} 不存在", file=sys.stderr)
        sys.exit(1)

    remote_path = remotes[remote_name]["path"]
    if not os.path.exists(remote_path):
        print(f"[fstimeline] 错误: 远程路径不存在: {remote_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[fstimeline] 从远程 {remote_name} 拉取 ({remote_path})")
    if args.dry_run:
        print("  (dry-run 模式，仅预览)")

    snaps_count, blobs_count, total_size = _sync_snapshots_and_blobs(remote_path, root, args.dry_run)

    if not args.dry_run:
        print(f"[fstimeline] 拉取完成: {snaps_count} 个快照, {blobs_count} 个 blob, {format_size(total_size)}")
    else:
        print(f"[fstimeline] dry-run 完成")


def cmd_clone(args):
    remote_path = os.path.abspath(args.source)
    dest_path = os.path.abspath(args.dest)

    if not os.path.exists(remote_path):
        print(f"[fstimeline] 错误: 源路径不存在: {remote_path}", file=sys.stderr)
        sys.exit(1)

    remote_fst = os.path.join(remote_path, FSTIMELINE_DIR)
    if not os.path.exists(remote_fst):
        print(f"[fstimeline] 错误: 源路径不是 fstimeline 仓库", file=sys.stderr)
        sys.exit(1)

    if os.path.exists(dest_path) and os.listdir(dest_path):
        print(f"[fstimeline] 错误: 目标目录已存在且非空: {dest_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[fstimeline] 克隆 {remote_path} 到 {dest_path}")
    if args.dry_run:
        print("  (dry-run 模式，仅预览)")

    os.makedirs(dest_path, exist_ok=True)

    if args.dry_run:
        snaps = get_snapshot_list(remote_path)
        blobs_dir = os.path.join(remote_path, BLOBS_DIR)
        blob_count = 0
        total_size = 0
        if os.path.exists(blobs_dir):
            for fn in os.listdir(blobs_dir):
                fp = os.path.join(blobs_dir, fn)
                if os.path.isfile(fp):
                    blob_count += 1
                    total_size += os.path.getsize(fp)
        print(f"  将克隆 {len(snaps)} 个快照")
        print(f"  将复制 {blob_count} 个 blob")
        print(f"  总数据量: {format_size(total_size)}")
        return

    dest_fst = os.path.join(dest_path, FSTIMELINE_DIR)
    shutil.copytree(remote_fst, dest_fst)

    snaps = get_snapshot_list(dest_path)
    if snaps:
        latest_ts = snaps[-1]
        state = build_full_state_from_snapshots(dest_path, latest_ts)
        for path, info in state.items():
            full = os.path.join(dest_path, path)
            parent = os.path.dirname(full)
            if parent:
                os.makedirs(parent, exist_ok=True)
            blob_path = os.path.join(dest_path, BLOBS_DIR, info["hash"]) if info.get("hash") else None
            if blob_path and os.path.exists(blob_path):
                shutil.copy2(blob_path, full)

    print(f"[fstimeline] 克隆完成: {len(snaps)} 个快照")


def cmd_hooks(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_hooks_dir(root)

    action = getattr(args, "action", "list")

    if action == "log":
        logs = get_hook_logs(root, getattr(args, "limit", 20))
        if not logs:
            print("[fstimeline] 暂无钩子执行记录")
            return
        print(f"[fstimeline] 最近 {len(logs)} 次钩子执行记录:")
        print(f"{'时间':<20} {'钩子':<20} {'状态':<10} {'耗时':>10}")
        print("-" * 62)
        for log in logs:
            dt = datetime.fromtimestamp(log.get("timestamp", 0)).strftime("%Y-%m-%d %H:%M:%S")
            hook = log.get("hook", "")
            exit_code = log.get("exit_code", -1)
            status = "成功" if exit_code == 0 else "失败"
            duration = f"{log.get('duration', 0):.3f}s"
            print(f"{dt:<20} {hook:<20} {status:<10} {duration:>10}")
        return

    hooks = list_hooks(root)
    print("[fstimeline] 钩子列表:")
    print(f"{'名称':<20} {'状态':<10}")
    print("-" * 32)
    for h in hooks:
        status = "已安装(可执行)" if h["executable"] else ("已安装" if h["exists"] else "未安装")
        print(f"  {h['name']:<18} {status}")
    print(f"\n  钩子目录: {os.path.join(root, HOOKS_DIR)}")


def cmd_blame(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)

    path = args.path
    snaps = get_snapshot_list(root)
    if not snaps:
        print("[fstimeline] 暂无快照", file=sys.stderr)
        sys.exit(1)

    file_history = []
    for ts in snaps:
        snap = load_snapshot(root, ts)
        if not snap:
            continue
        for f in snap.get("files", []):
            if f.get("path") == path:
                file_history.append((ts, f, snap))
                break

    events = read_events(root)
    for ev in events:
        if ev.get("path") == path and ev.get("event") in ("create", "modify"):
            file_history.append((ev.get("timestamp", 0), {"hash": ev.get("hash"), "size": ev.get("size")}, None))

    file_history.sort(key=lambda x: x[0])

    if not file_history:
        print(f"[fstimeline] 无 {path} 的历史记录", file=sys.stderr)
        sys.exit(1)

    latest_state = reconstruct_state_at(root, snaps[-1])
    if path not in latest_state:
        print(f"[fstimeline] 文件 {path} 在最新快照中不存在", file=sys.stderr)
        sys.exit(1)

    latest_hash = latest_state[path]["hash"]
    blob_path = os.path.join(root, BLOBS_DIR, latest_hash)
    if not os.path.exists(blob_path):
        print(f"[fstimeline] 找不到文件内容 blob", file=sys.stderr)
        sys.exit(1)

    if not is_text_file(blob_path):
        print(f"[fstimeline] 二进制文件不支持 blame", file=sys.stderr)
        sys.exit(1)

    with open(blob_path, "r", encoding="utf-8", errors="replace") as f:
        current_lines = f.readlines()

    line_blame = []
    prev_lines = []

    for ts, info, snap in file_history:
        file_hash = info.get("hash")
        if not file_hash:
            continue
        blob = os.path.join(root, BLOBS_DIR, file_hash)
        if not os.path.exists(blob):
            continue
        if not is_text_file(blob):
            continue

        with open(blob, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        if not prev_lines:
            for i, line in enumerate(lines):
                line_blame.append({"line": line, "ts": ts, "index": i + 1})
            prev_lines = lines
            continue

        import difflib
        new_blame = []
        matcher = difflib.SequenceMatcher(None, prev_lines, lines)
        prev_idx = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    new_blame.append(line_blame[i1 + k])
            elif tag == "replace" or tag == "insert":
                for k in range(j2 - j1):
                    new_blame.append({
                        "line": lines[j1 + k],
                        "ts": ts,
                        "index": j1 + k + 1,
                    })
            elif tag == "delete":
                pass

        line_blame = new_blame
        prev_lines = lines

    print(f"文件: {path}")
    print(f"总行数: {len(current_lines)}")
    print("-" * 80)
    for i, blame_info in enumerate(line_blame):
        ts = blame_info["ts"]
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        line_num = i + 1
        line_content = blame_info["line"].rstrip()
        print(f"{dt} | {line_num:>4} | {line_content}")


def cmd_patch(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)

    snap1 = args.snap1
    snap2 = args.snap2
    output = args.output

    ts1 = resolve_ref(root, snap1)
    ts2 = resolve_ref(root, snap2)
    if ts1 is None or ts2 is None:
        print("[fstimeline] 错误: 无效的快照引用", file=sys.stderr)
        sys.exit(1)

    s1 = reconstruct_state_at(root, ts1)
    s2 = reconstruct_state_at(root, ts2)

    p1, p2 = set(s1.keys()), set(s2.keys())
    added = p2 - p1
    deleted = p1 - p2
    modified = [p for p in p1 & p2 if s1[p]["hash"] != s2[p]["hash"]]

    patch_content = []
    patch_content.append(f"# fstimeline patch")
    patch_content.append(f"# from: {datetime.fromtimestamp(ts1).strftime('%Y-%m-%d %H:%M:%S')} ({snap1})")
    patch_content.append(f"# to: {datetime.fromtimestamp(ts2).strftime('%Y-%m-%d %H:%M:%S')} ({snap2})")
    patch_content.append("")

    for p in sorted(added):
        info = s2[p]
        blob_path = os.path.join(root, BLOBS_DIR, info["hash"]) if info.get("hash") else None
        if blob_path and os.path.exists(blob_path) and is_text_file(blob_path):
            diff = generate_unified_diff(root, p, None, info["hash"])
            patch_content.extend(diff)
        else:
            patch_content.append(f"# Binary file added: {p}")

    for p in sorted(deleted):
        info = s1[p]
        blob_path = os.path.join(root, BLOBS_DIR, info["hash"]) if info.get("hash") else None
        if blob_path and os.path.exists(blob_path) and is_text_file(blob_path):
            diff = generate_unified_diff(root, p, info["hash"], None)
            patch_content.extend(diff)
        else:
            patch_content.append(f"# Binary file deleted: {p}")

    for p in sorted(modified):
        old_hash = s1[p]["hash"]
        new_hash = s2[p]["hash"]
        old_blob = os.path.join(root, BLOBS_DIR, old_hash) if old_hash else None
        new_blob = os.path.join(root, BLOBS_DIR, new_hash) if new_hash else None
        if is_text_file(old_blob) or is_text_file(new_blob):
            diff = generate_unified_diff(root, p, old_hash, new_hash)
            patch_content.extend(diff)
        else:
            patch_content.append(f"# Binary file modified: {p}")

    patch_text = "".join(line if line.endswith("\n") else line + "\n" for line in patch_content)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(patch_text)
        print(f"[fstimeline] Patch 已导出到 {output}")
    else:
        print(patch_text, end="")


def cmd_apply(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    patterns = load_ignore_patterns(root)

    patch_file = args.patch
    if not os.path.exists(patch_file):
        print(f"[fstimeline] 错误: 找不到 patch 文件 {patch_file}", file=sys.stderr)
        sys.exit(1)

    with open(patch_file, "r", encoding="utf-8") as f:
        patch_content = f.read()

    import difflib
    current_state = scan_directory(root, patterns)

    print(f"[fstimeline] 应用 patch: {patch_file}")

    current_files = {}
    for p, info in current_state.items():
        blob_path = os.path.join(root, BLOBS_DIR, info["hash"]) if info.get("hash") else None
        if blob_path and os.path.exists(blob_path) and is_text_file(blob_path):
            try:
                with open(os.path.join(root, p), "r", encoding="utf-8", errors="replace") as f:
                    current_files[p] = f.readlines()
            except (IOError, OSError):
                pass

    patches = []
    lines = patch_content.splitlines(True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            from_file = line[4:].strip()
            if from_file.startswith("a/"):
                from_file = from_file[2:]
            if from_file == "/dev/null":
                from_file = None

            to_line = lines[i + 1] if i + 1 < len(lines) else ""
            if to_line.startswith("+++ "):
                to_file = to_line[4:].strip()
                if to_file.startswith("b/"):
                    to_file = to_file[2:]
                if to_file == "/dev/null":
                    to_file = None

                hunks = []
                i += 2
                while i < len(lines) and lines[i].startswith("@@"):
                    hunk_header = lines[i]
                    i += 1
                    hunk_lines = []
                    while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("--- "):
                        hunk_lines.append(lines[i])
                        i += 1
                    hunks.append((hunk_header, hunk_lines))

                if from_file and to_file:
                    patches.append({"type": "modify", "path": to_file, "hunks": hunks})
                elif not from_file and to_file:
                    patches.append({"type": "add", "path": to_file, "hunks": hunks})
                elif from_file and not to_file:
                    patches.append({"type": "delete", "path": from_file, "hunks": hunks})
            else:
                i += 1
        else:
            i += 1

    applied = 0
    for p in patches:
        path = p["path"]
        ptype = p["type"]

        if ptype == "delete":
            full_path = os.path.join(root, path)
            if os.path.exists(full_path):
                try:
                    os.remove(full_path)
                    print(f"  删除: {path}")
                    applied += 1
                except OSError as e:
                    print(f"  删除失败 {path}: {e}")
            else:
                print(f"  跳过(不存在): {path}")
            continue

        target_lines = []
        if ptype == "modify":
            full_path = os.path.join(root, path)
            if os.path.exists(full_path):
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        target_lines = f.readlines()
                except (IOError, OSError):
                    print(f"  读取失败: {path}")
                    continue

        new_content = target_lines[:]
        offset = 0

        for hunk_header, hunk_lines in p["hunks"]:
            import re
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", hunk_header)
            if not match:
                continue
            old_start = int(match.group(1)) - 1
            old_count = int(match.group(2)) if match.group(2) else 1
            new_start = int(match.group(3)) - 1
            new_count = int(match.group(4)) if match.group(4) else 1

            old_lines_hunk = []
            new_lines_hunk = []
            for hl in hunk_lines:
                if hl.startswith(" "):
                    old_lines_hunk.append(hl[1:])
                    new_lines_hunk.append(hl[1:])
                elif hl.startswith("-"):
                    old_lines_hunk.append(hl[1:])
                elif hl.startswith("+"):
                    new_lines_hunk.append(hl[1:])

            if ptype == "modify":
                end_pos = old_start + old_count
                new_content[old_start + offset:end_pos + offset] = new_lines_hunk
                offset += len(new_lines_hunk) - old_count
            elif ptype == "add":
                new_content.extend(new_lines_hunk)

        if ptype == "add" or ptype == "modify":
            full_path = os.path.join(root, path)
            parent = os.path.dirname(full_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            try:
                with open(full_path, "w", encoding="utf-8") as f:
                    f.writelines(new_content)
                action = "新增" if ptype == "add" else "修改"
                print(f"  {action}: {path}")
                applied += 1
            except (IOError, OSError) as e:
                print(f"  写入失败 {path}: {e}")

    print(f"\n[fstimeline] 已应用 {applied} 个文件变更")


def cmd_gc(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)

    snaps = get_snapshot_list(root)
    events = read_events(root)
    used_hashes = set()

    for ts in snaps:
        s = load_snapshot(root, ts)
        if s:
            for f in s.get("files", []):
                if f.get("hash"):
                    used_hashes.add(f["hash"])

    for e in events:
        if e.get("hash"):
            used_hashes.add(e.get("hash"))

    tags = get_tag_list(root)
    for t in tags:
        ts = get_tag_snapshot(root, t)
        if ts:
            state = build_full_state_from_snapshots(root, ts)
            for info in state.values():
                if info.get("hash"):
                    used_hashes.add(info["hash"])

    blobs_dir = os.path.join(root, BLOBS_DIR)
    chunks_dir = os.path.join(root, CHUNKS_DIR)

    removed_blobs = 0
    freed_blobs = 0
    if os.path.exists(blobs_dir):
        for fn in os.listdir(blobs_dir):
            if fn not in used_hashes:
                fp = os.path.join(blobs_dir, fn)
                try:
                    freed_blobs += os.path.getsize(fp)
                    os.remove(fp)
                    removed_blobs += 1
                except OSError:
                    pass

    removed_chunks = 0
    freed_chunks = 0
    if os.path.exists(chunks_dir):
        for fn in os.listdir(chunks_dir):
            if fn not in used_hashes:
                fp = os.path.join(chunks_dir, fn)
                try:
                    freed_chunks += os.path.getsize(fp)
                    os.remove(fp)
                    removed_chunks += 1
                except OSError:
                    pass

    print(f"[fstimeline] 垃圾回收完成:")
    print(f"  删除孤立 blob: {removed_blobs} 个, 释放 {format_size(freed_blobs)}")
    if removed_chunks > 0:
        print(f"  删除孤立 chunk: {removed_chunks} 个, 释放 {format_size(freed_chunks)}")
    print(f"  总释放空间: {format_size(freed_blobs + freed_chunks)}")


def cmd_bench(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    ensure_branch_storage(root)
    ensure_main_branch(root)
    patterns = load_ignore_patterns(root)

    print("[fstimeline] 基准测试")
    print("=" * 60)

    print("\n1. 目录扫描速度")
    print("-" * 40)
    start = time.time()
    state = scan_directory(root, patterns)
    scan_time = time.time() - start
    file_count = len(state)
    total_size = sum(f["size"] for f in state.values())
    print(f"  文件数: {file_count}")
    print(f"  总大小: {format_size(total_size)}")
    print(f"  扫描耗时: {scan_time:.3f}s")
    if scan_time > 0:
        print(f"  扫描速度: {file_count / scan_time:.1f} 文件/秒")
        print(f"  扫描速度: {format_size(total_size / scan_time)}/秒")

    print("\n2. 快照创建速度")
    print("-" * 40)
    start = time.time()
    for rel, info in state.items():
        store_blob(root, os.path.join(root, rel), info["hash"])
    blob_time = time.time() - start

    start = time.time()
    now = time.time()
    snap_data = {
        "timestamp": now,
        "full": True,
        "branch": "bench",
        "file_count": file_count,
        "total_size": total_size,
        "files": list(state.values()),
    }
    save_snapshot(root, now + 1000000, snap_data)
    snap_time = time.time() - start

    print(f"  Blob 存储耗时: {blob_time:.3f}s")
    print(f"  快照写入耗时: {snap_time:.3f}s")
    print(f"  总耗时: {blob_time + snap_time:.3f}s")

    print("\n3. Checkout 速度")
    print("-" * 40)
    test_dir = os.path.join(root, ".fstimeline_bench_test")
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)
    os.makedirs(test_dir, exist_ok=True)

    start = time.time()
    for path, info in state.items():
        full = os.path.join(test_dir, path)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        blob_path = os.path.join(root, BLOBS_DIR, info["hash"]) if info.get("hash") else None
        if blob_path and os.path.exists(blob_path):
            shutil.copy2(blob_path, full)
    checkout_time = time.time() - start

    print(f"  Checkout 耗时: {checkout_time:.3f}s")
    if checkout_time > 0:
        print(f"  Checkout 速度: {file_count / checkout_time:.1f} 文件/秒")
        print(f"  Checkout 速度: {format_size(total_size / checkout_time)}/秒")

    shutil.rmtree(test_dir, ignore_errors=True)

    snap_file = os.path.join(root, SNAPSHOTS_DIR, f"{now + 1000000}.json")
    if os.path.exists(snap_file):
        os.remove(snap_file)

    print("\n" + "=" * 60)
    print(" 基准测试完成")
    print("=" * 60)


def format_size(size, signed=False):
    sign = "+" if signed and size > 0 else ("" if not signed else "")
    size_abs = abs(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_abs < 1024.0:
            if signed and size < 0:
                return f"-{size_abs:.1f}{unit}"
            return f"{sign}{size_abs:.1f}{unit}"
        size_abs /= 1024.0
    return f"{sign}{size_abs:.1f}PB"


def parse_timestamp(s):
    if s is None:
        return None
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass
    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def main():
    parser = argparse.ArgumentParser(prog="fstimeline", description="文件系统变更追踪与回放工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_watch = sub.add_parser("watch", help="监控指定目录变更")
    p_watch.add_argument("dir", help="要监控的目录")
    p_watch.add_argument("--interval", type=int, default=30, help="扫描间隔(秒)")
    p_watch.add_argument("--debounce", type=float, default=0, help="防抖窗口(秒)，合并短时间内同一文件的多次变更")
    p_watch.set_defaults(func=cmd_watch)

    p_snap = sub.add_parser("snapshot", help="创建当前目录快照")
    p_snap.add_argument("--full", action="store_true", help="强制创建完整快照")
    p_snap.add_argument("-m", "--message", help="快照备注信息")
    p_snap.set_defaults(func=cmd_snapshot)

    p_ls = sub.add_parser("list-snapshots", help="列出所有快照")
    p_ls.set_defaults(func=cmd_list_snapshots)

    p_co = sub.add_parser("checkout", help="恢复目录到指定时间点/分支/标签")
    p_co.add_argument("timestamp", help="目标时间戳、分支名或标签名")
    p_co.set_defaults(func=cmd_checkout)

    p_rs = sub.add_parser("restore", help="恢复单个文件到历史版本")
    p_rs.add_argument("path", help="文件相对路径")
    p_rs.add_argument("timestamp", help="目标时间戳、分支名或标签名")
    p_rs.set_defaults(func=cmd_restore)

    p_df = sub.add_parser("diff", help="对比两个时间点的差异")
    p_df.add_argument("snap1", help="起始时间戳/分支/标签")
    p_df.add_argument("snap2", help="结束时间戳/分支/标签")
    p_df.add_argument("-u", "--unified", action="store_true", help="显示文本文件的行级统一差异")
    p_df.set_defaults(func=cmd_diff)

    p_hist = sub.add_parser("history", help="查看指定文件的变更历史")
    p_hist.add_argument("path", help="文件相对路径")
    p_hist.set_defaults(func=cmd_history)

    p_cat = sub.add_parser("cat", help="查看文件在指定时间的历史内容")
    p_cat.add_argument("path", help="文件相对路径")
    p_cat.add_argument("timestamp", help="目标时间戳、分支名或标签名")
    p_cat.set_defaults(func=cmd_cat)

    p_st = sub.add_parser("status", help="显示追踪状态")
    p_st.set_defaults(func=cmd_status)

    p_prune = sub.add_parser("prune", help="清理旧快照")
    p_prune.add_argument("--days", type=int, default=30, help="保留最近N天")
    p_prune.set_defaults(func=cmd_prune)

    p_cmp = sub.add_parser("compact", help="压缩blob存储(去重)")
    p_cmp.set_defaults(func=cmd_compact)

    p_rep = sub.add_parser("report", help="生成变更报告")
    p_rep.add_argument("--since", help="起始时间 (如 2024-01-01)")
    p_rep.add_argument("--until", help="结束时间")
    p_rep.set_defaults(func=cmd_report)

    p_tl = sub.add_parser("timeline", help="ASCII时间轴可视化")
    p_tl.add_argument("--width", type=int, default=60, help="时间轴宽度")
    p_tl.set_defaults(func=cmd_timeline)

    p_branch = sub.add_parser("branch", help="管理分支")
    p_branch.add_argument("name", nargs="?", help="分支名称")
    p_branch.add_argument("-d", "--delete", help="删除指定分支")
    p_branch.set_defaults(func=cmd_branch)

    p_tag = sub.add_parser("tag", help="管理标签")
    p_tag.add_argument("name", nargs="?", help="标签名称")
    p_tag.add_argument("target", nargs="?", help="目标快照引用")
    p_tag.add_argument("-d", "--delete", help="删除指定标签")
    p_tag.add_argument("-m", "--message", help="标签备注信息")
    p_tag.set_defaults(func=cmd_tag)

    p_merge = sub.add_parser("merge", help="合并分支到当前分支")
    p_merge.add_argument("branch", help="要合并的源分支名")
    p_merge.set_defaults(func=cmd_merge)

    p_remote = sub.add_parser("remote", help="管理远程仓库")
    p_remote_sub = p_remote.add_subparsers(dest="action")
    p_remote_add = p_remote_sub.add_parser("add", help="添加远程仓库")
    p_remote_add.add_argument("name", help="远程仓库名称")
    p_remote_add.add_argument("path", help="远程仓库路径")
    p_remote_rm = p_remote_sub.add_parser("remove", help="移除远程仓库")
    p_remote_rm.add_argument("name", help="远程仓库名称")
    p_remote_set = p_remote_sub.add_parser("set-url", help="设置远程仓库URL")
    p_remote_set.add_argument("name", help="远程仓库名称")
    p_remote_set.add_argument("path", help="新的远程仓库路径")
    p_remote_list = p_remote_sub.add_parser("list", help="列出远程仓库")
    p_remote.set_defaults(func=cmd_remote, action="list")

    p_push = sub.add_parser("push", help="推送到远程仓库")
    p_push.add_argument("remote", help="远程仓库名称")
    p_push.add_argument("--dry-run", action="store_true", help="预览将要同步的数据量")
    p_push.set_defaults(func=cmd_push)

    p_pull = sub.add_parser("pull", help="从远程仓库拉取")
    p_pull.add_argument("remote", help="远程仓库名称")
    p_pull.add_argument("--dry-run", action="store_true", help="预览将要同步的数据量")
    p_pull.set_defaults(func=cmd_pull)

    p_clone = sub.add_parser("clone", help="克隆远程仓库")
    p_clone.add_argument("source", help="源仓库路径")
    p_clone.add_argument("dest", help="目标目录")
    p_clone.add_argument("--dry-run", action="store_true", help="预览将要克隆的数据量")
    p_clone.set_defaults(func=cmd_clone)

    p_hooks = sub.add_parser("hooks", help="管理钩子")
    p_hooks_sub = p_hooks.add_subparsers(dest="action")
    p_hooks_list = p_hooks_sub.add_parser("list", help="列出已安装的钩子")
    p_hooks_log = p_hooks_sub.add_parser("log", help="查看钩子执行记录")
    p_hooks_log.add_argument("--limit", type=int, default=20, help="显示最近N条记录")
    p_hooks.set_defaults(func=cmd_hooks, action="list")

    p_blame = sub.add_parser("blame", help="显示文件每行最后修改的快照")
    p_blame.add_argument("path", help="文件相对路径")
    p_blame.set_defaults(func=cmd_blame)

    p_patch = sub.add_parser("patch", help="导出快照差异为patch文件")
    p_patch.add_argument("snap1", help="起始快照引用")
    p_patch.add_argument("snap2", help="结束快照引用")
    p_patch.add_argument("-o", "--output", help="输出patch文件路径")
    p_patch.set_defaults(func=cmd_patch)

    p_apply = sub.add_parser("apply", help="应用patch文件")
    p_apply.add_argument("patch", help="patch文件路径")
    p_apply.set_defaults(func=cmd_apply)

    p_gc = sub.add_parser("gc", help="垃圾回收，清理不被引用的blob和chunk")
    p_gc.set_defaults(func=cmd_gc)

    p_bench = sub.add_parser("bench", help="基准测试")
    p_bench.set_defaults(func=cmd_bench)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
