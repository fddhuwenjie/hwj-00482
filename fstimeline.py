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
EVENTS_FILE = os.path.join(FSTIMELINE_DIR, "events.jsonl")
SNAPSHOTS_DIR = os.path.join(FSTIMELINE_DIR, "snapshots")
IGNORE_FILE = ".fstignore"
STATE_FILE = os.path.join(FSTIMELINE_DIR, "state.json")


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


def cmd_watch(args):
    root = os.path.abspath(args.dir)
    os.chdir(root)
    ensure_storage(root)
    patterns = load_ignore_patterns(root)
    interval = args.interval

    print(f"[fstimeline] 开始监控目录: {root}")
    print(f"[fstimeline] 扫描间隔: {interval}秒")
    print(f"[fstimeline] 按 Ctrl+C 停止")

    state = scan_directory(root, patterns)
    for rel, info in state.items():
        store_blob(root, os.path.join(root, rel), info["hash"])

    try:
        while True:
            time.sleep(interval)
            new_state = scan_directory(root, patterns)
            now = time.time()

            old_paths = set(state.keys())
            new_paths = set(new_state.keys())

            for p in new_paths - old_paths:
                info = new_state[p]
                store_blob(root, os.path.join(root, p), info["hash"])
                append_event(root, {
                    "timestamp": now,
                    "event": "create",
                    "path": p,
                    "hash": info["hash"],
                    "size": info["size"],
                })
                print(f"  创建: {p}")

            for p in old_paths - new_paths:
                old_info = state[p]
                append_event(root, {
                    "timestamp": now,
                    "event": "delete",
                    "path": p,
                    "hash": old_info["hash"],
                    "size": old_info["size"],
                })
                print(f"  删除: {p}")

            for p in old_paths & new_paths:
                old_info = state[p]
                new_info = new_state[p]
                if old_info["hash"] != new_info["hash"] or old_info["size"] != new_info["size"]:
                    store_blob(root, os.path.join(root, p), new_info["hash"])
                    append_event(root, {
                        "timestamp": now,
                        "event": "modify",
                        "path": p,
                        "hash": new_info["hash"],
                        "size": new_info["size"],
                        "old_hash": old_info["hash"],
                        "old_size": old_info["size"],
                    })
                    print(f"  修改: {p}")

            state = new_state
    except KeyboardInterrupt:
        print("\n[fstimeline] 监控已停止")


def cmd_snapshot(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    patterns = load_ignore_patterns(root)

    current = scan_directory(root, patterns)
    for rel, info in current.items():
        store_blob(root, os.path.join(root, rel), info["hash"])

    snaps = get_snapshot_list(root)
    now = time.time()

    full = True
    files = []
    if snaps and not args.full:
        last_ts = snaps[-1]
        last_snap = load_snapshot(root, last_ts)
        last_state = {f["path"]: f for f in last_snap.get("files", [])}
        full = False
        for p, info in current.items():
            if p not in last_state or last_state[p]["hash"] != info["hash"]:
                files.append({**info, "status": "modified" if p in last_state else "added"})
        for p in last_state:
            if p not in current:
                files.append({"path": p, "status": "deleted", "hash": None, "size": 0, "mtime": now})
    else:
        full = True
        files = list(current.values())

    snap_data = {
        "timestamp": now,
        "full": full,
        "file_count": len(current),
        "total_size": sum(f["size"] for f in current.values()),
        "files": files,
    }
    if snaps:
        last_snap = load_snapshot(root, snaps[-1])
        snap_data["prev_file_count"] = last_snap.get("file_count", 0)
        snap_data["prev_total_size"] = last_snap.get("total_size", 0)
        snap_data["size_delta"] = snap_data["total_size"] - snap_data["prev_total_size"]
    else:
        snap_data["prev_file_count"] = 0
        snap_data["prev_total_size"] = 0
        snap_data["size_delta"] = snap_data["total_size"]

    save_snapshot(root, now, snap_data)
    dt = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[fstimeline] 快照已创建: {dt}")
    print(f"  文件数: {snap_data['file_count']} (变更: {snap_data['file_count'] - snap_data['prev_file_count']:+d})")
    print(f"  总大小: {format_size(snap_data['total_size'])} (变更: {format_size(snap_data['size_delta'], True)})")
    print(f"  类型: {'完整' if full else '增量'}")


def cmd_list_snapshots(args):
    root = os.path.abspath(".")
    snaps = get_snapshot_list(root)
    if not snaps:
        print("[fstimeline] 暂无快照")
        return

    print(f"{'时间':<20} {'文件数':>10} {'总大小':>12} {'大小变化':>12} {'类型':>6}")
    print("-" * 62)
    for ts in snaps:
        s = load_snapshot(root, ts)
        if not s:
            continue
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        fc = s.get("file_count", 0)
        ts_ = s.get("total_size", 0)
        delta = s.get("size_delta", 0)
        ftype = "完整" if s.get("full", True) else "增量"
        print(f"{dt:<20} {fc:>10} {format_size(ts_):>12} {format_size(delta, True):>12} {ftype:>6}")


def cmd_checkout(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    target_ts = parse_timestamp(args.timestamp)
    if target_ts is None:
        print("[fstimeline] 错误: 无效的时间戳格式", file=sys.stderr)
        sys.exit(1)

    patterns = load_ignore_patterns(root)
    target_state = reconstruct_state_at(root, target_ts)
    current = scan_directory(root, patterns)

    print(f"[fstimeline] 恢复到: {datetime.fromtimestamp(target_ts).strftime('%Y-%m-%d %H:%M:%S')}")

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


def cmd_restore(args):
    root = os.path.abspath(".")
    ensure_storage(root)
    target_ts = parse_timestamp(args.timestamp)
    if target_ts is None:
        print("[fstimeline] 错误: 无效的时间戳格式", file=sys.stderr)
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


def cmd_diff(args):
    root = os.path.abspath(".")
    ensure_storage(root)

    def get_state(ts_str):
        ts = parse_timestamp(ts_str)
        if ts is None:
            return None, None
        return ts, reconstruct_state_at(root, ts)

    ts1, s1 = get_state(args.snap1)
    ts2, s2 = get_state(args.snap2)
    if s1 is None or s2 is None:
        print("[fstimeline] 错误: 无效的时间戳", file=sys.stderr)
        sys.exit(1)

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
    target_ts = parse_timestamp(args.timestamp)
    if target_ts is None:
        print("[fstimeline] 错误: 无效的时间戳格式", file=sys.stderr)
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

    fst_size = 0
    fst_dir = os.path.join(root, FSTIMELINE_DIR)
    for dp, _, fns in os.walk(fst_dir):
        for fn in fns:
            fst_size += os.path.getsize(os.path.join(dp, fn))

    print("=" * 50)
    print(" fstimeline 状态报告")
    print("=" * 50)
    print(f"  监控目录:       {root}")
    print(f"  当前文件数:     {len(current)}")
    print(f"  当前总大小:     {format_size(sum(f['size'] for f in current.values()))}")
    print(f"  快照数量:       {len(snaps)}")
    print(f"  事件记录数:     {len(events)}")
    print(f"  Blob 文件数:    {blob_count}")
    print(f"  Blob 总大小:    {format_size(total_blobs_size)}")
    print(f"  .fstimeline大小:{format_size(fst_size)}")
    if snaps:
        print(f"  最早快照:       {datetime.fromtimestamp(snaps[0]).strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  最新快照:       {datetime.fromtimestamp(snaps[-1]).strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)


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
    p_watch.set_defaults(func=cmd_watch)

    p_snap = sub.add_parser("snapshot", help="创建当前目录快照")
    p_snap.add_argument("--full", action="store_true", help="强制创建完整快照")
    p_snap.set_defaults(func=cmd_snapshot)

    p_ls = sub.add_parser("list-snapshots", help="列出所有快照")
    p_ls.set_defaults(func=cmd_list_snapshots)

    p_co = sub.add_parser("checkout", help="恢复目录到指定时间点")
    p_co.add_argument("timestamp", help="目标时间戳或日期时间字符串")
    p_co.set_defaults(func=cmd_checkout)

    p_rs = sub.add_parser("restore", help="恢复单个文件到历史版本")
    p_rs.add_argument("path", help="文件相对路径")
    p_rs.add_argument("timestamp", help="目标时间戳或日期时间字符串")
    p_rs.set_defaults(func=cmd_restore)

    p_df = sub.add_parser("diff", help="对比两个时间点的差异")
    p_df.add_argument("snap1", help="起始时间戳")
    p_df.add_argument("snap2", help="结束时间戳")
    p_df.set_defaults(func=cmd_diff)

    p_hist = sub.add_parser("history", help="查看指定文件的变更历史")
    p_hist.add_argument("path", help="文件相对路径")
    p_hist.set_defaults(func=cmd_history)

    p_cat = sub.add_parser("cat", help="查看文件在指定时间的历史内容")
    p_cat.add_argument("path", help="文件相对路径")
    p_cat.add_argument("timestamp", help="目标时间戳")
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
