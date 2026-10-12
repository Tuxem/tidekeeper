import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tidal_dl
from tidal_dl import setlists

UUID_ONE = "11111111-1111-4111-8111-111111111111"
UUID_TWO = "22222222-2222-4222-8222-222222222222"


class SetlistTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.titles = {UUID_ONE: "One", UUID_TWO: "Two"}
        self.messages = []
        patches = [
            mock.patch.object(setlists, "getSetlistsPath", return_value=str(self.root / "setlists.json")),
            mock.patch.object(setlists, "getSetlistsFilePath", return_value=str(self.root / "setlists.txt")),
            mock.patch.object(setlists.TIDAL_API, "getPlaylist", side_effect=self._get_playlist),
            mock.patch.object(setlists.Printf, "success", side_effect=self.messages.append),
            mock.patch.object(setlists.Printf, "info", side_effect=self.messages.append),
            mock.patch.object(setlists.Printf, "err", side_effect=self.messages.append),
        ]
        for item in patches:
            item.start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _get_playlist(self, uuid):
        if uuid not in self.titles:
            raise Exception("HTTP 404 Playlist not found")
        return SimpleNamespace(uuid=uuid, title=self.titles[uuid])

    def test_add_accepts_url_or_bare_id_and_does_not_duplicate(self):
        setlists.addSetlist(f"https://tidal.com/browse/playlist/{UUID_ONE}?u")
        setlists.addSetlist(UUID_ONE)
        setlists.addSetlist(UUID_TWO)

        self.assertEqual([(item["uuid"], item["title"]) for item in setlists.loadSetlists()],
                         [(UUID_ONE, "One"), (UUID_TWO, "Two")])

    def test_add_rejects_what_is_not_a_playlist(self):
        for text in ("https://tidal.com/browse/album/123", "not-a-playlist", ""):
            with self.assertRaises(ValueError):
                setlists.addSetlist(text)
        with self.assertRaises(Exception):
            setlists.addSetlist("33333333-3333-4333-8333-333333333333")
        self.assertEqual(setlists.loadSetlists(), [])

    def test_remove_by_url_id_or_title(self):
        for uuid in (UUID_ONE, UUID_TWO):
            setlists.addSetlist(uuid)

        setlists.removeSetlist(f"https://tidal.com/browse/playlist/{UUID_ONE}")
        self.assertEqual([item["uuid"] for item in setlists.loadSetlists()], [UUID_TWO])
        setlists.removeSetlist("two")
        self.assertEqual(setlists.loadSetlists(), [])
        with self.assertRaises(ValueError):
            setlists.removeSetlist("two")

    def test_unreadable_file_counts_as_empty(self):
        (self.root / "setlists.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(setlists.loadSetlists(), [])

    def test_sync_runs_every_setlist_and_records_the_outcome(self):
        for uuid in (UUID_ONE, UUID_TWO):
            setlists.addSetlist(uuid)
        calls = []

        def fake_start(playlist):
            calls.append(playlist.uuid)
            return playlist.uuid == UUID_ONE

        with mock.patch.object(setlists, "start_playlist", side_effect=fake_start):
            code = setlists.syncSetlists()

        self.assertEqual(calls, [UUID_ONE, UUID_TWO])
        self.assertEqual(code, 1, "a setlist with failed tracks makes the run fail for cron")
        one, two = setlists.loadSetlists()
        self.assertEqual(one["lastStatus"], "ok")
        self.assertIn("some tracks failed", two["lastStatus"])
        self.assertTrue(one["lastSync"] and two["lastSync"])

    def test_one_broken_setlist_does_not_stop_the_others(self):
        for uuid in (UUID_ONE, UUID_TWO):
            setlists.addSetlist(uuid)
        del self.titles[UUID_ONE]
        calls = []

        with mock.patch.object(setlists, "start_playlist", side_effect=lambda p: calls.append(p.uuid) or True):
            code = setlists.syncSetlists()

        self.assertEqual(calls, [UUID_TWO])
        self.assertEqual(code, 1)
        one, two = setlists.loadSetlists()
        self.assertIn("Playlist not found", one["lastStatus"])
        self.assertEqual(two["lastStatus"], "ok")

    def test_sync_renames_a_setlist_renamed_on_tidal_and_all_good_exits_zero(self):
        setlists.addSetlist(UUID_ONE)
        self.titles[UUID_ONE] = "One (renamed)"

        with mock.patch.object(setlists, "start_playlist", return_value=True):
            code = setlists.syncSetlists()

        self.assertEqual(code, 0)
        self.assertEqual(setlists.loadSetlists()[0]["title"], "One (renamed)")

    def test_sync_without_setlists_is_a_no_op(self):
        with mock.patch.object(setlists, "start_playlist") as start:
            self.assertEqual(setlists.syncSetlists(), 0)
        start.assert_not_called()

    def test_sync_keeps_progress_bars_off_when_not_a_terminal(self):
        setlists.addSetlist(UUID_ONE)
        seen = []
        old = setlists.SETTINGS.showProgress
        setlists.SETTINGS.showProgress = True
        try:
            with mock.patch.object(setlists, "start_playlist",
                                   side_effect=lambda p: seen.append(setlists.SETTINGS.showProgress) or True), \
                 mock.patch.object(setlists.sys.stdout, "isatty", return_value=False, create=True):
                setlists.syncSetlists()
            self.assertEqual(seen, [False])
            self.assertTrue(setlists.SETTINGS.showProgress)
        finally:
            setlists.SETTINGS.showProgress = old

    def test_a_second_sync_started_while_one_runs_backs_off(self):
        setlists.addSetlist(UUID_ONE)
        outcome = {}

        def inner_sync(playlist):
            outcome["second"] = setlists.syncSetlists()
            return True

        with mock.patch.object(setlists, "start_playlist", side_effect=inner_sync):
            first = setlists.syncSetlists()

        self.assertEqual(outcome["second"], 1)
        self.assertEqual(first, 0)
        self.assertIn("Another sync is already running.", self.messages)

    def _write_list(self, *lines, name="setlists.txt"):
        path = self.root / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    def test_list_file_ignores_blank_lines_and_comments(self):
        self._write_list(
            "# my setlists",
            "",
            f"https://tidal.com/browse/playlist/{UUID_ONE}   # warmup",
            f"  {UUID_TWO}  ",
        )
        # a UTF-8 byte order mark (Windows editors) must not break the first line
        path = self.root / "bom.txt"
        path.write_bytes(b"\xef\xbb\xbf" + f"{UUID_ONE}\n".encode())

        self.assertEqual(setlists.readSetlistsFile(),
                         [(3, f"https://tidal.com/browse/playlist/{UUID_ONE}"), (4, UUID_TWO)])
        self.assertEqual(setlists.readSetlistsFile(str(path)), [(1, UUID_ONE)])

    def test_missing_default_list_file_is_empty_but_a_named_one_must_exist(self):
        self.assertEqual(setlists.readSetlistsFile(), [])
        with self.assertRaises(FileNotFoundError):
            setlists.readSetlistsFile(str(self.root / "nope.txt"))

    def test_sync_downloads_the_list_file_setlists_too_without_registering_them(self):
        setlists.addSetlist(UUID_ONE)
        self._write_list(UUID_TWO, UUID_ONE)
        calls = []

        with mock.patch.object(setlists, "start_playlist", side_effect=lambda p: calls.append(p.uuid) or True):
            code = setlists.syncSetlists()

        self.assertEqual(code, 0)
        self.assertEqual(calls, [UUID_ONE, UUID_TWO], "a setlist both registered and listed is synced once")
        self.assertEqual([item["uuid"] for item in setlists.loadSetlists()], [UUID_ONE])

    def test_sync_with_an_explicit_list_file(self):
        other = self._write_list(UUID_TWO, name="other.txt")
        calls = []

        with mock.patch.object(setlists, "start_playlist", side_effect=lambda p: calls.append(p.uuid) or True):
            self.assertEqual(setlists.syncSetlists(other), 0)

        self.assertEqual(calls, [UUID_TWO])

    def test_bad_list_file_lines_fail_the_run_but_not_the_other_lines(self):
        self._write_list("https://tidal.com/browse/album/123", UUID_TWO)
        calls = []

        with mock.patch.object(setlists, "start_playlist", side_effect=lambda p: calls.append(p.uuid) or True):
            code = setlists.syncSetlists()

        self.assertEqual(calls, [UUID_TWO])
        self.assertEqual(code, 1)
        self.assertTrue(any("line 1" in str(message) for message in self.messages))
        self.assertIn("1/1 setlists up to date", self.messages[-1])

    def test_a_list_file_with_only_bad_lines_still_fails(self):
        self._write_list("nonsense")
        with mock.patch.object(setlists, "start_playlist") as start:
            self.assertEqual(setlists.syncSetlists(), 1)
        start.assert_not_called()

    def test_print_lists_the_file_setlists(self):
        setlists.addSetlist(UUID_ONE)
        self._write_list(UUID_ONE, UUID_TWO, "junk")

        setlists.printSetlists()

        text = "\n".join(str(message) for message in self.messages)
        self.assertIn(f"[{UUID_TWO}] - from the list file, line 2", text)
        self.assertNotIn(f"[{UUID_ONE}] - from the list file", text)
        self.assertIn("List file, line 3", text)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.written = []
        self.table = ""
        patches = [
            mock.patch.object(setlists, "__readCrontab__", side_effect=lambda: self.table),
            mock.patch.object(setlists, "__writeCrontab__", side_effect=self.written.append),
            mock.patch.object(setlists, "getSyncLogPath", return_value="/home/me/.tidal-dl.sync.log"),
        ]
        for item in patches:
            item.start()
        self.addCleanup(mock.patch.stopall)

    @unittest.skipIf(os.name == "nt", "cron is not used on Windows")
    def test_schedule_appends_one_daily_line_and_keeps_other_entries(self):
        self.table = "0 1 * * * backup\n"

        line = setlists.scheduleSync("03:05")

        self.assertTrue(line.startswith("5 3 * * * "))
        self.assertIn("-m tidal_dl --sync", line)
        self.assertIn(">> /home/me/.tidal-dl.sync.log 2>&1", line)
        self.assertEqual(self.written, ["0 1 * * * backup\n" + line + "\n"])

    @unittest.skipIf(os.name == "nt", "cron is not used on Windows")
    def test_schedule_again_replaces_the_previous_time(self):
        self.table = setlists.scheduleSync("03:05") + "\n0 1 * * * backup\n"
        self.written.clear()

        line = setlists.scheduleSync("22:30")

        self.assertEqual(self.written, ["0 1 * * * backup\n" + line + "\n"])
        self.assertEqual(self.written[0].count("tidekeeper-sync"), 1)

    @unittest.skipIf(os.name == "nt", "cron is not used on Windows")
    def test_schedule_can_bake_in_a_list_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "my setlists.txt")
            open(path, "w").close()

            line = setlists.scheduleSync("03:05", path)

            self.assertIn("--sync --setlist-file '" + path + "'", line)
            with self.assertRaises(FileNotFoundError):
                setlists.scheduleSync("03:05", os.path.join(folder, "missing.txt"))

    def test_schedule_rejects_bad_times(self):
        for text in ("25:00", "3:75", "noon", ""):
            with self.assertRaises(ValueError):
                setlists.scheduleSync(text)
        self.assertEqual(self.written, [])

    @unittest.skipIf(os.name == "nt", "cron is not used on Windows")
    def test_unschedule_removes_only_our_line(self):
        self.table = "0 1 * * * backup\n" + setlists.scheduleSync("03:05") + "\n"
        self.written.clear()

        self.assertTrue(setlists.unscheduleSync())
        self.assertEqual(self.written, ["0 1 * * * backup\n"])

        self.table = "0 1 * * * backup\n"
        self.written.clear()
        self.assertFalse(setlists.unscheduleSync())
        self.assertEqual(self.written, [])


class SetlistCommandTests(unittest.TestCase):
    def _run(self, *args, login=True):
        old_argv = sys.argv
        sys.argv = ["tidekeeper", *args]
        try:
            with mock.patch.object(tidal_dl, "loginByConfig", return_value=login), \
                 mock.patch.object(tidal_dl, "loginByWeb") as web, \
                 mock.patch.object(tidal_dl.aigpy.path, "mkdirs", return_value=True), \
                 mock.patch.object(tidal_dl, "syncSetlists", return_value=0) as sync, \
                 mock.patch.object(tidal_dl, "addSetlist", return_value={"title": "T", "uuid": UUID_ONE}) as add, \
                 mock.patch.object(tidal_dl, "printSetlists") as listing:
                code = tidal_dl.mainCommand()
        finally:
            sys.argv = old_argv
        return code, sync, add, web, listing

    def test_sync_returns_the_sync_result_and_never_starts_a_web_login(self):
        code, sync, _, web, _ = self._run("--sync")
        self.assertEqual(code, 0)
        sync.assert_called_once_with(None)
        web.assert_not_called()

    def test_sync_without_a_saved_login_fails_with_a_non_zero_code(self):
        code, sync, _, web, _ = self._run("--sync", login=False)
        self.assertEqual(code, 1)
        sync.assert_not_called()
        web.assert_not_called()

    def test_add_registers_every_given_playlist(self):
        code, _, add, _, _ = self._run("--setlist-add", UUID_ONE, "--setlist-add", UUID_TWO)
        self.assertEqual(code, 0)
        self.assertEqual([call.args[0] for call in add.call_args_list], [UUID_ONE, UUID_TWO])

    def test_sync_passes_the_list_file_through(self):
        code, sync, _, _, _ = self._run("--sync", "--setlist-file", "/tmp/my.txt")
        self.assertEqual(code, 0)
        sync.assert_called_once_with("/tmp/my.txt")

    def test_sync_with_an_unreadable_list_file_fails(self):
        old_argv = sys.argv
        sys.argv = ["tidekeeper", "--sync", "--setlist-file", "/nonexistent/list.txt"]
        try:
            with mock.patch.object(tidal_dl, "loginByConfig", return_value=True), \
                 mock.patch.object(tidal_dl.aigpy.path, "mkdirs", return_value=True), \
                 mock.patch.object(setlists.Printf, "err"):
                code = tidal_dl.mainCommand()
        finally:
            sys.argv = old_argv
        self.assertEqual(code, 1)

    def test_list_needs_no_login(self):
        code, _, _, web, listing = self._run("--setlist-list", login=False)
        self.assertEqual(code, 0)
        listing.assert_called_once_with(None)
        web.assert_not_called()


if __name__ == "__main__":
    unittest.main()
