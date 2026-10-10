"""自动化流程：启动时检查有什么可以更新，
一键完成“自动取钥 → 导入聊天记录 → 登记附件 → 处理收件箱 → 关联附件”。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .discovery import SourceCandidate, default_qq_roots, discover
from .keystore import KeyStore, check_raw_key
from .media import INBOX_NAME, LIBRARY_NAME, auto_register_qq_media, inbox_files, ingest_inbox, link_media
from .sources import KeyRequired, UnsupportedSource, import_candidate
from .store import Archive


def inbox_dir(data_dir: Path) -> Path:
    return Path(data_dir) / INBOX_NAME


def library_dir(data_dir: Path) -> Path:
    return Path(data_dir) / LIBRARY_NAME


def phone_media_root(data_dir: Path) -> Path:
    """手机附件落地的根目录（媒体库的一部分）。"""
    from .media_pull import PHONE_DIR_NAME
    return Path(data_dir) / PHONE_DIR_NAME


@dataclass
class CheckResult:
    sources: list[SourceCandidate] = field(default_factory=list)
    ready: list[SourceCandidate] = field(default_factory=list)  # 有新内容且已有密钥
    need_key: list[SourceCandidate] = field(default_factory=list)  # 有新内容但缺密钥
    unsupported: list[SourceCandidate] = field(default_factory=list)  # 已识别但不支持导入，不是缺密钥
    inbox_count: int = 0

    @property
    def has_work(self) -> bool:
        return bool(self.ready or self.inbox_count)

    def message(self) -> str:
        parts = []
        if self.ready:
            parts.append(f"发现 {len(self.ready)} 处聊天记录有新内容，可以一键更新")
        if self.need_key:
            parts.append(f"{len(self.need_key)} 处聊天记录需要先填写密钥")
        if self.unsupported:
            parts.append(f"{len(self.unsupported)} 处 QQ 官方备份暂不支持导入")
        if self.inbox_count:
            parts.append(f"收件箱里有 {self.inbox_count} 个新文件等待整理")
        return "；".join(parts) or "所有聊天记录都是最新的"


def qq_roots(cfg: Config) -> list[Path]:
    roots = [Path(r) for r in cfg.qq_roots] or default_qq_roots()
    return roots


def check(cfg: Config, archive: Archive, keystore: KeyStore) -> CheckResult:
    res = CheckResult()
    res.sources = discover(qq_roots(cfg), cfg.backup_dirs, archive, keystore)
    for c in res.sources:
        if c.kind == "official-backup":
            # Recognised but not importable: never offer it as "needs a key".
            res.unsupported.append(c)
            continue
        if c.status == "imported" or c.kind == "package":
            continue
        if not c.needs_key or c.key_status in ("session", "saved"):
            if c.owner:
                res.ready.append(c)
        else:
            res.need_key.append(c)
    res.inbox_count = len(inbox_files(inbox_dir(cfg.data_path)))
    return res


@dataclass
class RunReport:
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.lines + [f"未完成：{e}" for e in self.errors])


# ---------------------------------------------------------------------------
# 自动取钥（FR-03）
# ---------------------------------------------------------------------------

@dataclass
class KeyAcquireReport:
    """自动取钥的结果，可以安全地显示与记录（不含完整密钥）。"""

    found: int = 0
    accounts: list[str] = field(default_factory=list)
    scanned_mb: int = 0
    seconds: float = 0.0
    process_count: int = 0
    lines: list[str] = field(default_factory=list)  # 给用户看的说明
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.found > 0

    def text(self) -> str:
        parts = list(self.lines)
        if self.errors:
            parts += self.errors
        return "\n".join(parts)


def key_warning() -> str:
    """执行自动取钥之前必须让用户看到的话（需求 5.5 节）。"""
    return ("自动获取密钥会读取正在运行的电脑 QQ 的内存（只读，不修改 QQ 的任何文件、"
            "取钥阶段不结束 QQ 进程、不联网上传）。读取过程中 QQ 可能短暂卡顿，极小概率闪退，"
            "重新打开即可。取到的密钥只保存在本机，用于解密你本人的聊天记录。")


def acquire_keys(cfg: Config, candidates: list[SourceCandidate], keystore: KeyStore, *,
                 remember: bool | None = None,
                 status: Callable[[str, int, int], None] | None = None,
                 cancel: threading.Event | None = None) -> KeyAcquireReport:
    """从正在运行的电脑 QQ 内存中取出这些来源需要的密钥，并用数据库页面校验。

    只处理 ``needs_key`` 的来源；成功后会写进 ``keystore``（本次运行有效，
    ``remember`` 为真时按用户设置写入本机加密文件）。
    """
    from . import keyscan

    rep = KeyAcquireReport()
    targets_src = [c for c in candidates if c.needs_key]
    if not targets_src:
        rep.lines.append("这些来源都不需要密钥。")
        return rep
    say = status or (lambda text, done, total: None)
    if not cfg.auto_key_enabled:
        rep.errors.append("自动取钥已在设置中关闭，请手动填写密钥。")
        return rep
    remember = cfg.remember_keys if remember is None else remember
    names = tuple(cfg.auto_key_process_names or ["QQ.exe"])
    window = max(1, int(cfg.auto_key_window_mb)) * 1024 * 1024
    say("正在读取 QQ 进程内存…", 0, 0)
    from .profiles import related_sources
    primary_paths={str(Path(c.path).resolve()).casefold() for c in targets_src}
    extra=[p for c in targets_src for p in related_sources(c) if p.needs_key]
    targets = keyscan.targets_from_candidates(targets_src+extra)
    if not targets:
        rep.errors.append("这些来源都读不出数据库信息，无法自动取钥。")
        return rep
    res = keyscan.extract_keys(
        targets, names=names, window=window, max_pids=max(1, int(cfg.auto_key_max_pids)),
        cancel=cancel,
        status=lambda s: say(s, 0, 0),
        progress=lambda done, total: say("正在读取 QQ 进程内存…", done, total),
    )
    rep.scanned_mb = int(res.bytes_scanned / 1048576)
    rep.seconds = res.seconds
    rep.process_count = len(res.pids_scanned)
    rep.errors += res.errors
    hits = keyscan.hits_from_result(targets, res)
    for hit in hits:
        checked = check_raw_key(hit.db_path, hit.key_hex)
        if not checked:
            continue
        key, params = checked
        if hit.owner:
            keystore.put_source(hit.owner, hit.db_path, key, params, remember=remember)
            if hit.owner not in rep.accounts:
                rep.accounts.append(hit.owner)
        if str(Path(hit.db_path).resolve()).casefold() in primary_paths:rep.found += 1
    if rep.found:
        who = "、".join(rep.accounts) if rep.accounts else "已发现的账号"
        rep.lines.append(f"自动获取密钥成功（{who}）：已通过数据库校验，"
                         f"扫描 {rep.process_count} 个 QQ 进程、{rep.scanned_mb} MB 内存、"
                         f"用时 {rep.seconds:.1f} 秒。")
        if not remember:
            rep.lines.append("密钥只在本次运行有效；如需长期保存，请在设置里勾选“记住密钥”。")
    else:
        rep.lines.append(f"自动获取密钥没有成功（扫描 {rep.process_count} 个 QQ 进程、"
                         f"{rep.scanned_mb} MB 内存、用时 {rep.seconds:.1f} 秒）。")
        rep.lines += keyscan.diagnose(targets, names=names, cancel=cancel)
        rep.lines.append("可手动填写密钥，使用已经取得的数据库密钥。QQ 的关于页面不提供数据库密钥。")
    return rep


def run_all(cfg: Config, archive: Archive, keystore: KeyStore, check_result: CheckResult, *,
            status: Callable[[str, int, int], None] | None = None,
            cancel: threading.Event | None = None, auto_key: bool = True) -> RunReport:
    rep = RunReport()
    say = status or (lambda text, done, total: None)
    work = cfg.data_path / "tmp"
    todo = list(check_result.ready)
    if check_result.unsupported:
        from .official_backup import LIMITATION, ALTERNATIVE
        rep.lines.append(f"未导入 {len(check_result.unsupported)} 处 QQ 官方备份：暂不支持导入（{LIMITATION}）{ALTERNATIVE}")
    if auto_key and check_result.need_key:
        say("正在自动获取密钥…", 0, 0)
        key_rep = acquire_keys(cfg, check_result.need_key, keystore, status=status, cancel=cancel)
        rep.lines += key_rep.lines
        rep.errors += [e for e in key_rep.errors if "已整批撤销" not in e]
        if key_rep.ok:
            recovered = [c for c in check_result.need_key
                         if c.owner and keystore.has(c.owner) in ("session", "saved")]
            todo += recovered
    for c in todo:
        if cancel is not None and cancel.is_set():
            break
        say(f"正在更新 {c.owner} 的聊天记录…", 0, 0)
        try:
            stats = import_candidate(
                archive, c, keystore, work_dir=work, cancel=cancel,
                progress=lambda s, total: say(f"正在读取 {c.owner} 的聊天记录", s.read, total),
            )
            rep.lines.append(f"QQ {c.owner}：新增 {stats.inserted} 条消息（共读取 {stats.read} 条）")
        except UnsupportedSource as exc:
            rep.errors.append(f"QQ {c.owner} 的来源暂不支持导入：{exc}")
            continue
        except KeyRequired:
            rep.errors.append(f"QQ {c.owner} 的密钥无法打开数据库，请重新填写")
            continue
        except Exception as exc:  # 单个来源失败不影响其他来源
            rep.errors.append(f"QQ {c.owner} 导入失败（{type(exc).__name__}），已整批撤销，数据未受影响")
            continue
        dirs = c.extra.get("media_dirs") or []
        if dirs:
            say(f"正在登记 {c.owner} 的图片和视频…", 0, 0)
            n = auto_register_qq_media(archive, dirs, cancel=cancel)
            rep.lines.append(f"QQ {c.owner}：登记电脑 QQ 里的附件 {n} 个")
    if check_result.inbox_count:
        say("正在整理收件箱…", 0, check_result.inbox_count)
        r = ingest_inbox(archive, inbox_dir(cfg.data_path), library_dir(cfg.data_path), cancel=cancel,
                         progress=lambda i, t, name: say("正在整理收件箱", i, t))
        rep.lines.append("收件箱：" + r.summary())
    say("正在把图片、视频和聊天记录对应起来…", 0, 0)
    try:
        linked = link_media(archive,cancel=cancel,progress=lambda i,t,s:say(s,i,t))
        rep.lines.append(f"附件对应：找到 {linked['linked']} 个，仍缺 {linked['missing']} 个")
    except InterruptedError:rep.errors.append('附件关联已取消，已导入正文保留')
    return rep


# ---------------------------------------------------------------------------
# USB 转移手机附件（FR-10）
# ---------------------------------------------------------------------------

@dataclass
class PhoneReport:
    device: str = ""
    dirs: list[dict] = field(default_factory=list)
    files: int = 0
    bytes: int = 0
    skipped: int = 0
    failed: int = 0
    resumed: int = 0
    verified: int = 0
    unverified: int = 0
    errors: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.lines + [f"未完成：{e}" for e in self.errors])


def _pull_phone_media(cfg: Config, archive: Archive | None = None, *, kinds=None, days: int | None = None,
                     device: str = "", status: Callable[[str, int, int], None] | None = None,
                     cancel: threading.Event | None = None, progress=None) -> PhoneReport:
    """一键把手机 QQ 的图片、视频、语音、文件通过 USB 拉到电脑。

    只读手机；分块续传；每个文件核对 SHA-256；可按类型与最近天数筛选。
    """
    import time as _time

    from .adb import Adb, AdbError, adb_hint, find_adb, list_qq_dirs
    from .media_pull import PullOptions, human_size, pull_qq_media
    rep = PhoneReport()
    say = status or (lambda text, done, total: None)
    path = find_adb(cfg.adb_path)
    if not path:
        rep.errors.append(adb_hint())
        return rep
    try:
        adb = Adb(path, device or cfg.phone_device)
        if not device and not cfg.phone_device:
            ready = adb.authorized_devices()
            if not ready:
                rep.errors.append(adb.describe_problem())
                return rep
            if len(ready) > 1:
                rep.errors.append("检测到多台设备，请先选择手机")
                return rep
            adb.serial = ready[0].serial
        info = adb.device_info()
        rep.device = adb.serial
        rep.lines.append(f"手机：{info.get('model') or adb.serial}"
                         f"（Android {info.get('android') or '未知'}，序列号 {adb.serial}）")
    except AdbError as exc:
        rep.errors.append(str(exc))
        return rep

    say("正在查看手机上有没有 QQ 的图片和文件…", 0, 0)
    try:
        dirs = list_qq_dirs(adb, cancel=cancel, include_app_storage=cfg.phone_include_cache)
    except AdbError as exc:
        rep.errors.append(str(exc))
        return rep
    if not dirs:
        rep.errors.append("手机上没有找到 QQ 的附件目录。请确认手机已解锁、已授权 USB 调试，"
                          "并且手机 QQ 里确实有图片/视频/文件。")
        return rep
    rep.dirs = dirs
    for d in dirs:
        rep.lines.append(f"发现{d['kind']}目录 {d['path']}：{d['count']} 个文件（{human_size(d['bytes'])}）")

    opts = PullOptions()
    if kinds:
        opts.kinds = tuple(kinds)
    elif cfg.phone_kinds:
        opts.kinds = tuple(cfg.phone_kinds)
    use_days = cfg.phone_days if days is None else days
    if use_days and use_days > 0:
        opts.start = int(_time.time()) - use_days * 86400
    opts.verify = bool(cfg.phone_verify)

    from .media_pull import pull_qq_media
    try:
        res = pull_qq_media(adb, dirs, rep.device, cfg.data_path, archive=archive, opts=opts,
                            progress=progress or (lambda i, t, name: say(f"正在传输第 {i}/{t} 个文件：{name}", i, t)),
                            status=lambda s: say(s, 0, 0), cancel=cancel)
    except AdbError as exc:
        rep.errors.append(str(exc))
        return rep
    rep.files, rep.bytes, rep.skipped, rep.failed = res.files, res.bytes, res.skipped, res.failed
    rep.resumed, rep.verified, rep.unverified = res.resumed, res.verified, res.unverified
    rep.errors += res.safe_errors()
    rep.lines.append("手机附件：" + res.summary())
    if cancel is not None and cancel.is_set():
        rep.errors.append("传输已取消，完成的文件已保存，下次可继续")
    if archive is not None and not (cancel and cancel.is_set()):
        try:
            linked = link_media(archive,cancel=cancel,progress=lambda i,t,s:say(s,i,t))
            rep.lines.append(f"附件关联：{linked['linked']} 个已关联，{linked['missing']} 个缺失")
        except InterruptedError:rep.errors.append('附件关联已取消，已完成的传输保留')
    return rep

def pull_phone_media(cfg,archive=None,**kwargs):
    import time
    started=int(time.time());rep=None;failure=''
    try:
        rep=_pull_phone_media(cfg,archive,**kwargs);return rep
    except Exception as exc:
        failure=str(exc);raise
    finally:
        if archive is not None:
            cancelled=bool(kwargs.get('cancel') and kwargs['cancel'].is_set())
            status='cancelled' if cancelled else 'failed' if failure else 'partial' if rep and rep.errors else 'done'
            with archive.con:archive.con.execute('INSERT INTO phone_runs(started_at,finished_at,status,files,bytes,skipped,failed,summary) VALUES(?,?,?,?,?,?,?,?)',
                (started,int(time.time()),status,rep.files if rep else 0,rep.bytes if rep else 0,rep.skipped if rep else 0,rep.failed if rep else 0,rep.text() if rep else failure))
