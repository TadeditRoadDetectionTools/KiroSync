"""client/run.py 純函式與 pull 流程的測試 (不接 Discord)。"""

import io
import json
import os
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import _paths  # noqa: F401

import run


def _write_session(wd: Path, sid: str, *, jsonl=b'{"kind":"Prompt"}\n', meta=None):
    (wd / f"{sid}.jsonl").write_bytes(jsonl)
    if meta is not None:
        (wd / f"{sid}.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")


class TestReadMeta(unittest.TestCase):
    def test_missing_file(self):
        self.assertEqual(run._read_meta(Path("/nonexistent.json")), {})
        self.assertIsNone(run._read_cwd(Path("/nonexistent.json")))

    def test_broken_json_tolerated(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            p.write_text("{broken", encoding="utf-8")
            self.assertEqual(run._read_meta(p), {})

    def test_non_dict_json_tolerated(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            p.write_text("[1,2]", encoding="utf-8")
            self.assertEqual(run._read_meta(p), {})


class TestBuildSessionZip(unittest.TestCase):
    def test_with_meta(self):
        with TemporaryDirectory() as td:
            wd = Path(td)
            _write_session(wd, "sid1", meta={"title": "我的任務", "cwd": "/proj"})
            blob, title, cwd = run._build_session_zip(wd, "sid1")
            self.assertEqual(title, "我的任務")
            self.assertEqual(cwd, "/proj")
            zf = zipfile.ZipFile(io.BytesIO(blob))
            self.assertEqual(sorted(zf.namelist()), ["sid1.json", "sid1.jsonl"])
            self.assertEqual(zf.read("sid1.jsonl"), b'{"kind":"Prompt"}\n')

    def test_meta_missing_is_normal(self):
        # .json 缺席是正常情況 (session 剛開) — 只打包 .jsonl
        with TemporaryDirectory() as td:
            wd = Path(td)
            _write_session(wd, "sid1")
            blob, title, cwd = run._build_session_zip(wd, "sid1")
            self.assertIsNone(title)
            self.assertIsNone(cwd)
            zf = zipfile.ZipFile(io.BytesIO(blob))
            self.assertEqual(zf.namelist(), ["sid1.jsonl"])


class TestRewriteCwd(unittest.TestCase):
    def test_rewrites_cwd_and_matching_permission_paths(self):
        meta = {
            "cwd": "/old/proj",
            "session_state": {"permissions": {"filesystem": {
                "allowed_read_paths": ["/old/proj", "/other"],
                "allowed_write_paths": ["/old/proj"],
                "denied_read_paths": [],
                "denied_write_paths": ["/secret"],
            }}},
        }
        raw = json.dumps(meta).encode("utf-8")
        with redirect_stdout(io.StringIO()):
            out = json.loads(run._rewrite_cwd(raw, "/new/home").decode("utf-8"))
        self.assertEqual(out["cwd"], "/new/home")
        fs = out["session_state"]["permissions"]["filesystem"]
        self.assertEqual(fs["allowed_read_paths"], ["/new/home", "/other"])  # 只換等於舊 cwd 的
        self.assertEqual(fs["allowed_write_paths"], ["/new/home"])
        self.assertEqual(fs["denied_write_paths"], ["/secret"])

    def test_no_permissions_block_tolerated(self):
        raw = json.dumps({"cwd": "/old"}).encode("utf-8")
        with redirect_stdout(io.StringIO()):
            out = json.loads(run._rewrite_cwd(raw, "/new").decode("utf-8"))
        self.assertEqual(out["cwd"], "/new")

    def test_broken_json_returned_unchanged(self):
        raw = b"{broken json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run._rewrite_cwd(raw, "/new"), raw)


class TestSnapshotSession(unittest.TestCase):
    """sync/export 共用的打包+上傳 helper: session 檔消失不能把監看迴圈炸掉。"""

    def test_vanished_session_returns_none_instead_of_raising(self):
        # 模擬 glob 看到檔案後、打包前被 Kiro 刪掉 → 檔案已不存在
        with TemporaryDirectory() as td:
            out = io.StringIO()
            with redirect_stdout(out):
                res = run._snapshot_session("https://hook", "alice",
                                            Path(td), "gone1234", 1024)
            self.assertIsNone(res)
            self.assertIn("略過", out.getvalue())

    def test_success_returns_parts_and_size(self):
        with TemporaryDirectory() as td:
            wd = Path(td)
            _write_session(wd, "sid1", meta={"title": "任務", "cwd": "/p"})
            calls = []

            def fake_post(webhook, user, sid, blob, *, title=None, cwd=None,
                          chunk_bytes=0, **kw):
                calls.append({"user": user, "sid": sid, "blob": blob,
                              "title": title, "cwd": cwd, "chunk": chunk_bytes})
                return 2

            with mock.patch.object(run, "post_snapshot", fake_post):
                res = run._snapshot_session("https://hook", "alice", wd, "sid1", 4096)
            nparts, nbytes = res
            self.assertEqual(nparts, 2)
            self.assertEqual(nbytes, len(calls[0]["blob"]))
            self.assertEqual(calls[0]["sid"], "sid1")
            self.assertEqual(calls[0]["title"], "任務")
            self.assertEqual(calls[0]["cwd"], "/p")
            self.assertEqual(calls[0]["chunk"], 4096)


def _make_pull_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


class TestCmdPull(unittest.TestCase):
    def _pull(self, wd: Path, blob: bytes, nparts: int = 2, cwd=None):
        """把 blob 切 nparts 片, 用假的 fetch_bytes 跑 cmd_pull。"""
        step = max(1, len(blob) // nparts + 1)
        parts = [blob[i:i + step] for i in range(0, len(blob), step)]
        urls = [f"https://cdn.example/p{i}" for i in range(len(parts))]
        table = dict(zip(urls, parts))
        args = SimpleNamespace(url=urls, cwd=cwd)
        with mock.patch.dict(os.environ, {"WATCH_DIR": str(wd)}), \
             mock.patch.object(run, "fetch_bytes", lambda u, log=print: table.get(u)), \
             redirect_stdout(io.StringIO()):
            run.cmd_pull(args)

    def test_multi_part_restore(self):
        blob = _make_pull_zip({
            "sid1.jsonl": b'{"kind":"Prompt"}\n' * 50,
            "sid1.json": json.dumps({"cwd": "/src/machine"}),
        })
        with TemporaryDirectory() as td:
            wd = Path(td) / "sessions"
            self._pull(wd, blob, nparts=3)
            self.assertEqual((wd / "sid1.jsonl").read_bytes(),
                             b'{"kind":"Prompt"}\n' * 50)
            self.assertTrue((wd / "sid1.json").exists())

    def test_pull_with_cwd_rewrite(self):
        blob = _make_pull_zip({
            "sid1.jsonl": b"{}\n",
            "sid1.json": json.dumps({"cwd": "/src/machine"}),
        })
        with TemporaryDirectory() as td:
            wd = Path(td) / "sessions"
            self._pull(wd, blob, cwd="/dest/here")
            meta = json.loads((wd / "sid1.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["cwd"], "/dest/here")

    def test_zip_slip_and_foreign_files_filtered(self):
        blob = _make_pull_zip({
            "../../evil.jsonl": b"x",       # 路徑穿越 → 只取檔名, 落在 wd 內
            "sub/dir/deep.jsonl": b"y",     # 目錄前綴剝掉
            "readme.txt": b"z",             # 非 .jsonl/.json → 跳過
        })
        with TemporaryDirectory() as td:
            root = Path(td)
            wd = root / "a" / "b" / "sessions"
            self._pull(wd, blob, nparts=1)
            self.assertTrue((wd / "evil.jsonl").exists())
            self.assertTrue((wd / "deep.jsonl").exists())
            self.assertFalse((wd / "readme.txt").exists())
            self.assertFalse((root / "evil.jsonl").exists())  # 沒逃出 wd

    def test_download_failure_aborts(self):
        with TemporaryDirectory() as td:
            args = SimpleNamespace(url=["https://cdn.example/gone"], cwd=None)
            with mock.patch.dict(os.environ, {"WATCH_DIR": td}), \
                 mock.patch.object(run, "fetch_bytes", lambda u, log=print: None), \
                 redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    run.cmd_pull(args)

    def test_garbage_blob_aborts(self):
        with TemporaryDirectory() as td:
            args = SimpleNamespace(url=["https://cdn.example/p0"], cwd=None)
            with mock.patch.dict(os.environ, {"WATCH_DIR": td}), \
                 mock.patch.object(run, "fetch_bytes", lambda u, log=print: b"not a zip"), \
                 redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    run.cmd_pull(args)


class TestCmdSessions(unittest.TestCase):
    def test_lists_sessions_and_tolerates_missing_meta(self):
        with TemporaryDirectory() as td:
            wd = Path(td)
            _write_session(wd, "aaaa1111", meta={"title": "有標題", "cwd": "/p"})
            _write_session(wd, "bbbb2222")  # 沒 .json → 也要能列
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"WATCH_DIR": str(wd)}), \
                 redirect_stdout(out):
                run.cmd_sessions(SimpleNamespace())
            text = out.getvalue()
            self.assertIn("aaaa1111", text)
            self.assertIn("有標題", text)
            self.assertIn("bbbb2222", text)
            self.assertIn("(無標題)", text)

    def test_missing_dir_exits(self):
        with mock.patch.dict(os.environ, {"WATCH_DIR": "/nonexistent/dir/xyz"}):
            with self.assertRaises(SystemExit):
                run.cmd_sessions(SimpleNamespace())


if __name__ == "__main__":
    unittest.main()
