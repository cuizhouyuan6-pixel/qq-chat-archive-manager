"""ADB（Android 调试桥）只读通道（FR-10）。

本模块只做三件事：找到 adb、问设备有什么文件、把文件读回电脑。

安全边界（NFR-01 / 约束 3.4）：

- 对手机**只执行只读命令**：``ls`` / ``find`` / ``stat`` / ``tail`` / ``dd`` / ``sha256sum``；
- 不安装任何 APK，不 push，不删除手机上的文件，不做 root 提权；
- 所有失败都变成明确的中文原因，不静默吞掉。

设备侧命令约定（Android 7+ 自带 toybox，以下命令均可用）：

- 列目录：``find <dir> -type f -printf '%p|%s|%T@\\n'``（失败退回 ``ls -ln``）；
- 分块取数据：``tail -c +<起始字节> <文件>``（失败退回 ``dd bs=1 skip=``）；
- 校验：``sha256sum`` / ``md5sum``（都没有时只按大小核对，并如实说明）。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_TIMEOUT = 60
LIST_TIMEOUT = 300
PULL_CHUNK = 4 * 1024 * 1024  # 每块 4 MB：断点续传的粒度
# 手机 QQ 在外部存储里放附件的常见位置（以实机列目录结果为准，能找到几个算几个）
QQ_MEDIA_CANDIDATES = (
    ("图片", "/sdcard/Pictures/QQ"),
    ("图片", "/sdcard/DCIM/QQ"),
    ("图片", "/sdcard/Tencent/QQ_Images"),
    ("视频", "/sdcard/Tencent/QQ_Video"),
    ("视频", "/sdcard/DCIM/Camera/QQ"),
    ("语音", "/sdcard/Tencent/QQ_Ptt"),
    ("文件", "/sdcard/Tencent/QQfile_recv"),
    ("文件", "/sdcard/tencent/QQfile_recv"),
    ("表情", "/sdcard/Tencent/QQ_Emoji"),
    ("图片", "/sdcard/Android/media/com.tencent.mobileqq/Tencent/QQ_Images"),
    ("文件", "/sdcard/Android/media/com.tencent.mobileqq/Tencent/QQfile_recv"),
)

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class AdbError(Exception):
    """ADB 不可用、设备未连接或命令失败。"""


class Cancelled(Exception):
    """用户取消。"""


# ---------------------------------------------------------------------------
# 找到 adb
# ---------------------------------------------------------------------------

def adb_candidates(configured: str = "") -> list[str]:
    """按优先级列出可能的 adb 位置。"""
    out: list[str] = []
    if configured:
        out.append(configured)
    env = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if env:
        out.append(str(Path(env) / "platform-tools" / "adb.exe"))
        out.append(str(Path(env) / "platform-tools" / "adb"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(str(Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe"))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramW6432")):
        if base:
            out.append(str(Path(base) / "Android" / "platform-tools" / "adb.exe"))
    for drive in ("C:", "D:", "E:"):
        out.append(f"{drive}\\platform-tools\\adb.exe")
        out.append(f"{drive}\\adb\\adb.exe")
    out.append(str(Path(__file__).resolve().parent.parent / "tools" / "platform-tools" / "adb.exe"))
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        out.append(str(base / "tools" / "platform-tools" / "adb.exe"))
        out.append(str(base / "platform-tools" / "adb.exe"))
    found = shutil.which("adb")
    if found:
        out.append(found)
    seen, uniq = set(), []
    for p in out:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def find_adb(configured: str = "") -> str | None:
    """返回第一个真实存在的 adb 路径；都找不到时返回 None。"""
    for p in adb_candidates(configured):
        if p and Path(p).is_file():
            return p
    return None


def adb_hint() -> str:
    return ("没有找到 adb。请安装 Android SDK 的 platform-tools（或下载 platform-tools 压缩包），"
            "然后在设置页填写 adb.exe 的完整路径；也可以把 platform-tools 文件夹放到程序目录下。")


# ---------------------------------------------------------------------------
# 运行命令
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    code: int
    out: bytes
    err: bytes

    @property
    def text(self) -> str:
        return self.out.decode("utf-8", "replace")

    @property
    def error_text(self) -> str:
        return self.err.decode("utf-8", "replace").strip()


class Adb:
    """一个 adb 可执行文件的封装。所有方法都对手机只读。"""

    def __init__(self, adb_path: str, serial: str = "", *, timeout: int = DEFAULT_TIMEOUT):
        if not adb_path:
            raise AdbError(adb_hint())
        self.adb = str(adb_path)
        self.serial = serial
        self.timeout = timeout

    # -- 基础 -------------------------------------------------------------
    def base_args(self) -> list[str]:
        args = [self.adb]
        if self.serial:
            args += ["-s", self.serial]
        return args

    def run(self, args: Iterable[str], *, timeout: int | None = None,
            cancel: threading.Event | None = None) -> RunResult:
        cmd = self.base_args() + list(args)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=_NO_WINDOW)
        except OSError as exc:
            raise AdbError(f"无法运行 adb（{exc}）") from exc
        limit = timeout if timeout is not None else self.timeout
        try:
            while True:
                try:
                    out, err = proc.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    limit -= 0.5
                    if cancel is not None and cancel.is_set():
                        proc.kill()
                        proc.communicate()
                        raise Cancelled("已取消")
                    if limit <= 0:
                        proc.kill()
                        proc.communicate()
                        raise AdbError(f"adb 命令超时（超过 {timeout or self.timeout} 秒）：{' '.join(cmd[1:4])}")
        finally:
            if proc.poll() is None:
                proc.kill()
        return RunResult(proc.returncode, out, err)

    def shell(self, command: str, *, timeout: int | None = None,
              cancel: threading.Event | None = None) -> RunResult:
        return self.run(["shell", "-T", command], timeout=timeout, cancel=cancel)

    def binary(self, command: str, *, timeout: int | None = None,
               cancel: threading.Event | None = None) -> RunResult:
        # Some Android builds convert LF to CRLF even with shell -T.
        return self.run(["exec-out", command], timeout=timeout, cancel=cancel)

    def shell_text(self, command: str, *, timeout: int | None = None,
                   cancel: threading.Event | None = None) -> str:
        res = self.shell(command, timeout=timeout, cancel=cancel)
        text = res.text
        if res.code != 0 and not text.strip():
            raise AdbError(clean_adb_error(res.error_text) or "手机上的命令执行失败")
        return text

    def shell_bytes(self, command: str, *, timeout: int | None = None,
                    cancel: threading.Event | None = None) -> bytes:
        res = self.binary(command, timeout=timeout, cancel=cancel)
        if res.code != 0:
            raise AdbError(clean_adb_error(res.error_text) or "读取手机文件失败")
        return res.out

    def read_chunk(self, remote_path: str, offset: int, length: int, *,
                   cancel: threading.Event | None = None) -> bytes:
        """分块读取手机文件（断点续传的基础）。见模块级 ``read_chunk``。"""
        return read_chunk(self, remote_path, offset, length, cancel=cancel)

    def version(self) -> str:
        try:
            res = self.run(["version"], timeout=20)
        except AdbError:
            return ""
        line = res.text.splitlines()[0] if res.text else ""
        return line.strip()

    # -- 设备 -------------------------------------------------------------
    def list_devices(self) -> list["Device"]:
        res = self.run(["devices", "-l"], timeout=30)
        text = res.text
        if res.code != 0 and not text.strip():
            raise AdbError(clean_adb_error(res.error_text) or "adb devices 执行失败")
        out: list[Device] = []
        for line in text.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            extra = {}
            for token in parts[2:]:
                if ":" in token:
                    k, v = token.split(":", 1)
                    extra[k] = v
            out.append(Device(serial=serial, state=state, model=extra.get("model", ""),
                              product=extra.get("product", ""), device=extra.get("device", "")))
        return out

    def authorized_devices(self) -> list["Device"]:
        return [d for d in self.list_devices() if d.state == "device"]

    def device_info(self) -> dict:
        props = ["ro.product.model", "ro.product.brand", "ro.build.version.release",
                 "ro.build.version.sdk", "ro.serialno"]
        text = self.shell_text("; ".join(f"getprop {p}" for p in props))
        values = [ln.strip() for ln in text.splitlines()]
        values += [""] * (len(props) - len(values))
        info = dict(zip(props, values))
        return {
            "model": info.get("ro.product.model", ""),
            "brand": info.get("ro.product.brand", ""),
            "android": info.get("ro.build.version.release", ""),
            "sdk": info.get("ro.build.version.sdk", ""),
            "serial": self.serial or info.get("ro.serialno", ""),
        }

    def describe_problem(self) -> str:
        """设备不可用时的中文原因。"""
        try:
            devices = self.list_devices()
        except AdbError as exc:
            return str(exc)
        if not devices:
            return "没有检测到手机。请用数据线连接手机，并在手机上把 USB 用途选为“传输文件（MTP）”。"
        if all(d.state == "unauthorized" for d in devices):
            return "手机还没有授权这台电脑。请在手机屏幕上点“允许 USB 调试”，然后重试。"
        if all(d.state == "offline" for d in devices):
            return "手机处于离线状态，请重新插拔数据线，或重启手机上的 USB 调试。"
        return "; ".join(f"{d.serial}: {d.state}" for d in devices)


@dataclass
class Device:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    device: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "device"

    @property
    def label(self) -> str:
        return f"{self.model or self.serial}（{self.serial}）"


def clean_adb_error(text: str) -> str:
    """把 adb 的英文报错换成能看懂的中文。"""
    t = (text or "").strip()
    low = t.lower()
    if not t:
        return ""
    if "device unauthorized" in low:
        return "手机尚未授权这台电脑，请在手机上点“允许 USB 调试”"
    if "no such file or directory" in low:
        return "手机上这个文件或目录不存在"
    if "permission denied" in low:
        return "手机拒绝访问这个位置（Android 11 以后 /Android/data 需要特殊权限）"
    if "not found" in low and "adb" in low:
        return "找不到 adb 可执行文件"
    if "more than one device" in low:
        return "连接了多台设备，请先指定要使用哪一台"
    if "closed" in low:
        return "与手机的连接中断，请重新插拔数据线"
    return t.splitlines()[-1][:200]


# ---------------------------------------------------------------------------
# 列目录
# ---------------------------------------------------------------------------

@dataclass
class RemoteFile:
    path: str
    size: int
    mtime: int = 0


def _quote(path: str) -> str:
    return shlex.quote(path)


def safe_remote_path(path: str) -> str:
    """手机端路径里不允许出现换行与控制字符，否则命令会被拼坏。"""
    if any(c in path for c in "\r\n\x00"):
        raise AdbError("文件名包含换行符，已跳过")
    return path


def list_files(adb: Adb, remote_dir: str, *, recursive: bool = True,
               limit: int = 200000, cancel: threading.Event | None = None) -> list[RemoteFile]:
    """列出手机目录下的文件（只读）。路径含空格也能正确解析。

    ``recursive`` 为假时只列当前这一层。``find`` 的 ``-printf`` 写不出来（老系统）时
    退回 ``ls -ln``——那种情况下文件名含空格可能解析不准，会少列或多列，但不报错。
    """
    safe_remote_path(remote_dir)
    depth = "" if recursive else "-maxdepth 1 "
    q = _quote(remote_dir)
    text = adb.shell_text(f"find {q} {depth}-type f -printf '%p|%s|%T@\\n' 2>/dev/null || "
                          f"ls -ln {q} 2>/dev/null",
                          timeout=LIST_TIMEOUT, cancel=cancel)
    out: list[RemoteFile] = []
    if text.strip():
        parsed = _parse_find(text, limit)
        if parsed:
            return parsed
        return _parse_ls(text, remote_dir)
    return out

def _parse_find(text: str, limit: int) -> list[RemoteFile]:
    """解析一行一个的 ``路径|大小|修改时间``。"""
    out: list[RemoteFile] = []
    for line in text.splitlines():
        parts = line.rsplit("|", 2)
        if len(parts) != 3:
            continue
        path, size_s, mtime_s = parts
        try:
            size = int(size_s)
        except ValueError:
            continue
        try:
            mtime = int(float(mtime_s))
        except ValueError:
            mtime = 0
        out.append(RemoteFile(path, size, mtime))
        if len(out) >= limit:
            raise AdbError("目录文件数达到扫描上限，请选择更具体的子目录，未报告完整扫描")
    return out


def _parse_ls(text: str, remote_dir: str) -> list[RemoteFile]:
    """解析 ``ls -ln`` 的输出（退回路径）。

    只切掉前 7 列（权限、链接数、用户、组、大小、日期、时间），剩下的整段当文件名——
    这样含空格的文件名也能保住。注意部分手机的 ``ls`` 只输出文件名而不是完整路径，
    这种情况下会把文件名接到 ``remote_dir`` 后面。
    """
    out: list[RemoteFile] = []
    for line in text.splitlines():
        if line.startswith("total"):
            continue
        parts = re.split(r"\s+", line.strip(), maxsplit=7)
        if len(parts) < 8:
            continue
        try:
            size = int(parts[4])
        except ValueError:
            continue
        name = parts[7].strip()
        if not name:
            continue
        path = name if name.startswith("/") else f"{remote_dir.rstrip('/')}/{name}"
        out.append(RemoteFile(path, size, 0))
    return out


def stat_file(adb: Adb, remote_path: str, *, cancel: threading.Event | None = None) -> RemoteFile | None:
    """取手机上单个文件的大小与修改时间；取不到返回 None。"""
    safe_remote_path(remote_path)
    text = adb.shell_text(f"stat -c '%s|%Y' {_quote(remote_path)} 2>/dev/null || "
                          f"ls -ln {_quote(remote_path)} 2>/dev/null",
                          timeout=30, cancel=cancel)
    for line in text.splitlines():
        line = line.strip()
        if "|" in line:
            a, _, b = line.partition("|")
            try:
                return RemoteFile(remote_path, int(a), int(float(b)))
            except ValueError:
                continue
        parts = line.split()
        if len(parts) >= 5 and not line.startswith("total"):
            try:
                return RemoteFile(remote_path, int(parts[4]), 0)
            except ValueError:
                continue
    return None


def remote_checksum(adb: Adb, remote_path: str, *, cancel: threading.Event | None = None) -> tuple[str, str]:
    """手机上算校验和，返回 (算法, 值)；不支持时返回 ("", "")。"""
    safe_remote_path(remote_path)
    for algo, cmd, digits in (("sha256", "sha256sum", 64), ("md5", "md5sum", 32)):
        result = adb.shell(f"{cmd} {_quote(remote_path)} 2>/dev/null", timeout=600, cancel=cancel)
        value = result.text.strip().split(" ")[0].strip().lower()
        if result.code == 0 and re.fullmatch(rf"[0-9a-f]{{{digits}}}", value):
            return algo, value
    return "", ""


def list_qq_dirs(adb: Adb, *, cancel: threading.Event | None = None, include_app_storage: bool = False) -> list[dict]:
    """找出手机上真实存在的 QQ 附件目录，附文件数与总大小。"""
    out = []
    candidates = list(QQ_MEDIA_CANDIDATES)
    if include_app_storage:
        base = "/sdcard/Android/data/com.tencent.mobileqq/Tencent"
        candidates += [("图片", base+"/QQ_Images"), ("文件", base+"/QQfile_recv"),
                       ("图片", base+"/MobileQQ/chatpic"), ("图片", base+"/MobileQQ/photo"),
                       ("视频", base+"/MobileQQ/shortvideo"), ("语音", base+"/audio"),
                       ("图片", "/sdcard/Android/data/com.tencent.mobileqq/files/Pictures")]
        result = adb.shell(f"ls -1 {_quote(base+'/MobileQQ')} 2>/dev/null", cancel=cancel)
        for account in result.text.splitlines():
            account = account.strip()
            if not re.fullmatch(r"[0-9]{5,12}", account):
                continue
            for kind, sub in (("语音","ptt"),("视频","shortvideo"),("图片","pic")):
                candidates.append((kind, f"{base}/MobileQQ/{account}/{sub}"))
    for kind, path in candidates:
        try:
            files = list_files(adb, path, cancel=cancel)
            probe = adb.shell_text(f"[ -d {_quote(path)} ] && echo yes || echo no", timeout=30, cancel=cancel)
        except AdbError:
            continue
        # 列得出文件，或者目录存在检查通过，都算“有”
        if files:
            out.append({"kind": kind, "path": path, "count": len(files),
                        "bytes": sum(f.size for f in files)})
        elif "yes" in probe:
            out.append({"kind": kind, "path": path, "count": 0, "bytes": 0})
    return out


# ---------------------------------------------------------------------------
# 分块读取（断点续传的基础）
# ---------------------------------------------------------------------------

def read_chunk(adb: "Adb", remote_path: str, offset: int, length: int, *,
               cancel: threading.Event | None = None) -> bytes:
    """读取手机上文件的 [offset, offset+length) 区间（只读）。

    优先用 ``tail -c +<offset+1>``（toybox 自带），失败退回 ``dd``。
    也可直接调用 ``Adb.read_chunk`` 方法。
    """
    safe_remote_path(remote_path)
    if offset < 0 or length < 0 or length > PULL_CHUNK:
        raise AdbError("读取范围无效")
    if length == 0:
        return b""
    start = offset + 1
    q = _quote(remote_path)
    tail_cmd = f"tail -c +{start} {q} 2>/dev/null | head -c {length}"
    try:
        res = adb.binary(tail_cmd, timeout=max(60, length // 65536), cancel=cancel)
        if res.code == 0 and res.out:
            return res.out
    except AdbError:
        pass
    dd_cmd = (f"dd if={q} bs=65536 skip={offset // 65536} 2>/dev/null "
              f"| tail -c +{offset % 65536 + 1} | head -c {length}")
    res = adb.binary(dd_cmd, timeout=max(60, length // 65536), cancel=cancel)
    if res.code != 0 and not res.out:
        raise AdbError(f"读取手机文件失败：{Path(remote_path).name}")
    return res.out
