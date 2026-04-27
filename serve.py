"""
serve.py — PEV-Eval Viewer HTTP Server

启动本地服务器，提供 API 接口供前端页面调用。
前端可以在浏览器里直接指定要解析的目录路径。

支持两种模式：
- 本地模式：直接指定本地目录路径
- 云端模式：通过文件上传处理评测数据

Usage:
    python serve.py                    # 默认 8080 端口
    python serve.py 9000               # 指定端口
    python serve.py --open             # 启动后自动打开浏览器
    python serve.py 9000 --open        # 指定端口 + 自动打开
"""

import json
import os
import sys
import urllib.parse
import tempfile
import zipfile
import uuid
import shutil
from http.server import HTTPServer, BaseHTTPRequestHandler

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner
import validator

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))


class ViewerHandler(BaseHTTPRequestHandler):
    """处理静态文件、API 请求和文件上传。"""

    def do_OPTIONS(self):
        """Handle CORS preflight for file:// or cross-origin access."""
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/upload":
            self._handle_upload()
        else:
            self._json_response(404, {"error": "not found"})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._serve_file("eval_viewer.html", "text/html; charset=utf-8")
        elif path == "/api/scan":
            self._handle_scan(qs)
        elif path == "/api/trial":
            self._handle_trial(qs)
        elif path == "/api/validate":
            self._handle_validate(qs)
        elif path.endswith(".html"):
            self._serve_file(path.lstrip("/"), "text/html; charset=utf-8")
        elif path.endswith(".js"):
            self._serve_file(path.lstrip("/"), "application/javascript; charset=utf-8")
        elif path.endswith(".css"):
            self._serve_file(path.lstrip("/"), "text/css; charset=utf-8")
        elif path.startswith("/viewer_trials/") and path.endswith(".json"):
            self._serve_file(path.lstrip("/"), "application/json; charset=utf-8")
        else:
            self._json_response(404, {"error": "not found"})

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _serve_file(self, filename, content_type):
        filepath = os.path.join(STATIC_DIR, filename)
        if not os.path.isfile(filepath):
            self._json_response(404, {"error": f"file not found: {filename}"})
            return
        with open(filepath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _json_response(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _get_root(self, qs):
        root = qs.get("root", [""])[0]
        if not root:
            return None, "missing 'root' parameter"
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            return None, f"directory not found: {root}"
        return root, None

    # ── API: /api/upload ──

    def _handle_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._json_response(400, {"error": "expected multipart/form-data"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 500 * 1024 * 1024:
            self._json_response(400, {"error": "file too large (max 500MB)"})
            return

        try:
            form_data = self.parse_multipart(content_length)
            file_field = form_data.get("file", None)

            if not file_field:
                self._json_response(400, {"error": "no file field 'file' in form data"})
                return

            filename = file_field.get("filename", "data.zip")
            data = file_field.get("data", b"")

            temp_dir = tempfile.mkdtemp(prefix="eval_viewer_")

            try:
                zip_path = os.path.join(temp_dir, filename)
                with open(zip_path, "wb") as f:
                    f.write(data)

                extract_dir = os.path.join(temp_dir, "extracted")
                os.makedirs(extract_dir, exist_ok=True)

                try:
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        zf.extractall(extract_dir)
                except zipfile.BadZipFile:
                    self._json_response(400, {"error": "invalid zip file"})
                    return

                scan_data = scanner.scan_root(extract_dir)

                for task in scan_data.get("tasks", []):
                    for trial in task.get("trials", []):
                        trial_dir = trial.get("dir_name")
                        if trial_dir:
                            try:
                                trial_detail = scanner.scan_trial_detail(extract_dir, task["task_id"], trial_dir)
                                if "error" not in trial_detail:
                                    trial["trajectory"] = trial_detail.get("trajectory", [])
                                    trial["code_files"] = trial_detail.get("code_files", {})
                            except Exception:
                                pass

                self._json_response(200, scan_data)

            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def parse_multipart(self, content_length):
        content_type = self.headers.get("Content-Type", "")
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
                break

        if not boundary:
            return {}

        body = self.rfile.read(content_length)
        result = {}

        parts = body.split(b"--" + boundary.encode())
        for part in parts:
            part = part.strip()
            if not part or part == b"--" or part == b"":
                continue

            if b"\r\n\r\n" not in part:
                continue

            header_section, content = part.split(b"\r\n\r\n", 1)

            header_text = header_section.decode("utf-8", errors="replace")
            headers = {}
            for line in header_text.split("\r\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    headers[key.strip().lower()] = val.strip()

            disposition = headers.get("content-disposition", "")
            field_name = None
            filename = None
            for item in disposition.split(";"):
                item = item.strip()
                if item.startswith("name="):
                    field_name = item[5:].strip('"')
                elif item.startswith("filename="):
                    filename = item[10:].strip('"')

            if field_name:
                data = content
                if content.endswith(b"\r\n"):
                    data = content[:-2]
                result[field_name] = {
                    "filename": filename,
                    "data": data
                }

        return result

    # ── API: /api/scan?root=... ──

    def _handle_scan(self, qs):
        root, err = self._get_root(qs)
        if err:
            self._json_response(400, {"error": err})
            return
        try:
            data = scanner.scan_root(root)
            self._json_response(200, data)
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    # ── API: /api/trial?root=...&task=...&trial=... ──

    def _handle_trial(self, qs):
        root, err = self._get_root(qs)
        if err:
            self._json_response(400, {"error": err})
            return
        task_id = qs.get("task", [""])[0]
        trial_dir = qs.get("trial", [""])[0]
        if not task_id or not trial_dir:
            self._json_response(400, {"error": "missing 'task' or 'trial' parameter"})
            return
        try:
            data = scanner.scan_trial_detail(root, task_id, trial_dir)
            if "error" in data:
                self._json_response(404, data)
            else:
                self._json_response(200, data)
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    # ── API: /api/validate?root=... ──

    def _handle_validate(self, qs):
        root, err = self._get_root(qs)
        if err:
            self._json_response(400, {"error": err})
            return
        try:
            result = validator.validate_path(root)
            self._json_response(200, result.to_dict())
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    # 抑制日志中的编码问题
    def log_message(self, fmt, *args):
        try:
            sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")
        except Exception:
            pass


class _WSGIBase:
    def __init__(self, environ, start_response):
        self.environ = environ
        self.start_response = start_response
        self._response_code = None
        self._response_headers = []

    def send_response(self, code):
        self._response_code = f"{code} {'OK' if code == 200 else 'Error'}"

    def send_header(self, name, value):
        self._response_headers.append((name, value))

    def end_headers(self):
        self.start_response(self._response_code, self._response_headers)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _serve_file(self, filename, content_type):
        filepath = os.path.join(STATIC_DIR, filename)
        if not os.path.isfile(filepath):
            self._json_response(404, {"error": f"file not found: {filename}"})
            return b""
        with open(filepath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.end_headers()
        return data

    def _json_response(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        return body


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    qs = environ.get("QUERY_STRING", "")
    parsed_qs = urllib.parse.parse_qs(qs)

    if path == "/" or path == "/index.html":
        handler = _WSGIBase(environ, start_response)
        data = handler._serve_file("eval_viewer.html", "text/html; charset=utf-8")
        return [data]

    elif path == "/api/scan":
        handler = _WSGIBase(environ, start_response)
        root = parsed_qs.get("root", [""])[0]
        if not root:
            body = handler._json_response(400, {"error": "missing 'root' parameter"})
            return [body]
        try:
            data = scanner.scan_root(root)
            body = handler._json_response(200, data)
        except Exception as e:
            body = handler._json_response(500, {"error": str(e)})
        return [body]

    elif path == "/api/trial":
        handler = _WSGIBase(environ, start_response)
        root = parsed_qs.get("root", [""])[0]
        if not root:
            body = handler._json_response(400, {"error": "missing 'root' parameter"})
            return [body]
        task_id = parsed_qs.get("task", [""])[0]
        trial_dir = parsed_qs.get("trial", [""])[0]
        if not task_id or not trial_dir:
            body = handler._json_response(400, {"error": "missing 'task' or 'trial' parameter"})
            return [body]
        try:
            data = scanner.scan_trial_detail(root, task_id, trial_dir)
            if "error" in data:
                body = handler._json_response(404, data)
            else:
                body = handler._json_response(200, data)
        except Exception as e:
            body = handler._json_response(500, {"error": str(e)})
        return [body]

    elif path == "/api/validate":
        handler = _WSGIBase(environ, start_response)
        root = parsed_qs.get("root", [""])[0]
        if not root:
            body = handler._json_response(400, {"error": "missing 'root' parameter"})
            return [body]
        try:
            result = validator.validate_path(root)
            body = handler._json_response(200, result.to_dict())
        except Exception as e:
            body = handler._json_response(500, {"error": str(e)})
        return [body]

    elif path == "/api/upload" and method == "POST":
        handler = _WSGIBase(environ, start_response)
        content_type = environ.get("CONTENT_TYPE", "")
        content_length = int(environ.get("CONTENT_LENGTH", 0))

        if "multipart/form-data" not in content_type:
            body = handler._json_response(400, {"error": "expected multipart/form-data"})
            return [body]

        if content_length > 500 * 1024 * 1024:
            body = handler._json_response(400, {"error": "file too large (max 500MB)"})
            return [body]

        try:
            wsgi_input = environ.get("wsgi.input")
            body_bytes = wsgi_input.read(content_length)

            from io import BytesIO
            body_stream = BytesIO(body_bytes)

            boundary = None
            for part in content_type.split(";"):
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part[9:].strip('"')
                    break

            if not boundary:
                body = handler._json_response(400, {"error": "no boundary found"})
                return [body]

            result = {}
            parts = body_bytes.split(b"--" + boundary.encode())
            for part in parts:
                part = part.strip()
                if not part or part == b"--" or part == b"":
                    continue

                if b"\r\n\r\n" not in part:
                    continue

                header_section, content = part.split(b"\r\n\r\n", 1)
                header_text = header_section.decode("utf-8", errors="replace")
                headers = {}
                for line in header_text.split("\r\n"):
                    if ":" in line:
                        key, val = line.split(":", 1)
                        headers[key.strip().lower()] = val.strip()

                disposition = headers.get("content-disposition", "")
                field_name = None
                filename = None
                for item in disposition.split(";"):
                    item = item.strip()
                    if item.startswith("name="):
                        field_name = item[5:].strip('"')
                    elif item.startswith("filename="):
                        filename = item[10:].strip('"')

                if field_name:
                    data = content
                    if content.endswith(b"\r\n"):
                        data = content[:-2]
                    result[field_name] = {"filename": filename, "data": data}

            file_field = result.get("file", None)
            if not file_field:
                body = handler._json_response(400, {"error": "no file field 'file' in form data"})
                return [body]

            filename = file_field.get("filename", "data.zip")
            file_data = file_field.get("data", b"")

            temp_dir = tempfile.mkdtemp(prefix="eval_viewer_")

            try:
                zip_path = os.path.join(temp_dir, filename)
                with open(zip_path, "wb") as f:
                    f.write(file_data)

                extract_dir = os.path.join(temp_dir, "extracted")
                os.makedirs(extract_dir, exist_ok=True)

                try:
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        zf.extractall(extract_dir)
                except zipfile.BadZipFile:
                    body = handler._json_response(400, {"error": "invalid zip file"})
                    return [body]

                scan_data = scanner.scan_root(extract_dir)

                for task in scan_data.get("tasks", []):
                    for trial in task.get("trials", []):
                        trial_dir = trial.get("dir_name")
                        if trial_dir:
                            try:
                                trial_detail = scanner.scan_trial_detail(extract_dir, task["task_id"], trial_dir)
                                if "error" not in trial_detail:
                                    trial["trajectory"] = trial_detail.get("trajectory", [])
                                    trial["code_files"] = trial_detail.get("code_files", {})
                            except Exception:
                                pass

                body = handler._json_response(200, scan_data)

            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

            return [body]

        except Exception as e:
            body = handler._json_response(500, {"error": str(e)})
            return [body]

    elif path.endswith(".html"):
        handler = _WSGIBase(environ, start_response)
        data = handler._serve_file(path.lstrip("/"), "text/html; charset=utf-8")
        return [data]

    elif path.endswith(".js"):
        handler = _WSGIBase(environ, start_response)
        data = handler._serve_file(path.lstrip("/"), "application/javascript; charset=utf-8")
        return [data]

    elif path.endswith(".css"):
        handler = _WSGIBase(environ, start_response)
        data = handler._serve_file(path.lstrip("/"), "text/css; charset=utf-8")
        return [data]

    elif path.startswith("/viewer_trials/") and path.endswith(".json"):
        handler = _WSGIBase(environ, start_response)
        data = handler._serve_file(path.lstrip("/"), "application/json; charset=utf-8")
        return [data]

    else:
        handler = _WSGIBase(environ, start_response)
        body = handler._json_response(404, {"error": "not found"})
        return [body]


def main():
    port = int(os.environ.get("PORT", 8080))
    auto_open = False

    for arg in sys.argv[1:]:
        if arg == "--open":
            auto_open = True
        elif arg.isdigit():
            port = int(arg)

    server = HTTPServer(("0.0.0.0", port), ViewerHandler)
    url = f"http://localhost:{port}"
    print(f"PEV-Eval Viewer Server")
    print(f"  URL: {url}")
    print(f"  Press Ctrl+C to stop\n")

    if auto_open:
        import webbrowser
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
