"""自动取钥与手机 USB 转移的实机自检脚本（不写任何数据）。

用途：在真实环境里确认这两条链路能不能用，并把结果打印成能直接看的报告。

    .\\.venv\\Scripts\\python.exe scripts\\verify_key_and_phone.py
    .\\.venv\\Scripts\\python.exe scripts\\verify_key_and_phone.py --no-phone
    .\\.venv\\Scripts\\python.exe scripts\\verify_key_and_phone.py --adb "D:\\platform-tools\\adb.exe"

做两件事：

1. **自动取钥（FR-03）**：列出正在运行的 QQ 进程，对每个电脑 QQ 库读出第 1 页的盐，
   扫描 QQ 进程内存取出候选密钥，并用数据库第 1 页 HMAC 校验。全程只读内存、
   不改 QQ 的任何文件。报告里**不会出现完整密钥**（只显示前 8 位）。
2. **手机附件（FR-10）**：找 adb、列设备、列出手机 QQ 的图片/视频/语音/文件目录
   （只读命令），并可选做一次小文件的实际拉取与校验。

如果第 1 步失败，脚本会明确告诉你是“该账号没登录”还是“内存布局对不上”。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qqarchive import keyscan  # noqa: E402
from qqarchive.adb import (Adb, AdbError, adb_hint, find_adb, list_files, list_qq_dirs,  # noqa: E402
                           remote_checksum)
from qqarchive.config import load_config  # noqa: E402
from qqarchive.discovery import discover, default_qq_roots  # noqa: E402
from qqarchive.media_pull import PullOptions, human_size, pull_file  # noqa: E402
from qqarchive.winmem import ProcessInfo, find_processes  # noqa: E402

LINE = "=" * 72


def say(text: str = "") -> None:
    print(text, flush=True)


def section(title: str) -> None:
    say()
    say(LINE)
    say(f"  {title}")
    say(LINE)


# ---------------------------------------------------------------------------
# 一、自动取钥
# ---------------------------------------------------------------------------

def check_key_extraction(cfg) -> tuple[bool, list]:
    section("一、自动取钥（读 QQ 进程内存取出数据库密钥并校验）")

    procs: list[ProcessInfo] = find_processes(cfg.auto_key_process_names or ["QQ.exe"])
    if not procs:
        say("✗ 没有检测到正在运行的电脑 QQ。")
        say("  请先启动并**登录**电脑 QQ，再运行本脚本。")
        return False, []
    say(f"✓ 检测到 {len(procs)} 个 QQ 进程：{', '.join(str(p) for p in procs)}")

    cands = [c for c in discover(default_qq_roots(), []) if c.needs_key]
    if not cands:
        say("✗ 没有找到加密的电脑 QQ 数据库（nt_msg.db）。")
        say("  默认位置：文档\\Tencent Files\\<QQ号>\\nt_qq\\nt_db\\nt_msg.db")
        return False, []
    say(f"✓ 找到 {len(cands)} 个待取钥的数据库：")
    for c in cands:
        size = human_size(c.size)
        say(f"    QQ {c.owner or '(未知账号)'}  {size}  {c.path}")

    targets = keyscan.targets_from_candidates(cands)
    if not targets:
        say("✗ 这些数据库都读不出第 1 页信息，可能不是 SQLCipher 库。")
        return False, []

    window = max(1, int(cfg.auto_key_window_mb)) * 1024 * 1024
    say()
    say(f"开始扫描内存（每个进程读 {window // 1024 // 1024} MB 一块，只读）…")
    started = time.time()
    res = keyscan.extract_keys(
        targets, names=tuple(cfg.auto_key_process_names or ["QQ.exe"]), window=window,
        max_pids=max(1, int(cfg.auto_key_max_pids)),
        status=lambda s: say("  " + s),
        progress=lambda done, total: None,
    )
    say(f"  扫描完成：{res.summary()}")

    hits = keyscan.hits_from_result(targets, res)
    if hits:
        say()
        say("✓ 取钥成功（密钥已通过数据库第 1 页 HMAC 校验）：")
        for h in hits:
            say(f"    {h.describe()}")
        say()
        say("  提示：完整密钥没有打印出来。程序里“自动获取密钥”成功后可直接用于导入。")
        return True, hits

    say()
    say("✗ 没有取到能通过校验的密钥。原因诊断：")
    for line in keyscan.diagnose(targets, names=tuple(cfg.auto_key_process_names or ["QQ.exe"])):
        say("    " + line)
    say()
    say("  也可以继续用“手动填写密钥”：在别处取到密钥后粘贴进程序即可。")
    return False, []


# ---------------------------------------------------------------------------
# 二、手机附件
# ---------------------------------------------------------------------------

def check_phone(cfg, do_pull: bool) -> bool:
    section("二、手机附件 USB 转移（只读手机，分块续传，SHA-256 校验）")

    adb_path = find_adb(cfg.adb_path)
    if not adb_path:
        say("✗ " + adb_hint())
        return False
    say(f"✓ 找到 adb：{adb_path}")

    try:
        adb = Adb(adb_path)
        say(f"  adb 版本：{adb.version() or '(未知)'}")
        devices = adb.list_devices()
    except AdbError as exc:
        say(f"✗ {exc}")
        return False
    if not devices:
        say("✗ " + adb.describe_problem())
        return False
    for d in devices:
        mark = "✓" if d.ready else "✗"
        say(f"  {mark} 设备 {d.label} 状态={d.state}")
    ready = [d for d in devices if d.ready]
    if not ready:
        say("✗ " + adb.describe_problem())
        return False

    adb.serial = ready[0].serial
    info = adb.device_info()
    say(f"✓ 使用设备：{info.get('model') or adb.serial}"
        f"（Android {info.get('android') or '未知'}，序列号 {adb.serial}）")

    say()
    say("正在列出手机 QQ 的附件目录（只读命令）…")
    dirs = list_qq_dirs(adb)
    if not dirs:
        say("✗ 没有找到 QQ 的附件目录。")
        say("  请确认：手机已解锁、QQ 里确实有图片/视频/文件，")
        say("  并且 USB 用途选的是“传输文件（MTP）”。")
        return False
    total = 0
    for d in dirs:
        total += d["bytes"]
        say(f"    {d['kind']}目录 {d['path']}：{d['count']} 个文件（{human_size(d['bytes'])}）")
    say(f"  合计：{sum(d['count'] for d in dirs)} 个文件，{human_size(total)}")

    filenames = [f for d in dirs for f in list_files(adb, d["path"], recursive=False)][:3]
    say()
    say("抽查文件（大小 + 手机端 SHA-256）：")
    for f in filenames:
        algo, digest = remote_checksum(adb, f.path)
        shown = f"{digest[:16]}…" if digest else "(手机不支持校验和，导入时只核对大小)"
        say(f"    {Path(f.path).name}  {human_size(f.size)}  {algo or '-'} {shown}")

    if not do_pull or not filenames:
        say()
        say("（本次未做实际拉取。加 --pull 可以真的拉一个文件下来做完整校验。）")
        return True

    import tempfile
    say()
    say("实际拉取一个小文件做完整校验（写入临时目录，测试后保留供查看）…")
    target_dir = Path(tempfile.mkdtemp(prefix="qqarchive-verify-"))
    f = min(filenames, key=lambda x: x.size)
    try:
        result = pull_file(adb, f, target_dir / Path(f.path).name,
                           opts=PullOptions(kinds=("image", "video", "voice", "file"),
                                            limit=1, min_size=0))
    except Exception as exc:  # noqa: BLE001 — 自检脚本要如实报告任何失败
        say(f"✗ 拉取失败：{type(exc).__name__}: {exc}")
        return False
    ok = result["path"].read_bytes()
    say(f"✓ 拉取成功：{result['path']}")
    say(f"    大小 {human_size(len(ok))}，手机端校验 {result['algo'] or '-'}，"
        f"本地 SHA-256 {result['sha256'][:16] if result['sha256'] else '(未计算)'}…")
    say(f"    校验结果：{'一致' if result['verified'] else '手机不支持校验和，已按大小核对'}")
    return True


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="自动取钥与手机 USB 转移自检")
    ap.add_argument("--no-key", action="store_true", help="跳过自动取钥检查")
    ap.add_argument("--no-phone", action="store_true", help="跳过手机检查")
    ap.add_argument("--pull", action="store_true", help="手机检查时真的拉一个文件下来校验")
    ap.add_argument("--adb", default="", help="指定 adb.exe 路径")
    args = ap.parse_args()

    cfg = load_config(apply_pending=False)
    if args.adb:
        cfg.adb_path = args.adb

    say("QQ 留档 v10 — 自动取钥 / 手机转移 自检")
    say(f"数据目录：{cfg.data_path}")
    say("说明：本脚本只读 QQ 进程内存与手机存储，不修改、不删除任何东西。")

    key_ok = True
    if not args.no_key:
        key_ok, _hits = check_key_extraction(cfg)
    phone_ok = True
    if not args.no_phone:
        phone_ok = check_phone(cfg, args.pull)

    section("结论")
    if not args.no_key:
        say(f"自动取钥：{'✓ 可用' if key_ok else '✗ 未成功（原因见上）'}")
    if not args.no_phone:
        say(f"手机转移：{'✓ 可用' if phone_ok else '✗ 未成功（原因见上）'}")
    say()
    say("把上面的输出（不含密钥）反馈给开发者即可定位问题。")
    return 0 if (key_ok and phone_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
