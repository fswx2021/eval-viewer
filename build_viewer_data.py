#!/usr/bin/env python3
"""
build_viewer_data.py — 构建通用 eval viewer 的索引数据和按需轨迹文件。

扫描 *_bench* 目录，收集所有任务和 trial 数据，输出:
  - viewer_data.js     索引数据 (<script src> 加载)
  - viewer_trials/     按需加载的轨迹文件 (fetch 加载)

Usage:
    python build_viewer_data.py
"""

import ast
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_JS = os.path.join(ROOT, "viewer_data.js")
OUT_TRIALS_DIR = os.path.join(ROOT, "viewer_trials")

TRAJ_TOOL_OUTPUT_LIMIT = 3000  # 截断 tool output 字符数


# ── helpers ──────────────────────────────────────────────────────────────────

def read_text(path):
    """读取文本文件，返回字符串或 None。"""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def read_json(path):
    """读取 JSON 文件，返回 dict/list 或 None。"""
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def parse_toml_simple(path):
    """极简 TOML 解析，只支持 [section] + key = value 格式。"""
    text = read_text(path)
    if text is None:
        return {}
    result = {}
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^\[(.+)]$', line)
        if m:
            section = m.group(1)
            continue
        m = re.match(r'^(\w+)\s*=\s*(.+)$', line)
        if m:
            key = f"{section}.{m.group(1)}" if section else m.group(1)
            val = m.group(2).strip()
            # 解析值
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val.startswith('[') and val.endswith(']'):
                val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",") if v.strip()]
            elif val == "true":
                val = True
            elif val == "false":
                val = False
            else:
                try:
                    val = int(val)
                except ValueError:
                    try:
                        val = float(val)
                    except ValueError:
                        pass
            result[key] = val
    return result


def parse_per_test_from_eval_report(report_path):
    """从 eval_report.txt 的 pytest 输出中解析每条测试的名称和状态。"""
    text = read_text(report_path)
    if not text:
        return []
    results = []
    # 匹配 pytest 行: ...::ClassName::test_name PASSED/FAILED/SKIPPED
    # ANSI 转义码: \x1b[...m
    clean = re.sub(r'\x1b\[[0-9;]*m', '', text)
    for line in clean.splitlines():
        m = re.search(r'::(\w+)::(\w+)\s+(PASSED|FAILED|SKIPPED)', line)
        if m:
            class_name, test_name, status = m.group(1), m.group(2), m.group(3)
            # 去重（eval_report 中 FAILED 的测试名可能出现多次）
            if not any(r["name"] == test_name for r in results):
                results.append({"name": test_name, "status": status, "class": class_name})
    return results


def parse_model_from_dirname(dirname):
    """从 trial 目录名 YYYYMMDD_HHMMSS_<model> 提取模型名。"""
    parts = dirname.split("_", 2)
    if len(parts) >= 3:
        model = parts[2]
        # 还原常见转义: glm-4_7 -> glm-4.7, kimi-k2_5 -> kimi-k2.5
        model = re.sub(r'(?<=[a-z])-(\d+)_(\d+)', lambda m: f'-{m.group(1)}.{m.group(2)}', model)
        # doubao-seed-2-0-pro- -> doubao-seed-2.0-pro
        model = model.rstrip("-")
        return model or "(unknown)"
    return "(unknown)"


def get_test_weight(test_name, weight_map, default_weight=1):
    """按 reference.json 的 weight_map 规则匹配测试权重。"""
    for pattern, weight in weight_map.items():
        if pattern in test_name:
            return weight
    return default_weight


# ── test annotation extraction ───────────────────────────────────────────────

def extract_test_annotations(test_file_path, weight_map, default_weight=1):
    """用 ast 模块从 test_*.py 提取测试类、方法名、docstring 和权重。

    Returns list of dicts:
        [{"class": "TestFoo", "name": "test_bar", "docstring": "...", "weight": 3}, ...]
    """
    source = read_text(test_file_path)
    if source is None:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            class_name = node.name
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
                    doc = ast.get_docstring(item) or ""
                    weight = get_test_weight(item.name, weight_map, default_weight)
                    annotations.append({
                        "class": class_name,
                        "name": item.name,
                        "docstring": doc,
                        "weight": weight,
                    })
    return annotations


# ── groundtruth type detection ───────────────────────────────────────────────

def detect_groundtruth_type(gt):
    """检测 groundtruth 类型: constraint (有 field_constraints) 或 freeform。"""
    if isinstance(gt, dict) and "field_constraints" in gt:
        return "constraint"
    return "freeform"


# ── trial detail builder (for viewer_trials/) ────────────────────────────────

def build_trial_detail(trial_dir, output_file_name):
    """构建单个 trial 的详细数据（轨迹 + 代码），保存到 viewer_trials/。"""
    detail = {"trajectory": [], "code_files": {}}

    # 1. 解析 trajectory.jsonl
    traj_path = os.path.join(trial_dir, "trajectory.jsonl")
    if os.path.isfile(traj_path):
        try:
            with open(traj_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    evt_type = event.get("type", "")
                    part = event.get("part", {})

                    # 截断 tool output
                    if evt_type == "tool_use":
                        state = part.get("state", {})
                        output = state.get("output", "")
                        if isinstance(output, str) and len(output) > TRAJ_TOOL_OUTPUT_LIMIT:
                            state["output"] = output[:TRAJ_TOOL_OUTPUT_LIMIT] + f"\n... [truncated, {len(output)} chars total]"
                        # 截断 tool input content (write_file)
                        inp = state.get("input", {})
                        if isinstance(inp, dict):
                            content = inp.get("content", "")
                            if isinstance(content, str) and len(content) > TRAJ_TOOL_OUTPUT_LIMIT:
                                inp["content"] = content[:TRAJ_TOOL_OUTPUT_LIMIT] + f"\n... [truncated, {len(content)} chars total]"

                    detail["trajectory"].append(event)
        except Exception:
            pass

    # 2. 收集 workspace 里的代码文件
    ws_dir = os.path.join(trial_dir, "workspace")
    if os.path.isdir(ws_dir):
        for fname in os.listdir(ws_dir):
            fpath = os.path.join(ws_dir, fname)
            if not os.path.isfile(fpath):
                continue
            # 收集 .py .js .sh .json(仅输出文件) .txt 文件
            _, ext = os.path.splitext(fname)
            if ext in (".py", ".js", ".sh", ".bat", ".ps1"):
                content = read_text(fpath)
                if content:
                    detail["code_files"][fname] = content
            elif fname == output_file_name:
                content = read_text(fpath)
                if content:
                    detail["code_files"][fname] = content

    return detail


# ── main build logic ─────────────────────────────────────────────────────────

def find_bench_dirs():
    """找到所有 *_bench* 目录。"""
    dirs = []
    for entry in os.listdir(ROOT):
        full = os.path.join(ROOT, entry)
        if os.path.isdir(full) and "_bench" in entry:
            dirs.append(full)
    return sorted(dirs)


def find_task_dirs(bench_dir):
    """找到 bench 下所有任务目录（有 task.toml 的目录）。"""
    tasks = []
    for entry in sorted(os.listdir(bench_dir)):
        full = os.path.join(bench_dir, entry)
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, "task.toml")):
            tasks.append(full)
    return tasks


def build_task_data(task_dir, bench_name):
    """构建单个任务的索引数据。"""
    task_toml = parse_toml_simple(os.path.join(task_dir, "task.toml"))
    task_id = task_toml.get("task.id", os.path.basename(task_dir))
    title = task_toml.get("task.title", task_id)

    # instruction
    instruction = read_text(os.path.join(task_dir, "instruction.md")) or ""

    # groundtruth
    gt = read_json(os.path.join(task_dir, "groundtruth.json"))
    gt_type = detect_groundtruth_type(gt) if gt else "unknown"

    # reference.json
    ref = read_json(os.path.join(task_dir, "tests", "reference.json")) or {}
    output_file = ref.get("output_file", "")
    check_cfg = ref.get("checks", {}).get("check_all_tests", {})
    weight_map = check_cfg.get("weight_map", {})
    default_weight = check_cfg.get("weight_per_test", 1)

    # test annotations
    test_dir = os.path.join(task_dir, "tests", "grader_env", "tests")
    test_annotations = []
    if os.path.isdir(test_dir):
        for tf in sorted(os.listdir(test_dir)):
            if tf.startswith("test_") and tf.endswith(".py"):
                test_annotations.extend(
                    extract_test_annotations(
                        os.path.join(test_dir, tf), weight_map, default_weight
                    )
                )

    # ENVIRONMENT_INFO.md (从首个 trial 取)
    env_info = ""

    # trials
    trials = []
    trials_dir = os.path.join(task_dir, "trials")
    if os.path.isdir(trials_dir):
        for trial_name in sorted(os.listdir(trials_dir)):
            trial_path = os.path.join(trials_dir, trial_name)
            if not os.path.isdir(trial_path):
                continue

            model = parse_model_from_dirname(trial_name)

            # environment info
            if not env_info:
                env_path = os.path.join(trial_path, "workspace", "ENVIRONMENT_INFO.md")
                env_info = read_text(env_path) or ""

            # outcome_grade (try trial root first, then workspace/)
            grade = read_json(os.path.join(trial_path, "outcome_grade.json")) or \
                    read_json(os.path.join(trial_path, "workspace", "outcome_grade.json"))

            trial_summary = {
                "dir_name": trial_name,
                "name": trial_name,
                "model": model,
                "score": None,
                "passed": None,
                "per_test": [],
                "process_score": None,
                "score_source": None,
            }

            if grade:
                trial_summary["score"] = grade.get("outcome_score_pct")
                trial_summary["passed"] = grade.get("passed")
                trial_summary["score_source"] = grade.get("score_source", "outcome")
                # per_test
                for detail in grade.get("details", []):
                    for pt in detail.get("per_test", []):
                        trial_summary["per_test"].append({
                            "name": pt["name"],
                            "status": pt["status"],
                        })
                # process_score
                ps = grade.get("process_score")
                if ps:
                    trial_summary["process_score"] = ps

            # fallback: parse per_test from eval_report.txt if not available
            if not trial_summary["per_test"]:
                report_path = os.path.join(trial_path, "eval_report.txt")
                report_tests = parse_per_test_from_eval_report(report_path)
                if report_tests:
                    trial_summary["per_test"] = [
                        {"name": t["name"], "status": t["status"], "class": t.get("class", "")} for t in report_tests
                    ]

            trials.append(trial_summary)

            # 构建 trial detail 文件 (轨迹 + 代码)
            trial_detail = build_trial_detail(trial_path, output_file)
            trial_out_dir = os.path.join(OUT_TRIALS_DIR, task_id)
            os.makedirs(trial_out_dir, exist_ok=True)
            trial_out_path = os.path.join(trial_out_dir, f"{trial_name}.json")
            with open(trial_out_path, "w", encoding="utf-8") as f:
                json.dump(trial_detail, f, ensure_ascii=False)
            print(f"  Trial: {trial_name} ({model}) -> {os.path.relpath(trial_out_path, ROOT)}")

    return {
        "task_id": task_id,
        "title": title,
        "bench": bench_name,
        "taxonomy": {
            "L1": task_toml.get("taxonomy.L1", ""),
            "capability": task_toml.get("taxonomy.capability", ""),
            "complexity": task_toml.get("taxonomy.complexity", ""),
        },
        "instruction": instruction,
        "groundtruth": gt,
        "groundtruth_type": gt_type,
        "test_annotations": test_annotations,
        "weight_map": weight_map,
        "env_info": env_info,
        "trials": trials,
    }


def main():
    print("=" * 60)
    print("Building viewer data...")
    print("=" * 60)

    os.makedirs(OUT_TRIALS_DIR, exist_ok=True)

    benchmarks = []
    all_tasks = []

    bench_dirs = find_bench_dirs()
    if not bench_dirs:
        print("ERROR: No *_bench* directories found in", ROOT)
        sys.exit(1)

    for bench_dir in bench_dirs:
        bench_name = os.path.basename(bench_dir)
        print(f"\nBenchmark: {bench_name}")

        task_dirs = find_task_dirs(bench_dir)
        task_ids = []

        for task_dir in task_dirs:
            task_id = os.path.basename(task_dir)
            print(f"  Task: {task_id}")
            task_data = build_task_data(task_dir, bench_name)
            all_tasks.append(task_data)
            task_ids.append(task_data["task_id"])

        benchmarks.append({
            "name": bench_name,
            "task_ids": task_ids,
        })

    # 输出 viewer_data.js
    index_data = {
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "benchmarks": benchmarks,
        "tasks": all_tasks,
    }

    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("// Auto-generated by build_viewer_data.py — do not edit\n")
        f.write("window.VIEWER_DATA = ")
        json.dump(index_data, f, ensure_ascii=False)
        f.write(";\n")

    # 统计
    total_trials = sum(len(t["trials"]) for t in all_tasks)
    js_size = os.path.getsize(OUT_JS)
    print(f"\n{'=' * 60}")
    print(f"Done!")
    print(f"  Benchmarks: {len(benchmarks)}")
    print(f"  Tasks:      {len(all_tasks)}")
    print(f"  Trials:     {total_trials}")
    print(f"  viewer_data.js: {js_size / 1024 / 1024:.1f} MB")
    print(f"  viewer_trials/: {sum(len(os.listdir(os.path.join(OUT_TRIALS_DIR, d))) for d in os.listdir(OUT_TRIALS_DIR) if os.path.isdir(os.path.join(OUT_TRIALS_DIR, d)))} files")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
