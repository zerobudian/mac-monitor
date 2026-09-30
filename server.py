#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mac-monitor —— 本机硬件实时监控面板（macOS，纯 Python 标准库，零依赖）

指标：CPU 占用率 / GPU 占用率 / 平均负载(1·5·15 分钟) / 风扇转速与负载 / 内存占用
启动：python3 server.py        （自动打开桌面常驻液态玻璃浮窗）
可选参数：--port N    指定端口（默认 8787，占用时自动 +1）
          --no-open   启动后不自动打开浏览器
          --browser   使用普通浏览器页面，不启动桌面浮窗
"""

import argparse
import csv
import ctypes
import io
import json
import os
import platform
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
INTERVAL = 1.0          # 采样间隔（秒）
HISTORY_MAX = 1800      # 内存保留的历史点数（30 分钟）

SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", SOURCE_DIR)
WEB_DIR = os.path.join(BUNDLE_DIR, "web")


# --------------------------------------------------------------------------- #
#  CPU：mach host_statistics64 HOST_CPU_LOAD_INFO 差值
# --------------------------------------------------------------------------- #
class CPUSampler:
    HOST_CPU_LOAD_INFO = 3

    class _Info(ctypes.Structure):
        _fields_ = [("cpu_ticks", ctypes.c_uint32 * 4)]  # user, system, idle, nice

    def __init__(self):
        libc = ctypes.CDLL(None)
        self._mach_host_self = libc.mach_host_self
        self._mach_host_self.restype = ctypes.c_uint32
        self._host_statistics64 = libc.host_statistics64
        self._host_statistics64.argtypes = [
            ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
        ]
        self._prev = None

    def usage(self):
        buf = self._Info()
        n = ctypes.c_uint32(ctypes.sizeof(buf))
        kr = self._host_statistics64(
            self._mach_host_self(), self.HOST_CPU_LOAD_INFO,
            ctypes.byref(buf), ctypes.byref(n)
        )
        if kr != 0:
            return None
        cur = list(buf.cpu_ticks)
        if self._prev is None:
            self._prev = cur
            return None
        # u32 计数器可能回绕，按无符号差值处理
        d = [(b - a) % (1 << 32) for a, b in zip(self._prev, cur)]
        self._prev = cur
        total = sum(d)
        if total <= 0:
            return None
        busy = (d[0] + d[1] + d[3]) * 100.0 / total
        return round(busy, 1)


# --------------------------------------------------------------------------- #
#  GPU：ioreg IOAccelerator 的 "Device Utilization %"
# --------------------------------------------------------------------------- #
class GPUSampler:
    _RE = re.compile(r'"Device Utilization %"\s*=\s*(\d+)')

    def __init__(self):
        self._lock = threading.Lock()
        self._fail_streak = 0

    def utilization(self):
        try:
            out = subprocess.run(
                ["ioreg", "-r", "-c", "IOAccelerator", "-d", "1"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            out = ""
        vals = [int(m) for m in self._RE.findall(out)]
        if not vals:
            return None
        return max(0, min(100, max(vals)))


# --------------------------------------------------------------------------- #
#  内存：vm_stat + hw.memsize
# --------------------------------------------------------------------------- #
class MemSampler:
    def __init__(self):
        try:
            self.total = int(subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip())
        except Exception:
            self.total = 0

    def usage(self):
        if not self.total:
            return None
        try:
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return None
        m = re.search(r"page size of (\d+)", out)
        ps = int(m.group(1)) if m else 4096

        def pages(label):
            mm = re.search(re.escape(label) + r":\s+(\d+)", out)
            return int(mm.group(1)) if mm else 0

        used_pages = (pages("Pages active") + pages("Pages wired down")
                      + pages("Pages occupied by compressor"))
        used = used_pages * ps
        return {
            "pct": round(used * 100.0 / self.total, 1),
            "used_gb": round(used / 1e9, 1),
            "total_gb": round(self.total / 1e9, 1),
        }


# --------------------------------------------------------------------------- #
#  风扇：AppleSMC 用户客户端直读（无需 root）
#  结构体布局取自 Apple 官方 PowerManagement 源码（pmconfigd/PrivateLib.c），
#  关键：pLimitData 全 u32、keyInfo.dataSize 为 IOByteCount（x86_64 用户态 = u32），
#  整体必须为 80 字节，否则内核返回 kIOReturnBadArgument。
# --------------------------------------------------------------------------- #
class SMCVersion(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_uint8), ("minor", ctypes.c_uint8),
        ("build", ctypes.c_uint8), ("reserved", ctypes.c_uint8),
        ("release", ctypes.c_uint16),
    ]


class SMCPLimitData(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint16), ("length", ctypes.c_uint16),
        ("cpuPLimit", ctypes.c_uint32), ("gpuPLimit", ctypes.c_uint32),
        ("memPLimit", ctypes.c_uint32),
    ]


# IOByteCount 的宽度随架构变化（见 IOTypes.h 的条件编译）：
# x86_64 用户态 = 4 字节（结构体共 80 字节，本机已实测）；arm64 = 8 字节（结构体共 88 字节，待真机验证）
IO_BYTE_COUNT = ctypes.c_uint64 if platform.machine() == "arm64" else ctypes.c_uint32


class SMCKeyInfoData(ctypes.Structure):
    _fields_ = [
        ("dataSize", IO_BYTE_COUNT), ("dataType", ctypes.c_uint32),
        ("dataAttributes", ctypes.c_uint8),
    ]


class SMCParamStruct(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_uint32), ("vers", SMCVersion),
        ("pLimitData", SMCPLimitData), ("keyInfo", SMCKeyInfoData),
        ("result", ctypes.c_uint8), ("status", ctypes.c_uint8),
        ("data8", ctypes.c_uint8), ("data32", ctypes.c_uint32),
        ("bytes", ctypes.c_uint8 * 32),
    ]


class FanSampler:
    SELECTOR_HANDLE_YPCEVENT = 2
    CMD_READ_KEYINFO = 9
    CMD_READ_BYTES = 5

    def __init__(self):
        self._io = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        self._libc = ctypes.CDLL(None)
        io = self._io
        io.IOServiceMatching.argtypes = [ctypes.c_char_p]
        io.IOServiceMatching.restype = ctypes.c_void_p
        io.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        io.IOServiceGetMatchingService.restype = ctypes.c_uint32
        io.IOServiceOpen.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        io.IOConnectCallStructMethod.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
        ]
        io.IOServiceClose.argtypes = [ctypes.c_uint32]
        io.IOObjectRelease.argtypes = [ctypes.c_uint32]
        self._task = ctypes.c_uint32.in_dll(self._libc, "mach_task_self_").value
        self._svc = 0
        self._conn = ctypes.c_uint32(0)
        self._count = None          # 风扇数量
        self._limits = []           # 每个风扇的 (min, max)
        self.note = None            # 不可用原因（中文）
        self._open()

    # ---- 底层 ----
    def _open(self):
        try:
            if self._svc:
                self._io.IOObjectRelease(self._svc)
                self._svc = 0
            self._svc = self._io.IOServiceGetMatchingService(
                0, self._io.IOServiceMatching(b"AppleSMC"))
            if not self._svc:
                self.note = "未找到 AppleSMC 服务"
                return
            kr = self._io.IOServiceOpen(self._svc, self._task, 0, ctypes.byref(self._conn))
            if kr != 0:
                self.note = f"无法打开 AppleSMC（0x{kr & 0xffffffff:08x}）"
                return
            # 初始化：读取风扇数量与每个风扇的转速范围
            count = self._read_decoded("FNum")
            if count is None:
                self.note = "SMC 未返回风扇信息"
                return
            self._count = int(count)
            self._limits = []
            for i in range(self._count):
                lo = self._read_decoded(f"F{i}Mn")
                hi = self._read_decoded(f"F{i}Mx")
                self._limits.append((float(lo) if lo is not None else 0.0,
                                     float(hi) if hi is not None else 0.0))
            self.note = None
        except Exception as exc:  # pragma: no cover
            self.note = f"SMC 初始化失败：{exc}"

    def _call(self, inp):
        out = SMCParamStruct()
        outsz = ctypes.c_size_t(ctypes.sizeof(out))
        kr = self._io.IOConnectCallStructMethod(
            self._conn.value, self.SELECTOR_HANDLE_YPCEVENT,
            ctypes.byref(inp), ctypes.sizeof(inp),
            ctypes.byref(out), ctypes.byref(outsz),
        )
        if kr != 0:
            raise OSError(f"IOConnectCallStructMethod 失败 0x{kr & 0xffffffff:08x}")
        return out

    @staticmethod
    def _fourcc(s):
        return int.from_bytes(s.encode("ascii"), "big")

    @staticmethod
    def _decode(dtype, data):
        if dtype == b"flt ":                       # 32 位浮点（小端）
            return struct.unpack("<f", data[:4])[0]
        if dtype == b"ui8 ":
            return data[0]
        if dtype == b"ui16":
            return struct.unpack(">H", data[:2])[0]
        if dtype == b"ui32":
            return struct.unpack(">I", data[:4])[0]
        if dtype == b"fpe2":                       # 14.2 定点
            return struct.unpack(">H", data[:2])[0] / 4.0
        if dtype == b"sp78":                       # 7.8 定点
            return struct.unpack(">h", data[:2])[0] / 256.0
        return None

    def _read_key(self, key):
        a = SMCParamStruct()
        a.key = self._fourcc(key)
        a.data8 = self.CMD_READ_KEYINFO
        out = self._call(a)
        if out.result != 0:
            return None
        ds = out.keyInfo.dataSize
        if ds == 0 or ds > 32:
            return None
        b = SMCParamStruct()
        b.key = self._fourcc(key)
        b.data8 = self.CMD_READ_BYTES
        b.keyInfo.dataSize = ds
        out2 = self._call(b)
        if out2.result != 0:
            return None
        # dataType 只由 keyinfo 调用返回，读取调用不回填
        return struct.pack(">I", out.keyInfo.dataType), bytes(out2.bytes[:ds])

    def _read_decoded(self, key):
        got = self._read_key(key)
        if not got:
            return None
        return self._decode(got[0], got[1])

    # ---- 供采样线程调用 ----
    def sample(self):
        """返回风扇列表 [{rpm, min, max, load_pct}]，不可用时返回 []。"""
        if self._count is None:
            self._open()
            if self._count is None:
                return []
        fans = []
        try:
            for i in range(self._count):
                rpm = self._read_decoded(f"F{i}Ac")
                if rpm is None:
                    raise OSError("读取风扇转速失败")
                lo, hi = (self._limits[i] if i < len(self._limits) else (0.0, 0.0))
                if hi > lo:
                    load = (rpm - lo) / (hi - lo) * 100.0
                else:
                    load = 0.0
                fans.append({
                    "rpm": int(round(rpm)),
                    "min": int(round(lo)),
                    "max": int(round(hi)),
                    "load_pct": round(max(0.0, min(100.0, load)), 1),
                })
        except OSError:
            # 连接失效则重开，下次采样重试
            try:
                if self._conn.value:
                    self._io.IOServiceClose(self._conn.value)
            except Exception:
                pass
            self._conn = ctypes.c_uint32(0)
            self._count = None
            self._open()
            return []
        if not fans:
            self.note = "此机型没有风扇"
        return fans

    def close(self):
        try:
            if self._conn.value:
                self._io.IOServiceClose(self._conn.value)
            if self._svc:
                self._io.IOObjectRelease(self._svc)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  应用状态：采样线程 + 历史 + 广播
# --------------------------------------------------------------------------- #
class App:
    def __init__(self):
        self.history = deque(maxlen=HISTORY_MAX)
        self.version = 0
        self.stop = False
        self.cond = threading.Condition()
        self.cpu = CPUSampler()
        self.gpu = GPUSampler()
        self.mem = MemSampler()
        self.fan = None               # 懒加载
        self.meta = None

    # ---- 采样 ----
    def take_sample(self):
        if self.fan is None:
            self.fan = FanSampler()
        fans = self.fan.sample()
        sample = {
            "ts": round(time.time(), 3),
            "cpu": self.cpu.usage(),
            "gpu": self.gpu.utilization(),
            "load": [round(x, 2) for x in os.getloadavg()],
            "mem": self.mem.usage(),
            "fans": fans,
            "fan_note": self.fan.note,
        }
        with self.cond:
            self.history.append(sample)
            self.version += 1
            self.cond.notify_all()
        return sample

    def build_meta(self, first_sample):
        try:
            cpu_model = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception:
            cpu_model = platform.processor() or "未知 CPU"
        self.meta = {
            "hostname": socket.gethostname(),
            "cpu_model": cpu_model,
            "os": f"macOS {platform.mac_ver()[0]}",
            "cores": os.cpu_count(),
            "interval": INTERVAL,
            "gpu_available": first_sample["gpu"] is not None,
            "fan_count": len(first_sample["fans"]),
            "fan_note": first_sample["fan_note"],
            "mem_total_gb": self.mem.total / 1e9 if self.mem.total else None,
        }

    def run(self):
        # 首次采样（CPU 需要一帧差值，多取一次）
        self.take_sample()
        first = self.take_sample()
        self.build_meta(first)
        while not self.stop:
            t0 = time.time()
            try:
                self.take_sample()
            except Exception as exc:  # pragma: no cover
                print(f"[采样错误] {exc}", file=sys.stderr)
            dt = time.time() - t0
            time.sleep(max(0.05, INTERVAL - dt))

    def latest(self):
        with self.cond:
            return self.history[-1] if self.history else None


# --------------------------------------------------------------------------- #
#  HTTP：仅监听 127.0.0.1，本地页面无需登录
# --------------------------------------------------------------------------- #
class MonitorHTTPServer(ThreadingHTTPServer):
    """忽略浏览器刷新或退出时常见的连接重置，保持终端输出干净。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "MacMonitor"
    sys_version = ""

    # ---- 基础工具 ----
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    @property
    def app(self):
        return self.server.app

    # ---- GET ----
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path

        if path == "/":
            self._send_file("index.html")
            return
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
                self._send(404, "not found")
                return
            self._send_file(name)
            return
        if path.startswith("/api/"):
            self._api(path, parse_qs(url.query))
            return
        self._send(404, "not found")

    def _send_file(self, name):
        full = os.path.join(WEB_DIR, os.path.basename(name))
        ext = os.path.splitext(full)[1].lower()
        types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".png": "image/png", ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self._send(404, "not found")
            return
        self._send(200, data, types.get(ext, "application/octet-stream"),
                   extra=[("Cache-Control", "no-store")])

    # ---- API ----
    def _api(self, path, qs):
        if path == "/api/meta":
            meta = self.app.meta or {}
            self._send(200, json.dumps(meta, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        if path == "/api/history":
            try:
                n = max(1, min(HISTORY_MAX, int(qs.get("n", ["300"])[0])))
            except ValueError:
                n = 300
            with self.app.cond:
                rows = list(self.app.history)[-n:]
            self._send(200, json.dumps(rows, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        if path == "/api/export":
            self._export_csv()
            return
        if path == "/api/stream":
            self._stream()
            return
        self._send(404, '{"error": "not found"}',
                   "application/json; charset=utf-8")

    def _export_csv(self):
        """导出当前内存中的全部采样历史，UTF-8 BOM 便于 Excel 直接打开。"""
        with self.app.cond:
            rows = list(self.app.history)
        fan_count = max((len(row.get("fans") or []) for row in rows), default=0)
        header = [
            "时间", "Unix 时间戳", "CPU (%)", "GPU (%)",
            "1 分钟负载", "5 分钟负载", "15 分钟负载",
            "内存 (%)", "已用内存 (GB)", "总内存 (GB)",
        ]
        for i in range(fan_count):
            header.extend([f"风扇 {i + 1} (RPM)", f"风扇 {i + 1} 负载 (%)"])

        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(header)
        for row in rows:
            ts = row.get("ts")
            load = ((row.get("load") or []) + [None, None, None])[:3]
            mem = row.get("mem") or {}
            values = [
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "",
                ts, row.get("cpu"), row.get("gpu"),
                *load, mem.get("pct"), mem.get("used_gb"), mem.get("total_gb"),
            ]
            fans = row.get("fans") or []
            for i in range(fan_count):
                fan = fans[i] if i < len(fans) else {}
                values.extend([fan.get("rpm"), fan.get("load_pct")])
            writer.writerow(["" if value is None else value for value in values])

        data = ("\ufeff" + out.getvalue()).encode("utf-8")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._send(200, data, "text/csv; charset=utf-8", extra=[
            ("Content-Disposition", f'attachment; filename="mac-monitor-{stamp}.csv"'),
            ("Cache-Control", "no-store"),
        ])

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        app = self.app
        last = app.version  # 等下一个新样本，避免推送尚未就绪的 null
        try:
            while not app.stop:
                with app.cond:
                    app.cond.wait_for(
                        lambda: app.version != last or app.stop, timeout=15)
                if app.stop:
                    break
                if app.version != last:
                    last = app.version
                    payload = json.dumps(app.latest(), ensure_ascii=False)
                    chunk = f"id: {last}\ndata: {payload}\n\n"
                else:
                    chunk = ": ping\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

# --------------------------------------------------------------------------- #
#  启动
# --------------------------------------------------------------------------- #
def pick_port(base):
    for port in range(base, base + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    return None


def launch_desktop(url):
    """启动随应用分发的原生外壳；源码运行时按需编译并缓存。"""
    bundled_binary = os.path.join(BUNDLE_DIR, "mac-monitor-desktop")
    if os.path.isfile(bundled_binary):
        binary = bundled_binary
        source = None
    else:
        source = os.path.join(SOURCE_DIR, "desktop.swift")
        binary = None

    compiler = "/usr/bin/swiftc"
    if binary is None:
        if not os.path.isfile(source) or not os.path.exists(compiler):
            return None
        runtime_dir = os.path.join(SOURCE_DIR, ".runtime")
        binary = os.path.join(runtime_dir, "mac-monitor-desktop")
        os.makedirs(runtime_dir, exist_ok=True)
        needs_build = (
            not os.path.isfile(binary)
            or os.path.getmtime(binary) < os.path.getmtime(source)
        )
        if needs_build:
            print("正在准备桌面浮窗（首次启动约需十几秒）…")
            result = subprocess.run(
                [
                    compiler, "-swift-version", "5",
                    "-framework", "Cocoa", "-framework", "WebKit",
                    source, "-o", binary,
                ],
                capture_output=True, text=True, timeout=90,
            )
            if result.returncode != 0:
                detail = result.stderr.strip().splitlines()
                if detail:
                    print(f"桌面浮窗编译失败：{detail[-1]}", file=sys.stderr)
                return None

    desktop_url = f"{url}/?desktop=1&native=1"
    try:
        return subprocess.Popen(
            [binary, desktop_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"桌面浮窗启动失败：{exc}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser(description="本机硬件实时监控面板")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-open", action="store_true", help="不自动打开任何界面")
    ap.add_argument("--browser", action="store_true", help="使用普通浏览器而非桌面浮窗")
    args = ap.parse_args()

    app = App()

    port = pick_port(args.port)
    if port is None:
        print(f"错误：端口 {args.port}~{args.port + 19} 均被占用。", file=sys.stderr)
        sys.exit(1)

    server = MonitorHTTPServer((HOST, port), Handler)
    server.app = app

    # 后台采样线程
    sampler = threading.Thread(target=app.run, daemon=True, name="sampler")
    sampler.start()

    # 等首帧元数据
    for _ in range(50):
        if app.meta:
            break
        time.sleep(0.1)

    url = f"http://{HOST}:{port}"
    m = app.meta or {}
    print()
    print("┌─────────────────────────────────────────────┐")
    print("│   本机监控面板已启动                         │")
    print(f"│   地址: {url:<39}│")
    gpu_ok = "✓" if m.get("gpu_available") else "✗ 不可用"
    fan_n = m.get("fan_count")
    fan_txt = f"✓ {fan_n} 个" if fan_n else (m.get("fan_note") or "✗ 不可用")
    print(f"│   GPU 占用: {gpu_ok:<29}│")
    print(f"│   风扇: {fan_txt:<34}│")
    print("│   Ctrl+C 停止                               │")
    print("└─────────────────────────────────────────────┘")
    print()

    desktop_process = None
    if not args.no_open:
        if platform.system() == "Darwin" and not args.browser:
            desktop_process = launch_desktop(url)
        if desktop_process is None:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    if desktop_process is not None:
        def stop_when_desktop_closes():
            desktop_process.wait()
            if not app.stop:
                server.shutdown()

        threading.Thread(
            target=stop_when_desktop_closes,
            daemon=True,
            name="desktop-watcher",
        ).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        app.stop = True
        with app.cond:
            app.cond.notify_all()
        if app.fan:
            app.fan.close()
        if desktop_process is not None and desktop_process.poll() is None:
            desktop_process.terminate()
        server.server_close()


if __name__ == "__main__":
    main()
