"""USB 转移手机上的聊天附件（FR-10）。

流程：列目录 → 按类型/时间筛选 → **分块拉取并支持断点续传** → 核对大小与校验和
→ 落到数据目录的「手机媒体/<设备>/<类型>/<年-月>/」→ 登记进归档库。

对手机只读（只跑 ``find``/``stat``/``tail``/``sha256sum``），不安装、不删除、不改动
手机上的任何文件（NFR-01）。本地未完成的传输保存为 ``.part`` + ``.part.json``
（记录远端路径、大小、修改时间、已确认字节数），下次接着传（NFR-04）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .adb import Adb, AdbError, Cancelled, PULL_CHUNK, RemoteFile, list_files, remote_checksum, stat_file
from .media import classify, hash_file
from .store import Archive, now

PHONE_DIR_NAME = "手机媒体"
REMOTE_ORIGIN = "phone"
_TMP_SUFFIX = ".part"
_MANIFEST_SUFFIX = ".part.json"
_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class PullError(Exception):
    """传输过程中出现无法继续的问题。"""


def safe_name(name: str) -> str:
    """把手机上的文件名变成 Windows 能用的名字（不改内容，只换非法字符）。"""
    cleaned = _SAFE.sub("_", name).strip(" .")
    if not cleaned:
        cleaned = "unnamed"
    # 兼顾 Windows 保留名
    if cleaned.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL"} or \
            re.fullmatch(r"COM[1-9]|LPT[1-9]", cleaned.upper().split(".")[0] or ""):
        cleaned = "_" + cleaned
    return cleaned


def human_size(n: int | None) -> str:
    if not n:
        return "0 B"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


@dataclass
class PullOptions:
    """筛选与目标设置。"""

    kinds: tuple[str, ...] = ("image", "video", "voice", "file")
    start: int | None = None  # 修改时间下界（Unix 秒）
    end: int | None = None  # 修改时间上界
    min_size: int = 1
    max_size: int | None = None
    recursive: bool = True
    verify: bool = True  # 手机上算 SHA-256 与本地比对
    resume: bool = True
    limit: int | None = None  # 最多拉多少个文件
    skip_existing: bool = True  # 已登记且大小一致的直接跳过


@dataclass
class PullResult:
    files: int = 0
    bytes: int = 0
    skipped: int = 0
    failed: int = 0
    resumed: int = 0
    verified: int = 0
    unverified: int = 0
    errors: list[str] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"拉取 {self.files} 个文件（{human_size(self.bytes)}）"]
        if self.resumed:
            parts.append(f"其中 {self.resumed} 个是接着上次继续传的")
        if self.skipped:
            parts.append(f"跳过已下载 {self.skipped} 个")
        if self.verified:
            parts.append(f"{self.verified} 个通过手机端校验和核对")
        if self.unverified:
            parts.append(f"{self.unverified} 个手机不支持校验，只核对了大小")
        if self.failed:
            parts.append(f"{self.failed} 个失败")
        return "；".join(parts)

    def safe_errors(self, limit: int = 10) -> list[str]:
        return self.errors[:limit]


def match_file(f: RemoteFile, opts: PullOptions) -> str | None:
    """判断一个远端文件是否需要拉取，返回类型（image/video/voice/file），不需要则 None。"""
    kind = classify(f.path)
    if kind == "file":
        parts = {p.lower() for p in Path(f.path).parts}
        if parts & {"chatpic", "qq_images", "pic"}: kind = "image"
        elif parts & {"ptt", "qq_ptt"}: kind = "voice"
        elif parts & {"shortvideo", "qq_video"}: kind = "video"
    if opts.kinds and kind not in opts.kinds:
        return None
    if f.size < opts.min_size:
        return None
    if opts.max_size is not None and f.size > opts.max_size:
        return None
    if (opts.start is not None or opts.end is not None) and not f.mtime:
        return None
    if opts.start is not None and f.mtime < opts.start:
        return None
    if opts.end is not None and f.mtime and f.mtime > opts.end:
        return None
    return kind


def _short_tag(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def local_target(root: Path, device: str, kind: str, remote: RemoteFile, *,
                 taken: set[str] | None = None) -> Path:
    """确定本地存放路径：手机媒体/<设备>/<类型>/<年-月>/<文件名>。

    ``taken`` 是本次已经分配给**别的**远端文件的路径集合；同一个远端文件重传时
    仍然用原名（支持覆盖续传），不同远端文件同名时加短哈希，绝不互相覆盖。
    """
    stamp = time.localtime(remote.mtime) if remote.mtime else time.localtime()
    month = time.strftime("%Y-%m", stamp)
    folder = Path(root) / PHONE_DIR_NAME / safe_name(device or "device") / kind / month
    name = safe_name(Path(remote.path).name)
    # Stable across batches, traversal order and separate source directories.
    stem, suffix = Path(name).stem[:120], Path(name).suffix[:20]
    target = folder / f"{stem}_{_short_tag(remote.path)}{suffix}"
    if taken is not None and str(target).lower() in taken:
        import hashlib
        tag = hashlib.sha256(remote.path.encode()).hexdigest()[:20]
        target = folder / f"{stem}_{tag}{suffix}"
    if taken is not None:
        taken.add(str(target).lower())
    return target


# ---------------------------------------------------------------------------
# 断点信息
# ---------------------------------------------------------------------------

def _manifest_path(part: Path) -> Path:
    return Path(str(part) + ".json")


def load_resume(part: Path) -> dict | None:
    """读取断点信息；与远端不一致（大小/修改时间变了）时丢弃重传。"""
    man = _manifest_path(part)
    if not part.exists() or not man.exists():
        return None
    try:
        data = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("received") != part.stat().st_size:
        return None
    return data


def save_resume(part: Path, remote: RemoteFile, received: int) -> None:
    tmp = _manifest_path(part).with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"path": remote.path, "size": remote.size, "mtime": remote.mtime,
                               "received": received}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _manifest_path(part))


def clear_resume(part: Path) -> None:
    for p in (part, _manifest_path(part)):
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 单个文件
# ---------------------------------------------------------------------------

def pull_file(adb: Adb, remote: RemoteFile, target: Path, *, opts: PullOptions | None = None,
              progress: Callable[[int, int], None] | None = None,
              cancel: threading.Event | None = None, verify: bool = True) -> dict:
    """把手机上的一个文件拉到本地（可分块续传、可校验）。

    返回 ``{"path": Path, "bytes": n, "resumed": int, "sha256": str|None,
    "md5": str|None, "verified": bool, "algo": str}``。
    """
    opts = opts or PullOptions()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + _TMP_SUFFIX)
    size, mtime = remote.size, remote.mtime
    if size <= 0:
        st = stat_file(adb, remote.path, cancel=cancel)
        if st is None:
            raise PullError(f"手机上读不到这个文件：{Path(remote.path).name}")
        size, mtime = st.size, st.mtime or remote.mtime

    # 远端校验和（手机支持才算；不支持就只核对大小并如实说明）
    remote_algo, remote_hash = ("", "")
    if verify and opts.verify:
        remote_algo, remote_hash = remote_checksum(adb, remote.path, cancel=cancel)

    offset = 0
    resumed_bytes = 0
    keep = False  # 出错或取消时保留 .part（下次接着传）；正常完成则清掉
    if opts.resume:
        state = load_resume(part)
        if state and state.get("path") == remote.path and state.get("size") == size and int(state.get("mtime") or 0) == int(mtime or 0):
            offset = int(state.get("received") or 0)
            if offset > size:
                offset = 0
            resumed_bytes = offset
    if offset == 0 and part.exists():
        part.unlink(missing_ok=True)

    mode = "r+b" if offset and part.exists() else "wb"
    fh = part.open(mode)
    try:
        if offset:
            fh.seek(offset)
        pos = offset
        while pos < size:
            if cancel is not None and cancel.is_set():
                keep = True
                raise Cancelled("已取消")
            want = min(PULL_CHUNK, size - pos)
            chunk = adb.read_chunk(remote.path, pos, want, cancel=cancel)
            if not chunk:
                keep = True
                raise PullError(f"读取中断：{Path(remote.path).name}")
            if len(chunk) > want:
                raise PullError("手机返回的字节数超过请求范围，传输已停止")
            fh.write(chunk)
            pos += len(chunk)
            if progress:
                progress(pos, size)
            if len(chunk) < want and pos < size:
                # 手机上文件被改动或读取提前结束：留在断点上，下次重试
                keep = True
                raise PullError(f"读取不完整：{Path(remote.path).name}")
            fh.flush()
            os.fsync(fh.fileno())
            save_resume(part, RemoteFile(remote.path, size, mtime), pos)
        fh.flush()
        os.fsync(fh.fileno())
    except BaseException:
        keep = True
        raise
    finally:
        fh.close()

    if part.stat().st_size != size:
        keep = True
        raise PullError(f"大小不符，已保留断点等下次续传：{Path(remote.path).name}")

    md5 = sha256 = None
    if verify and opts.verify:
        md5, sha256 = hash_file(part)
        if remote_algo == "sha256" and remote_hash and sha256 != remote_hash:
            _manifest_path(part).unlink(missing_ok=True)  # Retry from zero, never reuse corrupt bytes.
            raise PullError(f"校验不一致（SHA-256），已保留断点等下次重传：{Path(remote.path).name}")
        if remote_algo == "md5" and remote_hash and md5 != remote_hash:
            _manifest_path(part).unlink(missing_ok=True)  # Retry from zero, never reuse corrupt bytes.
            raise PullError(f"校验不一致（MD5），已保留断点等下次重传：{Path(remote.path).name}")

    after = stat_file(adb, remote.path, cancel=cancel)
    if after is None or after.size != size or (mtime and after.mtime != mtime):
        _manifest_path(part).unlink(missing_ok=True)
        raise PullError("手机文件在传输期间发生变化，请重试")
    os.replace(part, target)
    clear_resume(part)
    if mtime:
        try:
            os.utime(target, (mtime, mtime))
        except OSError:
            pass
    return {"path": target, "bytes": size, "resumed": resumed_bytes, "sha256": sha256,
            "md5": md5, "verified": bool(remote_hash), "algo": remote_algo or ""}


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------

def pull_tree(adb: Adb, remote_dir: str, target_root: Path, *, device: str = "",
              opts: PullOptions | None = None,
              progress: Callable[[int, int, str], None] | None = None,
              on_file: Callable[[dict], None] | None = None,
              status: Callable[[str], None] | None = None,
              archive: Archive | None = None,
              cancel: threading.Event | None = None) -> PullResult:
    """把一个手机目录（含子目录）里的附件拉到电脑。

    传入 ``archive`` 时会跳过已经登记过、大小一致的文件；
    ``on_file`` 每成功一个文件回调一次，便于即时登记进归档库。
    """
    opts = opts or PullOptions()
    res = PullResult()
    if status:
        status(f"正在读取手机目录：{remote_dir}")
    files = list_files(adb, remote_dir, recursive=opts.recursive, cancel=cancel)
    want: list[tuple[RemoteFile, str]] = []
    for f in files:
        kind = match_file(f, opts)
        if kind:
            want.append((f, kind))
    total = len(want)
    if opts.limit is not None:
        want = want[:opts.limit]
        total = len(want)
    taken: set[str] = set()
    known = {}
    if archive is not None and opts.skip_existing:
        known = {r["device_path"]: dict(r) for r in archive.con.execute(
            "SELECT * FROM media_files WHERE origin='phone' AND device=? AND status='done'", (device,))}
    for i, (f, kind) in enumerate(want, 1):
        if cancel is not None and cancel.is_set():
            break
        name = Path(f.path).name
        if progress:
            progress(i, total, name)
        target = local_target(target_root, device, kind, f, taken=taken)
        previous = known.get(f.path)
        if previous and previous["size"] == f.size and previous["mtime"] == f.mtime:
            existing = archive.resolve_media_path(previous["local_path"])
            if existing.is_file() and existing.stat().st_size == f.size:
                if opts.verify:
                    algo, digest = remote_checksum(adb, f.path, cancel=cancel)
                    local_md5, local_sha = hash_file(existing)
                    matches = bool(digest) and digest == (local_sha if algo == "sha256" else local_md5)
                else:
                    matches = True
                if matches:
                    res.skipped += 1
                    continue
        try:
            info = pull_file(adb, f, target, opts=opts, cancel=cancel)
        except Cancelled:
            break
        except (AdbError, PullError, OSError) as exc:
            res.failed += 1
            res.errors.append(f"{name}：{exc}")
            continue
        res.files += 1
        res.bytes += info["bytes"]
        if info["resumed"]:
            res.resumed += 1
        if info["verified"]:
            res.verified += 1
        else:
            res.unverified += 1
        record = {"device": device, "remote_path": f.path, "local_path": str(info["path"]),
                  "name": name, "kind": kind, "size": info["bytes"], "mtime": f.mtime,
                  "sha256": info["sha256"], "md5": info["md5"], "algo": info["algo"]}
        res.records.append(record)
        if on_file:
            on_file(record)
    return res


def register_pulled(archive: Archive, record: dict) -> None:
    """把拉取到的文件登记进归档库（同 origin+device+device_path 更新，不重复插入）。"""
    local = Path(record["local_path"])
    try:
        rel = archive.relative_media_path(local)
    except (OSError, ValueError):
        rel = str(local)
    with archive.con:
        archive.con.execute(
            "INSERT INTO media_files(origin, device, device_path, local_path, name, kind, size, mtime, "
            "sha256, md5, status, verified, pulled_at) VALUES(?,?,?,?,?,?,?,?,?,?,'done',?,?) "
            "ON CONFLICT(origin, device, device_path) DO UPDATE SET local_path=excluded.local_path, "
            "size=excluded.size, mtime=excluded.mtime, sha256=coalesce(excluded.sha256, media_files.sha256), "
            "md5=coalesce(excluded.md5, media_files.md5), status='done', verified=excluded.verified, "
            "pulled_at=excluded.pulled_at",
            (REMOTE_ORIGIN, record.get("device", ""), record["remote_path"], rel, record["name"],
             record["kind"], record["size"], record["mtime"], record.get("sha256"),
             record.get("md5"), record.get("algo") or "", now()),
        )


def pulled_count(archive: Archive, device: str = "") -> int:
    sql = "SELECT count(*) AS n FROM media_files WHERE origin=?"
    args: list = [REMOTE_ORIGIN]
    if device:
        sql += " AND device=?"
        args.append(device)
    return archive.con.execute(sql, args).fetchone()["n"]


def known_files(archive: Archive, device: str = "") -> dict[str, int]:
    """已登记的远端路径 → 大小，用于跳过重复下载。"""
    sql = "SELECT device_path, size FROM media_files WHERE origin=?"
    args: list = [REMOTE_ORIGIN]
    if device:
        sql += " AND device=?"
        args.append(device)
    return {r["device_path"]: (r["size"] or 0) for r in archive.con.execute(sql, args)}


def pull_qq_media(adb: Adb, dirs: Iterable[dict], device: str, data_dir: Path, *,
                  archive: Archive | None = None, opts: PullOptions | None = None,
                  progress: Callable[[int, int, str], None] | None = None,
                  status: Callable[[str], None] | None = None,
                  cancel: threading.Event | None = None) -> PullResult:
    """把手机上找到的多个 QQ 附件目录合并拉取，可顺带登记进归档库。"""
    opts = opts or PullOptions()
    total = PullResult()
    for d in dirs:
        if cancel is not None and cancel.is_set():
            break
        if not d.get("path"):
            continue
        if status:
            status(f"正在转移手机上的{d.get('kind', '')}：{d['path']}")
        one = pull_tree(adb, d["path"], Path(data_dir), device=device, opts=opts,
                        progress=progress, status=status, cancel=cancel, archive=archive,
                        on_file=(lambda rec: register_pulled(archive, rec)) if archive else None)
        total.files += one.files
        total.bytes += one.bytes
        total.skipped += one.skipped
        total.failed += one.failed
        total.resumed += one.resumed
        total.verified += one.verified
        total.unverified += one.unverified
        total.errors += one.errors
        total.records += one.records
    return total


def has_adb_files(data_dir: Path) -> list[Path]:
    """列出还没传完的断点文件（界面上可以提示“还有 N 个文件没传完”）。"""
    root = Path(data_dir) / PHONE_DIR_NAME
    if not root.exists():
        return []
    return sorted(root.rglob("*" + _TMP_SUFFIX))
