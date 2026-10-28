#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Docker Compose 中文备注 / 资源清单工具（仅用标准库）

子命令：
  collect   在装有 docker 的机器上采集（Linux / NAS 均可），输出 inventory.json
  render    读取 inventory.json，生成 终端表格 + Markdown + HTML 看板
  table     只打印终端表格
  demo      生成示例数据并渲染（不需要 docker，用于先看效果）
  serve     常驻 Web 服务：定时采集并通过 HTTP 提供看板（容器部署用）

备注来源（优先级从高到低）：
  1) 服务级 labels：note.name / note.desc / note.owner / note.tier / note.url
  2) 项目级 x-note（compose 文件顶层的 x-note: 块，compose 会忽略 x- 开头的键）

用法示例：
  python3 docker_notes.py collect -o inventory.json
  python3 docker_notes.py render inventory.json --out-dir out
  python3 docker_notes.py table inventory.json
  python3 docker_notes.py demo
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NOTE_KEYS = ("name", "desc", "owner", "tier", "url", "backup")


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
class Fail(Exception):
    pass


def sh(cmd, check=True):
    """执行命令并返回 stdout（UTF-8，容错）。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise Fail("找不到 docker 命令。请在装有 Docker CLI 的机器上运行 collect，"
                   "或改用 'demo' / 'render' 离线渲染。")
    except PermissionError:
        raise Fail("无权执行 docker。Linux 上请把用户加入 docker 组，或用 sudo 运行。")
    if check and p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip()
        raise Fail("命令失败: %s\n%s" % (" ".join(cmd), msg))
    return p.stdout


def human_bytes(n, base=1024):
    if n is None:
        return "-"
    n = float(n)
    units = ["B", "KiB", "MiB", "GiB", "TiB"] if base == 1024 else ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= base and i < len(units) - 1:
        n /= base
        i += 1
    if i == 0 or n >= 100:
        return "%.0f %s" % (n, units[i])
    return "%.1f %s" % (n, units[i])


def parse_bytes(s):
    """'128.5MiB' / '1.2GB' / '0B' -> 字节数"""
    if not s:
        return None
    s = s.strip()
    m = re.match(r"^([\d.]+)\s*([KMGTP]?i?B)$", s, re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    base = 1024 if "i" in unit else 1000
    power = {"b": 0, "kb": 1, "kib": 1, "mb": 2, "mib": 2, "gb": 3, "gib": 3,
             "tb": 4, "tib": 4, "pb": 5, "pib": 5}[unit]
    return int(val * (base ** power))


def parse_pair(s):
    """'1.2MB / 340kB' -> (rx, tx)"""
    if not s or "/" not in s:
        return (None, None)
    a, b = s.split("/", 1)
    return (parse_bytes(a), parse_bytes(b))


def disp_width(s):
    """终端显示宽度（中文按 2 列算）"""
    w = 0
    for ch in str(s):
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad(s, width):
    s = str(s)
    return s + " " * max(0, width - disp_width(s))


def truncate(s, width):
    s = str(s)
    if disp_width(s) <= width:
        return s
    out = ""
    for ch in s:
        if disp_width(out + ch) > width - 1:
            break
        out += ch
    return out + "…"


def uptime_text(started_at):
    if not started_at:
        return "-"
    try:
        t = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        now = dt.datetime.now(dt.timezone.utc)
        delta = now - t.astimezone(dt.timezone.utc)
    except Exception:
        return "-"
    secs = int(delta.total_seconds())
    if secs < 0:
        return "-"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return "%d 天 %d 小时" % (d, h)
    if h:
        return "%d 小时 %d 分" % (h, m)
    return "%d 分" % m


# --------------------------------------------------------------------------- #
# 备注解析
# --------------------------------------------------------------------------- #
def note_from_labels(labels):
    note = {}
    for k, v in (labels or {}).items():
        if k.startswith("note.") and k[5:] in NOTE_KEYS:
            note[k[5:]] = str(v)
    return note


def parse_xnote_regex(text):
    """不装 PyYAML 时，用最小解析读取顶层 x-note: 块（只支持 key: value）。"""
    m = re.search(r"^x-note:\s*$(.*?)(?=^\S)", text + "\n", re.M | re.S)
    if not m:
        return {}
    note = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        mm = re.match(r"^\s+([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$", line)
        if not mm:
            continue
        key, val = mm.group(1), mm.group(2)
        val = val.split(" #")[0].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        note[key] = val
    return note


def parse_path_map(raw):
    """把 'src:dst;src2:dst2' 解析成 [(src, dst)]，用于宿主机路径 -> 容器挂载路径。"""
    maps = []
    for item in (raw or "").split(";"):
        item = item.strip()
        if not item or ":" not in item:
            continue
        src, dst = item.rsplit(":", 1)
        if src and dst:
            maps.append((src.rstrip("/\\"), dst.rstrip("/\\")))
    return maps


def remap_path(path, path_map):
    """把 compose 文件在宿主机上的路径，映射到容器里的只读挂载点。"""
    if not path_map or not path:
        return path
    norm = path.replace("\\", "/")
    for src, dst in path_map:
        s = src.replace("\\", "/")
        if norm == s or norm.startswith(s + "/"):
            return dst + norm[len(s):]
    return path


def read_xnote(config_files, path_map=None):
    """从 compose 文件读 x-note（优先 PyYAML，缺失则正则）。"""
    for path in (config_files or []):
        if not path:
            continue
        candidates = [remap_path(path, path_map), path]
        for cand in candidates:
            if not cand or not os.path.isfile(cand):
                continue
            try:
                text = open(cand, encoding="utf-8").read()
            except Exception:
                continue
            note = _parse_note_text(text)
            if note:
                return note
    return {}


def _parse_note_text(text):
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            data = yaml.safe_load(text) or {}
            note = data.get("x-note") or {}
            if isinstance(note, dict):
                return {k: str(v) for k, v in note.items() if k in NOTE_KEYS}
        except Exception:
            pass
    note = parse_xnote_regex(text)
    return {k: v for k, v in note.items() if k in NOTE_KEYS} if note else {}


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #
def compose_ls():
    out = sh(["docker", "compose", "ls", "--all", "--format", "json"], check=False)
    if out.strip().startswith("["):
        try:
            return json.loads(out)
        except Exception:
            pass
    table = sh(["docker", "compose", "ls", "--all"], check=False)
    if not table.strip():
        # 输出为空说明命令本身跑不通（没装 compose 插件 / 权限不足），不要静默当成「没有项目」
        probe = sh(["docker", "compose", "version"], check=False).strip()
        if not probe:
            raise Fail("docker compose 插件不可用（%s）—— 容器镜像已内置 docker-cli-compose；"
                       "宿主机模式请安装 compose 插件" % (out.strip() or "无输出"))
        raise Fail("docker compose ls 无输出，可能是权限不足 —— 确认当前用户已加入 docker 组")
    rows = []
    for line in table.splitlines()[1:]:
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) >= 2:
            rows.append({"Name": parts[0], "Status": parts[1],
                         "ConfigFiles": parts[2] if len(parts) > 2 else ""})
    return rows


def collect(path_map=None):
    if shutil.which("docker") is None:
        raise Fail("未找到 docker 命令。")

    version = sh(["docker", "version", "--format", "{{.Server.Version}}"], check=False).strip()

    stats = {}
    raw_stats = sh(["docker", "stats", "--no-stream", "--format", "{{json .}}"], check=False)
    for line in raw_stats.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        stats[(row.get("Name") or "").lstrip("/")] = row

    projects = []
    for proj in compose_ls():
        name = proj.get("Name")
        if not name:
            continue
        cfgs = [c.strip() for c in (proj.get("ConfigFiles") or "").split(",") if c.strip()]
        ids = sh(["docker", "ps", "-aq",
                  "--filter", "label=com.docker.compose.project=%s" % name]).split()
        services = []
        if ids:
            infos = json.loads(sh(["docker", "inspect"] + ids) or "[]")
            for c in infos:
                cfg = c.get("Config") or {}
                labels = cfg.get("Labels") or {}
                net = (c.get("NetworkSettings") or {}).get("Networks") or {}
                ports = []
                for cport, binds in ((c.get("NetworkSettings") or {}).get("Ports") or {}).items():
                    if not binds:
                        continue
                    for b in binds:
                        ports.append({"container": cport,
                                      "host_ip": b.get("HostIp") or "",
                                      "host_port": b.get("HostPort") or ""})
                if (c.get("HostConfig") or {}).get("NetworkMode") == "host":
                    ports = [{"container": "host", "host_ip": "", "host_port": "host"}]

                cname = (c.get("Name") or "").lstrip("/")
                st = stats.get(cname) or {}
                cpu = None
                if st.get("CPUPerc"):
                    try:
                        cpu = float(st["CPUPerc"].rstrip("%"))
                    except Exception:
                        cpu = None
                mem_bytes = parse_bytes((st.get("MemUsage") or "").split("/")[0].strip())
                mem_pct = None
                if st.get("MemPerc"):
                    try:
                        mem_pct = float(st["MemPerc"].rstrip("%"))
                    except Exception:
                        mem_pct = None
                rx, tx = parse_pair(st.get("NetIO"))

                services.append({
                    "service": labels.get("com.docker.compose.service", cname),
                    "container": cname,
                    "image": cfg.get("Image", ""),
                    "state": (c.get("State") or {}).get("Status", ""),
                    "health": ((c.get("State") or {}).get("Health") or {}).get("Status", ""),
                    "uptime": uptime_text((c.get("State") or {}).get("StartedAt", "")),
                    "restart": (c.get("HostConfig") or {}).get("RestartPolicy", {}).get("Name", ""),
                    "note": note_from_labels(labels),
                    "ports": sorted(ports, key=lambda x: (x["host_port"], x["container"])),
                    "networks": [{"name": k, "ip": (v or {}).get("IPAddress", "")}
                                 for k, v in net.items()],
                    "cpu_percent": cpu,
                    "mem_bytes": mem_bytes,
                    "mem_percent": mem_pct,
                    "mem_limit_bytes": (c.get("HostConfig") or {}).get("Memory") or None,
                    "net_rx_bytes": rx,
                    "net_tx_bytes": tx,
                })

        services.sort(key=lambda s: s["service"])
        totals = {
            "cpu_percent": sum(s["cpu_percent"] or 0 for s in services) if services else 0,
            "mem_bytes": sum(s["mem_bytes"] or 0 for s in services),
            "net_rx_bytes": sum(s["net_rx_bytes"] or 0 for s in services),
            "net_tx_bytes": sum(s["net_tx_bytes"] or 0 for s in services),
        }
        projects.append({
            "name": name,
            "status": proj.get("Status", ""),
            "config_files": cfgs,
            "note": read_xnote(cfgs, path_map),
            "services": services,
            "totals": totals,
        })

    projects.sort(key=lambda p: p["name"])
    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", ""),
        "docker_version": version,
        "projects": projects,
    }


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def project_title(p):
    note = p.get("note") or {}
    return note.get("name") or "(未备注)"


def service_note_name(s):
    return (s.get("note") or {}).get("name", "")


def ports_text(s):
    if not s.get("ports"):
        return "-"
    out = []
    for p in s["ports"]:
        if p["host_port"] == "host":
            out.append("host 网络")
            continue
        if not p.get("host_port"):          # 只 expose 未 publish，不对外
            continue
        ip = "" if p["host_ip"] in ("", "0.0.0.0", "::") else p["host_ip"] + ":"
        out.append("%s%s→%s" % (ip, p["host_port"], p["container"]))
    return ", ".join(out) if out else "-"


def all_project_ports(p):
    seen, out = set(), []
    for s in p["services"]:
        for x in ports_text(s).split(", "):
            if x and x != "-" and x not in seen:
                seen.add(x)
                out.append(x)
    return out


def summarize(inv):
    projects = inv.get("projects") or []
    services = [s for p in projects for s in p["services"]]
    running = sum(1 for s in services if s.get("state") == "running")
    unlabeled = sum(1 for p in projects if not (p.get("note") or {}).get("name"))
    return {
        "projects": len(projects),
        "services": len(services),
        "running": running,
        "unlabeled": unlabeled,
        "cpu": sum(s.get("cpu_percent") or 0 for s in services),
        "mem": sum(s.get("mem_bytes") or 0 for s in services),
        "rx": sum(s.get("net_rx_bytes") or 0 for s in services),
        "tx": sum(s.get("net_tx_bytes") or 0 for s in services),
    }


def _state_text(s):
    st = s.get("state") or "-"
    if s.get("health"):
        st += "/" + s["health"]
    return st


def mem_text(s):
    """非运行容器不显示 0 B，统一显示 -"""
    if s.get("state") != "running" and not s.get("mem_bytes"):
        return "-"
    return human_bytes(s.get("mem_bytes"))


def cpu_text(s):
    return "-" if s.get("cpu_percent") is None else "%.1f%%" % s["cpu_percent"]


# --------------------------------------------------------------------------- #
# 终端表格
# --------------------------------------------------------------------------- #
def print_table(inv, show_net=True):
    t = summarize(inv)
    print("%s  %s" % (inv.get("host", ""), inv.get("generated_at", "")))
    print("项目 %d · 服务 %d · 运行中 %d · 未备注 %d · CPU 合计 %.1f%% · 内存合计 %s · 网络 ↓%s ↑%s"
          % (t["projects"], t["services"], t["running"], t["unlabeled"],
             t["cpu"], human_bytes(t["mem"]), human_bytes(t["rx"], 1000), human_bytes(t["tx"], 1000)))
    for p in inv.get("projects") or []:
        note = p.get("note") or {}
        head = "● %s" % p["name"]
        if note.get("name"):
            head += "  「%s」" % note["name"]
        bits = []
        if note.get("tier"):
            bits.append(note["tier"])
        if note.get("owner"):
            bits.append("负责人 " + note["owner"])
        if bits:
            head += "  " + " · ".join(bits)
        print()
        print(head)
        if note.get("desc"):
            print("  " + note["desc"])
        if note.get("url"):
            print("  入口: " + note["url"])
        cols = [("服务", 22), ("中文名", 16), ("状态", 12), ("端口", 26),
                ("CPU", 7), ("内存", 12)]
        if show_net:
            cols.append(("网络 ↓/↑", 20))
        print("  " + " ".join(pad(c[0], c[1]) for c in cols))
        print("  " + " ".join("-" * c[1] for c in cols))
        for s in p["services"]:
            row = [
                pad(truncate(s["service"], 22), 22),
                pad(truncate(service_note_name(s) or "-", 16), 16),
                pad(_state_text(s), 12),
                pad(truncate(ports_text(s), 26), 26),
                pad(cpu_text(s), 7),
                pad(mem_text(s), 12),
            ]
            if show_net:
                row.append(pad("%s / %s" % (human_bytes(s["net_rx_bytes"], 1000),
                                            human_bytes(s["net_tx_bytes"], 1000)), 20))
            print("  " + " ".join(row))
        tot = p.get("totals") or {}
        print("  " + pad("合计", 22) + pad("", 16) + pad("", 12) + pad("", 26)
              + pad("%.1f%%" % (tot.get("cpu_percent") or 0), 7)
              + pad(human_bytes(tot.get("mem_bytes")), 12)
              + (pad("%s / %s" % (human_bytes(tot.get("net_rx_bytes"), 1000),
                                  human_bytes(tot.get("net_tx_bytes"), 1000)), 20) if show_net else ""))
    print()


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def render_md(inv):
    t = summarize(inv)
    L = []
    L.append("# Docker Compose 项目清单")
    L.append("")
    L.append("> 主机 **%s** ｜ 采集时间 %s ｜ 共 **%d** 个项目 / **%d** 个服务（运行中 %d）"
             % (inv.get("host", "-"), inv.get("generated_at", "-"), t["projects"], t["services"], t["running"]))
    L.append(">")
    L.append("> 资源合计：CPU **%.1f%%** ｜ 内存 **%s** ｜ 网络 ↓ **%s** ↑ **%s**"
             % (t["cpu"], human_bytes(t["mem"]), human_bytes(t["rx"], 1000), human_bytes(t["tx"], 1000)))
    if t["unlabeled"]:
        L.append(">")
        L.append("> ⚠️ 有 %d 个项目还没写中文备注（在 compose 里加 `x-note:` 或服务 `labels: note.name`）" % t["unlabeled"])
    L.append("")
    L.append("## 目录")
    L.append("")
    for i, p in enumerate(inv.get("projects") or [], 1):
        L.append("%d. %s · %s" % (i, p["name"], project_title(p)))
    L.append("")
    for i, p in enumerate(inv.get("projects") or [], 1):
        note = p.get("note") or {}
        L.append("---")
        L.append("")
        title = "## %d. %s · %s" % (i, p["name"], project_title(p))
        if note.get("tier"):
            title += " `%s`" % note["tier"]
        if note.get("owner"):
            title += " · 负责人 %s" % note["owner"]
        L.append(title)
        L.append("")
        if note.get("desc"):
            L.append("> " + note["desc"])
            L.append("")
        if note.get("url"):
            L.append("- 访问入口：%s" % note["url"])
        if p.get("config_files"):
            L.append("- 编排文件：`%s`" % "`, `".join(p["config_files"]))
        if p.get("status"):
            L.append("- 状态：%s" % p["status"])
        ports = all_project_ports(p)
        L.append("- 端口：%s" % ("、".join("`%s`" % x for x in ports) if ports else "无对外端口"))
        nets = sorted({n["name"] for s in p["services"] for n in s.get("networks", [])})
        L.append("- 网络：%s" % ("、".join("`%s`" % n for n in nets) if nets else "-"))
        L.append("")
        L.append("| 服务 | 中文名 | 状态 | 端口 | CPU | 内存 | 网络 ↓/↑ | 运行时长 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for s in p["services"]:
            L.append("| `%s` | %s | %s | %s | %s | %s | %s / %s | %s |" % (
                s["service"],
                service_note_name(s) or "-",
                _state_text(s),
                ports_text(s),
                cpu_text(s),
                mem_text(s),
                human_bytes(s["net_rx_bytes"], 1000),
                human_bytes(s["net_tx_bytes"], 1000),
                s.get("uptime", "-"),
            ))
        tot = p.get("totals") or {}
        L.append("| **合计** | | | | **%.1f%%** | **%s** | **%s / %s** | |" % (
            tot.get("cpu_percent") or 0,
            human_bytes(tot.get("mem_bytes")),
            human_bytes(tot.get("net_rx_bytes"), 1000),
            human_bytes(tot.get("net_tx_bytes"), 1000),
        ))
        L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
TIER_COLOR = {"P0": "#C0504D", "P1": "#ED7D31", "P2": "#7F7F7F"}

HTML_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docker Compose 项目看板 · __HOST__</title>
<style>
  :root{
    --ink:#1f2328; --muted:#6b7280; --line:#e5e7eb; --bg:#f5f7fa; --card:#fff;
    --brand:#1F4E79; --brand2:#2E75B6; --ok:#2E7D5B; --bad:#C0504D; --warn:#ED7D31;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",Roboto,"Helvetica Neue",Arial,sans-serif}
  .wrap{max-width:1500px;margin:0 auto;padding:28px 24px 60px}
  h1{font-size:22px;margin:0 0 6px;color:var(--brand);letter-spacing:.3px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:18px;line-height:1.8}
  .sub b{color:var(--ink)}
  .banner{display:none;margin:0 0 16px;padding:10px 14px;border-radius:10px;font-size:13px;
          background:#fff4f2;border:1px solid #f3d3cd;color:#8a2f24;line-height:1.7}
  .banner.show{display:block}
  .banner code{background:#fff;padding:1px 5px;border-radius:4px;font-size:12px}
  .toolbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:0 0 20px}
  .toolbar input{flex:1 1 280px;max-width:420px;padding:9px 12px;border:1px solid var(--line);
        border-radius:8px;background:#fff;font-size:13px;outline:none}
  .toolbar input:focus{border-color:var(--brand2);box-shadow:0 0 0 3px rgba(46,117,182,.12)}
  .legend{display:flex;gap:14px;color:var(--muted);font-size:12px;flex-wrap:wrap}
  .legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(560px,1fr));gap:18px;align-items:start}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
        padding:16px 18px 10px;box-shadow:0 1px 2px rgba(16,24,40,.04),0 8px 24px rgba(16,24,40,.04)}
  .card header{display:flex;justify-content:space-between;gap:12px;align-items:baseline;flex-wrap:wrap}
  .ptitle{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .pname{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
         font-size:13px;color:var(--muted);background:#f0f3f7;padding:2px 7px;border-radius:6px}
  .cnname{font-size:17px;font-weight:700;color:var(--brand)}
  .badge{color:#fff;font-size:11px;font-weight:700;padding:1px 7px;border-radius:999px;letter-spacing:.5px}
  .meta{color:var(--muted);font-size:12px;white-space:nowrap}
  .desc{margin:10px 0 6px;color:#374151;font-size:13px}
  .chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:6px 0}
  .chips .label{font-size:12px;color:var(--muted);min-width:30px}
  .chip{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;background:#eef4fb;
        color:#1F4E79;border:1px solid #dbe7f4;border-radius:6px;padding:1px 7px}
  .chip.ghost{background:#f7f7f8;color:#4b5563;border-color:#ececef}
  .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0 8px}
  .kpi{background:#f8fafc;border:1px solid #eef1f5;border-radius:10px;padding:8px 10px}
  .kpi span{display:block;font-size:11px;color:var(--muted)}
  .kpi b{font-size:14px;color:var(--ink);font-weight:600}
  table{width:100%;border-collapse:collapse;margin-top:6px}
  th{font-size:11.5px;color:var(--muted);text-align:left;font-weight:600;
     border-bottom:1px solid var(--line);padding:6px 8px 6px 0}
  td{font-size:12.5px;padding:8px 8px 8px 0;border-bottom:1px solid #f1f3f6;vertical-align:top}
  tr:last-child td{border-bottom:none}
  td.svc code{font-size:12px;color:var(--ink)}
  .cn{font-size:12px;color:var(--brand2);margin-top:1px}
  .state{font-size:12px;padding:0 6px;border-radius:5px;background:#eef7f1;color:var(--ok)}
  .state.bad{background:#fdeeee;color:var(--bad)}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-left:4px}
  .dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)}
  .ports{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:#374151;word-break:break-all}
  .metric .bar{display:inline-block;width:56px;height:5px;background:#e9edf2;border-radius:3px;
        vertical-align:middle;margin-right:6px;overflow:hidden}
  .metric .bar i{display:block;height:100%;border-radius:3px}
  .num{font-size:11.5px;color:#374151}
  .net{font-size:11.5px;color:#4b5563;white-space:nowrap}
  .muted{color:var(--muted)} .small{font-size:11px}
  footer{margin-top:26px;color:var(--muted);font-size:12px;text-align:center}
  @media (max-width:640px){ .grid{grid-template-columns:1fr} .kpis{grid-template-columns:repeat(2,1fr)} }
</style>
</head>
<body>
<div class="wrap">
  <div class="banner" id="banner"></div>
  <h1>Docker Compose 项目看板</h1>
  <div class="sub">__SUBTITLE__</div>
  <div class="toolbar">
    <input id="q" placeholder="搜索：项目名 / 中文名 / 端口 / 负责人…" autocomplete="off">
    <div class="legend">
      <span><i style="background:#2E7D5B"></i>running</span>
      <span><i style="background:#C0504D"></i>exited / 异常</span>
      <span><i style="background:#ED7D31"></i>health 异常</span>
      <span><i style="background:#ED7D31"></i>P1</span>
      <span><i style="background:#C0504D"></i>P0</span>
    </div>
  </div>
  <div class="grid" id="grid">
__CARDS__
  </div>
  <footer>由 docker_notes.py 生成 · 数据为 docker stats 单次采样（瞬时值） · 未输入搜索词时每 2 分钟自动刷新</footer>
</div>
<script>
(function(){
  var q = document.getElementById('q');
  var cards = [].slice.call(document.querySelectorAll('.card'));
  q.addEventListener('input', function(){
    var v = q.value.trim().toLowerCase();
    cards.forEach(function(c){
      c.style.display = (!v || (c.dataset.search||'').toLowerCase().indexOf(v) >= 0) ? '' : 'none';
    });
  });
  // 定时重载拿最新采集结果；输入了搜索词或标签页在后台时不动，避免打断查看
  setTimeout(function(){
    if (document.visibilityState === 'visible' && !q.value.trim()) location.reload();
  }, 120000);
})();
</script>
</body>
</html>
"""


def _badge(note):
    tier = (note or {}).get("tier")
    if not tier:
        return ""
    color = TIER_COLOR.get(tier, "#2E75B6")
    return '<span class="badge" style="background:%s">%s</span>' % (color, html.escape(tier))


def _bar(pct, color):
    if pct is None:
        return '<span class="muted">-</span>'
    w = max(0.0, min(100.0, float(pct)))
    return ('<span class="bar"><i style="width:%.0f%%;background:%s"></i></span>'
            '<span class="num">%.1f%%</span>' % (w, color, float(pct)))


def render_card(p):
    note = p.get("note") or {}
    ports = all_project_ports(p)
    networks = sorted({n["name"] for s in p["services"] for n in s.get("networks", [])})
    port_chips = "".join('<span class="chip">%s</span>' % html.escape(x) for x in ports) \
        or '<span class="muted">无对外端口</span>'
    net_chips = "".join('<span class="chip ghost">%s</span>' % html.escape(x) for x in networks) \
        or '<span class="muted">-</span>'

    rows = []
    for s in p["services"]:
        state = s.get("state") or "-"
        hcls = "ok" if state == "running" else "bad"
        health = ""
        if s.get("health"):
            health = '<span class="dot %s"></span>' % ("ok" if s["health"] == "healthy" else "warn")
        cn = service_note_name(s)
        rows.append(
            "<tr><td class='svc'><code>%s</code>%s</td>"
            "<td><span class='state %s'>%s</span> %s<div class='muted small'>%s</div></td>"
            "<td class='ports'>%s</td>"
            "<td class='metric'>%s</td>"
            "<td class='metric'>%s<div class='muted small'>%s</div></td>"
            "<td class='net'>&darr; %s<br>&uarr; %s</td></tr>" % (
                html.escape(s["service"]),
                ("<div class='cn'>%s</div>" % html.escape(cn)) if cn else "",
                hcls, html.escape(_state_text(s)), health,
                html.escape(s.get("uptime", "")),
                html.escape(ports_text(s)),
                _bar(s["cpu_percent"], "#2E75B6"),
                _bar(s["mem_percent"], "#1F4E79"),
                html.escape(mem_text(s)),
                html.escape(human_bytes(s["net_rx_bytes"], 1000)),
                html.escape(human_bytes(s["net_tx_bytes"], 1000)),
            ))

    tot = p.get("totals") or {}
    search = " ".join([p["name"], note.get("name", ""), note.get("desc", ""),
                       note.get("owner", ""), " ".join(ports)])
    meta_bits = []
    if note.get("owner"):
        meta_bits.append("负责人 " + html.escape(note["owner"]))
    if p.get("status"):
        meta_bits.append(html.escape(p["status"]))
    desc = ("<p class='desc'>%s</p>" % html.escape(note["desc"])) if note.get("desc") else ""

    return """    <section class="card" data-search="{search}">
      <header>
        <div class="ptitle">
          <span class="pname">{pname}</span>
          <span class="cnname">{cn}</span>
          {badge}
        </div>
        <div class="meta">{meta}</div>
      </header>
      {desc}
      <div class="chips"><span class="label">端口</span>{ports}</div>
      <div class="chips"><span class="label">网络</span>{nets}</div>
      <div class="kpis">
        <div class="kpi"><span>CPU 合计</span><b>{cpu:.1f}%</b></div>
        <div class="kpi"><span>内存合计</span><b>{mem}</b></div>
        <div class="kpi"><span>网络 ↓ / ↑</span><b>{rx} / {tx}</b></div>
        <div class="kpi"><span>服务数</span><b>{n}</b></div>
      </div>
      <table>
        <thead><tr><th>服务</th><th>状态 / 运行</th><th>端口映射</th><th>CPU</th><th>内存</th><th>网络</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
""".format(
        search=html.escape(search),
        pname=html.escape(p["name"]),
        cn=html.escape(project_title(p)),
        badge=_badge(note),
        meta=" ｜ ".join(meta_bits),
        desc=desc,
        ports=port_chips,
        nets=net_chips,
        cpu=tot.get("cpu_percent") or 0,
        mem=human_bytes(tot.get("mem_bytes")),
        rx=human_bytes(tot.get("net_rx_bytes"), 1000),
        tx=human_bytes(tot.get("net_tx_bytes"), 1000),
        n=len(p["services"]),
        rows="".join(rows),
    )


def render_html(inv):
    t = summarize(inv)
    subtitle = ("主机 <b>%s</b> ｜ 采集时间 %s ｜ 项目 <b>%d</b> · 服务 <b>%d</b>（运行中 %d）｜ "
                "CPU 合计 <b>%.1f%%</b> ｜ 内存合计 <b>%s</b> ｜ 网络 ↓%s ↑%s" % (
                    html.escape(inv.get("host", "-")),
                    html.escape(inv.get("generated_at", "-")),
                    t["projects"], t["services"], t["running"],
                    t["cpu"], human_bytes(t["mem"]),
                    human_bytes(t["rx"], 1000), human_bytes(t["tx"], 1000)))
    if t["unlabeled"]:
        subtitle += ("<br>⚠️ <b>%d</b> 个项目还没写中文备注：在 compose 里加 <code>x-note:</code> "
                     "或服务 <code>labels: note.name</code>" % t["unlabeled"])
    cards = "".join(render_card(p) for p in inv.get("projects") or [])
    out = HTML_HEAD.replace("__HOST__", html.escape(inv.get("host", "-")))
    out = out.replace("__SUBTITLE__", subtitle).replace("__CARDS__", cards)
    return out


# --------------------------------------------------------------------------- #
# Web 服务（容器部署用）
# --------------------------------------------------------------------------- #
class State:
    """采集结果的内存快照；采集失败时保留上一次成功的数据。"""

    def __init__(self, seed=None):
        self.lock = threading.Lock()
        self.inventory = seed
        self.error = None
        self.updated_at = time.time() if seed else None
        self.ok_count = 0

    def set_ok(self, inv):
        with self.lock:
            self.inventory = inv
            self.error = None
            self.updated_at = time.time()
            self.ok_count += 1

    def set_error(self, msg):
        with self.lock:
            self.error = msg

    def snapshot(self):
        with self.lock:
            return self.inventory, self.error, self.updated_at


def collector_loop(state, interval, path_map):
    while True:
        try:
            state.set_ok(collect(path_map))
        except Exception as exc:                      # 采集失败不退出，保留旧数据
            state.set_error(str(exc))
        time.sleep(max(10, interval))


def _prom_label(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_metrics(inv, state):
    out = [
        "# HELP docker_notes_collect_success 最近一次采集是否成功",
        "# TYPE docker_notes_collect_success gauge",
        "docker_notes_collect_success %d" % (0 if state[1] else 1),
        "# HELP docker_notes_last_collect_timestamp_seconds 最近一次成功采集的时间戳",
        "# TYPE docker_notes_last_collect_timestamp_seconds gauge",
        "docker_notes_last_collect_timestamp_seconds %s" % (("%.0f" % state[2]) if state[2] else "0"),
        "# HELP docker_notes_project_cpu_percent 项目 CPU 合计（占单核百分比）",
        "# TYPE docker_notes_project_cpu_percent gauge",
        "# HELP docker_notes_project_mem_bytes 项目内存合计（字节）",
        "# TYPE docker_notes_project_mem_bytes gauge",
        "# HELP docker_notes_project_net_rx_bytes 项目网络累计接收（字节）",
        "# TYPE docker_notes_project_net_rx_bytes gauge",
        "# HELP docker_notes_project_net_tx_bytes 项目网络累计发送（字节）",
        "# TYPE docker_notes_project_net_tx_bytes gauge",
        "# HELP docker_notes_project_services 项目服务数",
        "# TYPE docker_notes_project_services gauge",
        "# HELP docker_notes_project_up 项目内运行中的容器数",
        "# TYPE docker_notes_project_up gauge",
    ]
    if inv:
        for p in inv.get("projects") or []:
            labels = 'project="%s",name="%s"' % (_prom_label(p["name"]), _prom_label(project_title(p)))
            tot = p.get("totals") or {}
            up = sum(1 for s in p["services"] if s.get("state") == "running")
            out.append("docker_notes_project_cpu_percent{%s} %.2f" % (labels, tot.get("cpu_percent") or 0))
            out.append("docker_notes_project_mem_bytes{%s} %d" % (labels, tot.get("mem_bytes") or 0))
            out.append("docker_notes_project_net_rx_bytes{%s} %d" % (labels, tot.get("net_rx_bytes") or 0))
            out.append("docker_notes_project_net_tx_bytes{%s} %d" % (labels, tot.get("net_tx_bytes") or 0))
            out.append("docker_notes_project_services{%s} %d" % (labels, len(p["services"])))
            out.append("docker_notes_project_up{%s} %d" % (labels, up))
    return "\n".join(out) + "\n"


def make_handler(state, basic_auth=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "docker-notes"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        # ---------- helpers ----------
        def _authed(self):
            if not basic_auth:
                return True
            hdr = self.headers.get("Authorization", "")
            if not hdr.startswith("Basic "):
                return False
            try:
                raw = base64.b64decode(hdr[6:]).decode("utf-8")
            except Exception:
                return False
            return raw == basic_auth

        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _unauthorized(self):
            self._send(401, "需要认证\n", "text/plain; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="docker-notes", charset="UTF-8"'})

        # ---------- routes ----------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._authed():
                return self._unauthorized()
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            inv, err, updated = state.snapshot()

            if path == "/healthz":
                age = int(time.time() - updated) if updated else None
                body = {"ok": err is None and inv is not None, "age_seconds": age,
                        "projects": len((inv or {}).get("projects") or []), "error": err}
                return self._send(200 if body["ok"] else 503, json.dumps(body, ensure_ascii=False),
                                  "application/json; charset=utf-8")

            if path == "/metrics":
                return self._send(200, render_metrics(inv, (inv, err, updated)),
                                  "text/plain; charset=utf-8")

            if path == "/api/inventory.json":
                if inv is None:
                    return self._send(503, json.dumps({"error": err or "首次采集进行中"},
                                                      ensure_ascii=False),
                                      "application/json; charset=utf-8")
                return self._send(200, json.dumps(inv, ensure_ascii=False, indent=2),
                                  "application/json; charset=utf-8")

            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if path in ("/", "/index.html"):
                if inv is None:
                    msg = err or "首次采集进行中，请稍候刷新…"
                    return self._send(503, (
                        "<!DOCTYPE html><meta charset='utf-8'>"
                        "<body style=\"font:14px/1.7 -apple-system,'Microsoft YaHei',sans-serif;"
                        "padding:48px;color:#334\"><h2>docker-notes 正在启动</h2>"
                        "<p>%s</p><p style='color:#6b7280'>每 15 秒自动重试；"
                        "若持续失败，检查 docker.sock 是否挂载、当前用户是否有 docker 权限。</p>"
                        "</body>" % html.escape(str(msg))), "text/html; charset=utf-8")

                doc = render_html(inv)
                if err:
                    banner = (
                        '<div class="banner show"><b>采集失败</b>：%s<br>'
                        '下方为 %s 的快照，服务会自动重试。</div>'
                        % (html.escape(err), html.escape(inv.get("generated_at", "-")))
                    )
                    doc = doc.replace('<div class="banner" id="banner"></div>', banner)
                return self._send(200, doc)

            return self._send(404, "404 Not Found\n", "text/plain; charset=utf-8")

    return Handler


def cmd_serve(args):
    path_map = parse_path_map(args.path_map or os.environ.get("PATH_MAP", ""))
    seed = None
    if args.inventory and os.path.isfile(args.inventory):
        seed = load_json(args.inventory)

    state = State(seed)
    interval = int(args.refresh or os.environ.get("REFRESH_SECONDS") or 300)
    threading.Thread(target=collector_loop, args=(state, interval, path_map), daemon=True).start()

    port = int(args.port or os.environ.get("PORT") or 8080)
    bind = args.bind or os.environ.get("BIND") or "0.0.0.0"
    auth = (args.basic_auth if args.basic_auth is not None else os.environ.get("BASIC_AUTH", "")) or None

    httpd = ThreadingHTTPServer((bind, port), make_handler(state, auth))
    httpd.daemon_threads = True
    print("docker-notes 已启动: http://%s:%d  |  采集间隔 %ds  |  认证 %s  |  路径映射 %s"
          % (bind, port, interval, "已开启" if auth else "关闭", path_map or "无"), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- #
# demo 数据
# --------------------------------------------------------------------------- #
def demo_inventory():
    def svc(service, cn, state, ports, cpu, mem_mib, mem_pct, rx, tx, net, ip, up, health="healthy"):
        return {
            "service": service, "container": "demo-%s-1" % service, "image": "example/%s:latest" % service,
            "state": state, "health": health, "uptime": up, "restart": "unless-stopped",
            "note": {"name": cn} if cn else {},
            "ports": ports,
            "networks": [{"name": net, "ip": ip}],
            "cpu_percent": cpu, "mem_bytes": int(mem_mib * 1024 * 1024) if mem_mib else 0,
            "mem_percent": mem_pct, "mem_limit_bytes": 2 * 1024 ** 3,
            "net_rx_bytes": rx, "net_tx_bytes": tx,
        }

    projects = [
        {
            "name": "shop",
            "status": "running(3)",
            "config_files": ["/opt/shop/docker-compose.yml"],
            "note": {"name": "电商后端", "tier": "P0", "owner": "平台组",
                     "desc": "订单 / 支付 / 库存主链路，生产环境；改动需走变更单。"},
            "services": [
                svc("api", "订单接口", "running",
                    [{"container": "8080/tcp", "host_ip": "0.0.0.0", "host_port": "18080"}],
                    12.4, 486, 23.7, 5_400_000, 1_900_000, "shop_default", "172.20.0.2", "6 天 3 小时"),
                svc("mysql", "主数据库", "running",
                    [{"container": "3306/tcp", "host_ip": "127.0.0.1", "host_port": "13306"}],
                    3.1, 812, 39.6, 2_100_000, 3_800_000, "shop_default", "172.20.0.3", "6 天 3 小时"),
                svc("redis", "缓存", "running",
                    [{"container": "6379/tcp", "host_ip": "", "host_port": "6379"}],
                    0.6, 96, 4.7, 900_000, 700_000, "shop_default", "172.20.0.4", "6 天 3 小时"),
            ],
        },
        {
            "name": "blog",
            "status": "running(2)",
            "config_files": ["/opt/blog/docker-compose.yml"],
            "note": {"name": "官网 / 博客", "tier": "P1", "owner": "市场部",
                     "desc": "对外内容站，走 Cloudflare 回源；夜间自动备份到 NAS。"},
            "services": [
                svc("web", "前台站点", "running",
                    [{"container": "80/tcp", "host_ip": "0.0.0.0", "host_port": "80"},
                     {"container": "443/tcp", "host_ip": "0.0.0.0", "host_port": "443"}],
                    1.2, 184, 9.0, 12_000_000, 8_600_000, "blog_default", "172.30.0.2", "21 天 8 小时"),
                svc("db", "文章库", "running",
                    [{"container": "5432/tcp", "host_ip": "", "host_port": ""}],
                    0.4, 220, 10.7, 300_000, 260_000, "blog_default", "172.30.0.3", "21 天 8 小时"),
            ],
        },
        {
            "name": "monitoring",
            "status": "running(3)",
            "config_files": ["/opt/monitoring/docker-compose.yml"],
            "note": {"name": "监控告警", "tier": "P2", "owner": "运维组",
                     "desc": "Prometheus + Grafana + Node Exporter，只在内网访问。"},
            "services": [
                svc("prometheus", "指标采集", "running",
                    [{"container": "9090/tcp", "host_ip": "10.0.0.5", "host_port": "9090"}],
                    2.8, 640, 31.2, 1_400_000, 980_000, "monitoring_default", "172.31.0.2", "12 天 1 小时"),
                svc("grafana", "看板", "running",
                    [{"container": "3000/tcp", "host_ip": "10.0.0.5", "host_port": "3000"}],
                    1.1, 210, 10.2, 700_000, 2_400_000, "monitoring_default", "172.31.0.3", "12 天 1 小时"),
                svc("node-exporter", "主机指标", "running",
                    [{"container": "9100/tcp", "host_ip": "", "host_port": ""}],
                    0.2, 18, 0.9, 120_000, 90_000, "host", "10.0.0.5", "12 天 1 小时"),
            ],
        },
        {
            "name": "legacy-crm",
            "status": "exited(1)",
            "config_files": ["/opt/legacy/docker-compose.yml"],
            "note": {},
            "services": [
                svc("crm", "", "exited",
                    [{"container": "8000/tcp", "host_ip": "0.0.0.0", "host_port": "8000"}],
                    None, 0, None, None, None, "legacy_default", "", "-", health=""),
            ],
        },
    ]
    for p in projects:
        p["services"].sort(key=lambda s: s["service"])
        p["totals"] = {
            "cpu_percent": sum(s["cpu_percent"] or 0 for s in p["services"]),
            "mem_bytes": sum(s["mem_bytes"] or 0 for s in p["services"]),
            "net_rx_bytes": sum(s["net_rx_bytes"] or 0 for s in p["services"]),
            "net_tx_bytes": sum(s["net_tx_bytes"] or 0 for s in p["services"]),
        }
    projects.sort(key=lambda p: p["name"])
    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": "nas01 (示例数据)",
        "docker_version": "24.0.7",
        "projects": projects,
    }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def load_json(path):
    if not path or not os.path.isfile(path):
        raise Fail("找不到清单文件：%s（先用 collect 采集，或跑 demo 生成示例）" % path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def render_all(inv, out_dir):
    out_dir = out_dir or "out"
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, "inventory.md")
    html_path = os.path.join(out_dir, "inventory.html")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_md(inv))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(inv))
    print_table(inv)
    print("Markdown -> %s" % md_path)
    print("HTML     -> %s" % html_path)


def cmd_collect(args):
    inv = collect()
    out = args.output or "inventory.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(inv, f, ensure_ascii=False, indent=2)
    print("已采集 %d 个项目 -> %s" % (len(inv["projects"]), out))
    if args.render:
        render_all(inv, args.out_dir)


def cmd_render(args):
    render_all(load_json(args.input), args.out_dir)


def cmd_table(args):
    print_table(load_json(args.input))


def cmd_demo(args):
    inv = demo_inventory()
    base = args.out_dir or "out"
    os.makedirs(base, exist_ok=True)
    sample = os.path.join(base, "inventory.sample.json")
    with open(sample, "w", encoding="utf-8") as f:
        json.dump(inv, f, ensure_ascii=False, indent=2)
    print("示例数据 -> %s" % sample)
    render_all(inv, base)


def main():
    ap = argparse.ArgumentParser(
        description="Docker Compose 中文备注 / 端口 / CPU / 内存 / 网络清单工具",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="在装有 docker 的机器上采集")
    c.add_argument("-o", "--output", default="inventory.json")
    c.add_argument("--out-dir", default="out")
    c.add_argument("--render", action="store_true", help="采集完顺便渲染")
    c.set_defaults(func=cmd_collect)

    r = sub.add_parser("render", help="由 inventory.json 渲染表格/Markdown/HTML")
    r.add_argument("input", nargs="?", default="inventory.json")
    r.add_argument("--out-dir", default="out")
    r.set_defaults(func=cmd_render)

    t = sub.add_parser("table", help="只打印终端表格")
    t.add_argument("input", nargs="?", default="inventory.json")
    t.set_defaults(func=cmd_table)

    d = sub.add_parser("demo", help="生成示例数据并渲染（不需要 docker）")
    d.add_argument("--out-dir", default="out")
    d.set_defaults(func=cmd_demo)

    s = sub.add_parser("serve", help="常驻 Web 服务（容器部署用）")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--bind", default=None)
    s.add_argument("--refresh", type=int, default=None, help="采集间隔秒，默认 300")
    s.add_argument("--path-map", default=None, help="宿主机路径:容器路径[;...]，用于读 compose 的 x-note")
    s.add_argument("--inventory", default=None, help="启动时的种子数据（采集失败时兜底展示）")
    s.add_argument("--basic-auth", default=None, help="user:pass，启用 HTTP Basic 认证")
    s.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    try:
        args.func(args)
    except Fail as e:
        print("错误: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
