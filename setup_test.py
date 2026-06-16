#!/usr/bin/env python3
"""设置测试目录和预生成历史快照"""
import os
import sys
import json
import time
import shutil
import hashlib
from datetime import datetime, timedelta

TEST_DIR = "test_project"
FSTIMELINE_DIR = ".fstimeline"
BLOBS_DIR = os.path.join(FSTIMELINE_DIR, "blobs")
SNAPSHOTS_DIR = os.path.join(FSTIMELINE_DIR, "snapshots")
EVENTS_FILE = os.path.join(FSTIMELINE_DIR, "events.jsonl")


def sha256_file(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def store_blob(root, content, file_hash):
    blob_path = os.path.join(root, BLOBS_DIR, file_hash)
    if not os.path.exists(blob_path):
        with open(blob_path, "wb") as f:
            f.write(content)


def append_event(root, event):
    events_path = os.path.join(root, EVENTS_FILE)
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def save_snapshot(root, ts, data):
    path = os.path.join(root, SNAPSHOTS_DIR, f"{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    root = os.path.abspath(TEST_DIR)
    
    if os.path.exists(root):
        shutil.rmtree(root)
    
    os.makedirs(root)
    os.chdir(root)
    
    os.makedirs(BLOBS_DIR, exist_ok=True)
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    
    now = time.time()
    
    snap1_time = now - 3600 * 24 * 3
    snap2_time = now - 3600 * 24 * 2
    snap3_time = now - 3600 * 24 * 1
    
    files_v1 = {
        "README.md": b"# Test Project\n\nThis is a test project for fstimeline.\n",
        "main.py": b"#!/usr/bin/env python3\n\ndef main():\n    print('Hello, World!')\n\nif __name__ == '__main__':\n    main()\n",
        "utils.py": b"def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n",
        "config.json": b'{\n  "name": "test-project",\n  "version": "1.0.0",\n  "debug": false\n}\n',
        "data.txt": b"Line 1: sample data\nLine 2: more data\nLine 3: end of file\n",
        "docs/notes.md": b"# Notes\n\nSome quick notes about the project.\n",
        "src/__init__.py": b"",
        "src/module1.py": b"class Module1:\n    def __init__(self):\n        self.value = 42\n",
        "src/module2.py": b"def process_data(data):\n    return [x * 2 for x in data]\n",
        "tests/test_basic.py": b"import unittest\n\nclass TestBasic(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1 + 1, 2)\n",
    }
    
    for rel_path, content in files_v1.items():
        full_path = os.path.join(root, rel_path)
        parent = os.path.dirname(full_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(content)
        os.utime(full_path, (snap1_time, snap1_time))
    
    files_v1_info = {}
    for rel_path, content in files_v1.items():
        full_path = os.path.join(root, rel_path)
        st = os.stat(full_path)
        h = sha256_bytes(content)
        store_blob(root, content, h)
        files_v1_info[rel_path] = {
            "path": rel_path,
            "size": len(content),
            "mtime": snap1_time,
            "hash": h,
        }
    
    snap1 = {
        "timestamp": snap1_time,
        "full": True,
        "file_count": len(files_v1_info),
        "total_size": sum(f["size"] for f in files_v1_info.values()),
        "prev_file_count": 0,
        "prev_total_size": 0,
        "size_delta": sum(f["size"] for f in files_v1_info.values()),
        "files": list(files_v1_info.values()),
    }
    save_snapshot(root, snap1_time, snap1)
    
    print(f"快照 1 已创建: {datetime.fromtimestamp(snap1_time)}")
    print(f"  文件数: {snap1['file_count']}")
    print(f"  总大小: {snap1['total_size']} 字节")
    
    files_v2 = {}
    files_v2.update(files_v1_info)
    
    events_v1_to_v2 = []
    
    modified_v2 = {
        "main.py": b"#!/usr/bin/env python3\nimport sys\n\ndef main():\n    name = sys.argv[1] if len(sys.argv) > 1 else 'World'\n    print(f'Hello, {name}!')\n\nif __name__ == '__main__':\n    main()\n",
        "utils.py": b"def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n\ndef multiply(a, b):\n    return a * b\n\ndef divide(a, b):\n    if b == 0:\n        raise ValueError('Cannot divide by zero')\n    return a / b\n",
        "config.json": b'{\n  "name": "test-project",\n  "version": "1.1.0",\n  "debug": true,\n  "max_retries": 3\n}\n',
    }
    
    for rel_path, new_content in modified_v2.items():
        full_path = os.path.join(root, rel_path)
        with open(full_path, "wb") as f:
            f.write(new_content)
        os.utime(full_path, (snap2_time, snap2_time))
        
        old_info = files_v1_info[rel_path]
        new_hash = sha256_bytes(new_content)
        store_blob(root, new_content, new_hash)
        
        new_info = {
            "path": rel_path,
            "size": len(new_content),
            "mtime": snap2_time,
            "hash": new_hash,
        }
        files_v2[rel_path] = new_info
        
        events_v1_to_v2.append({
            "timestamp": snap2_time - 1800,
            "event": "modify",
            "path": rel_path,
            "hash": new_hash,
            "size": len(new_content),
            "old_hash": old_info["hash"],
            "old_size": old_info["size"],
        })
    
    deleted_v2 = ["data.txt"]
    for rel_path in deleted_v2:
        full_path = os.path.join(root, rel_path)
        if os.path.exists(full_path):
            os.remove(full_path)
        old_info = files_v1_info[rel_path]
        del files_v2[rel_path]
        
        events_v1_to_v2.append({
            "timestamp": snap2_time - 1200,
            "event": "delete",
            "path": rel_path,
            "hash": old_info["hash"],
            "size": old_info["size"],
        })
    
    added_v2 = {
        "src/helpers.py": b"def format_number(n):\n    return f'{n:,}'\n\ndef truncate(s, max_len=50):\n    if len(s) <= max_len:\n        return s\n    return s[:max_len - 3] + '...'\n",
        "docs/api.md": b"# API Documentation\n\n## Module1\n\n### Methods\n- `__init__()`: Initialize module\n",
    }
    
    for rel_path, content in added_v2.items():
        full_path = os.path.join(root, rel_path)
        parent = os.path.dirname(full_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(content)
        os.utime(full_path, (snap2_time, snap2_time))
        
        h = sha256_bytes(content)
        store_blob(root, content, h)
        
        new_info = {
            "path": rel_path,
            "size": len(content),
            "mtime": snap2_time,
            "hash": h,
        }
        files_v2[rel_path] = new_info
        
        events_v1_to_v2.append({
            "timestamp": snap2_time - 600,
            "event": "create",
            "path": rel_path,
            "hash": h,
            "size": len(content),
        })
    
    events_v1_to_v2.sort(key=lambda e: e["timestamp"])
    for ev in events_v1_to_v2:
        append_event(root, ev)
    
    diff_files = []
    for rel_path, new_info in files_v2.items():
        if rel_path not in files_v1_info:
            diff_files.append({**new_info, "status": "added"})
        elif files_v1_info[rel_path]["hash"] != new_info["hash"]:
            diff_files.append({**new_info, "status": "modified"})
    for rel_path in files_v1_info:
        if rel_path not in files_v2:
            old_info = files_v1_info[rel_path]
            diff_files.append({
                "path": rel_path,
                "status": "deleted",
                "hash": None,
                "size": 0,
                "mtime": snap2_time,
            })
    
    snap2 = {
        "timestamp": snap2_time,
        "full": False,
        "file_count": len(files_v2),
        "total_size": sum(f["size"] for f in files_v2.values()),
        "prev_file_count": snap1["file_count"],
        "prev_total_size": snap1["total_size"],
        "size_delta": sum(f["size"] for f in files_v2.values()) - snap1["total_size"],
        "files": diff_files,
    }
    save_snapshot(root, snap2_time, snap2)
    
    print(f"\n快照 2 已创建: {datetime.fromtimestamp(snap2_time)}")
    print(f"  文件数: {snap2['file_count']} (变更: {snap2['file_count'] - snap1['file_count']:+d})")
    print(f"  修改: {len(modified_v2)}, 删除: {len(deleted_v2)}, 新增: {len(added_v2)}")
    
    files_v3 = {}
    files_v3.update(files_v2)
    
    events_v2_to_v3 = []
    
    modified_v3 = {
        "README.md": b"# Test Project\n\nThis is a test project for fstimeline file system tracking tool.\n\n## Features\n- Watch directory changes\n- Create snapshots\n- Time travel to any point in history\n- View file history\n- Generate reports\n",
        "src/module1.py": b"class Module1:\n    \"\"\"Main module class with enhanced functionality.\"\"\"\n    \n    def __init__(self, initial_value=42):\n        self.value = initial_value\n    \n    def increment(self, amount=1):\n        self.value += amount\n        return self.value\n    \n    def get_value(self):\n        return self.value\n",
    }
    
    for rel_path, new_content in modified_v3.items():
        full_path = os.path.join(root, rel_path)
        with open(full_path, "wb") as f:
            f.write(new_content)
        os.utime(full_path, (snap3_time, snap3_time))
        
        old_info = files_v2[rel_path]
        new_hash = sha256_bytes(new_content)
        store_blob(root, new_content, new_hash)
        
        new_info = {
            "path": rel_path,
            "size": len(new_content),
            "mtime": snap3_time,
            "hash": new_hash,
        }
        files_v3[rel_path] = new_info
        
        events_v2_to_v3.append({
            "timestamp": snap3_time - 2400,
            "event": "modify",
            "path": rel_path,
            "hash": new_hash,
            "size": len(new_content),
            "old_hash": old_info["hash"],
            "old_size": old_info["size"],
        })
    
    deleted_v3 = ["src/module2.py"]
    for rel_path in deleted_v3:
        full_path = os.path.join(root, rel_path)
        if os.path.exists(full_path):
            os.remove(full_path)
        old_info = files_v2[rel_path]
        del files_v3[rel_path]
        
        events_v2_to_v3.append({
            "timestamp": snap3_time - 1800,
            "event": "delete",
            "path": rel_path,
            "hash": old_info["hash"],
            "size": old_info["size"],
        })
    
    added_v3 = {
        "src/module3.py": b"class DataProcessor:\n    def __init__(self, data):\n        self.data = data\n    \n    def filter_positive(self):\n        return [x for x in self.data if x > 0]\n    \n    def sum(self):\n        return sum(self.data)\n",
        "tests/test_utils.py": b"import unittest\nfrom utils import add, subtract, multiply, divide\n\nclass TestUtils(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n    \n    def test_subtract(self):\n        self.assertEqual(subtract(5, 3), 2)\n    \n    def test_multiply(self):\n        self.assertEqual(multiply(2, 3), 6)\n    \n    def test_divide(self):\n        self.assertEqual(divide(6, 2), 3)\n        with self.assertRaises(ValueError):\n            divide(1, 0)\n",
        "requirements.txt": b"# Python dependencies\nrequests>=2.28.0\nclick>=8.0.0\n",
    }
    
    for rel_path, content in added_v3.items():
        full_path = os.path.join(root, rel_path)
        parent = os.path.dirname(full_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(content)
        os.utime(full_path, (snap3_time, snap3_time))
        
        h = sha256_bytes(content)
        store_blob(root, content, h)
        
        new_info = {
            "path": rel_path,
            "size": len(content),
            "mtime": snap3_time,
            "hash": h,
        }
        files_v3[rel_path] = new_info
        
        events_v2_to_v3.append({
            "timestamp": snap3_time - 600,
            "event": "create",
            "path": rel_path,
            "hash": h,
            "size": len(content),
        })
    
    events_v2_to_v3.sort(key=lambda e: e["timestamp"])
    for ev in events_v2_to_v3:
        append_event(root, ev)
    
    diff_files_v3 = []
    for rel_path, new_info in files_v3.items():
        if rel_path not in files_v2:
            diff_files_v3.append({**new_info, "status": "added"})
        elif files_v2[rel_path]["hash"] != new_info["hash"]:
            diff_files_v3.append({**new_info, "status": "modified"})
    for rel_path in files_v2:
        if rel_path not in files_v3:
            old_info = files_v2[rel_path]
            diff_files_v3.append({
                "path": rel_path,
                "status": "deleted",
                "hash": None,
                "size": 0,
                "mtime": snap3_time,
            })
    
    snap3 = {
        "timestamp": snap3_time,
        "full": False,
        "file_count": len(files_v3),
        "total_size": sum(f["size"] for f in files_v3.values()),
        "prev_file_count": snap2["file_count"],
        "prev_total_size": snap2["total_size"],
        "size_delta": sum(f["size"] for f in files_v3.values()) - snap2["total_size"],
        "files": diff_files_v3,
    }
    save_snapshot(root, snap3_time, snap3)
    
    print(f"\n快照 3 已创建: {datetime.fromtimestamp(snap3_time)}")
    print(f"  文件数: {snap3['file_count']} (变更: {snap3['file_count'] - snap2['file_count']:+d})")
    print(f"  修改: {len(modified_v3)}, 删除: {len(deleted_v3)}, 新增: {len(added_v3)}")
    
    ignore_content = """# 忽略的文件和目录
*.log
*.tmp
__pycache__/
*.pyc
node_modules/
.env
"""
    with open(os.path.join(root, ".fstignore"), "w", encoding="utf-8") as f:
        f.write(ignore_content)
    
    print(f"\n{'='*50}")
    print(f"测试目录已创建: {root}")
    print(f"{'='*50}")
    print(f"\n三个历史快照:")
    print(f"  1. {datetime.fromtimestamp(snap1_time).strftime('%Y-%m-%d %H:%M:%S')} - 初始版本 (10个文件)")
    print(f"  2. {datetime.fromtimestamp(snap2_time).strftime('%Y-%m-%d %H:%M:%S')} - 修改/删除/新增 (11个文件)")
    print(f"  3. {datetime.fromtimestamp(snap3_time).strftime('%Y-%m-%d %H:%M:%S')} - 再次修改 (13个文件)")
    print(f"\n使用方法:")
    print(f"  cd {TEST_DIR}")
    print(f"  python ../fstimeline.py list-snapshots")
    print(f"  python ../fstimeline.py status")
    print(f"  python ../fstimeline.py history main.py")
    print(f"  python ../fstimeline.py diff {snap1_time:.0f} {snap3_time:.0f}")
    print(f"  python ../fstimeline.py report --since \"{datetime.fromtimestamp(snap1_time).strftime('%Y-%m-%d')}\"")


if __name__ == "__main__":
    main()
