"""手机附件 USB 转移（FR-10）测试。

用一个假的 adb（内存里的“手机文件系统”）驱动真实的传输逻辑，
覆盖：列目录解析、按类型与日期筛选、分块拉取、**断点续传**、SHA-256 校验、
校验不一致时拒绝落地、取消时保留断点、登记进归档库。
"""

from __future__ import annotations

import hashlib
import re
import shlex
import threading
from pathlib import Path

import pytest

from qqarchive.adb import (Adb, AdbError, PULL_CHUNK, RemoteFile, RunResult, clean_adb_error,
                           list_files, list_qq_dirs, remote_checksum, safe_remote_path,
                           stat_file)
from qqarchive.media_pull import (PHONE_DIR_NAME, PullOptions, has_adb_files, human_size,
                                  known_files, load_resume, local_target, match_file, pull_file,
                                  pull_qq_media, pull_tree, pulled_count, register_pulled,
                                  safe_name)
from qqarchive.store import Archive

# ---------------------------------------------------------------------------
# 假的 adb：在内存里模拟一台手机的只读命令
# ---------------------------------------------------------------------------

CUT = object()  # 标记：读到一半“断开”


class FakePhone:
    """内存版手机文件系统。"""

    def __init__(self, files: dict[str, bytes], *, with_sha256: bool = True, with_md5: bool = True,
                 use_ls: bool = False):
        self.files = {p: bytes(d) for p, d in files.items()}
        self.with_sha256 = with_sha256
        self.with_md5 = with_md5
        self.use_ls = use_ls
        self.commands: list[str] = []
        self.mtimes = {}

    # -- 目录结构 ---------------------------------------------------------
    def dirs(self) -> set[str]:
        out: set[str] = set()
        for p in self.files:
            parts = p.strip("/").split("/")
            for i in range(1, len(parts)):
                out.add("/" + "/".join(parts[:i]))
        return out

    def children(self, folder: str) -> list[str]:
        folder = folder.rstrip("/")
        return [p for p in self.files if p.startswith(folder + "/") and "/" not in p[len(folder) + 1:]]


class FakeAdb(Adb):
    """替换掉真实 adb 进程，只保留命令解析与拼装逻辑（被测代码不变）。"""

    def __init__(self, phone: FakePhone, serial: str = "FAKESERIAL"):
        # 不吃 adb_path，因为根本不启动进程
        self.adb = "fake-adb"
        self.serial = serial
        self.timeout = 60
        self.phone = phone
        self.cut_after: int | None = None  # 读到第 N 个字节后模拟断开
        self.reads = 0

    # -- 命令执行 ---------------------------------------------------------
    def run(self, args, *, timeout=None, cancel=None):
        """本假手机只支持 ``devices -l``（设备列表），其余都走 shell。"""
        if list(args)[:1] == ["exec-out"]:
            return self.shell(list(args)[1], timeout=timeout, cancel=cancel)
        if list(args)[:1] == ["devices"]:
            rows = ["List of devices attached",
                    f"{self.serial}\tdevice product:fakeproduct model:FAKEPHONE device:fakedevice"]
            return RunResult(0, ("\n".join(rows) + "\n").encode(), b"")
        raise AssertionError(f"假 adb 不支持的命令：{list(args)}")

    def shell(self, command, *, timeout=None, cancel=None):
        from qqarchive.adb import RunResult
        self.phone.commands.append(command)
        if command.startswith("getprop"):
            out = "FAKEPHONE\nFakeBrand\n16\n36\nFAKESERIAL"
            return RunResult(0, out.encode(), b"")
        out = self._dispatch(command)
        if out is None:
            return RunResult(1, b"", b"fake: not found")
        return RunResult(0, out, b"")

    def shell_text(self, command, *, timeout=None, cancel=None):
        return self.shell(command).text

    # -- 解析 -------------------------------------------------------------
    @staticmethod
    def _clean(part: str) -> str:
        """去掉重定向与前后空白。"""
        for token in ("2>/dev/null", "2>&1", "1>/dev/null"):
            part = part.replace(token, " ")
        return part.strip()

    @staticmethod
    def _split_outside_quotes(cmd: str, sep: str) -> list[str]:
        """按 sep 切分，但引号内部的 sep 不切（例如 find -printf '%p|%s'）。"""
        out, buf, quote = [], [], ""
        i = 0
        while i < len(cmd):
            c = cmd[i]
            if quote:
                if c == quote:
                    quote = ""
                buf.append(c)
                i += 1
                continue
            if c in "'\"":
                quote = c
                buf.append(c)
                i += 1
                continue
            if cmd.startswith(sep, i):
                out.append("".join(buf))
                buf = []
                i += len(sep)
                continue
            buf.append(c)
            i += 1
        out.append("".join(buf))
        return out

    def _tokenize(self, part: str) -> list[str]:
        """把单个命令切成参数；被引号保护的空格会完整保留。"""
        try:
            return shlex.split(self._clean(part))
        except ValueError:
            return []

    def _stages(self, alternative: str) -> list[list[str]]:
        return [t for t in (self._tokenize(p) for p in self._split_outside_quotes(alternative, "|")) if t]

    def _path_of(self, args: list[str]) -> str:
        """从命令参数（含命令名）里取出路径。

        跳过命令名与 -c N / +N / bs= / skip= / count= 等选项；路径含空格时用空格拼回。
        """
        out: list[str] = []
        i = 1  # 跳过命令名
        while i < len(args):
            a = args[i]
            if a == "-c":  # tail -c N / head -c N
                i += 2
                continue
            if re.fullmatch(r"\+\d+", a):  # tail 的起始偏移
                i += 1
                continue
            if a.startswith("if="):
                out.append(a[3:])
            elif re.fullmatch(r"(bs|skip|count|of|status)=.*", a):
                pass
            else:
                out.append(a)
            i += 1
        return " ".join(out).strip()

    def _dispatch(self, command: str) -> bytes | None:
        """按 shell 语义执行：先按 || 分支，再按 | 管道，第一个成功的分支生效。"""
        for alternative in self._split_outside_quotes(command, "||"):
            if not alternative.strip():
                continue
            stages = self._stages(alternative)
            if not stages:
                continue
            head = stages[0]
            if head[0] == "[":
                path = head[-2] if len(head) >= 3 else ""
                return b"yes" if path.rstrip("/") in self.phone.dirs() else b"no"
            if head[0] == "echo":
                return (" ".join(head[1:])).encode()
            try:
                result = self._run_stages(stages)
            except (ValueError, KeyError, IndexError):
                result = None
            if result is not None:
                return result
        return None

    def _run_stages(self, stages) -> bytes | None:
        head = stages[0]
        if head[0] == "find":
            return self._find(head)
        if head[0] == "stat":
            return self._stat(head)
        if head[0] in ("sha256sum", "md5sum"):
            return self._sum(head)
        if head[0] == "ls":
            return self._ls(head)
        if head[0] == "tail":
            data = self._read_file(self._path_of(head))
            if data is None:
                return None  # 读取被切断：让命令失败，由上层按断点处理
            start = int(next(a for a in head if a.startswith("+"))) - 1
            return self._apply(stages[1:], data[start:])
        if head[0] == "dd":
            data = self._read_file(self._path_of(head))
            if data is None:
                return None
            bs = next((int(a[3:]) for a in head if a.startswith("bs=")), 65536)
            skip = next((int(a[5:]) for a in head if a.startswith("skip=")), 0)
            return self._apply(stages[1:], data[skip * bs:])
        return None

    def _apply(self, stages, data: bytes) -> bytes:
        for stage in stages:
            if stage[0] == "tail":
                data = data[int(next(a for a in stage if a.startswith("+"))) - 1:]
            elif stage[0] == "head":
                n = int(stage[stage.index("-c") + 1]) if "-c" in stage else int(stage[-1])
                data = data[:n]
        return data

    def _read_file(self, path: str) -> bytes | None:
        """读一次手机文件；模拟断线时返回 None（命令失败）。"""
        self.reads += 1
        if self.cut_after is not None and self.reads > self.cut_after:
            return None
        return self.phone.files.get(path, b"")

    def _find(self, args) -> bytes:
        if self.phone.use_ls:
            return None  # 强制走 ls 回退路径
        if "-type" in args and "-maxdepth" in args:
            return None  # 本假手机不支持“限定深度”的写法，走回退
        target = args[1]
        entries = []
        for path, data in sorted(self.phone.files.items()):
            if path.startswith(target.rstrip("/") + "/"):
                entries.append(f"{path}|{len(data)}|1700000000.0")
        return ("\n".join(entries) + ("\n" if entries else "")).encode()

    def _ls(self, args) -> bytes:
        # 真机会拼 find；本假手机只支持一句话式的 ls -ln <目录>
        if len(args) < 3 or args[1] != "-ln":
            return b""
        target = args[2]
        rows = ["total 8"]
        for path in sorted(self.phone.children(target)):
            data = self.phone.files[path]
            # 只输出文件名（部分手机的 ls 就是这个行为），含空格时按 ls 的惯例不转义
            rows.append(f"-rw-rw---- 1 u0_a1 u0_a1 {len(data):>8} 2026-02-26 08:38 {Path(path).name}")
        return ("\n".join(rows) + "\n").encode()

    def _stat(self, args) -> bytes:
        path = self._path_of(args)
        if path not in self.phone.files:
            return b""
        return f"{len(self.phone.files[path])}|{self.phone.mtimes.get(path, 1700000000)}".encode()

    def _sum(self, args) -> bytes:
        algo = args[0]
        if (algo == "sha256sum" and not self.phone.with_sha256) or \
                (algo == "md5sum" and not self.phone.with_md5):
            return b""
        path = self._path_of(args)
        data = self.phone.files.get(path)
        if data is None:
            return b""
        digest = hashlib.sha256(data).hexdigest() if algo == "sha256sum" else hashlib.md5(data).hexdigest()
        return f"{digest}  {path}\n".encode()

    @staticmethod
    def _path_after(args, prefix: str = "") -> str:
        """取出参数里的路径；路径含空格时会被拆成多个参数，这里重新拼起来。"""
        if prefix:
            first = next((i for i, a in enumerate(args) if a.startswith(prefix)), None)
            if first is None:
                return ""
            return args[first][len(prefix):] + "".join(args[first + 1:])
        return args[-1]

    def _tail(self, segs) -> bytes:
        args = segs[0]
        # tail -c +N <file>   （文件路径可能含空格，被拆成了多个参数）
        start = int(args[2].lstrip("+")) - 1
        path = self._path_after(args[3:])
        data = self.phone.files.get(path, b"")
        self.reads += 1
        if self.cut_after is not None and self.reads > self.cut_after:
            return b""
        out = data[start:]
        for seg in segs[1:]:
            if seg[0] == "head":
                out = out[:int(seg[2])]
        return out

    def _dd(self, segs) -> bytes:
        args = segs[0]
        path = self._path_after(args, "if=")
        bs = next((int(a[3:]) for a in args if a.startswith("bs=")), 65536)
        skip = next((int(a[5:]) for a in args if a.startswith("skip=")), 0)
        data = self.phone.files.get(path, b"")[skip * bs:]
        self.reads += 1
        for seg in segs[1:]:
            if seg[0] == "tail":
                data = data[int(seg[2].lstrip("+")) - 1:]
            if seg[0] == "head":
                data = data[:int(seg[2])]
        return data


@pytest.fixture
def phone():
    return FakePhone({
        "/sdcard/Pictures/QQ/a.jpg": b"JPEGDATA" * 10,
        "/sdcard/Pictures/QQ/b.png": b"PNGDATA" * 20,
        "/sdcard/Pictures/QQ/带 空格 的照片.jpg": b"SPACED" * 5,
        "/sdcard/Pictures/QQ/sub/c.png": b"SUBDIR" * 7,
        "/sdcard/Tencent/QQfile_recv/报告.pdf": b"PDF" * 100,
        "/sdcard/Tencent/QQ_Video/clip.mp4": b"MP4" * 300,
        "/sdcard/Tencent/QQ_Ptt/voice.amr": b"AMR" * 7,
        "/sdcard/Pictures/QQ/notes.txt": b"hello",
    })


@pytest.fixture
def adb(phone):
    return FakeAdb(phone)


def cut_reads(adb: FakeAdb, after: int) -> None:
    """让这个假手机在第 after+1 次分块读取时“断线”，用于测断点续传。

    直接替换 ``read_chunk``（真正负责读数据的入口），分块读取行为完全可控。
    """
    real = type(adb).read_chunk
    state = {"count": 0}

    def flaky(self, remote_path, offset, length, *, cancel=None):
        if state["count"] >= after:
            return b""
        state["count"] += 1
        return real(self, remote_path, offset, length, cancel=cancel)

    adb.read_chunk = flaky.__get__(adb, type(adb))  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 命令层
# ---------------------------------------------------------------------------

def test_list_files_parses_size_and_mtime(adb):
    files = list_files(adb, "/sdcard/Pictures/QQ")
    names = {Path(f.path).name: f for f in files}
    assert set(names) == {"a.jpg", "b.png", "带 空格 的照片.jpg", "notes.txt", "c.png"}
    assert names["a.jpg"].size == 80
    assert names["a.jpg"].mtime == 1700000000
    assert names["带 空格 的照片.jpg"].size == 30


def test_list_files_non_recursive(adb):
    """只列当前这一层（recursive=False），子目录里的文件不算。"""
    files = list_files(adb, "/sdcard/Pictures/QQ", recursive=False)
    assert {Path(f.path).name for f in files} == {"a.jpg", "b.png", "带 空格 的照片.jpg", "notes.txt"}
    assert all("/sub/" not in f.path for f in files)


def test_list_files_with_spaces_in_path(adb):
    """路径含空格时命令要正确引用，不能把路径拆成两个参数。"""
    files = list_files(adb, "/sdcard/Pictures/QQ")
    spaced = [f for f in files if " " in f.path]
    assert len(spaced) == 1
    assert spaced[0].size == 30


def test_list_files_falls_back_to_ls(phone):
    """手机上没有 find -printf（老系统）时，退回 ls -ln 也能列出文件与大小。

    注意：``ls -ln`` 在某些手机上只输出文件名而不是完整路径，而且不转义空格，
    所以这条回退路径是有损的——这里只要求它能列出普通文件名。
    """
    phone.use_ls = True
    adb = FakeAdb(phone)
    files = list_files(adb, "/sdcard/Pictures/QQ", recursive=False)
    names = {Path(f.path).name for f in files}
    assert {"a.jpg", "b.png", "notes.txt"} <= names
    by_name = {Path(f.path).name: f for f in files}
    assert by_name["a.jpg"].size == 80
    assert all(f.path.startswith("/sdcard/Pictures/QQ/") for f in files)

def test_stat_and_checksum(adb):
    st = stat_file(adb, "/sdcard/Pictures/QQ/a.jpg")
    assert st is not None and st.size == 80
    algo, value = remote_checksum(adb, "/sdcard/Pictures/QQ/a.jpg")
    assert algo == "sha256"
    assert value == hashlib.sha256(b"JPEGDATA" * 10).hexdigest()


def test_checksum_absent_reported_honestly(phone):
    phone.with_sha256 = False
    phone.with_md5 = False
    adb = FakeAdb(phone)
    assert remote_checksum(adb, "/sdcard/Pictures/QQ/a.jpg") == ("", "")


def test_read_chunk_returns_exact_slice(adb):
    data = b"0123456789" * 100
    adb.phone.files["/sdcard/x.bin"] = data
    assert type(adb).read_chunk(adb, "/sdcard/x.bin", 0, 10) == data[:10]
    assert type(adb).read_chunk(adb, "/sdcard/x.bin", 995, 20) == data[995:1000]


def test_read_chunk_rejects_newline_in_path():
    with pytest.raises(AdbError):
        safe_remote_path("/sdcard/bad\nname.jpg")


def test_list_qq_dirs_only_existing(phone):
    adb = FakeAdb(phone)
    dirs = list_qq_dirs(adb)
    paths = {d["path"] for d in dirs}
    assert "/sdcard/Pictures/QQ" in paths
    assert "/sdcard/Tencent/QQfile_recv" in paths
    assert "/sdcard/Tencent/QQ_Images" not in paths  # 手机上不存在
    pictures = next(d for d in dirs if d["path"] == "/sdcard/Pictures/QQ")
    assert pictures["count"] == 5  # a.jpg / b.png / 带空格.jpg / sub/c.png / notes.txt


def test_device_info_and_problem_message(adb):
    info = adb.device_info()
    assert info["model"] == "FAKEPHONE"
    assert info["android"] == "16"


def test_clean_adb_error_translates():
    assert "授权" in clean_adb_error("error: device unauthorized.")
    assert "不存在" in clean_adb_error("adb: error: remote object 'x' not found: No such file or directory")
    assert "拒绝访问" in clean_adb_error("cat: /data/x: Permission denied")


# ---------------------------------------------------------------------------
# 文件名与目标路径
# ---------------------------------------------------------------------------

def test_safe_name_replaces_windows_illegal_chars():
    assert safe_name('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert safe_name("  . ") == "unnamed"
    assert safe_name("CON.txt").startswith("_")
    assert safe_name("正常名字.jpg") == "正常名字.jpg"


def test_local_target_layout(tmp_path):
    remote = RemoteFile("/sdcard/Pictures/QQ/a.jpg", 80, 1700000000)
    target = local_target(tmp_path, "FAKESERIAL", "image", remote)
    assert PHONE_DIR_NAME in str(target)
    assert "FAKESERIAL" in str(target)
    assert target.parent.name == "2023-11"  # 1700000000 -> 2023-11
    assert target.name.startswith("a_") and target.suffix == ".jpg"


def test_local_target_avoids_collision_between_different_remotes(tmp_path):
    """不同远端文件同名时加短哈希，绝不互相覆盖。"""
    taken: set[str] = set()
    one = RemoteFile("/sdcard/A/x.jpg", 1, 1700000000)
    two = RemoteFile("/sdcard/B/x.jpg", 2, 1700000000)
    first = local_target(tmp_path, "S", "image", one, taken=taken)
    second = local_target(tmp_path, "S", "image", two, taken=taken)
    assert first != second
    assert first.name.startswith("x_")
    assert second.name.startswith("x_")


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"


# ---------------------------------------------------------------------------
# 筛选
# ---------------------------------------------------------------------------

def test_match_file_by_kind_and_time():
    img = RemoteFile("/sdcard/Pictures/QQ/a.jpg", 100, 1700000000)
    assert match_file(img, PullOptions(kinds=("image",))) == "image"
    assert match_file(img, PullOptions(kinds=("video",))) is None
    assert match_file(img, PullOptions(start=1700000001)) is None
    assert match_file(img, PullOptions(end=1699999999)) is None
    assert match_file(img, PullOptions(min_size=1000)) is None
    assert match_file(RemoteFile("/sdcard/x/a.txt", 100, 1700000000), PullOptions()) == "file"


# ---------------------------------------------------------------------------
# 单文件拉取、校验、断点续传
# ---------------------------------------------------------------------------

def test_pull_file_verifies_sha256(adb, tmp_path):
    remote = RemoteFile("/sdcard/Pictures/QQ/a.jpg", 80, 1700000000)
    info = pull_file(adb, remote, tmp_path / "a.jpg")
    assert info["path"].read_bytes() == b"JPEGDATA" * 10
    assert info["sha256"] == hashlib.sha256(b"JPEGDATA" * 10).hexdigest()
    assert info["verified"] is True
    assert info["resumed"] == 0
    assert not list(tmp_path.glob("*.part*")), "完成后不应留下断点文件"


def test_pull_file_without_remote_checksum_is_marked_unverified(phone, tmp_path):
    phone.with_sha256 = False
    phone.with_md5 = False
    adb = FakeAdb(phone)
    remote = RemoteFile("/sdcard/Pictures/QQ/a.jpg", 80, 1700000000)
    info = pull_file(adb, remote, tmp_path / "a.jpg")
    assert info["verified"] is False
    assert info["algo"] == ""
    assert info["path"].exists()


def test_pull_file_rejects_checksum_mismatch(phone, tmp_path):
    """手机上算出的校验和与本地不一致时不得落地，并保留断点。"""
    adb = FakeAdb(phone)
    phone.files["/sdcard/Pictures/QQ/bad.jpg"] = b"REALDATA" * 10
    original = adb._sum

    def lying_sum(args):
        if args[-1].endswith("bad.jpg"):
            return b"0" * 64 + b"  bad.jpg\n"
        return original(args)

    adb._sum = lying_sum  # type: ignore[method-assign]
    remote = RemoteFile("/sdcard/Pictures/QQ/bad.jpg", 80, 1700000000)
    with pytest.raises(Exception) as exc:
        pull_file(adb, remote, tmp_path / "bad.jpg")
    assert "校验不一致" in str(exc.value)
    assert not (tmp_path / "bad.jpg").exists(), "校验不过就不应该落地"
    assert (tmp_path / "bad.jpg.part").exists(), "应保留断点供下次重传"


def test_pull_file_resumes_from_partial(phone, tmp_path):
    """中断后重来：只补齐剩下的部分，且结果与完整文件一致。"""
    data = bytes(range(256)) * (PULL_CHUNK // 128)  # 跨多个分块
    phone.files["/sdcard/big.bin"] = data
    adb = FakeAdb(phone)
    target = tmp_path / "big.bin"
    remote = RemoteFile("/sdcard/big.bin", len(data), 1700000000)

    # 第一次：手机上读到一部分就断了
    cut_reads(adb, 1)
    with pytest.raises(Exception):
        pull_file(adb, remote, target)
    part = target.with_name(target.name + ".part")
    assert part.exists()
    partial = part.stat().st_size
    assert 0 < partial < len(data)
    state = load_resume(part)
    assert state and state["received"] == partial and state["size"] == len(data)

    # 第二次：不再断开，应当从 partial 处继续
    adb.read_chunk = type(adb).read_chunk.__get__(adb, type(adb))  # type: ignore[method-assign]
    adb.reads = 0
    info = pull_file(adb, remote, target)
    assert target.read_bytes() == data
    assert info["resumed"] == partial, "应该从上次的位置继续，而不是从头再来"
    assert not part.exists()


def test_pull_file_restarts_when_remote_changed(phone, tmp_path):
    """手机上文件变了（大小不同）时不能续传，必须重下。"""
    data = b"A" * (PULL_CHUNK * 2 + 500)
    phone.files["/sdcard/c.bin"] = data
    adb = FakeAdb(phone)
    target = tmp_path / "c.bin"
    remote = RemoteFile("/sdcard/c.bin", len(data), 1700000000)
    cut_reads(adb, 1)
    with pytest.raises(Exception):
        pull_file(adb, remote, target)
    part = target.with_name(target.name + ".part")
    before = part.stat().st_size

    adb.read_chunk = type(adb).read_chunk.__get__(adb, type(adb))  # type: ignore[method-assign]
    new_data = b"B" * (PULL_CHUNK * 3)
    phone.files["/sdcard/c.bin"] = new_data
    phone.mtimes["/sdcard/c.bin"] = 1700000001
    info = pull_file(adb, RemoteFile("/sdcard/c.bin", len(new_data), 1700000001), target)
    assert target.read_bytes() == new_data
    assert info["resumed"] == 0, "远端变了就不能接着传"
    assert before > 0


def test_pull_file_cancel_keeps_partial(phone, tmp_path):
    """用户取消时保留断点，下次可以接着传。"""
    data = b"Z" * (PULL_CHUNK * 2 + 1000)
    phone.files["/sdcard/z.bin"] = data
    adb = FakeAdb(phone)
    cancel = threading.Event()
    target = tmp_path / "z.bin"
    remote = RemoteFile("/sdcard/z.bin", len(data), 1700000000)

    def on_progress(done, total):
        cancel.set()  # 第一块之后就取消

    with pytest.raises(Exception):
        pull_file(adb, remote, target, progress=on_progress, cancel=cancel)
    part = target.with_name(target.name + ".part")
    assert part.exists() and part.stat().st_size > 0
    assert not target.exists()
    assert load_resume(part) is not None


def test_has_adb_files_lists_pending(tmp_path):
    root = tmp_path / PHONE_DIR_NAME / "S" / "image" / "2026-02"
    root.mkdir(parents=True)
    (root / "a.jpg.part").write_bytes(b"x")
    (root / "a.jpg.part.json").write_text("{}", encoding="utf-8")
    (root / "done.jpg").write_bytes(b"y")
    pending = has_adb_files(tmp_path)
    assert [p.name for p in pending] == ["a.jpg.part"]


# ---------------------------------------------------------------------------
# 批量与归档登记
# ---------------------------------------------------------------------------

def test_pull_tree_filters_and_records(adb, tmp_path):
    res = pull_tree(adb, "/sdcard/Pictures/QQ", tmp_path, device="FAKESERIAL",
                    opts=PullOptions(kinds=("image",)))
    assert res.files == 4  # a.jpg / b.png / 带 空格 的照片.jpg / sub/c.png
    assert res.failed == 0
    assert res.bytes == 80 + 140 + 30 + 42
    for rec in res.records:
        assert rec["kind"] == "image"
        assert Path(rec["local_path"]).exists()
        assert rec["sha256"]


def test_pull_tree_limit_and_kinds(adb, tmp_path):
    res = pull_tree(adb, "/sdcard/Pictures/QQ", tmp_path, opts=PullOptions(limit=1))
    assert res.files == 1


def test_pull_tree_reports_failures_without_stopping(phone, tmp_path):
    """单个文件读不出来时，其余文件照常完成，失败原因如实记录。"""
    adb = FakeAdb(phone)
    phone.files["/sdcard/Pictures/QQ/broken.jpg"] = b"X" * 100
    real_read = type(adb).read_chunk

    def flaky(self, remote_path, offset, length, *, cancel=None):
        if remote_path.endswith("broken.jpg"):
            return b""  # 手机上读不出来
        return real_read(self, remote_path, offset, length, cancel=cancel)

    adb.read_chunk = flaky.__get__(adb, type(adb))  # type: ignore[method-assign]
    res = pull_tree(adb, "/sdcard/Pictures/QQ", tmp_path, opts=PullOptions(kinds=("image",)))
    assert res.failed == 1
    assert res.files == 4, "一个失败不影响其他文件"
    assert any("broken.jpg" in e for e in res.errors)


def test_register_and_skip_already_pulled(config_home, adb, tmp_path):
    """第二次拉取时，已登记且大小一致的文件直接跳过，不重复下载。"""
    archive = Archive(tmp_path / "archive.db")
    try:
        res = pull_tree(adb, "/sdcard/Pictures/QQ", tmp_path, device="FAKESERIAL",
                        opts=PullOptions(kinds=("image",)), archive=archive,
                        on_file=lambda rec: register_pulled(archive, rec))
        assert res.files == 4
        assert pulled_count(archive, "FAKESERIAL") == 4
        known = known_files(archive, "FAKESERIAL")
        assert len(known) == 4
        adb.reads = 0
        again = pull_tree(adb, "/sdcard/Pictures/QQ", tmp_path, device="FAKESERIAL",
                          opts=PullOptions(kinds=("image",)), archive=archive,
                          on_file=lambda rec: register_pulled(archive, rec))
        assert again.files == 0
        assert again.skipped == 4
        assert adb.reads == 0, "跳过的文件不应该再去读手机"
        assert pulled_count(archive, "FAKESERIAL") == 4
    finally:
        archive.close()


def test_register_pulled_updates_same_remote_path(config_home, tmp_path):
    archive = Archive(tmp_path / "archive.db")
    try:
        f = tmp_path / "x.jpg"
        f.write_bytes(b"data")
        rec = {"device": "S", "remote_path": "/sdcard/x.jpg", "local_path": str(f), "name": "x.jpg",
               "kind": "image", "size": 4, "mtime": 1, "sha256": "a" * 64, "md5": "b" * 32,
               "algo": "sha256"}
        register_pulled(archive, rec)
        rec2 = dict(rec, size=8)
        register_pulled(archive, rec2)
        rows = archive.con.execute("SELECT count(*) AS n FROM media_files WHERE origin='phone'").fetchone()
        assert rows["n"] == 1
        size = archive.con.execute("SELECT size FROM media_files WHERE origin='phone'").fetchone()["size"]
        assert size == 8
    finally:
        archive.close()


def test_pull_qq_media_merges_directories(config_home, adb, tmp_path):
    archive = Archive(tmp_path / "archive.db")
    try:
        dirs = [{"kind": "图片", "path": "/sdcard/Pictures/QQ"},
                {"kind": "文件", "path": "/sdcard/Tencent/QQfile_recv"}]
        res = pull_qq_media(adb, dirs, "FAKESERIAL", tmp_path, archive=archive,
                            opts=PullOptions(kinds=("image", "file")))
        assert res.files == 6  # 4 张图 + 1 个 pdf + 1 个 txt
        assert pulled_count(archive, "FAKESERIAL") == 6
        assert res.records
    finally:
        archive.close()


def test_pull_options_date_filter(adb, tmp_path):
    res = pull_tree(adb, "/sdcard/Pictures/QQ", tmp_path,
                    opts=PullOptions(start=1800000000))
    assert res.files == 0, "时间范围之外的文件不应被拉取"
