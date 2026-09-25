"""Local Chinese web interface for Zotero PDF Harvester."""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request

from .cli import Harvester, apply_env, env_file

APP = Flask(__name__)
ROOT = Path.cwd()
STATE = {"running": False, "lines": [], "started": "", "exit_code": None}
LOCK = threading.Lock()

PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Zotero PDF 补全</title>
<style>body{font:15px system-ui;margin:0;background:#f4f6f8;color:#18212b}.wrap{max-width:860px;margin:35px auto;padding:0 18px}h1{margin-bottom:6px}.card{background:white;border-radius:12px;padding:22px;margin:16px 0;box-shadow:0 2px 14px #0001}label{display:block;margin:12px 0 5px;font-weight:600}input,select,button{font:inherit;padding:10px;border:1px solid #ccd3db;border-radius:7px}input,select{width:100%;box-sizing:border-box}button{background:#1769aa;color:white;border:0;cursor:pointer;margin-top:18px}button:disabled{opacity:.55}.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}.check{font-weight:400}.check input{width:auto}pre{background:#111923;color:#dce7f2;min-height:180px;max-height:430px;overflow:auto;padding:14px;border-radius:8px;white-space:pre-wrap}.hint{color:#5d6874;font-size:13px}.warn{background:#fff5db;border-left:4px solid #e6a700;padding:10px}</style></head><body><div class="wrap">
<h1>Zotero PDF 批量补全</h1><div class="hint">公开来源优先；校园网/学校登录模式只使用你本人有权访问的订阅。</div>
<div class="card"><form id="f"><label>联系邮箱</label><input name="email" type="email" required value="{{ email }}" placeholder="用于公开学术 API 的礼貌访问">
<label>Zotero 分类</label><select name="collection" required>{% for c in collections %}<option value="{{ c.key }}">{{ c.name }}</option>{% endfor %}</select>
<div class="row"><div><label>并发数</label><input name="workers" type="number" min="1" max="32" value="12"></div><div><label>单站超时（秒）</label><input name="timeout" type="number" min="5" max="120" value="18"></div></div>
<label class="check"><input name="institutional" type="checkbox" checked> 启用校园网/机构浏览器（首次会打开独立浏览器，请完成学校登录；以后自动复用状态）</label>
<div class="warn">请先启动 Zotero。浏览器状态仅保存在本机，不会上传 GitHub。</div><button id="go">开始补全</button></form></div>
<div class="card"><b>运行日志</b><pre id="log">尚未开始</pre></div></div>
<script>const f=document.querySelector('#f'),log=document.querySelector('#log'),go=document.querySelector('#go');
f.onsubmit=async e=>{e.preventDefault();go.disabled=true;let d=Object.fromEntries(new FormData(f));d.institutional=document.querySelector('[name=institutional]').checked;let r=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});if(!r.ok){alert(await r.text());go.disabled=false}};
setInterval(async()=>{let s=await (await fetch('/status')).json();log.textContent=s.lines.join('\n')||'尚未开始';log.scrollTop=log.scrollHeight;go.disabled=s.running},1000);</script></body></html>'''


def _load_collections(email: str):
    try:
        h = Harvester(email or "local@example.invalid", ROOT / "downloads", fallback_cli="")
        return [{"key": x["key"], "name": x.get("data", {}).get("name", x["key"])} for x in h.collections()]
    except Exception:
        return []


@APP.get("/")
def index():
    env = env_file(ROOT / ".env")
    email = env.get("ZPH_EMAIL", "")
    return render_template_string(PAGE, email=email, collections=_load_collections(email))


def _execute(cmd):
    with LOCK:
        STATE.update(running=True, lines=[], started=time.strftime("%Y-%m-%d %H:%M:%S"), exit_code=None)
    process = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in process.stdout or []:
        with LOCK:
            STATE["lines"].append(line.rstrip())
            STATE["lines"] = STATE["lines"][-500:]
    code = process.wait()
    with LOCK:
        STATE.update(running=False, exit_code=code)
        STATE["lines"].append("完成。" if code == 0 else f"运行失败，退出码 {code}")


@APP.post("/run")
def run_job():
    data = request.get_json(force=True)
    with LOCK:
        if STATE["running"]:
            return Response("已有任务正在运行", status=409)
    cmd = [sys.executable, "-m", "zotero_pdf_harvester.cli", "--collection", str(data["collection"]),
           "--email", str(data["email"]), "--workers", str(int(data.get("workers", 12))),
           "--timeout", str(int(data.get("timeout", 18)))]
    if data.get("institutional"):
        cmd.append("--institutional-browser")
    threading.Thread(target=_execute, args=(cmd,), daemon=True).start()
    return jsonify(ok=True)


@APP.get("/status")
def status():
    with LOCK:
        return jsonify(STATE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    apply_env(env_file(ROOT / ".env"), ROOT)
    if not args.no_open:
        threading.Timer(1, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()
    APP.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
