"""来源编排：打开来源、分析会话、按会话与时间段导入（FR-01/02/04/05）。"""

from __future__ import annotations

import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .discovery import SourceCandidate, source_fingerprint
from .importer import Importer, ImportRequest, ImportStats
from .keystore import KeyStore
from .ntqq import ConversationInfo, Filter, NtqqSource
from .sqlcipher import CipherError, CipherParams, Key
from .store import Archive
from .source_guard import StableSource
import apsw


class KeyRequired(Exception):
    """来源已加密，但还没有可用的密钥。"""


class UnsupportedSource(KeyRequired):
    """来源已识别，但当前版本不支持导入（附原因）；既不是缺密钥，也不是导入失败。

    继承 KeyRequired 只为兼容既有调用方；需要区分的调用方必须先捕获本类。"""


@contextmanager
def snapshot(path: Path, work_dir: Path, enabled: bool = True):
    """把（仍加密的）库文件与 -wal 复制到数据目录的临时区再读取，避免 QQ 运行中写入造成读到半页。

    复制前后大小与修改时间不一致时重试。用完即删除。复制品仍是加密数据。
    """
    if not enabled:
        yield Path(path)
        return
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="snap-", dir=work_dir))
    try:
        src = Path(path)
        for _ in range(5):
            before = [(p.stat().st_size, p.stat().st_mtime_ns) for p in (src, Path(str(src) + "-wal")) if p.exists()]
            shutil.copy2(src, tmp / "nt_msg.db")
            wal = Path(str(src) + "-wal")
            (tmp / "nt_msg.db-wal").unlink(missing_ok=True)
            if wal.exists():
                shutil.copy2(wal, tmp / "nt_msg.db-wal")
            after = [(p.stat().st_size, p.stat().st_mtime_ns) for p in (src, Path(str(src) + "-wal")) if p.exists()]
            if before == after:
                break
        else:
            raise OSError("QQ 数据库持续变化，无法取得一致快照；请退出 QQ 后重试")
        yield tmp / "nt_msg.db"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def open_ntqq(candidate: SourceCandidate, keystore: KeyStore | None, key: Key | None = None,
              params: CipherParams | None = None, path: Path | None = None) -> NtqqSource:
    if candidate.kind=='official-backup':
        from .official_backup import LIMITATION
        raise UnsupportedSource(LIMITATION)
    path = path or Path(candidate.path)
    if not candidate.needs_key:
        return NtqqSource(path, candidate.owner)
    tries: list[tuple[Key, CipherParams | None]] = []
    if key is not None:
        tries.append((key, params))
    if keystore is not None:
        tries += keystore.candidates(candidate.owner,candidate.path)
    if not tries:
        raise KeyRequired("需要先填写该数据库的密钥")
    last = None
    for k, p in tries:
        try:
            src = NtqqSource(path, candidate.owner, k, p)
            if keystore is not None and candidate.owner:
                keystore.put(candidate.owner, k, src.params, remember=False)
            return src
        except CipherError as exc:
            last = exc
            if p is not None:
                try:  # 保存的参数可能因文件头偏移不同而不适用，再自动探测一次
                    src = NtqqSource(path, candidate.owner, k, None)
                    return src
                except CipherError as exc2:
                    last = exc2
    raise KeyRequired("已有密钥都无法打开该数据库，请重新填写") from last


def analyze(candidate: SourceCandidate, keystore: KeyStore | None, work_dir: Path,
            use_snapshot: bool = False, cancel=None) -> list[ConversationInfo]:
    before = source_fingerprint(Path(candidate.path))
    with StableSource(candidate.path), snapshot(Path(candidate.path), work_dir, use_snapshot) as p:
        with open_ntqq(candidate, keystore, path=p) as src:
            src.set_cancel(cancel)
            result = src.conversations()
            if not use_snapshot and source_fingerprint(Path(candidate.path)) != before:
                raise OSError('读取期间 QQ 数据库发生变化；请保留本程序，退出 QQ 后重试')
            return result


def import_candidate(archive: Archive, candidate: SourceCandidate, keystore: KeyStore | None, *,
                     owner: str | None = None, flt: Filter | None = None, work_dir: Path,
                     use_snapshot: bool = False, progress: Callable[[ImportStats, int], None] | None = None,
                     cancel: threading.Event | None = None) -> ImportStats:
    if candidate.kind == "package":
        from .package import import_package
        return import_package(archive, Path(candidate.path), progress=progress, cancel=cancel, flt=flt)
    owner = owner or candidate.owner
    if not owner:
        raise ValueError("请先指定该来源所属的 QQ 号（归档账号）")
    candidate.owner = owner
    fp = source_fingerprint(Path(candidate.path))
    with StableSource(candidate.path) as stable, snapshot(Path(candidate.path), work_dir, use_snapshot and candidate.kind == "pc") as p:
        with open_ntqq(candidate, keystore, path=p) as src:
            src.set_cancel(cancel)
            # Large QQ sources are streamed once; unknown totals stay indeterminate.
            total = 0 if src.table_reader else src.count(flt)
            req = ImportRequest(
                source_kind={"pc": "ntqq-pc", "backup": "ntqq-backup", "plain": "ntqq-plain"}.get(candidate.kind,
                                                                                                   "ntqq"),
                source_label=candidate.label,
                source_fingerprint=fp if flt is None or (flt.start is None and flt.end is None
                                                         and flt.conversations is None) else "",
                owner=owner,
                range_start=flt.start if flt else None,
                range_end=flt.end if flt else None,
                params={
                    "conversations": sorted(f"{k}:{p}" for k, p in flt.conversations) if flt and flt.conversations
                    is not None else "all",
                    "cipher": src.params.describe() if src.params else "plain",
                },
            )
            cb = (lambda s: progress(s, total)) if progress else None
            def guarded_messages():
                yield from src.messages(flt)
                stable.check()
                if not use_snapshot and source_fingerprint(Path(candidate.path)) != fp:
                    raise OSError('读取期间 QQ 数据库发生变化；本批导入已撤销，请保留本程序，退出 QQ 后重试')
            return Importer(archive).run(req, guarded_messages(), progress=cb, cancel=cancel)
