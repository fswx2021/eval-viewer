"""
scanner.py — 评测目录扫描与数据提取

纯逻辑模块，不含 HTTP / CLI。供 serve.py 调用。
扫描给定根目录下所有 *_bench* 文件夹，提取任务和 trial 数据。
"""

import ast
import json
import os
import re

TRAJ_TOOL_OUTPUT_LIMIT = 3000


# ── helpers ──────────────────────────────────────────────────────────────────

def read_text(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def read_json(path):
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def parse_toml_simple(path):
    """极简 TOML 解析，只支持 [section] + key = value。"""
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


def parse_model_from_dirname(dirname):
    parts = dirname.split("_", 2)
    if len(parts) >= 3:
        model = parts[2]
        model = re.sub(r'(?<=[a-z])-(\d+)_(\d+)', lambda m: f'-{m.group(1)}.{m.group(2)}', model)
        model = model.rstrip("-")
        return model or "(unknown)"
    return "(unknown)"


def parse_per_test_from_eval_report(report_path):
    """从 eval_report.txt 的 pytest 输出中解析每条测试的名称和状态。"""
    text = read_text(report_path)
    if not text:
        return []
    results = []
    clean = re.sub(r'\x1b\[[0-9;]*m', '', text)
    for line in clean.splitlines():
        m = re.search(r'::(\w+)::(\w+)\s+(PASSED|FAILED|SKIPPED)', line)
        if m:
            class_name, test_name, status = m.group(1), m.group(2), m.group(3)
            if not any(r["name"] == test_name for r in results):
                results.append({"name": test_name, "status": status, "class": class_name})
    return results


def _first_existing(paths):
    """返回第一个存在的路径，或 None。"""
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def _find_sub_eval_dir(task_dir):
    """对于多模型任务（如 game_dev_001），找到含完整评测结构的子目录。
    返回子目录路径，或 None（标准结构时不需要）。"""
    # 先检查根目录是否已有标准结构
    if (os.path.isfile(os.path.join(task_dir, "instruction.md"))
            or os.path.isdir(os.path.join(task_dir, "trials"))):
        return None
    # 找子目录中有 instruction.md + trials/ 的
    for entry in os.listdir(task_dir):
        sub = os.path.join(task_dir, entry)
        if not os.path.isdir(sub) or entry.startswith("."):
            continue
        if (os.path.isfile(os.path.join(sub, "instruction.md"))
                and os.path.isdir(os.path.join(sub, "trials"))):
            return sub
    return None


def get_test_weight(test_name, weight_map, default_weight=1):
    for pattern, weight in weight_map.items():
        if pattern in test_name:
            return weight
    return default_weight


# ── test annotation extraction ───────────────────────────────────────────────

def extract_test_annotations(test_file_path, weight_map, default_weight=1):
    source = read_text(test_file_path)
    if source is None:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            # 支持 Test* 和其他包含 test/Test 的类名
            class_name = node.name
            has_tests = any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_")
                for item in node.body
            )
            if not has_tests:
                continue
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


def detect_groundtruth_type(gt):
    if isinstance(gt, dict) and "field_constraints" in gt:
        return "constraint"
    return "freeform"


# ── scan index ───────────────────────────────────────────────────────────────

def find_bench_dirs(root):
    """找到 root 下所有 *_bench* 目录。如果 root 本身就是 bench 目录则返回自身。"""
    if "_bench" in os.path.basename(root):
        return [root]
    dirs = []
    for entry in os.listdir(root):
        full = os.path.join(root, entry)
        if os.path.isdir(full) and "_bench" in entry:
            dirs.append(full)
    return sorted(dirs)


def find_task_dirs(bench_dir):
    tasks = []
    for entry in sorted(os.listdir(bench_dir)):
        full = os.path.join(bench_dir, entry)
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, "task.toml")):
            tasks.append(full)
    return tasks


def scan_task(task_dir, bench_name):
    """扫描单个任务，返回索引数据（不含轨迹详情）。
    兼容多种目录布局：标准结构、多模型子目录结构、扁平 tests 结构。"""
    task_toml = parse_toml_simple(os.path.join(task_dir, "task.toml"))
    task_id = task_toml.get("task.id", os.path.basename(task_dir))
    title = task_toml.get("task.title", task_id)

    # 多模型任务：资源可能在子目录里（如 agent_game/、agent模型/）
    sub_eval = _find_sub_eval_dir(task_dir)
    base = sub_eval or task_dir

    # instruction.md：优先 task_dir，其次 sub_eval
    instruction = read_text(os.path.join(task_dir, "instruction.md")) or ""
    if not instruction and sub_eval:
        instruction = read_text(os.path.join(sub_eval, "instruction.md")) or ""

    # groundtruth.json
    gt_path = _first_existing([
        os.path.join(task_dir, "groundtruth.json"),
        os.path.join(base, "groundtruth.json"),
    ])
    gt = read_json(gt_path) if gt_path else None
    gt_type = detect_groundtruth_type(gt) if gt else "unknown"

    # reference.json
    ref_path = _first_existing([
        os.path.join(task_dir, "tests", "reference.json"),
        os.path.join(base, "tests", "reference.json"),
    ])
    ref = read_json(ref_path) if ref_path else {}
    if ref is None:
        ref = {}
    output_file = ref.get("output_file", "")
    check_cfg = ref.get("checks", {}).get("check_all_tests", {})
    weight_map = check_cfg.get("weight_map", {})
    default_weight = check_cfg.get("weight_per_test", 1)

    # test annotations：搜索多个候选位置
    test_annotations = []
    test_search_dirs = [
        os.path.join(task_dir, "tests", "grader_env", "tests"),
        os.path.join(base, "tests", "grader_env", "tests"),
        os.path.join(task_dir, "tests"),  # 扁平结构 fallback
        os.path.join(base, "tests"),
    ]
    found_tests = False
    for td in test_search_dirs:
        if not os.path.isdir(td):
            continue
        for tf in sorted(os.listdir(td)):
            if tf.startswith("test_") and tf.endswith(".py"):
                annos = extract_test_annotations(os.path.join(td, tf), weight_map, default_weight)
                if annos:
                    test_annotations.extend(annos)
                    found_tests = True
        if found_tests:
            break  # 用第一个有 test 的目录

    # trials
    env_info = ""
    trials = []
    trials_dir = _first_existing([
        os.path.join(task_dir, "trials"),
        os.path.join(base, "trials") if sub_eval else None,
    ])
    if trials_dir and os.path.isdir(trials_dir):
        for trial_name in sorted(os.listdir(trials_dir)):
            trial_path = os.path.join(trials_dir, trial_name)
            if not os.path.isdir(trial_path):
                continue

            model = parse_model_from_dirname(trial_name)

            # env_info：多个候选位置
            if not env_info:
                env_info = (read_text(os.path.join(trial_path, "workspace", "ENVIRONMENT_INFO.md"))
                            or read_text(os.path.join(trial_path, "ENVIRONMENT_INFO.md"))
                            or "")

            # outcome_grade.json：workspace/ 内或 trial 根目录
            grade = (read_json(os.path.join(trial_path, "workspace", "outcome_grade.json"))
                     or read_json(os.path.join(trial_path, "outcome_grade.json")))

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
                for detail in grade.get("details", []):
                    for pt in detail.get("per_test", []):
                        trial_summary["per_test"].append({"name": pt["name"], "status": pt["status"]})
                ps = grade.get("process_score")
                if ps:
                    trial_summary["process_score"] = ps

            # fallback: 从 eval_report.txt 解析 per_test
            if not trial_summary["per_test"]:
                report_path = os.path.join(trial_path, "eval_report.txt")
                report_tests = parse_per_test_from_eval_report(report_path)
                if report_tests:
                    trial_summary["per_test"] = [
                        {"name": t["name"], "status": t["status"], "class": t.get("class", "")}
                        for t in report_tests
                    ]

            trials.append(trial_summary)

    # 多模型任务：扫描根目录下其他模型子目录（非 sub_eval）的 outcome_grade.json
    if sub_eval:
        for entry in sorted(os.listdir(task_dir)):
            sub = os.path.join(task_dir, entry)
            if sub == sub_eval or not os.path.isdir(sub) or entry.startswith("."):
                continue
            # 有 outcome_grade.json 的子目录视为一个"手动评分的 trial"
            grade = read_json(os.path.join(sub, "outcome_grade.json"))
            if grade:
                trials.append({
                    "dir_name": entry,
                    "model": entry,
                    "score": grade.get("outcome_score_pct"),
                    "passed": grade.get("passed"),
                    "per_test": [{"name": pt["name"], "status": pt["status"]}
                                 for detail in grade.get("details", [])
                                 for pt in detail.get("per_test", [])],
                    "process_score": grade.get("process_score"),
                    "score_source": grade.get("score_source", "outcome"),
                })

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
        "output_file": output_file,
        "trials": trials,
    }


def scan_root(root):
    """扫描根目录，返回完整索引数据。"""
    root = os.path.abspath(root)
    benchmarks = []
    all_tasks = []

    for bench_dir in find_bench_dirs(root):
        bench_name = os.path.basename(bench_dir)
        task_dirs = find_task_dirs(bench_dir)
        task_ids = []
        for td in task_dirs:
            task_data = scan_task(td, bench_name)
            all_tasks.append(task_data)
            task_ids.append(task_data["task_id"])
        benchmarks.append({"name": bench_name, "task_ids": task_ids})

    return {
        "root": root,
        "benchmarks": benchmarks,
        "tasks": all_tasks,
    }


# ── trial detail (on-demand) ────────────────────────────────────────────────

def scan_trial_detail(root, task_id, trial_dir_name):
    """扫描单个 trial 的详细数据（轨迹 + 代码），按需调用。"""
    task_dir = _find_task_dir(root, task_id)
    if not task_dir:
        return {"error": f"Task '{task_id}' not found"}

    sub_eval = _find_sub_eval_dir(task_dir)
    base = sub_eval or task_dir

    ref_path = _first_existing([
        os.path.join(task_dir, "tests", "reference.json"),
        os.path.join(base, "tests", "reference.json"),
    ])
    ref = read_json(ref_path) if ref_path else {}
    if ref is None:
        ref = {}
    output_file = ref.get("output_file", "")

    # trial 可能在 task_dir/trials/ 或 base/trials/，也可能是多模型子目录
    trial_path = None
    for candidate in [
        os.path.join(task_dir, "trials", trial_dir_name),
        os.path.join(base, "trials", trial_dir_name) if sub_eval else None,
        os.path.join(task_dir, trial_dir_name),  # 多模型子目录
    ]:
        if candidate and os.path.isdir(candidate):
            trial_path = candidate
            break

    if not trial_path:
        return {"error": f"Trial '{trial_dir_name}' not found"}

    detail = {"trajectory": [], "code_files": {}}

    # trajectory
    traj_path = os.path.join(trial_path, "trajectory.jsonl")
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
                    if event.get("type") == "tool_use":
                        state = event.get("part", {}).get("state", {})
                        output = state.get("output", "")
                        if isinstance(output, str) and len(output) > TRAJ_TOOL_OUTPUT_LIMIT:
                            state["output"] = output[:TRAJ_TOOL_OUTPUT_LIMIT] + f"\n... [truncated, {len(output)} chars total]"
                        inp = state.get("input", {})
                        if isinstance(inp, dict):
                            content = inp.get("content", "")
                            if isinstance(content, str) and len(content) > TRAJ_TOOL_OUTPUT_LIMIT:
                                inp["content"] = content[:TRAJ_TOOL_OUTPUT_LIMIT] + f"\n... [truncated, {len(content)} chars total]"
                    detail["trajectory"].append(event)
        except Exception:
            pass

    # code files：workspace/ 内或 trial 根目录
    code_exts = {".py", ".js", ".sh", ".bat", ".ps1", ".html", ".htm", ".css"}
    for search_dir in [os.path.join(trial_path, "workspace"), trial_path]:
        if not os.path.isdir(search_dir):
            continue
        for fname in sorted(os.listdir(search_dir)):
            if fname in detail["code_files"]:
                continue  # workspace 优先
            fpath = os.path.join(search_dir, fname)
            if not os.path.isfile(fpath):
                continue
            _, ext = os.path.splitext(fname)
            skip = {"outcome_grade.json", "ENVIRONMENT_INFO.md", "instruction.md",
                    "trajectory.jsonl", "eval_report.txt"}
            if fname in skip:
                continue
            if ext in code_exts or fname == output_file:
                content = read_text(fpath)
                if content and len(content) < 500000:
                    detail["code_files"][fname] = content

    return detail


def _find_task_dir(root, task_id):
    """在 root 下找到 task_id 对应的目录。"""
    for bench_dir in find_bench_dirs(root):
        for entry in os.listdir(bench_dir):
            full = os.path.join(bench_dir, entry)
            if os.path.isdir(full):
                toml = parse_toml_simple(os.path.join(full, "task.toml"))
                tid = toml.get("task.id", entry)
                if tid == task_id:
                    return full
    return None
