"""
validator.py — 评测目录格式验证器

可作为模块被 serve.py 导入，也可以直接 CLI 运行：
    python validator.py                       # 提示输入路径
    python validator.py D:\\web_scraping_eval  # 验证指定目录
    python validator.py D:\\...\\task_001      # 验证单个任务
"""

import ast
import json
import os
import re
import sys

# ── 验证结果 ──────────────────────────────────────────────────────────────────

class ValidationResult:
    def __init__(self):
        self.errors = []    # (path, msg, fix)
        self.warnings = []  # (path, msg, fix)
        self.info = []      # (path, msg)

    def error(self, path, msg, fix=""):
        self.errors.append({"path": path, "msg": msg, "fix": fix})

    def warn(self, path, msg, fix=""):
        self.warnings.append({"path": path, "msg": msg, "fix": fix})

    def note(self, path, msg):
        self.info.append({"path": path, "msg": msg})

    @property
    def ok(self):
        return len(self.errors) == 0

    def to_dict(self):
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "info": self.info,
            "summary": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "info": len(self.info),
            },
        }


# ── TOML 简单解析 ────────────────────────────────────────────────────────────

def _parse_toml(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return None, str(e)
    result = {}
    section = ""
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r'^\[(.+)]$', stripped)
        if m:
            section = m.group(1)
            continue
        m = re.match(r'^(\w+)\s*=\s*(.+)$', stripped)
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
            result[key] = val
        else:
            return None, f"Line {i}: cannot parse '{stripped}'"
    return result, None


def _validate_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return None, "file encoding error, must be UTF-8"
    except Exception as e:
        return None, str(e)
    if not content.strip():
        return None, "file is empty"
    try:
        return json.loads(content), None
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"


def _validate_test_file(path):
    warnings = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except Exception as e:
        return 0, [f"cannot read: {e}"]
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return 0, [f"Python syntax error: {e}"]
    test_count = 0
    has_class = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            has_class = True
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
                    test_count += 1
                    if not ast.get_docstring(item):
                        warnings.append(f"{node.name}.{item.name}: missing docstring")
    if not has_class:
        warnings.append("no Test* class found")
    if test_count == 0:
        warnings.append("no test_* methods found")
    return test_count, warnings


TRIAL_DIR_RE = re.compile(r'^(\d{8})_(\d{6})_(.*)$')


# ── 核心验证 ──────────────────────────────────────────────────────────────────

def validate_task(task_dir, result):
    task_name = os.path.basename(task_dir)

    # task.toml
    toml_path = os.path.join(task_dir, "task.toml")
    if not os.path.isfile(toml_path):
        result.error(task_name, "missing task.toml (required to identify task directory)",
                     fix=f'[task]\nid = "{task_name}"\ntitle = "Task Title"\nworkspace_layout = "flat"\nneeds_network = true\n\n[taxonomy]\nL1 = "your_category"')
        return

    toml_data, toml_err = _parse_toml(toml_path)
    if toml_err:
        result.error(task_name, f"task.toml parse error: {toml_err}")
        return

    task_id = toml_data.get("task.id")
    if not task_id:
        result.error(task_name, "task.toml missing [task] id",
                     fix=f'id = "{task_name}"')
    elif task_id != task_name:
        result.warn(task_name, f"task.toml id='{task_id}' differs from dir name '{task_name}'")

    if not toml_data.get("task.title"):
        result.warn(task_name, "task.toml missing [task] title (viewer will show empty title)")

    if not toml_data.get("taxonomy.L1"):
        result.warn(task_name, "task.toml missing [taxonomy] L1 (used for viewer grouping)")

    # instruction.md
    inst_path = os.path.join(task_dir, "instruction.md")
    if not os.path.isfile(inst_path):
        result.warn(task_name, "missing instruction.md (Instruction tab will be empty)")
    else:
        try:
            with open(inst_path, "r", encoding="utf-8") as f:
                content = f.read()
            if len(content.strip()) < 10:
                result.warn(task_name, "instruction.md content too short (<10 chars)")
        except UnicodeDecodeError:
            result.error(task_name, "instruction.md encoding error, must be UTF-8")

    # groundtruth.json
    gt_path = os.path.join(task_dir, "groundtruth.json")
    if not os.path.isfile(gt_path):
        result.warn(task_name, "missing groundtruth.json (Groundtruth tab will be empty)")
    else:
        gt_data, gt_err = _validate_json(gt_path)
        if gt_err:
            result.error(task_name, f"groundtruth.json: {gt_err}")

    # tests/reference.json
    ref_path = os.path.join(task_dir, "tests", "reference.json")
    if not os.path.isfile(ref_path):
        result.warn(task_name, "missing tests/reference.json (no test weights)")
    else:
        ref_data, ref_err = _validate_json(ref_path)
        if ref_err:
            result.error(task_name, f"tests/reference.json: {ref_err}")
        elif isinstance(ref_data, dict):
            if "output_file" not in ref_data:
                result.warn(task_name, "tests/reference.json missing output_file")
            checks = ref_data.get("checks", {})
            if not checks:
                result.warn(task_name, "tests/reference.json missing checks config")
            for cn, cc in checks.items():
                wm = cc.get("weight_map")
                if wm and not isinstance(wm, dict):
                    result.error(task_name, f"checks.{cn}.weight_map must be object")

    # test files
    test_dir = os.path.join(task_dir, "tests", "grader_env", "tests")
    if os.path.isdir(test_dir):
        test_files = [f for f in os.listdir(test_dir) if f.startswith("test_") and f.endswith(".py")]
        if not test_files:
            result.warn(task_name, "tests/grader_env/tests/ has no test_*.py files")
        for tf in test_files:
            count, tw = _validate_test_file(os.path.join(test_dir, tf))
            if count == 0:
                result.warn(task_name, f"{tf}: no valid tests found")
            for w in tw:
                result.warn(task_name, f"{tf}: {w}")
    else:
        result.warn(task_name, "missing tests/grader_env/tests/ (Test Results tab will lack annotations)")

    # trials
    trials_dir = os.path.join(task_dir, "trials")
    if not os.path.isdir(trials_dir):
        result.note(task_name, "no trials/ directory (no eval results yet)")
        return

    trial_dirs = [d for d in sorted(os.listdir(trials_dir)) if os.path.isdir(os.path.join(trials_dir, d))]
    if not trial_dirs:
        result.note(task_name, "trials/ is empty")
        return

    for td in trial_dirs:
        trial_path = os.path.join(trials_dir, td)
        trial_rel = f"{task_name}/trials/{td}"

        m = TRIAL_DIR_RE.match(td)
        if not m:
            result.warn(trial_rel, f"dir name '{td}' does not match YYYYMMDD_HHMMSS_<model>",
                        fix="Expected: 20260421_120000_model-name")
        elif not m.group(3):
            result.warn(trial_rel, f"dir name '{td}' missing model slug after timestamp")

        traj_path = os.path.join(trial_path, "trajectory.jsonl")
        if not os.path.isfile(traj_path):
            result.warn(trial_rel, "missing trajectory.jsonl")
        else:
            try:
                with open(traj_path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                if first_line:
                    evt = json.loads(first_line)
                    if "type" not in evt:
                        result.warn(trial_rel, "trajectory.jsonl first line missing 'type' field")
            except json.JSONDecodeError:
                result.error(trial_rel, "trajectory.jsonl first line is not valid JSON (NDJSON format)")
            except UnicodeDecodeError:
                result.error(trial_rel, "trajectory.jsonl encoding error, must be UTF-8")

        ws_dir = os.path.join(trial_path, "workspace")
        if not os.path.isdir(ws_dir):
            result.warn(trial_rel, "missing workspace/ directory")
        else:
            grade_path = os.path.join(ws_dir, "outcome_grade.json")
            if os.path.isfile(grade_path):
                gd, ge = _validate_json(grade_path)
                if ge:
                    result.error(trial_rel, f"outcome_grade.json: {ge}")
                elif isinstance(gd, dict):
                    if "outcome_score_pct" not in gd and "outcome_score" not in gd:
                        result.warn(trial_rel, "outcome_grade.json missing score fields")
                    details = gd.get("details", [])
                    if isinstance(details, list):
                        for di, det in enumerate(details):
                            pt = det.get("per_test")
                            if isinstance(pt, list):
                                for pi, t in enumerate(pt):
                                    if not isinstance(t, dict) or "name" not in t or "status" not in t:
                                        result.warn(trial_rel, f"per_test[{pi}] missing name/status")
                                        break


def validate_bench(bench_dir, result):
    bench_name = os.path.basename(bench_dir)
    if "_bench" not in bench_name:
        result.warn(bench_name, f"dir name '{bench_name}' does not contain '_bench', scanner will not find it",
                    fix="Rename to include '_bench', e.g. my_task_bench")

    if not os.path.isdir(bench_dir):
        result.error(bench_name, f"path does not exist: {bench_dir}")
        return

    task_dirs = []
    for entry in sorted(os.listdir(bench_dir)):
        full = os.path.join(bench_dir, entry)
        if os.path.isdir(full):
            if os.path.isfile(os.path.join(full, "task.toml")):
                task_dirs.append(full)
            else:
                has_any = any(os.path.exists(os.path.join(full, f))
                             for f in ("instruction.md", "groundtruth.json"))
                has_trials = os.path.isdir(os.path.join(full, "trials"))
                if has_any or has_trials:
                    result.error(f"{bench_name}/{entry}",
                                 "looks like a task dir but missing task.toml",
                                 fix=f'Create {entry}/task.toml:\n[task]\nid = "{entry}"\ntitle = "Title"')

    if not task_dirs:
        result.error(bench_name, "no task directories found (subdirs with task.toml)")
        return

    result.note(bench_name, f"found {len(task_dirs)} tasks")
    for td in task_dirs:
        validate_task(td, result)


def validate_path(path):
    """验证指定路径，返回 ValidationResult。自动判断是 bench 还是 task 还是 root。"""
    result = ValidationResult()
    path = os.path.abspath(path)

    if not os.path.isdir(path):
        result.error(path, "path does not exist or is not a directory")
        return result

    # 是单个 task？
    if os.path.isfile(os.path.join(path, "task.toml")):
        validate_task(path, result)
        return result

    # 是 bench 目录？（含 _bench 或里面有 task.toml 子目录）
    has_tasks = any(os.path.isfile(os.path.join(path, d, "task.toml"))
                    for d in os.listdir(path) if os.path.isdir(os.path.join(path, d)))
    if "_bench" in os.path.basename(path) or has_tasks:
        validate_bench(path, result)
        return result

    # 当作 root，扫描 *_bench* 子目录
    bench_dirs = [os.path.join(path, d) for d in sorted(os.listdir(path))
                  if os.path.isdir(os.path.join(path, d)) and "_bench" in d]
    if not bench_dirs:
        result.error(os.path.basename(path), "no *_bench* directories found")
        return result

    result.note("root", f"found {len(bench_dirs)} bench dirs: {', '.join(os.path.basename(d) for d in bench_dirs)}")
    for bd in bench_dirs:
        validate_bench(bd, result)
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def _color(code, text):
    if not (sys.stdout.isatty() or os.environ.get("FORCE_COLOR")):
        return text
    return f"\033[{code}m{text}\033[0m"


def print_results(result):
    if result.errors:
        print(f"\n{_color('1;31', f'ERRORS ({len(result.errors)})')}  -- must fix\n")
        for item in result.errors:
            print(f"  x {_color('1', item['path'])}: {item['msg']}")
            if item['fix']:
                for line in item['fix'].split('\n'):
                    print(f"    -> {_color('2', line)}")
            print()

    if result.warnings:
        print(f"{_color('1;33', f'WARNINGS ({len(result.warnings)})')}  -- recommended\n")
        for item in result.warnings:
            print(f"  ! {_color('1', item['path'])}: {item['msg']}")
            if item['fix']:
                for line in item['fix'].split('\n'):
                    print(f"    -> {_color('2', line)}")
            print()

    if result.info:
        print(f"{_color('1;36', f'INFO ({len(result.info)})')}\n")
        for item in result.info:
            print(f"  . {_color('1', item['path'])}: {item['msg']}")
        print()

    if result.ok and not result.warnings:
        print(_color("1;32", "[PASS] All checks passed!"))
    elif result.ok:
        print(_color("1;33", f"[WARN] No errors, but {len(result.warnings)} warnings"))
    else:
        print(_color("1;31", f"[FAIL] {len(result.errors)} errors, {len(result.warnings)} warnings"))


def main():
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        target = os.path.abspath(sys.argv[1])
    else:
        target = input("Enter path to validate: ").strip()
        if not target:
            print("No path given.")
            sys.exit(1)
        target = os.path.abspath(target)

    print(f"Validating: {target}\n")
    result = validate_path(target)
    print_results(result)
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
