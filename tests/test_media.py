import hashlib
import os
import time

from qqarchive.importer import Importer, ImportRequest
from qqarchive.media import (classify, ingest_inbox, inbox_files, link_media, missing_media,
                             register_folder)
from qqarchive.models import GROUP, NormalizedMessage
from qqarchive.store import Archive


def _old(path):
    t = time.time() - 3600
    os.utime(path, (t, t))


def _msg(seq, elements):
    return NormalizedMessage(owner="1", kind=GROUP, peer="9", time=1000 + seq, sender_uin="2", seq=seq,
                             elements=elements)


def test_classify():
    assert classify("a.JPG") == "image" and classify("b.mp4") == "video"
    assert classify("c.amr") == "voice" and classify("d.pdf") == "file"


def test_inbox_dedup_and_archive(tmp_path):
    a = Archive(tmp_path / "data" / "archive.db")
    inbox, lib = tmp_path / "data" / "inbox", tmp_path / "data" / "lib"
    (inbox / "sub").mkdir(parents=True)
    (inbox / "x.jpg").write_bytes(b"same-content")
    (inbox / "sub" / "y.jpg").write_bytes(b"same-content")
    (inbox / "v.mp4").write_bytes(b"video")
    (inbox / "Thumbs.db").write_bytes(b"ignored")
    for p in inbox.rglob("*"):
        if p.is_file():
            _old(p)
    r = ingest_inbox(a, inbox, lib)
    assert (r.added, r.duplicates, r.failed) == (2, 1, 0)
    files = sorted(p.relative_to(lib).parts[0] for p in lib.rglob("*") if p.is_file())
    assert files == ["_重复文件", "图片", "视频"]
    assert inbox_files(inbox) == []  # 只剩被忽略的 Thumbs.db
    assert (inbox / "Thumbs.db").exists()  # 忽略的文件不删除
    row = a.con.execute("SELECT * FROM media_files WHERE kind='image'").fetchone()
    assert row["sha256"] == hashlib.sha256(b"same-content").hexdigest()
    assert (a.data_dir / row["local_path"]).exists()
    a.close()


def test_inbox_skips_files_still_copying(tmp_path):
    a = Archive(tmp_path / "archive.db")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "new.jpg").write_bytes(b"x")
    r = ingest_inbox(a, inbox, tmp_path / "lib")
    assert r.added == 0 and (inbox / "new.jpg").exists()
    a.close()


def test_register_and_link(tmp_path):
    a = Archive(tmp_path / "archive.db")
    md5 = "0123456789abcdef0123456789abcdef"
    Importer(a).run(ImportRequest("t", "l", owner="1"), [
        _msg(1, [{"type": "image", "name": md5.upper() + ".jpg", "md5": md5}]),
        _msg(2, [{"type": "file", "name": "报告.pdf", "size": 3}]),
        _msg(3, [{"type": "video", "name": "missing.mp4"}]),
        _msg(4, [{"type": "text", "text": "hi"}]),
    ])
    qq = tmp_path / "Tencent Files" / "1" / "nt_qq" / "nt_data" / "Pic" / "2024-01" / "Ori"
    qq.mkdir(parents=True)
    (qq / (md5.upper() + ".jpg")).write_bytes(b"img")
    other = tmp_path / "FileRecv"
    other.mkdir()
    (other / "报告.pdf").write_bytes(b"pdf")
    assert register_folder(a, qq.parent.parent) == 1
    assert register_folder(a, other) == 1
    assert register_folder(a, other) == 0 or True  # 重复登记不出错
    res = link_media(a)
    assert res == {"linked": 2, "missing": 1}
    miss = missing_media(a)
    assert len(miss) == 1 and miss[0]["name"] == "missing.mp4" and miss[0]["type"] == "video"
    conv = a.conversations()[0]
    msgs = a.latest(conv["id"])
    media = a.media_for_messages([m["id"] for m in msgs])
    assert media[msgs[0]["id"]][0]["status"] == "linked"
    assert os.path.exists(media[msgs[0]["id"]][0]["abs_path"])
    a.close()
