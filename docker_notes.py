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
import copy
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
from urllib.parse import parse_qs, urlsplit

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

HTML_HEAD = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docker Compose 项目看板 · __HOST__</title>
<style>
  :root{
    --ink:#1f2328; --muted:#6b7280; --line:#e5e7eb; --bg:#f5f7fa; --card:#fff;
    --brand:#1F4E79; --brand2:#2E75B6; --ok:#2E7D5B; --bad:#C0504D; --warn:#ED7D31;
    --shadow:0 1px 2px rgba(16,24,40,.04),0 8px 24px rgba(16,24,40,.04);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",Roboto,"Helvetica Neue",Arial,sans-serif}
  .wrap{max-width:1500px;margin:0 auto;padding:26px 24px 90px}
  h1{font-size:22px;margin:0 0 6px;color:var(--brand);letter-spacing:.3px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:16px;line-height:1.8}
  .sub b{color:var(--ink)}
  .banner{display:none;margin:0 0 16px;padding:10px 14px;border-radius:10px;font-size:13px;
          background:#fff4f2;border:1px solid #f3d3cd;color:#8a2f24;line-height:1.7}
  .banner.show{display:block}
  .banner code{background:#fff;padding:1px 5px;border-radius:4px;font-size:12px}

  /* ---------- 工具条 ---------- */
  .toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 10px}
  .toolbar input[type=search]{flex:1 1 260px;max-width:380px;padding:8px 12px;border:1px solid var(--line);
        border-radius:8px;background:#fff;font-size:13px;outline:none}
  .toolbar input[type=search]:focus{border-color:var(--brand2);box-shadow:0 0 0 3px rgba(46,117,182,.12)}
  .views{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff}
  .vbtn{border:0;background:#fff;color:var(--muted);font-size:12.5px;padding:7px 12px;cursor:pointer}
  .vbtn+.vbtn{border-left:1px solid var(--line)}
  .vbtn.active{background:#eef4fb;color:var(--brand);font-weight:600}
  .ghost-btn{border:1px solid var(--line);background:#fff;border-radius:8px;padding:7px 11px;
        font-size:12.5px;color:#374151;cursor:pointer}
  .ghost-btn:hover{border-color:var(--brand2);color:var(--brand)}
  #sortby{padding:7px 8px;border:1px solid var(--line);border-radius:8px;background:#fff;
          font-size:12.5px;color:#374151;outline:none}
  .muted{color:var(--muted)} .small{font-size:11.5px}

  /* ---------- 筛选 ---------- */
  .filters{display:flex;gap:8px 18px;flex-wrap:wrap;align-items:center;margin:0 0 16px;
           padding:9px 12px;background:#fff;border:1px solid var(--line);border-radius:10px}
  .fgroup{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
  .flabel{font-size:11.5px;color:var(--muted);margin-right:2px}
  .fchip{display:inline-flex;gap:5px;align-items:center;font-size:12px;color:#374151;
         border:1px solid var(--line);border-radius:999px;padding:2px 9px 2px 7px;cursor:pointer;user-select:none}
  .fchip:hover{border-color:var(--brand2)}
  .fchip input{margin:0;accent-color:var(--brand2)}
  .fchip.on{background:#eef4fb;border-color:#cfe0f2;color:var(--brand);font-weight:600}

  /* ---------- 占比图 ---------- */
  .panel{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 16px 14px;
         margin:0 0 18px;box-shadow:var(--shadow)}
  .phead{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:10px}
  .phead h2{font-size:14px;margin:0;color:var(--brand);font-weight:600}
  .seg{display:flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}
  .seg button{border:0;background:#fff;color:var(--muted);font-size:12px;padding:4px 10px;cursor:pointer}
  .seg button+button{border-left:1px solid var(--line)}
  .seg button.active{background:var(--brand);color:#fff}
  .chart{display:flex;height:44px;border-radius:8px;overflow:hidden;background:#eef1f5}
  .chart .cseg{position:relative;height:100%;display:flex;align-items:center;justify-content:center;
       font-size:11.5px;color:#fff;font-weight:600;white-space:nowrap;overflow:hidden;cursor:pointer;
       transition:filter .12s;border-right:1px solid rgba(255,255,255,.55)}
  .chart .cseg:last-child{border-right:0}
  .chart .cseg:hover{filter:brightness(1.1)}
  .chart .cseg.other{background:#cbd5df;color:#42505e;font-weight:500}
  .chart .cseg.dim{opacity:.35}
  .chart .cseg.flash{outline:2px solid #1f2328;outline-offset:-2px}
  .lgbar{display:flex;gap:6px 16px;flex-wrap:wrap;margin-top:10px}
  .lg{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:#374151;cursor:pointer;
      padding:1px 2px;border-radius:5px}
  .lg:hover{background:#f3f6fa}
  .lg i{width:9px;height:9px;border-radius:2px;display:inline-block}
  .lg b{font-weight:600}
  .lg .v{color:var(--muted);font-family:ui-monospace,Consolas,monospace}
  .tip{position:fixed;z-index:60;pointer-events:none;background:#1f2328;color:#fff;font-size:12px;
       padding:6px 9px;border-radius:7px;box-shadow:0 6px 18px rgba(0,0,0,.22);opacity:0;transition:opacity .1s;
       white-space:pre-line;line-height:1.6;max-width:320px}
  .tip.on{opacity:1}

  /* ---------- 卡片 ---------- */
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(560px,1fr));gap:18px;align-items:start}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
        padding:16px 18px 10px;box-shadow:var(--shadow);scroll-margin-top:16px;position:relative}
  .card.hide{display:none}
  .card.picked{outline:2px solid var(--brand2);outline-offset:1px}
  .card.flash{animation:flash 1.4s ease}
  @keyframes flash{0%{box-shadow:0 0 0 4px rgba(46,117,182,.35)}100%{box-shadow:var(--shadow)}}
  .card header{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap}
  .ptitle{display:flex;align-items:center;gap:9px;flex-wrap:wrap;min-width:0}
  .pick{width:15px;height:15px;accent-color:var(--brand2);cursor:pointer;flex:none}
  .pname{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
         font-size:12.5px;color:var(--muted);background:#f0f3f7;padding:2px 7px;border-radius:6px}
  .cnname{font-size:17px;font-weight:700;color:var(--brand)}
  .badge{color:#fff;font-size:11px;font-weight:700;padding:1px 7px;border-radius:999px;letter-spacing:.5px}
  .src{font-size:10.5px;color:#8a5a00;background:#fff4e0;border:1px solid #f3dfb4;
       padding:1px 6px;border-radius:999px}
  .acts{display:flex;gap:6px;align-items:center;flex:none}
  .btn{font-size:12px;text-decoration:none;border:1px solid #cfe0f2;background:#eef4fb;color:var(--brand);
       padding:3px 10px;border-radius:7px;cursor:pointer;white-space:nowrap}
  .btn:hover{background:#e2eefb}
  .btn.primary{background:var(--brand);border-color:var(--brand);color:#fff}
  .btn.primary:hover{background:#17405f}
  .btn.danger{background:#fdeeee;border-color:#f3cfcd;color:var(--bad)}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .ibtn{border:1px solid var(--line);background:#fff;color:#4b5563;width:26px;height:26px;border-radius:7px;
        cursor:pointer;font-size:13px;line-height:1;display:inline-flex;align-items:center;justify-content:center}
  .ibtn:hover{border-color:var(--brand2);color:var(--brand)}
  .meta{color:var(--muted);font-size:12px;margin:6px 0 0}
  .desc{margin:8px 0 6px;color:#374151;font-size:13px}
  .chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:5px 0}
  .chips .label{font-size:12px;color:var(--muted);min-width:30px}
  .chip{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;background:#eef4fb;
        color:#1F4E79;border:1px solid #dbe7f4;border-radius:6px;padding:1px 7px;text-decoration:none}
  .chip.ghost{background:#f7f7f8;color:#4b5563;border-color:#ececef}
  a.chip:hover{background:#dceaf9}
  .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0 8px}
  .kpi{background:#f8fafc;border:1px solid #eef1f5;border-radius:10px;padding:8px 10px}
  .kpi span{display:block;font-size:11px;color:var(--muted)}
  .kpi b{font-size:14px;color:var(--ink);font-weight:600}
  table{width:100%;border-collapse:collapse;margin-top:6px}
  th{font-size:11.5px;color:var(--muted);text-align:left;font-weight:600;
     border-bottom:1px solid var(--line);padding:6px 8px 6px 0}
  td{font-size:12.5px;padding:8px 8px 8px 0;border-bottom:1px solid #f1f3f6;vertical-align:top}
  tr:last-child td{border-bottom:none}
  .svcbtn{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--ink);background:none;
          border:0;padding:0;cursor:pointer;border-bottom:1px dashed #c8d2dd}
  .svcbtn:hover{color:var(--brand2);border-bottom-color:var(--brand2)}
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

  /* 列表视图 */
  .grid.list{grid-template-columns:1fr;gap:10px}
  .grid.list .card{padding:10px 16px 6px}
  .grid.list .card header{margin-bottom:2px}
  .grid.list .desc{margin:4px 0}
  .grid.list .kpis{grid-template-columns:repeat(4,minmax(0,190px));margin:8px 0 6px}
  .grid.list td{padding:6px 8px 6px 0}
  .grid.list table{table-layout:fixed}
  .grid.list th:nth-child(1),.grid.list td:nth-child(1){width:22%}
  .grid.list th:nth-child(2),.grid.list td:nth-child(2){width:20%}
  .grid.list th:nth-child(3),.grid.list td:nth-child(3){width:22%}
  .grid.list th:nth-child(4),.grid.list td:nth-child(4){width:12%}
  .grid.list th:nth-child(5),.grid.list td:nth-child(5){width:12%}
  .grid.list th:nth-child(6),.grid.list td:nth-child(6){width:12%}

  /* ---------- 底部选择条 ---------- */
  .selbar{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:50;display:flex;gap:10px;
          align-items:center;background:#1f2328;color:#fff;border-radius:999px;padding:8px 14px;
          box-shadow:0 10px 30px rgba(0,0,0,.25);font-size:13px}
  .selbar[hidden]{display:none}
  .selbar b{color:#fff}
  .selbar button{border:1px solid rgba(255,255,255,.25);background:rgba(255,255,255,.08);color:#fff;
        border-radius:999px;padding:4px 11px;font-size:12px;cursor:pointer}
  .selbar button:hover{background:rgba(255,255,255,.18)}

  /* ---------- 弹窗 ---------- */
  dialog{border:0;border-radius:14px;padding:0;box-shadow:0 24px 60px rgba(0,0,0,.28);
         width:min(720px,94vw);max-height:88vh;overflow:hidden}
  dialog::backdrop{background:rgba(20,26,33,.42)}
  .dlg-head{display:flex;justify-content:space-between;align-items:center;gap:10px;
        padding:13px 18px;border-bottom:1px solid var(--line);background:#fbfcfd}
  .dlg-head h3{margin:0;font-size:15px;color:var(--brand)}
  .dlg-body{padding:14px 18px 18px;overflow:auto;max-height:70vh}
  .dlg-foot{display:flex;justify-content:flex-end;gap:8px;padding:11px 18px;border-top:1px solid var(--line);
        background:#fbfcfd}
  .field{display:flex;gap:10px;align-items:flex-start;margin-bottom:11px}
  .field label{flex:0 0 68px;font-size:12.5px;color:var(--muted);padding-top:6px}
  .field input,.field textarea,.field select{flex:1;padding:7px 10px;border:1px solid var(--line);
        border-radius:8px;font-size:13px;font-family:inherit;outline:none;background:#fff}
  .field input:focus,.field textarea:focus,.field select:focus{border-color:var(--brand2);
        box-shadow:0 0 0 3px rgba(46,117,182,.12)}
  .field textarea{min-height:60px;resize:vertical}
  .svc-list{border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin:2px 0 4px}
  .srow{display:flex;gap:10px;align-items:center;margin:5px 0}
  .srow code{font-size:12px;color:var(--muted);flex:0 0 130px;overflow:hidden;text-overflow:ellipsis}
  .srow input{flex:1;padding:5px 9px;border:1px solid var(--line);border-radius:7px;font-size:12.5px}
  .tabs{display:flex;gap:4px;padding:0 18px;border-bottom:1px solid var(--line);background:#fbfcfd}
  .tab{border:0;background:none;padding:9px 12px;font-size:13px;color:var(--muted);cursor:pointer;
       border-bottom:2px solid transparent}
  .tab.active{color:var(--brand);border-bottom-color:var(--brand);font-weight:600}
  .pane{display:none;padding:14px 18px;overflow:auto;max-height:62vh}
  .pane.active{display:block}
  pre.log{background:#0f1720;color:#d7e2ee;font-family:ui-monospace,Consolas,monospace;font-size:12px;
       line-height:1.55;padding:12px 14px;border-radius:10px;margin:0;white-space:pre-wrap;word-break:break-all;
       max-height:52vh;overflow:auto}
  .kv{display:grid;grid-template-columns:120px 1fr;gap:6px 12px;font-size:12.5px}
  .kv dt{color:var(--muted)}
  .kv dd{margin:0;word-break:break-all}
  .cmdbar{display:flex;gap:8px;margin-bottom:10px}
  .cmdbar input{flex:1;padding:7px 10px;border:1px solid var(--line);border-radius:8px;font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
  .toast{position:fixed;left:50%;transform:translateX(-50%);top:18px;z-index:70;background:#1f2328;color:#fff;
        padding:9px 16px;border-radius:10px;font-size:13px;box-shadow:0 10px 28px rgba(0,0,0,.25);
        opacity:0;pointer-events:none;transition:opacity .18s,top .18s}
  .toast.on{opacity:1;top:24px}
  .toast.err{background:#8a2f24}
  footer{margin-top:26px;color:var(--muted);font-size:12px;text-align:center}
  @media (max-width:640px){
    .grid{grid-template-columns:1fr} .kpis{grid-template-columns:repeat(2,1fr)}
    .grid.list .kpis{grid-template-columns:repeat(2,1fr)}
    .wrap{padding:18px 14px 100px}
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="banner" id="banner"></div>
  <h1>Docker Compose 项目看板</h1>
  <div class="sub">__SUBTITLE__</div>

  <div class="toolbar">
    <input id="q" type="search" placeholder="搜索：项目名 / 中文名 / 端口 / 负责人…" autocomplete="off">
    <div class="views" role="group" aria-label="视图">
      <button class="vbtn active" data-view="tile" type="button">▦ 平铺</button>
      <button class="vbtn" data-view="list" type="button">☰ 列表</button>
    </div>
    <button class="ghost-btn" id="clearf" type="button">清除筛选</button>
    <select id="sortby" title="按资源排序">
      <option value="none">默认顺序</option>
      <option value="cpu">按 CPU</option>
      <option value="mem">按内存</option>
      <option value="tx">按上行</option>
      <option value="rx">按下行</option>
    </select>
    <button class="ghost-btn" id="sortdir" type="button" title="切换升降序">降序 ↓</button>
    <span class="muted small" id="fcount"></span>
  </div>

  <div class="filters" id="filters">
    <div class="fgroup">
      <span class="flabel">状态</span>
      <label class="fchip"><input type="checkbox" data-group="state" value="running">运行中</label>
      <label class="fchip"><input type="checkbox" data-group="state" value="exited">已退出</label>
      <label class="fchip"><input type="checkbox" data-group="state" value="mixed">部分运行</label>
    </div>
    <div class="fgroup">
      <span class="flabel">健康</span>
      <label class="fchip"><input type="checkbox" data-group="health" value="bad">健康异常</label>
    </div>
    <div class="fgroup">
      <span class="flabel">等级</span>
      <label class="fchip"><input type="checkbox" data-group="tier" value="P0">P0</label>
      <label class="fchip"><input type="checkbox" data-group="tier" value="P1">P1</label>
      <label class="fchip"><input type="checkbox" data-group="tier" value="P2">P2</label>
      <label class="fchip"><input type="checkbox" data-group="tier" value="none">未分层</label>
    </div>
    <div class="fgroup">
      <label class="fchip"><input type="checkbox" id="onlysel">仅看已选</label>
    </div>
  </div>

  <section class="panel">
    <div class="phead">
      <h2>资源占比 · 前 8 名</h2>
      <div class="seg" id="metricseg">
        <button data-metric="cpu" class="active" type="button">CPU</button>
        <button data-metric="mem" type="button">内存</button>
        <button data-metric="tx" type="button">上行</button>
        <button data-metric="rx" type="button">下行</button>
      </div>
      <span class="muted small" id="chartupd">占比按全部项目合计计算，每 5 秒刷新；点色块看数值</span>
    </div>
    <div class="chart" id="chart"><div class="cseg other" style="width:100%">暂无数据</div></div>
    <div class="lgbar" id="chartlegend"></div>
  </section>

  <div class="grid" id="grid">
__CARDS__
  </div>
  <footer>由 docker_notes.py 生成 · 数据为 docker stats 单次采样（瞬时值） · 未输入搜索词时每 2 分钟自动刷新</footer>
</div>

<div class="selbar" id="selbar" hidden>
  已选 <b id="selcount">0</b> 个项目
  <button id="selallbtn" type="button">全选当前</button>
  <button id="selcopy" type="button">复制清单</button>
  <button id="selclear" type="button">清空</button>
</div>

<dialog id="editdlg">
  <form method="dialog" id="editform">
    <div class="dlg-head">
      <h3>编辑备注 · <span id="e_title" class="muted"></span></h3>
      <span class="muted small">保存在看板本地，不修改 compose 文件</span>
    </div>
    <div class="dlg-body">
      <input type="hidden" id="e_key">
      <div class="field"><label>中文名</label><input id="e_name" maxlength="40" placeholder="例：电商后端"></div>
      <div class="field"><label>说明</label><textarea id="e_desc" maxlength="300" placeholder="是什么 + 关键约束"></textarea></div>
      <div class="field"><label>负责人</label><input id="e_owner" maxlength="40" placeholder="平台组 / 张伟"></div>
      <div class="field"><label>等级</label>
        <select id="e_tier">
          <option value="">未设置</option><option value="P0">P0 核心</option>
          <option value="P1">P1 重要</option><option value="P2">P2 一般</option>
        </select>
      </div>
      <div class="field"><label>访问地址</label><input id="e_url" maxlength="200" placeholder="https://shop.example.com"></div>
      <div class="field"><label>服务名</label>
        <div class="svc-list" id="e_services"></div>
      </div>
    </div>
    <div class="dlg-foot">
      <button class="btn" value="cancel" type="button" id="e_cancel">取消</button>
      <button class="btn primary" value="save" type="button" id="e_save">保存</button>
    </div>
  </form>
</dialog>

<dialog id="ctldlg">
  <div class="dlg-head">
    <h3>容器控制台 · <span id="c_title" class="muted"></span></h3>
    <button class="ibtn" type="button" id="c_close">✕</button>
  </div>
  <div class="tabs" id="ctabs">
    <button class="tab active" data-tab="info" type="button">基本信息</button>
    <button class="tab" data-tab="logs" type="button">日志</button>
    <button class="tab" data-tab="shell" type="button">终端</button>
    <button class="tab" data-tab="ops" type="button">操作</button>
  </div>
  <div class="pane active" id="pane-info"><div class="muted small">加载中…</div></div>
  <div class="pane" id="pane-logs">
    <div class="cmdbar">
      <input id="loglines" value="200" size="4" style="flex:0 0 70px" aria-label="行数">
      <button class="btn" id="logreload" type="button">刷新日志</button>
      <span class="muted small" style="align-self:center">docker logs --tail N --timestamps</span>
    </div>
    <pre class="log" id="logbox">（点「刷新日志」加载）</pre>
  </div>
  <div class="pane" id="pane-shell">
    <div class="cmdbar">
      <input id="cmdinput" placeholder="例：ls -al /app   ·   env | sort" autocomplete="off">
      <button class="btn primary" id="cmdrun" type="button">执行</button>
    </div>
    <div class="muted small" style="margin-bottom:8px">
      单条命令、非交互式（docker exec … sh -lc）。需要 <code>ALLOW_EXEC=1</code>。
    </div>
    <pre class="log" id="cmdout">（输出会显示在这里）</pre>
  </div>
  <div class="pane" id="pane-ops">
    <div class="muted small" id="ops_hint" style="margin-bottom:10px"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn" data-op="start" type="button">启动</button>
      <button class="btn" data-op="stop" type="button">停止</button>
      <button class="btn danger" data-op="restart" type="button">重启</button>
    </div>
  </div>
</dialog>

<div class="tip" id="tip"></div>
<div class="toast" id="toast"></div>

<script>
window.__DN = __DN_JSON__;
window.__INV = __INV_JSON__;
</script>
<script>
(function(){
  var D = window.__DN || {};
  var INV = window.__INV || {projects:[]};
  var grid = document.getElementById('grid');
  var q = document.getElementById('q');
  var fcount = document.getElementById('fcount');
  var selbar = document.getElementById('selbar');
  var tip = document.getElementById('tip');
  var sel = Object.create(null);
  var filters = {state:[], health:[], tier:[]};
  var onlySel = false;
  var metric = 'cpu';
  var COLORS = ['#1F4E79','#2E75B6','#4E9BD8','#7FB3E0','#2E7D5B','#59A17F','#ED7D31','#C0504D'];

  function $(s, r){ return (r || document).querySelector(s); }
  function $$(s, r){ return [].slice.call((r || document).querySelectorAll(s)); }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

  function toast(msg, isErr){
    var t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast on' + (isErr ? ' err' : '');
    clearTimeout(toast._t);
    toast._t = setTimeout(function(){ t.className = 'toast'; }, 2600);
  }

  /* ---------------- 过滤 / 搜索 / 视图 ---------------- */
  function apply(){
    var cards = $$('.card', grid), shown = 0;
    cards.forEach(function(c){
      var ok = true;
      ['state','health','tier'].forEach(function(g){
        var vals = filters[g];
        if (vals.length && vals.indexOf(c.dataset[g]) < 0) ok = false;
      });
      if (ok && onlySel && !sel[c.dataset.key]) ok = false;
      var kw = q.value.trim().toLowerCase();
      if (ok && kw && (c.dataset.search || '').toLowerCase().indexOf(kw) < 0) ok = false;
      c.classList.toggle('hide', !ok);
      if (ok) shown++;
    });
    fcount.textContent = '显示 ' + shown + ' / ' + cards.length + ' 个项目';
    syncSel();
  }

  function setView(v){
    grid.classList.toggle('list', v === 'list');
    $$('.vbtn').forEach(function(b){ b.classList.toggle('active', b.dataset.view === v); });
    try { localStorage.setItem('dn.view', v); } catch (e) {}
  }

  /* ---------------- 按资源排序 ---------------- */
  var sortBy = 'none', sortDesc = true;

  function sortCards(){
    if (sortBy === 'none') return;
    var val = {};
    (INV.projects || []).forEach(function(p){ val[p.name] = metricVal(p, sortBy); });
    var cards = $$('.card', grid);
    cards.sort(function(a, b){
      var d = (val[a.dataset.key] || 0) - (val[b.dataset.key] || 0);
      return sortDesc ? -d : d;
    });
    cards.forEach(function(c){ grid.appendChild(c); });
  }

  /* ---------------- 勾选 ---------------- */
  function syncSel(){
    var keys = Object.keys(sel);
    document.getElementById('selcount').textContent = keys.length;
    selbar.hidden = keys.length === 0;
    $$('.card', grid).forEach(function(c){ c.classList.toggle('picked', !!sel[c.dataset.key]); });
  }

  function selectedProjects(){
    return INV.projects.filter(function(p){ return sel[p.name]; });
  }

  /* ---------------- 占比图 ---------------- */
  function bytes(n){
    n = Number(n) || 0;
    if (n < 1000) return n + ' B';
    var u = ['KB','MB','GB','TB'], i = -1;
    do { n /= 1000; i++; } while (n >= 1000 && i < u.length - 1);
    return n.toFixed(n >= 10 ? 1 : 2) + ' ' + u[i];
  }
  function metricVal(p, m){
    var t = p.totals || {};
    if (m === 'cpu') return t.cpu_percent || 0;
    if (m === 'mem') return t.mem_bytes || 0;
    if (m === 'rx') return t.net_rx_bytes || 0;
    return t.net_tx_bytes || 0;
  }
  function metricText(v, m){
    return m === 'cpu' ? (Number(v) || 0).toFixed(1) + '%' : bytes(v);
  }

  function renderChart(){
    var ps = (INV.projects || []).slice();
    var total = ps.reduce(function(s, p){ return s + metricVal(p, metric); }, 0);
    var chart = document.getElementById('chart');
    var legend = document.getElementById('chartlegend');
    if (!ps.length || total <= 0){
      chart.innerHTML = '<div class="cseg other" style="width:100%">暂无数据</div>';
      legend.innerHTML = '';
      return;
    }
    ps.sort(function(a, b){ return metricVal(b, metric) - metricVal(a, metric); });
    var top = ps.slice(0, 8);
    var rest = total - top.reduce(function(s, p){ return s + metricVal(p, metric); }, 0);
    var parts = [], lgs = [];
    top.forEach(function(p, i){
      var v = metricVal(p, metric), pct = v / total * 100;
      var color = COLORS[i % COLORS.length];
      var label = (p.note && p.note.name) || p.name;
      var tipText = label + '（' + p.name + '）\n' + metricText(v, metric) + ' · ' + pct.toFixed(1) + '%';
      parts.push('<div class="cseg" data-key="' + esc(p.name) + '" data-tip="' + esc(tipText) +
                 '" style="width:' + pct.toFixed(3) + '%;background:' + color + '">' +
                 (pct >= 6 ? esc(label) : '') + '</div>');
      lgs.push('<span class="lg" data-key="' + esc(p.name) + '"><i style="background:' + color + '"></i>' +
               '<b>' + esc(label) + '</b><span class="v">' + metricText(v, metric) +
               ' · ' + pct.toFixed(1) + '%</span></span>');
    });
    if (rest > 0.0001){
      var rp = rest / total * 100;
      parts.push('<div class="cseg other" data-tip="其他 ' + (ps.length - top.length) + ' 个项目\n' +
                 metricText(rest, metric) + ' · ' + rp.toFixed(1) + '%" style="width:' + rp.toFixed(3) + '%">' +
                 (rp >= 6 ? '其他' : '') + '</div>');
      lgs.push('<span class="lg"><i style="background:#cbd5df"></i><b>其他 ' + (ps.length - top.length) +
               ' 个</b><span class="v">' + metricText(rest, metric) + ' · ' + rp.toFixed(1) + '%</span></span>');
    }
    chart.innerHTML = parts.join('');
    legend.innerHTML = lgs.join('');
    if (D.live) document.getElementById('chartupd').textContent =
      '每 5 秒刷新 · 更新于 ' + new Date().toLocaleTimeString();
  }

  function showTip(text, e){
    tip.textContent = text || '';
    tip.classList.add('on');
    tip.style.left = Math.min(window.innerWidth - 260, e.clientX + 14) + 'px';
    tip.style.top = (e.clientY + 16) + 'px';
  }

  /* ---------------- 编辑备注 ---------------- */
  function openEdit(key){
    var p = null;
    INV.projects.forEach(function(x){ if (x.name === key) p = x; });
    if (!p) return;
    if (!D.live){ toast('静态导出文件，只有 serve 模式能改备注', true); return; }
    var n = p.note || {};
    document.getElementById('e_key').value = key;
    document.getElementById('e_title').textContent = p.name;
    document.getElementById('e_name').value = n.name || '';
    document.getElementById('e_desc').value = n.desc || '';
    document.getElementById('e_owner').value = n.owner || '';
    document.getElementById('e_tier').value = n.tier || '';
    document.getElementById('e_url').value = n.url || '';
    var box = document.getElementById('e_services');
    box.innerHTML = '';
    (p.services || []).forEach(function(s){
      var row = document.createElement('div');
      row.className = 'srow';
      row.innerHTML = '<code title="' + esc(s.service) + '">' + esc(s.service) + '</code>' +
        '<input data-svc="' + esc(s.service) + '" placeholder="服务中文名" maxlength="40" value="' +
        esc((s.note || {}).name || '') + '">';
      box.appendChild(row);
    });
    document.getElementById('editdlg').showModal();
  }

  function saveEdit(){
    var key = document.getElementById('e_key').value;
    var items = [{
      scope: 'project', key: key,
      fields: {
        name: document.getElementById('e_name').value.trim(),
        desc: document.getElementById('e_desc').value.trim(),
        owner: document.getElementById('e_owner').value.trim(),
        tier: document.getElementById('e_tier').value,
        url: document.getElementById('e_url').value.trim()
      }
    }];
    $$('#e_services input').forEach(function(inp){
      var svcKey = key + '/' + inp.dataset.svc;
      var old = '';
      INV.projects.forEach(function(p){
        if (p.name !== key) return;
        (p.services || []).forEach(function(s){
          if (s.service === inp.dataset.svc) old = (s.note || {}).name || '';
        });
      });
      if (inp.value.trim() !== old){
        items.push({ scope: 'service', key: svcKey, fields: { name: inp.value.trim() } });
      }
    });
    api('/api/notes', 'POST', { items: items }).then(function(res){
      if (!res || !res.ok) throw new Error((res && res.error) || '保存失败');
      applyCardHtml(key, res.html);
      document.getElementById('editdlg').close();
      toast('备注已保存');
    }).catch(function(e){ toast(e.message || '保存失败', true); });
  }

  function applyCardHtml(key, htmlStr){
    if (!htmlStr) return;
    var holder = document.createElement('div');
    holder.innerHTML = htmlStr;
    var fresh = holder.firstElementChild;
    if (!fresh) return;
    var old = $('.card[data-key="' + CSS.escape(key) + '"]', grid);
    if (old) old.replaceWith(fresh);
    else grid.appendChild(fresh);
    hydrateCards();
    syncSel();
    apply();
    if (sel[key]) { var cb = $('.pick', fresh); if (cb) cb.checked = true; }
  }

  /* ---------------- 容器控制台 ---------------- */
  var curContainer = null;

  function api(path, method, body){
    var opt = { method: method || 'GET', headers: {} };
    if (body){
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    return fetch(path, opt).then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(j){
        if (!r.ok && !j.error) j.error = 'HTTP ' + r.status;
        j.__status = r.status;
        return j;
      });
    });
  }

  function openConsole(container, service){
    if (!D.live){ toast('静态导出文件，只有 serve 模式能看控制台', true); return; }
    curContainer = container;
    document.getElementById('c_title').textContent = service;
    document.getElementById('pane-info').innerHTML = '<div class="muted small">加载中…</div>';
    document.getElementById('logbox').textContent = '（点「刷新日志」加载）';
    document.getElementById('cmdout').textContent = '（输出会显示在这里）';
    var hint = document.getElementById('ops_hint');
    hint.textContent = D.allow_actions
      ? '启停会真实作用于运行中的容器，注意业务影响。'
      : '未开启：需要 ALLOW_ACTIONS=1 且设置 BASIC_AUTH 后重启容器。';
    $$('#pane-ops .btn').forEach(function(b){ b.disabled = !D.allow_actions; });
    setTab('info');
    document.getElementById('ctldlg').showModal();
    loadInfo(container);
  }

  function setTab(name){
    $$('#ctabs .tab').forEach(function(b){ b.classList.toggle('active', b.dataset.tab === name); });
    ['info','logs','shell','ops'].forEach(function(n){
      document.getElementById('pane-' + n).classList.toggle('active', n === name);
    });
    if (name === 'logs') loadLogs();
  }

  function loadInfo(container){
    api('/api/inspect?container=' + encodeURIComponent(container)).then(function(j){
      var box = document.getElementById('pane-info');
      if (!j.ok){ box.innerHTML = '<div class="muted">' + esc(j.error || '读取失败') + '</div>'; return; }
      var d = j.data || {}, rows = [
        ['容器', d.name], ['镜像', d.image], ['状态', d.status + (d.health ? ' / ' + d.health : '')],
        ['创建时间', d.created], ['启动时间', d.started], ['重启策略', d.restart],
        ['端口', (d.ports || []).join('、') || '-'], ['网络', (d.networks || []).join('、') || '-'],
        ['数据卷', (d.mounts || []).join('、') || '-'], ['工作目录', d.workdir || '-'],
        ['环境变量', (d.env || []).length + ' 项'],
        ['标签', (d.labels || []).length + ' 项']
      ];
      box.innerHTML = '<dl class="kv">' + rows.map(function(r){
        return '<dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd>';
      }).join('') +
      '<dt>环境变量</dt><dd><pre class="log" style="max-height:180px">' +
        esc((d.env || []).join('\n')) + '</pre></dd></dl>';
    });
  }

  function loadLogs(){
    if (!curContainer) return;
    var n = parseInt(document.getElementById('loglines').value, 10) || 200;
    var box = document.getElementById('logbox');
    box.textContent = '加载中…';
    api('/api/logs?container=' + encodeURIComponent(curContainer) + '&lines=' + n).then(function(j){
      if (!j.ok){ box.textContent = j.error || '读取失败'; return; }
      box.textContent = j.text || '（无输出）';
      box.scrollTop = box.scrollHeight;
    });
  }

  function runCmd(){
    if (!curContainer) return;
    var input = document.getElementById('cmdinput');
    var cmd = input.value.trim();
    if (!cmd) return;
    var out = document.getElementById('cmdout');
    out.textContent = '$ ' + cmd + '\n运行中…';
    api('/api/exec', 'POST', { container: curContainer, cmd: cmd }).then(function(j){
      if (!j.ok){ out.textContent += '\n' + (j.error || '执行失败'); return; }
      out.textContent = '$ ' + cmd + '\n' + (j.output || '(无输出)') + '\n[exit ' + j.exit + ']';
      out.scrollTop = out.scrollHeight;
    });
  }

  function doOp(op){
    if (!D.allow_actions){ toast('未开启容器操作', true); return; }
    var label = {start:'启动', stop:'停止', restart:'重启'}[op];
    if (!confirm('确定要' + label + '容器 ' + curContainer + ' 吗？')) return;
    api('/api/action', 'POST', { container: curContainer, action: op }).then(function(j){
      if (!j.ok){ toast(j.error || (label + '失败'), true); return; }
      toast(label + '成功');
      if (j.inventory) { INV = j.inventory; renderChart(); }
      loadInfo(curContainer);
      setTimeout(function(){ location.reload(); }, 1200);
    });
  }

  /* ---------------- 事件绑定 ---------------- */
  function hydrateCards(){
    $$('.card', grid).forEach(function(c){
      var cb = $('.pick', c);
      if (cb) cb.checked = !!sel[c.dataset.key];
    });
    // 「打开」按钮：没有 note.url 时按当前访问的主机拼 http://host:port
    $$('.openbtn[data-port]', grid).forEach(function(a){
      var ip = a.dataset.ip || location.hostname;
      a.href = 'http://' + ip + ':' + a.dataset.port;
      a.target = '_blank';
      a.rel = 'noopener';
    });
  }

  q.addEventListener('input', apply);

  $$('#filters input[type=checkbox]').forEach(function(cb){
    cb.addEventListener('change', function(){
      if (cb.id === 'onlysel'){ onlySel = cb.checked; cb.closest('.fchip').classList.toggle('on', cb.checked); apply(); return; }
      var g = cb.dataset.group, v = cb.value;
      var arr = filters[g], i = arr.indexOf(v);
      if (cb.checked && i < 0) arr.push(v);
      if (!cb.checked && i >= 0) arr.splice(i, 1);
      cb.closest('.fchip').classList.toggle('on', cb.checked);
      apply();
    });
  });

  document.getElementById('clearf').addEventListener('click', function(){
    filters = {state:[], health:[], tier:[]};
    onlySel = false;
    $$('#filters input[type=checkbox]').forEach(function(cb){ cb.checked = false; });
    $$('.fchip').forEach(function(c){ c.classList.remove('on'); });
    q.value = '';
    apply();
  });

  $$('.vbtn').forEach(function(b){
    b.addEventListener('click', function(){ setView(b.dataset.view); });
  });

  document.getElementById('sortby').addEventListener('change', function(e){
    sortBy = e.target.value;
    sortCards();
  });
  document.getElementById('sortdir').addEventListener('click', function(){
    sortDesc = !sortDesc;
    this.textContent = sortDesc ? '降序 ↓' : '升序 ↑';
    sortCards();
  });

  grid.addEventListener('change', function(e){
    var t = e.target;
    if (!t.classList || !t.classList.contains('pick')) return;
    var key = t.closest('.card').dataset.key;
    if (t.checked) sel[key] = true; else delete sel[key];
    syncSel();
    if (onlySel) apply();
  });

  grid.addEventListener('click', function(e){
    var svc = e.target.closest && e.target.closest('.svcbtn');
    if (svc){ openConsole(svc.dataset.container, svc.dataset.service); return; }
    var act = e.target.closest && e.target.closest('[data-act="edit"]');
    if (act){ openEdit(act.closest('.card').dataset.key); return; }
  });

  document.getElementById('selallbtn').addEventListener('click', function(){
    $$('.card', grid).forEach(function(c){ if (!c.classList.contains('hide')) sel[c.dataset.key] = true; });
    hydrateCards(); syncSel();
  });
  document.getElementById('selclear').addEventListener('click', function(){
    sel = Object.create(null); hydrateCards(); syncSel(); if (onlySel) apply();
  });
  document.getElementById('selcopy').addEventListener('click', function(){
    var ps = selectedProjects();
    if (!ps.length){ toast('还没有选中项目', true); return; }
    var lines = ['| 项目 | 中文名 | 等级 | 状态 | 端口 | 地址 |', '|---|---|---|---|---|---|'];
    ps.forEach(function(p){
      var n = p.note || {};
      var ports = [];
      (p.services || []).forEach(function(s){
        (s.ports || []).forEach(function(x){ if (x.host_port && x.host_port !== 'host') ports.push(x.host_port + '→' + x.container); });
      });
      lines.push('| ' + p.name + ' | ' + (n.name || '-') + ' | ' + (n.tier || '-') + ' | ' +
                 (p.status || '-') + ' | ' + (ports.join(', ') || '-') + ' | ' + (n.url || '-') + ' |');
    });
    var text = lines.join('\n');
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(function(){ toast('已复制 ' + ps.length + ' 个项目'); });
    else { window.prompt('复制到剪贴板：', text); }
  });

  /* 图表交互 */
  document.getElementById('chart').addEventListener('mousemove', function(e){
    var s = e.target.closest('.cseg');
    if (!s || !s.dataset.tip){ tip.classList.remove('on'); return; }
    showTip(s.dataset.tip, e);
  });
  document.getElementById('chart').addEventListener('mouseleave', function(){ tip.classList.remove('on'); });
  document.getElementById('chartlegend').addEventListener('mousemove', function(e){
    var s = e.target.closest('.lg');
    if (!s) { tip.classList.remove('on'); return; }
    var c = $('.cseg[data-key="' + CSS.escape(s.dataset.key || '') + '"]', document.getElementById('chart'));
    if (c && c.dataset.tip) showTip(c.dataset.tip, e);
  });
  document.getElementById('chartlegend').addEventListener('click', function(e){
    var s = e.target.closest('.lg');
    if (!s || !s.dataset.key) return;
    var card = $('.card[data-key="' + CSS.escape(s.dataset.key) + '"]', grid);
    if (!card) return;
    if (card.classList.contains('hide')){
      filters = {state:[], health:[], tier:[]}; onlySel = false; q.value = '';
      $$('#filters input[type=checkbox]').forEach(function(cb){ cb.checked = false; });
      $$('.fchip').forEach(function(c){ c.classList.remove('on'); });
      apply();
    }
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.remove('flash');
    void card.offsetWidth;
    card.classList.add('flash');
  });

  document.getElementById('metricseg').addEventListener('click', function(e){
    var b = e.target.closest('button[data-metric]');
    if (!b) return;
    metric = b.dataset.metric;
    $$('#metricseg button').forEach(function(x){ x.classList.toggle('active', x === b); });
    renderChart();
  });

  /* 弹窗 */
  document.getElementById('e_cancel').addEventListener('click', function(){ document.getElementById('editdlg').close(); });
  document.getElementById('e_save').addEventListener('click', saveEdit);
  document.getElementById('c_close').addEventListener('click', function(){ document.getElementById('ctldlg').close(); });
  $$('#ctabs .tab').forEach(function(b){ b.addEventListener('click', function(){ setTab(b.dataset.tab); }); });
  document.getElementById('logreload').addEventListener('click', loadLogs);
  document.getElementById('cmdrun').addEventListener('click', runCmd);
  document.getElementById('cmdinput').addEventListener('keydown', function(e){ if (e.key === 'Enter') runCmd(); });
  $$('#pane-ops .btn').forEach(function(b){ b.addEventListener('click', function(){ doOp(b.dataset.op); }); });

  /* 初始化 */
  var hv = (location.hash || '').replace('#', '');
  if (hv === 'list' || hv === 'tile') setView(hv);
  else { try { setView(localStorage.getItem('dn.view') || 'tile'); } catch (e) { setView('tile'); } }
  hydrateCards();
  apply();
  sortCards();
  renderChart();

  /* 静态导出文件没有后端：隐藏会失败的入口提示 */
  if (!D.live){
    document.getElementById('chartupd').textContent = '静态导出：数值来自导出那一刻，刷新页面不会更新';
  } else {
    setInterval(function(){
      fetch('/api/inventory.json').then(function(r){ return r.json(); }).then(function(inv){
        if (!inv || !inv.projects) return;
        INV = inv;
        renderChart();
      }).catch(function(){});
    }, 5000);
  }

  /* 定时整页刷新：编辑/控制台打开或有勾选时不动，避免打断操作 */
  setTimeout(function(){
    if (document.visibilityState !== 'visible') return;
    if (document.querySelector('dialog[open]')) return;
    if (Object.keys(sel).length) return;
    if (q.value.trim()) return;
    location.reload();
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


def _project_state(p):
    """给前端筛选用：running / exited / mixed / empty"""
    states = [s.get("state") for s in p.get("services") or []]
    if not states:
        return "empty"
    running = [x for x in states if x == "running"]
    if len(running) == len(states):
        return "running"
    if not running:
        return "exited"
    return "mixed"


def _project_health(p):
    for s in p.get("services") or []:
        h = s.get("health")
        if h and h != "healthy":
            return "bad"
    return "none"


def _open_target(p):
    """返回 (端口, 主机) 供前端拼 http://host:port。

    note.url 优先（直接用它，不需要端口）；否则取第一个已发布的端口。
    """
    note = p.get("note") or {}
    if note.get("url"):
        return None, ""
    for s in p.get("services") or []:
        for x in s.get("ports") or []:
            port = x.get("host_port")
            if port and port != "host":
                ip = x.get("host_ip") or ""
                if ip in ("0.0.0.0", "::", ""):
                    ip = ""
                return port, ip
    return None, ""


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
            "<tr><td class='svc'><button type='button' class='svcbtn' "
            "data-container='%s' data-service='%s' title='打开控制台：信息 / 日志 / 终端'>%s</button>%s</td>"
            "<td><span class='state %s'>%s</span> %s<div class='muted small'>%s</div></td>"
            "<td class='ports'>%s</td>"
            "<td class='metric'>%s</td>"
            "<td class='metric'>%s<div class='muted small'>%s</div></td>"
            "<td class='net'>&darr; %s<br>&uarr; %s</td></tr>" % (
                html.escape(s.get("container", "")),
                html.escape(s["service"]),
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
    svc_terms = []
    for s in p["services"]:
        svc_terms.append(s["service"])
        if (s.get("note") or {}).get("name"):
            svc_terms.append(s["note"]["name"])
    search = " ".join([p["name"], note.get("name", ""), note.get("desc", ""),
                       note.get("owner", ""), " ".join(ports), " ".join(svc_terms)])

    meta_bits = []
    if note.get("owner"):
        meta_bits.append("负责人 " + html.escape(note["owner"]))
    if p.get("status"):
        meta_bits.append(html.escape(p["status"]))
    if note.get("backup"):
        meta_bits.append("备份 " + html.escape(note["backup"]))
    desc = ("<p class='desc'>%s</p>" % html.escape(note["desc"])) if note.get("desc") else ""

    tier = (note.get("tier") or "none").upper() if note.get("tier") else "none"
    if tier not in ("P0", "P1", "P2"):
        tier = "none"

    # 打开按钮：有 url 用 url，否则用第一个发布端口（前端按当前访问主机的 IP 拼）
    open_btn = ""
    if note.get("url"):
        open_btn = ('<a class="btn openbtn" href="%s" target="_blank" rel="noopener">打开</a>'
                    % html.escape(note["url"]))
    else:
        port, ip = _open_target(p)
        if port:
            open_btn = ('<a class="btn openbtn" href="#" data-port="%s" data-ip="%s">打开</a>'
                        % (html.escape(str(port)), html.escape(ip)))
    if note.get("url"):
        url_chip = ('<div class="chips"><span class="label">地址</span>'
                    '<a class="chip" href="%s" target="_blank" rel="noopener">%s</a></div>'
                    % (html.escape(note["url"]), html.escape(note["url"])))
    else:
        url_chip = ""

    src_badge = ('<span class="src" title="在界面里改过，覆盖 compose 中的备注">界面备注</span>'
                 if p.get("note_override") else "")

    return """    <section class="card" data-key="{key}" data-search="{search}" data-state="{state}" data-health="{health}" data-tier="{tier}">
      <header>
        <div class="ptitle">
          <input type="checkbox" class="pick" title="选中此项目" aria-label="选中 {cn}">
          <span class="pname">{pname}</span>
          <span class="cnname">{cn}</span>
          {badge}{src}
        </div>
        <div class="acts">
          {open_btn}
          <button class="ibtn" type="button" data-act="edit" title="编辑备注">✎</button>
        </div>
      </header>
      <div class="meta">{meta}</div>
      {desc}
      <div class="chips"><span class="label">端口</span>{ports}</div>
      <div class="chips"><span class="label">网络</span>{nets}</div>
      {urlchip}
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
        key=html.escape(p["name"]),
        search=html.escape(search),
        state=_project_state(p),
        health=_project_health(p),
        tier=tier,
        pname=html.escape(p["name"]),
        cn=html.escape(project_title(p)),
        badge=_badge(note),
        src=src_badge,
        open_btn=open_btn,
        meta=" ｜ ".join(meta_bits),
        desc=desc,
        ports=port_chips,
        nets=net_chips,
        urlchip=url_chip,
        cpu=tot.get("cpu_percent") or 0,
        mem=human_bytes(tot.get("mem_bytes")),
        rx=human_bytes(tot.get("net_rx_bytes"), 1000),
        tx=human_bytes(tot.get("net_tx_bytes"), 1000),
        n=len(p["services"]),
        rows="".join(rows),
    )


def render_html(inv, flags=None):
    flags = dict(flags or {})
    t = summarize(inv)
    subtitle = ("主机 <b>%s</b> ｜ 采集时间 %s ｜ 项目 <b>%d</b> · 服务 <b>%d</b>（运行中 %d）｜ "
                "CPU 合计 <b>%.1f%%</b> ｜ 内存合计 <b>%s</b> ｜ 网络 ↓%s ↑%s" % (
                    html.escape(inv.get("host", "-")),
                    html.escape(inv.get("generated_at", "-")),
                    t["projects"], t["services"], t["running"],
                    t["cpu"], human_bytes(t["mem"]),
                    human_bytes(t["rx"], 1000), human_bytes(t["tx"], 1000)))
    if t["unlabeled"]:
        subtitle += ("<br>⚠️ <b>%d</b> 个项目还没写中文备注：点卡片右上角 ✎ 直接补，"
                     "或在 compose 里加 <code>x-note:</code>" % t["unlabeled"])
    cards = "".join(render_card(p) for p in inv.get("projects") or [])

    dn_json = json.dumps({
        "live": bool(flags.get("live")),
        "allow_actions": bool(flags.get("allow_actions")),
        "allow_exec": bool(flags.get("allow_exec")),
    }, ensure_ascii=False)
    try:
        inv_json = json.dumps(inv, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        inv_json = '{"projects":[]}'
    inv_json = inv_json.replace("</", "<\\/")

    out = HTML_HEAD.replace("__HOST__", html.escape(inv.get("host", "-")))
    out = out.replace("__SUBTITLE__", subtitle).replace("__CARDS__", cards)
    out = out.replace("__DN_JSON__", dn_json).replace("__INV_JSON__", inv_json)
    return out


# --------------------------------------------------------------------------- #
# Web 服务（容器部署用）
# --------------------------------------------------------------------------- #
class NoteStore:
    """界面里改的备注，落在本地 JSON，覆盖 compose 的 x-note / 容器 labels。

    不直接改 compose 文件：YAML 往返会丢注释和排版，而且改项目级备注要重建容器。
    """

    SCOPES = ("projects", "services")
    SCOPE_KEYS = {"project": "projects", "service": "services"}

    def __init__(self, path=None):
        self.path = path or ""
        self.lock = threading.Lock()
        self.data = {"projects": {}, "services": {}}
        self.load()

    # ---------- 持久化 ----------
    def load(self):
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            raw = json.loads(open(self.path, encoding="utf-8").read() or "{}")
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        for k in self.SCOPES:
            v = raw.get(k)
            if isinstance(v, dict):
                self.data[k] = {str(a): {str(x): str(y) for x, y in (b or {}).items()}
                                for a, b in v.items() if isinstance(b, dict)}

    def save(self):
        if not self.path:
            raise Fail("备注文件未配置（NOTES_FILE 为空），无法保存")
        d = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as exc:
            raise Fail("备注文件写入失败（%s）：确认容器挂载了可写目录，如 -v docker-notes-data:/data" % exc)

    # ---------- 合并 ----------
    def apply(self, inv):
        for p in inv.get("projects") or []:
            hit = self.data["projects"].get(p["name"])
            if hit:
                p["note"] = dict(p.get("note") or {}, **hit)
                p["note_override"] = True
            for s in p.get("services") or []:
                key = "%s/%s" % (p["name"], s.get("service"))
                hit = self.data["services"].get(key)
                if hit:
                    s["note"] = dict(s.get("note") or {}, **hit)
                    s["note_override"] = True
        return inv

    def update(self, items):
        """items: [{"scope":"project|service","key":"...","fields":{...}}]；空值删除该字段。"""
        if not isinstance(items, list) or not items:
            raise Fail("items 必须是非空数组")
        with self.lock:
            for it in items:
                if not isinstance(it, dict):
                    raise Fail("items 元素必须是对象")
                scope = it.get("scope")
                bucket = self.SCOPE_KEYS.get(scope)
                if not bucket:
                    raise Fail("未知 scope：%s（只能是 project / service）" % scope)
                key = str(it.get("key") or "").strip()
                fields = it.get("fields") or {}
                if not key or len(key) > 200 or not isinstance(fields, dict):
                    raise Fail("缺少 key 或 fields 不合法")
                cur = self.data[bucket].setdefault(key, {})
                for k, v in fields.items():
                    k = str(k)
                    if k not in NOTE_KEYS:
                        continue
                    v = ("" if v is None else str(v)).strip()
                    if not v:
                        cur.pop(k, None)
                        continue
                    if len(v) > 400:
                        raise Fail("字段 %s 太长（最多 400 字）" % k)
                    cur[k] = v
                if not cur:
                    self.data[bucket].pop(key, None)
            self.save()
        return True


def apply_overrides(inv, store):
    return store.apply(inv) if store else inv


class State:
    """采集结果的内存快照；采集失败时保留上一次成功的数据。

    `base` 是不套备注的原始采集结果；`inventory` 是套了界面备注（notes.json）
    之后对外展示的版本。改备注时只需要从 base 重新套一遍覆盖层，不用重新采集。
    """

    def __init__(self, seed=None, store=None, path_map=None):
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.base = copy.deepcopy(seed) if seed is not None else None
        self.inventory = copy.deepcopy(seed) if seed is not None else None
        self.error = None
        self.updated_at = time.time() if seed else None
        self.ok_count = 0
        self.store = store
        self.path_map = path_map
        if self.inventory is not None and self.store:
            self.store.apply(self.inventory)

    def set_ok(self, inv):
        merged = copy.deepcopy(inv) if self.store else inv
        if self.store:
            self.store.apply(merged)
        with self.lock:
            self.base = inv
            self.inventory = merged
            self.error = None
            self.updated_at = time.time()
            self.ok_count += 1

    def set_error(self, msg):
        with self.lock:
            self.error = msg

    def snapshot(self):
        with self.lock:
            return self.inventory, self.error, self.updated_at

    def refresh(self):
        with self.refresh_lock:
            inv = collect(self.path_map)
            self.set_ok(inv)
            return inv

    def refresh_safe(self):
        try:
            return self.refresh()
        except Exception as exc:
            self.set_error(str(exc))
            return None

    def reapply_notes(self):
        """备注改动后不重新采集，只把覆盖层重新套到上一次采集结果上。"""
        with self.lock:
            if self.base is None:
                return None
            inv = copy.deepcopy(self.base)
            if self.store:
                self.store.apply(inv)
            self.inventory = inv
            return inv


def collector_loop(state, interval, path_map=None):
    if path_map is not None:
        state.path_map = path_map
    while True:
        state.refresh_safe()
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


CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


def make_handler(state, basic_auth=None, store=None, path_map=None,
                 allow_actions=False, allow_exec=False):
    flags = {"live": True, "allow_actions": bool(allow_actions),
             "allow_exec": bool(allow_exec)}

    class Handler(BaseHTTPRequestHandler):
        server_version = "docker-notes"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        # ---------- 基础 ----------
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

        def _json(self, code, obj, extra=None):
            self._send(code, json.dumps(obj, ensure_ascii=False),
                       "application/json; charset=utf-8", extra)

        def _reject(self, code, obj):
            """直接拒绝且不读取请求体：必须关连接，否则残留字节会污染下一个请求。"""
            self.close_connection = True
            self._json(code, obj, {"Connection": "close"})

        def _unauthorized(self):
            self.close_connection = True
            self._send(401, "需要认证\n", "text/plain; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="docker-notes", charset="UTF-8"',
                        "Connection": "close"})

        def _cross_origin(self):
            origin = self.headers.get("Origin") or ""
            if not origin:
                return False
            host = (self.headers.get("Host") or "").lower()
            try:
                netloc = urlsplit(origin).netloc.lower()
            except Exception:
                return True
            return not host or netloc != host

        def _read_json(self, limit=65536):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                return {}
            if n > limit:
                raise ValueError("请求体太大")
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

        def _docker(self, argv, timeout=25):
            if shutil.which("docker") is None:
                return 127, "未找到 docker 命令"
            try:
                p = subprocess.run(["docker"] + argv, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout)
            except subprocess.TimeoutExpired:
                return 124, "命令超时（%d 秒）" % timeout
            except OSError as exc:
                return 126, str(exc)
            return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()

        # ---------- GET ----------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._authed():
                return self._unauthorized()
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)
            inv, err, updated = state.snapshot()

            if path == "/healthz":
                age = int(time.time() - updated) if updated else None
                body = {"ok": err is None and inv is not None, "age_seconds": age,
                        "projects": len((inv or {}).get("projects") or []), "error": err}
                return self._send(200 if body["ok"] else 503,
                                  json.dumps(body, ensure_ascii=False),
                                  "application/json; charset=utf-8")

            if path == "/metrics":
                return self._send(200, render_metrics(inv, (inv, err, updated)),
                                  "text/plain; charset=utf-8")

            if path == "/api/inventory.json":
                if inv is None:
                    return self._json(503, {"error": err or "首次采集进行中"})
                return self._json(200, inv)

            if path == "/api/inspect":
                name = (qs.get("container") or [""])[0]
                if not CONTAINER_RE.match(name):
                    return self._json(400, {"ok": False, "error": "容器名不合法"})
                rc, out = self._docker(["inspect", name], timeout=20)
                if rc != 0:
                    return self._json(404, {"ok": False, "error": out or "读取失败"})
                try:
                    raw = json.loads(out)[0]
                except Exception:
                    return self._json(500, {"ok": False, "error": "inspect 结果解析失败"})
                cfg = raw.get("Config") or {}
                st = raw.get("State") or {}
                ns = raw.get("NetworkSettings") or {}
                ports = []
                for cp, binds in (ns.get("Ports") or {}).items():
                    for b in binds or []:
                        ports.append("%s:%s→%s" % (b.get("HostIp") or "0.0.0.0",
                                                   b.get("HostPort"), cp))
                return self._json(200, {"ok": True, "data": {
                    "name": (raw.get("Name") or "").lstrip("/"),
                    "image": cfg.get("Image", ""),
                    "status": st.get("Status", ""),
                    "health": (st.get("Health") or {}).get("Status", ""),
                    "created": (raw.get("Created") or "").replace("T", " ")[:19],
                    "started": (st.get("StartedAt") or "").replace("T", " ")[:19],
                    "restart": ((raw.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name", ""),
                    "workdir": cfg.get("WorkingDir", ""),
                    "ports": sorted(ports),
                    "networks": list((ns.get("Networks") or {}).keys()),
                    "mounts": ["%s→%s" % (m.get("Source"), m.get("Destination"))
                               for m in (raw.get("Mounts") or [])],
                    "env": [e for e in (cfg.get("Env") or []) if not e.startswith("PATH=")],
                    "labels": ["%s=%s" % (k, v) for k, v in (cfg.get("Labels") or {}).items()
                               if not k.startswith("com.docker.compose.")][:40],
                }})

            if path == "/api/logs":
                name = (qs.get("container") or [""])[0]
                if not CONTAINER_RE.match(name):
                    return self._json(400, {"ok": False, "error": "容器名不合法"})
                try:
                    n = max(1, min(2000, int((qs.get("lines") or ["200"])[0])))
                except ValueError:
                    n = 200
                rc, out = self._docker(["logs", "--tail", str(n), "--timestamps", name], timeout=30)
                if len(out) > 200000:
                    out = out[-200000:]
                return self._json(200 if rc == 0 else 500,
                                  {"ok": rc == 0, "text": out,
                                   "error": None if rc == 0 else out})

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

                doc = render_html(inv, flags)
                if err:
                    banner = (
                        '<div class="banner show"><b>采集失败</b>：%s<br>'
                        '下方为 %s 的快照，服务会自动重试。</div>'
                        % (html.escape(err), html.escape(inv.get("generated_at", "-")))
                    )
                    doc = doc.replace('<div class="banner" id="banner"></div>', banner)
                return self._send(200, doc)

            return self._send(404, "404 Not Found\n", "text/plain; charset=utf-8")

        # ---------- POST ----------
        def do_POST(self):
            if not self._authed():
                return self._unauthorized()
            if self._cross_origin():
                return self._reject(403, {"ok": False, "error": "跨站请求被拒绝"})
            if "application/json" not in (self.headers.get("Content-Type") or "").lower():
                return self._reject(415, {"ok": False, "error": "请求体必须是 application/json"})
            try:
                body = self._read_json()
            except Exception as exc:
                return self._reject(400, {"ok": False, "error": "请求体解析失败：%s" % exc})
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "error": "请求体必须是 JSON 对象"})

            path = urlsplit(self.path).path.rstrip("/") or "/"

            # 编辑备注
            if path == "/api/notes":
                if store is None:
                    return self._json(403, {"ok": False,
                                            "error": "未配置备注文件（NOTES_FILE），编辑已禁用"})
                items = body.get("items")
                try:
                    store.update(items)
                except Fail as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
                key = ""
                for it in items or []:
                    if not isinstance(it, dict):
                        continue
                    k = str(it.get("key") or "")
                    key = k.split("/", 1)[0] if it.get("scope") == "service" else k
                    if key:
                        break
                inv = state.reapply_notes()
                if inv is None:
                    inv, _e, _u = state.snapshot()
                proj = next((p for p in (inv or {}).get("projects") or [] if p["name"] == key), None)
                if proj is None:
                    # 采集结果里暂时没有这个项目（刚被删或还没采到），退回上一次对外快照
                    prev, _e, _u = state.snapshot()
                    proj = next((p for p in (prev or {}).get("projects") or [] if p["name"] == key), None)
                return self._json(200, {"ok": True, "html": render_card(proj) if proj else ""})

            # 立即重采
            if path == "/api/refresh":
                inv = state.refresh_safe()
                return self._json(200 if inv else 500,
                                  {"ok": bool(inv),
                                   "error": None if inv else (state.snapshot()[1] or "采集失败"),
                                   "inventory": inv})

            # 启动 / 停止 / 重启
            if path == "/api/action":
                if not allow_actions:
                    return self._json(403, {"ok": False,
                                            "error": "未开启容器操作：设置 ALLOW_ACTIONS=1 并重启容器"})
                if not basic_auth:
                    return self._json(403, {"ok": False,
                                            "error": "开启容器操作前必须先设置 BASIC_AUTH"})
                name = str(body.get("container") or "")
                op = str(body.get("action") or "")
                if op not in ("start", "stop", "restart"):
                    return self._json(400, {"ok": False, "error": "action 只能是 start / stop / restart"})
                if not CONTAINER_RE.match(name):
                    return self._json(400, {"ok": False, "error": "容器名不合法"})
                rc, out = self._docker([op, name], timeout=60)
                inv = state.refresh_safe() if rc == 0 else None
                return self._json(200 if rc == 0 else 500,
                                  {"ok": rc == 0, "exit": rc, "output": out,
                                   "error": None if rc == 0 else out,
                                   "inventory": inv})

            # 单条命令
            if path == "/api/exec":
                if not allow_exec:
                    return self._json(403, {"ok": False,
                                            "error": "未开启终端：设置 ALLOW_EXEC=1 并重启容器"})
                if not basic_auth:
                    return self._json(403, {"ok": False, "error": "开启终端前必须先设置 BASIC_AUTH"})
                name = str(body.get("container") or "")
                cmd = str(body.get("cmd") or "").strip()
                if not CONTAINER_RE.match(name):
                    return self._json(400, {"ok": False, "error": "容器名不合法"})
                if not cmd or len(cmd) > 2000:
                    return self._json(400, {"ok": False, "error": "命令为空或过长"})
                rc, out = self._docker(["exec", name, "sh", "-lc", cmd], timeout=30)
                if len(out) > 100000:
                    out = out[:100000] + "\n…（输出已截断）"
                return self._json(200, {"ok": True, "exit": rc, "output": out})

            return self._json(404, {"ok": False, "error": "404 Not Found"})

    return Handler


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def cmd_serve(args):
    path_map = parse_path_map(args.path_map or os.environ.get("PATH_MAP", ""))
    seed = None
    if args.inventory and os.path.isfile(args.inventory):
        seed = load_json(args.inventory)

    # 备注文件：没显式给就按容器 / 宿主机惯例挑一个；显式给空字符串 = 关闭编辑
    notes = getattr(args, "notes", None)
    if notes is None:
        notes = os.environ.get("NOTES_FILE")
    if notes is None:
        notes = "/data/notes.json" if os.path.isdir("/data") else \
            os.path.join(os.getcwd(), "notes.json")
    store = NoteStore(notes) if notes else None

    allow_actions = bool(getattr(args, "allow_actions", False)) or _truthy(os.environ.get("ALLOW_ACTIONS"))
    allow_exec = bool(getattr(args, "allow_exec", False)) or _truthy(os.environ.get("ALLOW_EXEC"))

    state = State(seed, store=store, path_map=path_map)
    interval = int(args.refresh or os.environ.get("REFRESH_SECONDS") or 300)
    threading.Thread(target=collector_loop, args=(state, interval, path_map),
                     daemon=True).start()

    port = int(args.port or os.environ.get("PORT") or 8080)
    bind = args.bind or os.environ.get("BIND") or "0.0.0.0"
    auth = (args.basic_auth if getattr(args, "basic_auth", None) is not None
            else os.environ.get("BASIC_AUTH", "")) or None

    httpd = ThreadingHTTPServer((bind, port), make_handler(
        state, auth, store=store, path_map=path_map,
        allow_actions=allow_actions, allow_exec=allow_exec))
    httpd.daemon_threads = True

    print("docker-notes 已启动: http://%s:%d  |  采集 %ds  |  认证 %s  |  备注 %s  |  启停 %s  |  终端 %s"
          % (bind, port, interval, "开" if auth else "关",
             notes or "关闭", "开" if allow_actions else "关",
             "开" if allow_exec else "关"), flush=True)
    if (allow_actions or allow_exec) and not auth:
        print("警告：ALLOW_ACTIONS / ALLOW_EXEC 已开启但没设 BASIC_AUTH，"
              "这两个接口会被拒绝（避免整网段都能停容器）。", flush=True)
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
    s.add_argument("--notes", default=None,
                   help="界面备注的 JSON 路径（默认 /data/notes.json 或 ./notes.json；传空串关闭编辑）")
    s.add_argument("--allow-actions", action="store_true",
                   help="允许在界面启停容器（需同时设置 BASIC_AUTH）")
    s.add_argument("--allow-exec", action="store_true",
                   help="允许在界面执行单条命令（需同时设置 BASIC_AUTH）")
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
