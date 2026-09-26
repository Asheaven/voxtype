# -*- coding: utf-8 -*-
"""
VoxType（无窗口版）
===================
按一下 F10 开始/结束 -> 豆包流式语音识别 2.0（WebSocket）-> 文字自动填入当前输入框。

无黑窗口：用 pythonw.exe 运行；屏幕右下角有一个小悬浮窗提示状态（录音中/识别文本/错误）。
右键点击悬浮窗可退出程序。

用法：
  python voice_input.py           正常启动（悬浮窗）
  python voice_input.py --selftest 自测：录 5 秒并打印识别结果（在 cmd 里运行）
"""
import json
import math
import os
import queue
import struct
import sys
import threading
import time
import uuid

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_NAME = "VoxType"
LEGACY_APP_NAMES = ("ChatGPTVoiceInput",)
DEFAULT_CONFIG = {
    "api_key": "",
    "resource_id": "volc.seedasr.sauc.duration",
    "ws_url": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
    "hotkey": "f10",
    "auto_send": False,
    "max_wait_seconds": None,
    "appearance": {
        "accent_color": "#B9C0C8",
    },
    "audio": {
        "rate": 16000,
        "chunk_ms": 200,
        "device_index": None,
        "device_name": "realtek",
    },
}


def _data_dir():
    override = os.environ.get("VOX_TYPE_DATA_DIR")
    if override:
        path = os.path.abspath(override)
    else:
        if os.name == "nt":
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
        else:
            base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
                os.path.expanduser("~"), ".config"
            )
        path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


DATA_DIR = _data_dir()
CONFIG_PATH = os.environ.get("VOX_TYPE_CONFIG") or os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.environ.get("VOX_TYPE_LOG") or os.path.join(DATA_DIR, "voice_input.log")


def _legacy_config_paths():
    paths = [os.path.join(BASE_DIR, "config.json")]
    if getattr(sys, "frozen", False):
        paths.append(os.path.join(os.path.dirname(BASE_DIR), "config.json"))

    if os.name == "nt":
        legacy_base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        legacy_base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
    for app_name in LEGACY_APP_NAMES:
        paths.append(os.path.join(legacy_base, app_name, "config.json"))

    result = []
    for path in paths:
        path = os.path.abspath(path)
        if path != os.path.abspath(CONFIG_PATH) and path not in result:
            result.append(path)
    return result


def normalize_config(raw):
    raw = raw or {}
    cfg = {}
    for key, value in DEFAULT_CONFIG.items():
        cfg[key] = value.copy() if isinstance(value, dict) else value
    for key, value in raw.items():
        if key in ("audio", "appearance") and isinstance(value, dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def _read_config_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return normalize_config(json.load(f))


def save_config(cfg):
    cfg = normalize_config(cfg)
    os.makedirs(os.path.dirname(os.path.abspath(CONFIG_PATH)), exist_ok=True)
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, CONFIG_PATH)


def load_config():
    if os.path.exists(CONFIG_PATH):
        return _read_config_file(CONFIG_PATH)

    # 兼容旧版本：首次升级时自动迁移原目录中的配置，但不会修改旧文件。
    for legacy_path in _legacy_config_paths():
        if not os.path.exists(legacy_path):
            continue
        try:
            cfg = _read_config_file(legacy_path)
        except Exception:
            continue
        if (cfg.get("api_key") or "").strip():
            save_config(cfg)
            return cfg
    return None


def needs_setup(cfg):
    if not cfg:
        return True
    return not (
        (cfg.get("api_key") or "").strip()
        and (cfg.get("resource_id") or "").strip()
        and (cfg.get("ws_url") or "").strip()
    )


def _parse_hex_color(value):
    try:
        value = str(value).strip().lstrip("#")
        if len(value) == 3:
            value = "".join(ch * 2 for ch in value)
        if len(value) != 6:
            return None
        return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))
    except (TypeError, ValueError):
        return None


def _blend_color(base, accent, amount):
    base_rgb = _parse_hex_color(base) or (0, 0, 0)
    accent_rgb = _parse_hex_color(accent) or base_rgb
    amount = max(0.0, min(1.0, amount))
    rgb = tuple(
        int(round(base_rgb[index] * (1.0 - amount) + accent_rgb[index] * amount))
        for index in range(3)
    )
    return "#%02X%02X%02X" % rgb


def _visible_accent(value):
    rgb = _parse_hex_color(value)
    if not rgb:
        return DEFAULT_CONFIG["appearance"]["accent_color"]
    if max(rgb) >= 96:
        return "#%02X%02X%02X" % rgb
    lifted = tuple(int(round(channel + (255 - channel) * 0.45)) for channel in rgb)
    return "#%02X%02X%02X" % lifted


def _display_refresh_rate():
    """读取当前主显示器刷新率；读取失败时按 60Hz 处理。"""
    if os.name != "nt":
        return 60
    try:
        import ctypes
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetDC.argtypes = [ctypes.c_void_p]
        user32.GetDC.restype = ctypes.c_void_p
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.ReleaseDC.restype = ctypes.c_int
        gdi32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
        gdi32.GetDeviceCaps.restype = ctypes.c_int

        hdc = user32.GetDC(None)
        if not hdc:
            return 60
        try:
            hz = int(gdi32.GetDeviceCaps(hdc, 116))
        finally:
            user32.ReleaseDC(None, hdc)
        if 30 <= hz <= 480:
            return hz
    except Exception:
        pass
    return 60


def _begin_high_resolution_timer():
    if os.name != "nt":
        return False
    try:
        import ctypes
        return ctypes.windll.winmm.timeBeginPeriod(1) == 0
    except Exception:
        return False


def _end_high_resolution_timer():
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass


def configure_api(initial=None, parent=None):
    """首次运行或重新配置 API；密钥只保存在本机用户目录。"""
    import tkinter as tk
    from tkinter import messagebox

    bg = "#050506"
    surface = "#0D0E10"
    border = "#25272B"
    text = "#F2F3F4"
    muted = "#85888D"
    accent = "#E6E7E9"
    accent_text = "#080808"

    base = normalize_config(initial)
    is_child = parent is not None
    win = tk.Toplevel(parent) if is_child else tk.Tk()
    win.title("VoxType Setup")
    win.configure(bg=bg)
    win.resizable(False, False)
    if is_child:
        win.transient(parent)
        win.grab_set()
    else:
        win.attributes("-topmost", True)

    body = tk.Frame(win, bg=bg, padx=24, pady=22)
    body.pack(fill="both", expand=True)
    tk.Label(
        body,
        text="VoxType",
        bg=bg,
        fg=text,
        font=("Segoe UI", 16, "bold"),
        anchor="w",
    ).grid(row=0, column=0, sticky="ew")
    tk.Label(
        body,
        text="Connect your speech recognition service.",
        bg=bg,
        fg=muted,
        font=("Segoe UI", 9),
        anchor="w",
    ).grid(row=1, column=0, sticky="ew", pady=(4, 18))

    api_var = tk.StringVar(value=base.get("api_key", ""))
    resource_var = tk.StringVar(value=base.get("resource_id", ""))
    url_var = tk.StringVar(value=base.get("ws_url", ""))

    fields = [
        ("API Key", api_var, True),
        ("Resource ID", resource_var, False),
        ("WebSocket URL", url_var, False),
    ]
    for row, (label, var, secret) in enumerate(fields, start=2):
        field = tk.Frame(body, bg=bg)
        field.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        tk.Label(
            field,
            text=label,
            bg=bg,
            fg=muted,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(0, 5))
        entry = tk.Entry(
            field,
            textvariable=var,
            width=52,
            show="*" if secret else "",
            bg=surface,
            fg=text,
            insertbackground=text,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor="#55595F",
            font=("Segoe UI", 10),
        )
        entry.pack(fill="x", ipady=7)

    tk.Label(
        body,
        text="Stored locally only. Never written to source, logs, or builds.",
        bg=bg,
        fg=muted,
        font=("Segoe UI", 8),
        anchor="w",
    ).grid(row=5, column=0, sticky="ew", pady=(2, 4))

    result = {"cfg": None}

    def submit():
        api_key = api_var.get().strip()
        resource_id = resource_var.get().strip() or DEFAULT_CONFIG["resource_id"]
        ws_url = url_var.get().strip() or DEFAULT_CONFIG["ws_url"]
        if not api_key:
            messagebox.showerror("Setup incomplete", "Please enter an API key.", parent=win)
            return
        cfg = normalize_config(base)
        cfg.update({
            "api_key": api_key,
            "resource_id": resource_id,
            "ws_url": ws_url,
        })
        save_config(cfg)
        result["cfg"] = cfg
        win.destroy()

    def cancel():
        win.destroy()

    buttons = tk.Frame(body, bg=bg)
    buttons.grid(row=6, column=0, sticky="e", pady=(16, 0))
    tk.Button(
        buttons,
        text="Save",
        width=10,
        command=submit,
        bg=accent,
        fg=accent_text,
        activebackground="#FFFFFF",
        activeforeground=accent_text,
        relief="flat",
        bd=0,
        padx=12,
        pady=7,
        cursor="hand2",
        font=("Segoe UI", 9, "bold"),
    ).pack(side="right")
    tk.Button(
        buttons,
        text="Cancel",
        width=10,
        command=cancel,
        bg=surface,
        fg=text,
        activebackground="#202226",
        activeforeground=text,
        relief="flat",
        bd=0,
        padx=12,
        pady=7,
        cursor="hand2",
        font=("Segoe UI", 9),
    ).pack(side="right", padx=(0, 8))

    win.protocol("WM_DELETE_WINDOW", cancel)
    win.update_idletasks()
    if is_child:
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - win.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - win.winfo_height()) // 2
            win.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:
            pass
        win.wait_window()
    else:
        x = (win.winfo_screenwidth() - win.winfo_width()) // 2
        y = (win.winfo_screenheight() - win.winfo_height()) // 2
        win.geometry("+%d+%d" % (max(0, x), max(0, y)))
        win.mainloop()
    return result["cfg"]


def log(msg):
    """写入运行日志（无窗口模式下排错用），不记录识别文本。"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def safe_print(*args, **kwargs):
    """无控制台（pythonw）环境下 print 不报错。"""
    try:
        print(*args, **kwargs)
    except Exception:
        pass

# ---------------- 二进制协议帧 ----------------
def build_frame(msg_type, flags, payload=b"", serialization=0, compression=0):
    header = bytearray(4)
    header[0] = (0b0001 << 4) | 0b0001          # version=1, header size=1(即4字节)
    header[1] = (msg_type << 4) | (flags & 0x0F)
    header[2] = (serialization << 4) | compression
    header[3] = 0
    return bytes(header) + struct.pack(">I", len(payload)) + payload


def parse_frame(data):
    if not data or len(data) < 4:
        return None
    mt = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    if mt == 0b1001:  # full server response
        if len(data) < 12:
            return (mt, flags, b"")
        size = struct.unpack(">I", data[8:12])[0]
        return (mt, flags, data[12:12 + size])
    if mt == 0b1111:  # error message
        if len(data) < 12:
            return (mt, flags, b"")
        size = struct.unpack(">I", data[8:12])[0]
        return (mt, flags, data[12:12 + size])
    return (mt, flags, data[4:])


def select_input_device(cfg):
    """
    选择录音用的麦克风设备（返回设备编号或 None=使用系统默认）：
    1. 优先 config 里指定的 device_index；
    2. 否则按 audio.device_name 关键词匹配输入设备（默认 'realtek'，固定用内置麦，
       自动排除名称含"外部/外置/耳机/headphone/external"的设备，避免插耳机后切换）；
    3. 都没有则用系统默认输入设备。
    """
    import sounddevice as sd
    audio = cfg["audio"]
    idx = audio.get("device_index")
    if idx is not None:
        try:
            info = sd.query_devices(idx)
            if info["max_input_channels"] > 0:
                return idx
        except Exception:
            pass
    name_kw = (audio.get("device_name") or "").lower().strip()
    try:
        devs = sd.query_devices()
    except Exception:
        return None
    for i, d in enumerate(devs):
        if d["max_input_channels"] <= 0:
            continue
        n = (d["name"] or "").lower()
        if name_kw and name_kw in n:
            if any(x in n for x in ("外部", "外置", "耳机", "headphone", "external")):
                continue
            return i
    return None


# ---------------- 流式识别会话 ----------------
class StreamAsrSession:
    def __init__(self, cfg, stop_event=None, audio_queue=None):
        self.cfg = cfg
        self.stop_event = stop_event
        self.audio_queue = audio_queue
        self.state = {
            "text": "",
            "final_received": False,
            "error": None,
            "stop_recording": False,
            "sent_final": False,
            "ws_error": None,
        }
        self.ws = None

    def _stop_requested(self):
        return self.state["stop_recording"] or (
            self.stop_event is not None and self.stop_event.is_set()
        )

    def connect(self):
        from websocket import create_connection
        headers = {
            "X-Api-Key": self.cfg["api_key"],
            "X-Api-Resource-Id": self.cfg["resource_id"],
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        }
        self.ws = create_connection(
            self.cfg["ws_url"],
            header=["%s: %s" % (k, v) for k, v in headers.items()],
            timeout=15,
        )
        req = {
            "user": {"uid": "chatgpt-voice-input"},
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": self.cfg["audio"]["rate"],
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": True,
            },
        }
        payload = json.dumps(req, ensure_ascii=False).encode("utf-8")
        self.ws.send_binary(build_frame(0b0001, 0b0000, payload, serialization=0b0001))

    def _do_connect(self, ready):
        """后台建立 WebSocket 连接，结果用 ready 事件通知录音线程"""
        try:
            self.connect()
        except Exception as e:
            self.state["ws_error"] = "Connection failed: %s" % e
            self.state["stop_recording"] = True
            self.state["final_received"] = True
        finally:
            ready.set()

    def _recv_loop(self):
        from websocket import WebSocketTimeoutException
        # 等待连接建立（并行连接时 ws 需要时间就绪）
        while self.ws is None and not self._stop_requested():
            time.sleep(0.02)
        if self.ws is None:
            return
        while not self._stop_requested() or not self.state["final_received"]:
            try:
                self.ws.settimeout(0.5)
                data = self.ws.recv()
            except WebSocketTimeoutException:
                continue
            except Exception as e:
                self.state["ws_error"] = "Connection lost: %s" % e
                self.state["final_received"] = True
                return
            parsed = parse_frame(data)
            if not parsed:
                continue
            mt, flags, payload = parsed
            if mt == 0b1001:
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                res = obj.get("result") or {}
                text = res.get("text", "")
                if text:
                    self.state["text"] = text
                if flags == 0b0011:
                    self.state["final_received"] = True
            elif mt == 0b1111:
                self.state["error"] = "Server error: %s" % payload.decode("utf-8", "ignore")
                self.state["final_received"] = True

    def _record_loop(self, ready):
        import sounddevice as sd
        import numpy as np
        import collections
        rate = self.cfg["audio"]["rate"]
        chunk = rate * self.cfg["audio"]["chunk_ms"] // 1000
        stream = None
        if self.audio_queue is None:
            dev = select_input_device(self.cfg)
            try:
                stream = sd.InputStream(
                    samplerate=rate, channels=1, dtype="int16",
                    blocksize=chunk, device=dev,
                )
                stream.start()
            except Exception as e:
                detail = "Microphone unavailable"
                try:
                    cur = sd.query_devices(dev)
                    detail += " (device=%s)" % (cur["name"] if cur else "default")
                except Exception:
                    pass
                detail += ": %s" % e
                log(detail)
                self.state["ws_error"] = detail
                self.state["final_received"] = True
                self.state["stop_recording"] = True
                return
        # 自动增益（AGC）：初始就带一定增益并快速响应，小声音开头也能识别
        self._agc_gain = 4.0
        TARGET_PEAK = 16000.0   # 目标峰值（int16 满量程 32767 的一半），留 headroom 防削波
        MAX_GAIN = 40.0         # 最大放大倍数（约 32dB）

        def agc(raw_bytes):
            arr = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
            peak = float(np.abs(arr).max())
            if peak < 1e-3:
                return raw_bytes
            target = min(TARGET_PEAK / peak, MAX_GAIN)
            if target > self._agc_gain:
                # 增益快速上升（每块翻倍），约 0.8 秒内到 40 倍，避免爆音
                self._agc_gain = min(target, self._agc_gain * 2.0)
            else:
                # 大声音时增益直接回落，防止削波
                self._agc_gain = target
            if self._agc_gain > 1.0:
                out = np.clip(arr * self._agc_gain, -32768, 32767).astype(np.int16)
                return out.tobytes()
            return raw_bytes

        buf = collections.deque()
        flushed = False
        last = b""
        try:
            while not self._stop_requested():
                if self.audio_queue is None:
                    data, _overflowed = stream.read(chunk)
                    raw = data.tobytes()
                else:
                    try:
                        raw = self.audio_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                if not ready.is_set():
                    # 连接还没好：先缓存开头音频（最多约 1.2 秒），稍后补发
                    buf.append(raw)
                    if len(buf) > 6:
                        buf.popleft()
                    continue
                if not flushed:
                    if self.ws is None:
                        break
                    flushed = True
                    while buf:
                        b = buf.popleft()
                        last = b
                        self.ws.send_binary(build_frame(0b0010, 0b0000, agc(b)))
                last = raw
                self.ws.send_binary(build_frame(0b0010, 0b0000, agc(raw)))
        except Exception as e:
            self.state["ws_error"] = "Could not send audio: %s" % e
        finally:
            try:
                self.ws.send_binary(build_frame(0b0010, 0b0010, last))
            except Exception:
                pass
            self.state["sent_final"] = True
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

    def run(self, duration=None):
        # 并行：边建立连接边开始录音；开头音频先缓存，连接就绪后补发，避免丢开头
        ready = threading.Event()
        t_conn = threading.Thread(target=self._do_connect, args=(ready,), daemon=True)
        t_recv = threading.Thread(target=self._recv_loop, daemon=True)
        t_rec = threading.Thread(target=self._record_loop, args=(ready,), daemon=True)
        t_conn.start()
        t_recv.start()
        t_rec.start()

        started = time.monotonic()
        while not self.state["final_received"]:
            if duration is not None and time.monotonic() - started >= duration:
                self.state["stop_recording"] = True
            if self.stop_event is not None and self.stop_event.is_set():
                self.state["stop_recording"] = True
            if self.state["stop_recording"]:
                break
            time.sleep(0.05)

        # 服务端先结束本段时，也要通知发送线程停止本段。
        if self.state["final_received"] and not self.state["stop_recording"]:
            self.state["stop_recording"] = True

        wait = 0
        while not self.state["sent_final"] and wait < 3:
            time.sleep(0.1)
            wait += 0.1

        max_wait = self.cfg.get("max_wait_seconds")
        try:
            max_wait = float(max_wait) if max_wait is not None else None
        except (TypeError, ValueError):
            max_wait = None
        wait = 0
        while not self.state["final_received"]:
            if max_wait is not None and max_wait > 0 and wait >= max_wait:
                break
            time.sleep(0.05)
            wait += 0.05

        try:
            self.ws.close()
        except Exception:
            pass
        return {
            "text": self.state["text"],
            "error": self.state["error"] or self.state["ws_error"],
            "final": self.state["final_received"],
        }


class ContinuousAsrSession:
    """保持麦克风常开，跨多个 WebSocket 分段拼接识别结果。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._current = None
        self._text = ""
        self._level = 0.0
        self._audio_error = None
        chunk_ms = max(50, int(cfg["audio"]["chunk_ms"]))
        max_chunks = max(10, int(2000 / chunk_ms))
        self.audio_queue = queue.Queue(maxsize=max_chunks)

    @property
    def state(self):
        with self._lock:
            current = self._current
            text = self._text
            level = self._level
            error = self._audio_error
        if current is not None:
            live = current.state.get("text") or ""
            if live:
                text += live
        return {"text": text, "level": level, "error": error}

    def stop(self):
        self.stop_event.set()
        with self._lock:
            self._level = 0.0
            current = self._current
        if current is not None:
            current.state["stop_recording"] = True

    def _put_audio(self, raw):
        try:
            self.audio_queue.put_nowait(raw)
            return
        except queue.Full:
            pass
        try:
            self.audio_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self.audio_queue.put_nowait(raw)
        except queue.Full:
            pass

    def _audio_loop(self):
        import sounddevice as sd
        import numpy as np
        rate = self.cfg["audio"]["rate"]
        chunk = rate * self.cfg["audio"]["chunk_ms"] // 1000
        chunk_bytes = chunk * 2
        level_chunk = max(160, rate // 50)
        dev = select_input_device(self.cfg)
        stream = None
        try:
            stream = sd.InputStream(
                samplerate=rate, channels=1, dtype="int16",
                blocksize=level_chunk, device=dev,
            )
            stream.start()
        except Exception as e:
            detail = "Microphone unavailable"
            try:
                cur = sd.query_devices(dev)
                detail += " (device=%s)" % (cur["name"] if cur else "default")
            except Exception:
                pass
            detail += ": %s" % e
            with self._lock:
                self._audio_error = detail
            log(detail)
            self.stop_event.set()
            return

        pending = bytearray()
        try:
            while not self.stop_event.is_set():
                data, _overflowed = stream.read(level_chunk)
                samples = data.astype(np.float32, copy=False)
                rms = float(np.sqrt(np.mean(samples * samples)))
                if rms > 0:
                    db = 20.0 * np.log10(max(rms, 1.0) / 32768.0)
                    target = min(1.0, max(0.0, (db + 60.0) / 60.0))
                else:
                    target = 0.0
                with self._lock:
                    self._level = target
                pending.extend(data.tobytes())
                if len(pending) >= chunk_bytes:
                    batch = bytes(pending[:chunk_bytes])
                    del pending[:chunk_bytes]
                    self._put_audio(batch)
        except Exception as e:
            detail = "Microphone read failed: %s" % e
            with self._lock:
                self._audio_error = detail
            log(detail)
            self.stop_event.set()
        finally:
            if pending:
                self._put_audio(bytes(pending))
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def run(self):
        audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
        audio_thread.start()
        parts = []
        final_error = None
        quick_failures = 0
        try:
            while not self.stop_event.is_set():
                segment = StreamAsrSession(
                    self.cfg,
                    stop_event=self.stop_event,
                    audio_queue=self.audio_queue,
                )
                with self._lock:
                    self._current = segment
                started = time.monotonic()
                result = segment.run()
                elapsed = time.monotonic() - started
                with self._lock:
                    self._current = None

                text = (result["text"] or "").strip()
                if text:
                    parts.append(text)
                    with self._lock:
                        self._text = "".join(parts)
                    log("识别分段 %d，长度 %d" % (len(parts), len(text)))

                if self.stop_event.is_set():
                    break

                log("识别分段结束，自动继续")
                error = result["error"]
                if error and not text:
                    if elapsed < 1.0:
                        quick_failures += 1
                    else:
                        quick_failures = 0
                    if quick_failures >= 3:
                        final_error = error
                        break
                    time.sleep(min(0.5, 0.1 * quick_failures))
                    continue
                quick_failures = 0
                time.sleep(0.05)
        finally:
            self.stop_event.set()
            audio_thread.join(timeout=1.0)

        with self._lock:
            audio_error = self._audio_error
            text = self._text
        error = audio_error or final_error
        return {"text": text, "error": error, "final": True}


# ---------------- 应用状态与流程 ----------------
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = {"phase": "idle", "text": "", "message": ""}
        self.session = None
        self.recording = False

    def start(self):
        if self.recording:
            return
        self.recording = True
        log("F10: 开始录音")
        self.state.update(phase="listening", text="", message="")
        s = ContinuousAsrSession(self.cfg)
        self.session = s
        # 连接在后台并行建立（run 内完成），按下 F10 立即开始录音并缓存开头
        threading.Thread(target=self._run, args=(s,), daemon=True).start()

    def stop(self):
        if self.session:
            log("F10: 结束录音")
            self.session.stop()

    def _run(self, s):
        result = s.run()
        self.recording = False
        text = (result["text"] or "").strip()
        if text:
            if result["error"]:
                log("分段识别结束，保留已有文字: %s" % result["error"])
            self.state.update(phase="done", text=text, message=text)
            try:
                paste_to_focus(text)
                if self.cfg.get("auto_send"):
                    import keyboard as kb
                    kb.send("enter")
            except Exception as e:
                self.state.update(phase="error", message="Could not paste text: %s" % e)
            return
        if result["error"]:
            self.state.update(phase="error", message="%s" % result["error"])
            return
        self.state.update(phase="error", message="No speech detected")


def paste_to_focus(text):
    import pyperclip
    import keyboard
    log("开始填入，长度 %d" % len(text))
    pyperclip.copy(text)
    time.sleep(0.08)
    try:
        # 先取消可能存在的选中并移到行尾：保证只追加、绝不覆盖/删除已有文字
        keyboard.send("end")
        time.sleep(0.03)
    except Exception:
        pass
    keyboard.send("ctrl+v")
    log("填入完成，长度 %d" % len(text))


# ---------------- 悬浮窗 UI ----------------
class FloatingUI:
    WIDTH = 320
    HEIGHT = 58
    RADIUS = 29
    TRANSPARENT = "#010203"

    SURFACE_IDLE = "#08090A"
    SURFACE_LISTEN = "#0B0D10"
    SURFACE_DONE = "#0A0D0B"
    SURFACE_ERROR = "#100A0C"
    BORDER_IDLE = "#242629"
    BORDER_LISTEN = "#34383D"
    BORDER_DONE = "#26352B"
    BORDER_ERROR = "#43272D"
    METER_X = 16
    METER_Y = 51
    METER_TRACK = "#202226"
    DEFAULT_ACCENT = "#B9C0C8"
    FG = "#F4F4F4"
    MUTED = "#85888D"

    def __init__(self, app):
        import tkinter as tk
        self.tk = tk
        self.app = app
        self._theme = self._build_theme(self._accent_color())
        self._refresh_hz = _display_refresh_rate()
        self._frame_interval = 1.0 / self._refresh_hz
        self.root = tk.Tk()
        self.root.title("VoxType")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=self.TRANSPARENT)
        canvas_bg = self.TRANSPARENT
        try:
            self.root.wm_attributes("-transparentcolor", self.TRANSPARENT)
        except Exception:
            canvas_bg = self.SURFACE_IDLE
            self.root.configure(bg=canvas_bg)
        try:
            self.root.attributes("-alpha", 0.97)
        except Exception:
            pass

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(
            "%dx%d+%d+%d"
            % (self.WIDTH, self.HEIGHT, sw - self.WIDTH - 24, sh - self.HEIGHT - 64)
        )
        self.canvas = tk.Canvas(
            self.root,
            width=self.WIDTH,
            height=self.HEIGHT,
            bg=canvas_bg,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self._surface = self._rounded_rect(
            self.canvas,
            1,
            1,
            self.WIDTH - 1,
            self.HEIGHT - 1,
            self.RADIUS,
            fill=self._theme["idle"][0],
            outline=self._theme["idle"][1],
            width=1,
        )
        self._meter_track = self.canvas.create_line(
            self.METER_X,
            self.METER_Y,
            self.WIDTH - self.METER_X,
            self.METER_Y,
            fill=self._theme["meter_track"],
            width=3,
            capstyle="round",
            state="hidden",
        )
        self._meter_fill = self.canvas.create_line(
            self.METER_X,
            self.METER_Y,
            self.METER_X,
            self.METER_Y,
            fill=self._theme["meter_fill"],
            width=3,
            capstyle="round",
            state="hidden",
        )
        self._title = self.canvas.create_text(
            20,
            21,
            text="VoxType",
            anchor="w",
            fill=self.FG,
            font=("Segoe UI", 11, "bold"),
        )
        self._subtitle = self.canvas.create_text(
            20,
            38,
            text="Press F10 to start",
            anchor="w",
            fill=self.MUTED,
            font=("Segoe UI", 9),
        )

        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<Button-3>", self._context_menu)

        # 常态隐藏：平时不显示，按 F10 时自动弹出，识别完自动隐藏
        self.root.withdraw()
        self._phase_since = time.time()
        self._last_phase = None
        self._last_ui_signature = None
        self._meter_visible = False
        self._meter_target = 0.0
        self._meter_level = 0.0
        self._meter_last_time = time.perf_counter()
        self._next_frame = self._meter_last_time

    @staticmethod
    def _rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return canvas.create_polygon(
            points,
            smooth=True,
            splinesteps=36,
            **kwargs,
        )

    def _drag_start(self, e):
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry("+%d+%d" % (e.x_root - self._dx, e.y_root - self._dy))

    def _context_menu(self, e):
        menu = self.tk.Menu(
            self.root,
            tearoff=0,
            bg=self.SURFACE_IDLE,
            fg=self.FG,
            activebackground="#24262A",
            activeforeground=self.FG,
            bd=0,
            relief="flat",
            font=("Segoe UI", 9),
        )
        menu.add_command(label="Accent Color…", command=self._choose_accent)
        menu.add_command(label="Reset Accent", command=self._reset_accent)
        menu.add_separator()
        menu.add_command(label="Configure API…", command=self._reconfigure)
        menu.add_separator()
        menu.add_command(label="Quit", command=self.root.quit)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _accent_color(self):
        appearance = self.app.cfg.get("appearance") or {}
        return appearance.get("accent_color") or self.DEFAULT_ACCENT

    def _build_theme(self, accent):
        accent = _parse_hex_color(accent) or _parse_hex_color(self.DEFAULT_ACCENT)
        accent = "#%02X%02X%02X" % accent
        return {
            "idle": (
                self.SURFACE_IDLE,
                _blend_color(self.BORDER_IDLE, accent, 0.18),
            ),
            "listening": (
                _blend_color(self.SURFACE_LISTEN, accent, 0.12),
                _blend_color(self.BORDER_LISTEN, accent, 0.58),
            ),
            "done": (
                self.SURFACE_DONE,
                _blend_color(self.BORDER_DONE, accent, 0.32),
            ),
            "error": (
                self.SURFACE_ERROR,
                _blend_color(self.BORDER_ERROR, accent, 0.32),
            ),
            "meter_track": _blend_color(self.METER_TRACK, accent, 0.12),
            "meter_fill": _visible_accent(accent),
        }

    def _apply_theme(self, accent):
        self._theme = self._build_theme(accent)
        self.canvas.itemconfigure(
            self._surface,
            fill=self._theme["idle"][0],
            outline=self._theme["idle"][1],
        )
        self.canvas.itemconfigure(self._meter_track, fill=self._theme["meter_track"])
        self.canvas.itemconfigure(self._meter_fill, fill=self._theme["meter_fill"])
        self._last_ui_signature = None

    def _save_accent(self, color):
        cfg = normalize_config(self.app.cfg)
        cfg["appearance"]["accent_color"] = color.upper()
        save_config(cfg)
        self.app.cfg = cfg
        self._apply_theme(color)

    def _choose_accent(self):
        from tkinter import colorchooser
        _, color = colorchooser.askcolor(
            color=self._accent_color(),
            title="VoxType Accent Color",
            parent=self.root,
        )
        if color:
            self._save_accent(color)

    def _reset_accent(self):
        self._save_accent(self.DEFAULT_ACCENT)

    def _reconfigure(self):
        from tkinter import messagebox
        cfg = configure_api(self.app.cfg, parent=self.root)
        if not cfg:
            return
        self.app.cfg = cfg
        messagebox.showinfo(
            "API Updated",
            "Your settings were saved and will apply to the next recording.",
            parent=self.root,
        )

    def run(self):
        timer_active = _begin_high_resolution_timer()
        self._next_frame = time.perf_counter()
        self._meter_last_time = self._next_frame
        try:
            self._tick()
            self.root.mainloop()
        finally:
            if timer_active:
                _end_high_resolution_timer()

    def _tick(self):
        now = time.perf_counter()
        st = self.app.state
        phase = st["phase"]
        if phase == "listening":
            self._show()
            level = self.app.session.state.get("level", 0.0) if self.app.session else 0.0
            self._set("Listening…", "Press F10 to stop", "listening", level)
        elif phase == "done":
            self._show()
            self._set("Inserted", "Text pasted at cursor", "done")
            if time.time() - self._phase_since > 3:
                self.app.state["phase"] = "idle"
        elif phase == "error":
            self._show()
            message = (st.get("message") or "Unknown error").strip()
            self._set("Couldn't complete", message, "error")
            if time.time() - self._phase_since > 4:
                self.app.state["phase"] = "idle"
        else:
            # 待命状态：隐藏悬浮窗（按 F10 时自动弹出）
            self._hide()
            self._set("VoxType", "Press F10 to start", "idle")
        if phase != self._last_phase:
            self._phase_since = time.time()
            self._last_phase = phase
        self._animate_meter(now)
        self._schedule_next_frame(now)

    def _schedule_next_frame(self, now):
        interval = self._frame_interval if self._meter_visible else 0.05
        self._next_frame += interval
        if self._next_frame <= now or self._next_frame - now > interval:
            self._next_frame = now + interval
        delay_ms = max(1, int(round((self._next_frame - now) * 1000)))
        self.root.after(delay_ms, self._tick)

    def _show(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()

    def _hide(self):
        if self.root.state() != "withdrawn":
            self.root.withdraw()

    def _set(self, title, subtitle, state, level=0.0):
        surface, border = self._theme.get(state, self._theme["idle"])
        if len(subtitle) > 44:
            subtitle = subtitle[:43] + "…"

        signature = (title, subtitle, state)
        if signature != self._last_ui_signature:
            self.canvas.itemconfigure(
                self._surface,
                fill=surface,
                outline=border,
            )
            self.canvas.itemconfigure(self._title, text=title)
            self.canvas.itemconfigure(self._subtitle, text=subtitle)
            self._last_ui_signature = signature

        meter_visible = state == "listening"
        if meter_visible != self._meter_visible:
            item_state = "normal" if meter_visible else "hidden"
            self.canvas.itemconfigure(self._meter_track, state=item_state)
            self.canvas.itemconfigure(self._meter_fill, state=item_state)
            self._meter_visible = meter_visible
            if meter_visible:
                self._meter_level = 0.0
                self._meter_last_time = time.perf_counter()

        if meter_visible:
            try:
                self._meter_target = float(level)
            except (TypeError, ValueError):
                self._meter_target = 0.0
            self._meter_target = max(0.0, min(1.0, self._meter_target))
        else:
            self._meter_target = 0.0
            self._meter_level = 0.0
            self.canvas.coords(
                self._meter_fill,
                self.METER_X,
                self.METER_Y,
                self.METER_X,
                self.METER_Y,
            )

    def _animate_meter(self, now):
        if not self._meter_visible:
            return
        dt = now - self._meter_last_time
        self._meter_last_time = now
        dt = max(0.0, min(dt, 0.05))
        if dt == 0.0:
            return

        target = self._meter_target
        current = self._meter_level
        tau = 0.045 if target >= current else 0.16
        alpha = 1.0 - math.exp(-dt / tau)
        self._meter_level += (target - current) * alpha
        if abs(target - self._meter_level) < 0.0005:
            self._meter_level = target

        x1 = self.METER_X
        x2 = self.WIDTH - self.METER_X
        x = x1 + (x2 - x1) * self._meter_level
        self.canvas.coords(self._meter_fill, x1, self.METER_Y, x, self.METER_Y)


# ---------------- 热键监听 ----------------
def build_hotkey_handler(cfg, app):
    import keyboard
    hotkey = cfg["hotkey"].lower().strip()

    def toggle_recording():
        if app.recording:
            app.stop()
        else:
            app.start()

    # 在按键释放时切换，避免长按重复触发；suppress 阻止热键传给当前应用。
    keyboard.add_hotkey(
        hotkey, toggle_recording, suppress=True, trigger_on_release=True
    )
    log("已拦截 %s 键不向应用传递" % hotkey)
    return keyboard


def selftest():
    cfg = load_config()
    if needs_setup(cfg):
        cfg = configure_api(cfg)
    if not cfg:
        print("API setup is incomplete. Self-test cancelled.")
        return
    print("Self-test: recording starts in 5 seconds. Please say a short phrase.")
    time.sleep(5)
    s = StreamAsrSession(cfg)
    print("[Recording] Speak now…")
    try:
        s.connect()
    except Exception as e:
        print("[Error] Could not connect to the recognition service: %s" % e)
        return
    result = s.run(duration=5)
    if result["error"]:
        print("[Error] %s" % result["error"])
        return
    text = (result["text"] or "").strip()
    if not text:
        print("[Notice] No speech detected. Check your microphone and try again.")
    else:
        print("[Transcript] %s" % text)
    print("Self-test complete.")


def main():
    import traceback
    cfg = load_config()
    force_setup = "--setup" in sys.argv
    if force_setup or needs_setup(cfg):
        cfg = configure_api(cfg)
    if not cfg:
        safe_print("API setup is incomplete. The app did not start.")
        return
    log("=== 程序启动 ===")
    # 枚举一次可用输入设备，写入日志便于排查麦克风问题
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        log("可用输入设备: " + "; ".join(
            "%d:%s" % (i, d["name"])
            for i, d in enumerate(devs) if d["max_input_channels"] > 0
        ))
    except Exception as e:
        log("设备枚举失败: %s" % e)
    app = App(cfg)
    ui = None
    try:
        import tkinter  # noqa
        ui = FloatingUI(app)
        log("悬浮窗创建成功")
    except Exception as e:
        log("悬浮窗创建失败: %s\n%s" % (e, traceback.format_exc()))
        safe_print("Floating window unavailable: %s (running in background)" % e)
    try:
        kb = build_hotkey_handler(cfg, app)
        log("热键注册成功: %s" % cfg["hotkey"])
    except Exception as e:
        log("热键注册失败: %s\n%s" % (e, traceback.format_exc()))
        safe_print("Hotkey registration failed: %s" % e)
        return
    safe_print("Ready. Press %s to start or stop. Right-click the floating window for settings or to quit." % cfg["hotkey"].upper())
    try:
        if ui:
            ui.run()
        else:
            kb.wait()
    except Exception as e:
        log("运行异常: %s" % traceback.format_exc())


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
