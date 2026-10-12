import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tidal_dl
from tidal_dl import dedupe, download, trackindex
from tidal_dl.enums import AudioQuality, Type


SETTING_KEYS = (
    "downloadPath",
    "checkExist",
    "linkDuplicateTracks",
    "usePlaylistFolder",
    "albumFolderFormat",
    "playlistFolderFormat",
    "trackFileFormat",
    "audioQuality",
    "audioQualityPriority",
)

UUID_ONE = "11111111-1111-4111-8111-111111111111"
UUID_TWO = "22222222-2222-4222-8222-222222222222"


class FolderPatternTests(unittest.TestCase):
    def _match(self, folder_format, token, id_pattern, *names):
        patterns = dedupe.__folderPatterns__(folder_format, token, id_pattern)
        self.assertEqual(len(patterns), len(names))
        found = None
        for pattern, name in zip(patterns, names):
            match = pattern.fullmatch(name)
            if match is None:
                return None
            found = match.groupdict().get("id") or found
        return found

    def test_default_playlist_format(self):
        folder_format = "Playlist/{PlaylistName} [{PlaylistUUID}]"
        self.assertEqual(
            self._match(folder_format, "{PlaylistUUID}", r"[0-9a-fA-F-]{36}", "Playlist", f"Apéro [été] [{UUID_ONE}]"),
            UUID_ONE,
        )
        self.assertIsNone(self._match(folder_format, "{PlaylistUUID}", r"[0-9a-fA-F-]{36}", "Playlist", "MP3"))

    def test_default_album_format_with_empty_flag(self):
        folder_format = "{ArtistName}/{Flag} {AlbumTitle} [{AlbumID}] [{AlbumYear}]"
        self.assertEqual(self._match(folder_format, "{AlbumID}", r"\d+", "Tokyo Machine", " ROCK IT [105831973] [2017]"), "105831973")
        self.assertEqual(self._match(folder_format, "{AlbumID}", r"\d+", "Artist", "[E] Album [42] [2020]"), "42")
        self.assertIsNone(self._match(folder_format, "{AlbumID}", r"\d+", "Adele", "25 (2015) [FLAC]"))

    def test_format_without_id_cannot_be_scanned(self):
        self.assertIsNone(dedupe.__folderPatterns__("Playlist/{PlaylistName}", "{PlaylistUUID}", r"[0-9a-fA-F-]{36}"))


class NameOnDiskTests(unittest.TestCase):
    def test_path_is_returned_as_the_folder_spells_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "LOS DEL MAR - Macarena.flac").write_bytes(b"audio")
            listings = {}

            self.assertEqual(
                dedupe.__nameOnDisk__(os.path.join(temp_dir, "Los Del Mar - Macarena.flac"), listings),
                os.path.join(temp_dir, "LOS DEL MAR - Macarena.flac"),
            )
            self.assertEqual(
                dedupe.__nameOnDisk__(os.path.join(temp_dir, "Missing.flac"), listings),
                os.path.join(temp_dir, "Missing.flac"),
            )


class LibraryDedupeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.old_settings = {key: getattr(dedupe.SETTINGS, key) for key in SETTING_KEYS}
        dedupe.SETTINGS.downloadPath = str(self.root) + "/"
        dedupe.SETTINGS.checkExist = True
        dedupe.SETTINGS.linkDuplicateTracks = True
        dedupe.SETTINGS.usePlaylistFolder = True
        dedupe.SETTINGS.albumFolderFormat = "{ArtistName}/{Flag} {AlbumTitle} [{AlbumID}] [{AlbumYear}]"
        dedupe.SETTINGS.playlistFolderFormat = "Playlist/{PlaylistName} [{PlaylistUUID}]"
        dedupe.SETTINGS.trackFileFormat = "{ArtistName} - {TrackTitle}"
        dedupe.SETTINGS.audioQuality = AudioQuality.HiFi
        dedupe.SETTINGS.audioQualityPriority = []

        self.playlists = {}
        self.albums = {}
        self.messages = []
        patches = [
            mock.patch.object(dedupe.TIDAL_API, "getPlaylist", side_effect=self._get_playlist),
            mock.patch.object(dedupe.TIDAL_API, "getAlbum", side_effect=self._get_album),
            mock.patch.object(dedupe.TIDAL_API, "getItems", side_effect=self._get_items),
            mock.patch.object(dedupe.Printf, "success", side_effect=self.messages.append),
            mock.patch.object(dedupe.Printf, "info", side_effect=self.messages.append),
            mock.patch.object(dedupe.Printf, "err", side_effect=self.messages.append),
        ]
        for item in patches:
            item.start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        for key, value in self.old_settings.items():
            setattr(dedupe.SETTINGS, key, value)
        self.temp_dir.cleanup()

    def _get_playlist(self, uuid):
        if uuid not in self.playlists:
            raise Exception("Playlist not found")
        return SimpleNamespace(uuid=uuid, title=self.playlists[uuid][0])

    def _get_album(self, album_id):
        return self.albums[str(album_id)][0]

    def _get_items(self, item_id, item_type):
        source = self.playlists if item_type == Type.Playlist else self.albums
        return list(source[str(item_id)][1]), []

    def _artist(self):
        return SimpleNamespace(name="Artist")

    def _track(self, track_id, title):
        artist = self._artist()
        return SimpleNamespace(
            id=track_id,
            artists=[artist],
            artist=artist,
            album=SimpleNamespace(id=123, title="Album"),
            title=title,
            version=None,
            explicit=False,
            trackNumber=1,
            trackNumberOnPlaylist=1,
            volumeNumber=1,
            audioQuality="LOSSLESS",
            duration=180,
        )

    def _album(self):
        artist = self._artist()
        return SimpleNamespace(
            id=123,
            artists=[artist],
            artist=artist,
            title="Album",
            releaseDate="2026-01-02",
            audioQuality="LOSSLESS",
            audioModes=[],
            explicit=False,
            duration=180,
            numberOfTracks=1,
            numberOfVideos=0,
            numberOfVolumes=1,
            type="ALBUM",
            cover=None,
        )

    def _file(self, relative, content=b"flac-bytes"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _two_playlists(self, second_content=b"flac-bytes"):
        self.playlists[UUID_ONE] = ("One", [self._track(456, "Track"), self._track(457, "Solo")])
        self.playlists[UUID_TWO] = ("Two", [self._track(456, "Track"), self._track(458, "Missing")])
        first = self._file(f"Playlist/One [{UUID_ONE}]/Artist - Track.flac")
        self._file(f"Playlist/One [{UUID_ONE}]/Artist - Solo.flac", b"solo")
        second = self._file(f"Playlist/Two [{UUID_TWO}]/Artist - Track.flac", second_content)
        return first, second

    def test_identical_copies_in_two_playlists_become_one_file(self):
        first, second = self._two_playlists()

        stats = dedupe.dedupeLibrary()

        self.assertEqual(stats["linked"], 1)
        self.assertEqual(stats["freed"], len(b"flac-bytes"))
        self.assertTrue(os.path.samefile(first, second))
        self.assertEqual(second.read_bytes(), b"flac-bytes")
        root = str(self.root)
        self.assertEqual(
            sorted(path for path, _ in trackindex.findTrackCopies(root, 456)),
            sorted([str(first), str(second)]),
        )
        self.assertEqual(len(trackindex.findTrackCopies(root, 457)), 1)
        self.assertEqual(trackindex.findTrackCopies(root, 458), [])

    def test_running_again_changes_nothing(self):
        self._two_playlists()
        dedupe.dedupeLibrary()
        index = self.root / trackindex.TRACK_INDEX_FILE
        before = index.read_text(encoding="utf-8")

        stats = dedupe.dedupeLibrary()

        self.assertEqual(stats["linked"], 0)
        self.assertEqual(stats["alreadyLinked"], 1)
        self.assertEqual(index.read_text(encoding="utf-8"), before)

    def test_dry_run_writes_nothing(self):
        first, second = self._two_playlists()

        stats = dedupe.dedupeLibrary(dryRun=True)

        self.assertEqual(stats["linked"], 1)
        self.assertFalse(os.path.samefile(first, second))
        self.assertFalse((self.root / trackindex.TRACK_INDEX_FILE).exists())
        self.assertEqual(trackindex.findTrackCopies(str(self.root), 456), [])

    def test_copies_with_different_content_are_left_alone(self):
        first, second = self._two_playlists(second_content=b"flac-OTHER")

        stats = dedupe.dedupeLibrary()

        self.assertEqual(stats["linked"], 0)
        self.assertEqual(stats["different"], 1)
        self.assertFalse(os.path.samefile(first, second))
        self.assertEqual(first.read_bytes(), b"flac-bytes")
        self.assertEqual(second.read_bytes(), b"flac-OTHER")

    def test_file_is_left_intact_when_it_cannot_be_hard_linked(self):
        first, second = self._two_playlists()

        with mock.patch.object(download.os, "link", side_effect=OSError("Operation not permitted")):
            stats = dedupe.dedupeLibrary()

        self.assertEqual(stats["linked"], 0)
        self.assertEqual(stats["failed"], 1)
        self.assertFalse(os.path.samefile(first, second))
        self.assertEqual(second.read_bytes(), b"flac-bytes")
        self.assertEqual([item.name for item in second.parent.iterdir()], ["Artist - Track.flac"])

    def test_files_outside_tidal_folders_are_never_touched(self):
        first, second = self._two_playlists()
        personal = self._file("Adele/25 (2015) [FLAC]/Artist - Track.flac")
        also_personal = self._file("Playlist/MP3/Artist - Track.flac")

        dedupe.dedupeLibrary()

        self.assertTrue(os.path.samefile(first, second))
        self.assertFalse(os.path.samefile(first, personal))
        self.assertFalse(os.path.samefile(first, also_personal))

    def test_folder_of_a_playlist_renamed_on_tidal_is_still_matched(self):
        first, second = self._two_playlists()
        self.playlists[UUID_TWO] = ("Two (new name)", self.playlists[UUID_TWO][1])

        dedupe.dedupeLibrary()

        self.assertTrue(os.path.samefile(first, second))

    def test_identical_file_in_a_playlist_deleted_on_tidal_is_merged_and_indexed(self):
        first, second = self._two_playlists()
        del self.playlists[UUID_TWO]
        orphan = self._file(f"Playlist/Two [{UUID_TWO}]/Artist - Gone.flac", b"gone")

        stats = dedupe.dedupeLibrary()

        self.assertTrue(any("Playlist not found" in item for item in self.messages))
        self.assertEqual(stats["linked"], 1)
        self.assertTrue(os.path.samefile(first, second))
        # Identical to an indexed file, so it is known to hold the same track.
        self.assertEqual(
            sorted(path for path, _ in trackindex.findTrackCopies(str(self.root), 456)),
            sorted([str(first), str(second)]),
        )
        self.assertEqual(orphan.read_bytes(), b"gone")
        self.assertNotIn("Gone", (self.root / trackindex.TRACK_INDEX_FILE).read_text(encoding="utf-8"))

    def test_file_named_with_the_former_default_format_is_indexed(self):
        self.playlists[UUID_ONE] = ("One", [self._track(456, "Track")])
        self.albums["123"] = (self._album(), [self._track(456, "Track")])
        in_playlist = self._file(f"Playlist/One [{UUID_ONE}]/Artist - Track.flac")
        in_album = self._file("Artist/ Album [123] [2026]/01 - Artist - Track.flac")

        stats = dedupe.dedupeLibrary()

        self.assertEqual(stats["linked"], 1)
        self.assertTrue(os.path.samefile(in_playlist, in_album))
        self.assertEqual(len(trackindex.findTrackCopies(str(self.root), 456)), 2)

    def test_album_copy_and_playlist_copy_are_merged(self):
        self.playlists[UUID_ONE] = ("One", [self._track(456, "Track")])
        self.albums["123"] = (self._album(), [self._track(456, "Track")])
        in_playlist = self._file(f"Playlist/One [{UUID_ONE}]/Artist - Track.flac")
        in_album = self._file("Artist/ Album [123] [2026]/Artist - Track.flac")

        stats = dedupe.dedupeLibrary()

        self.assertEqual(stats["linked"], 1)
        self.assertTrue(os.path.samefile(in_playlist, in_album))

    def test_names_that_depend_on_the_stream_are_merged_but_not_indexed(self):
        first, second = self._two_playlists()
        dedupe.SETTINGS.trackFileFormat = "{ArtistName} - {TrackTitle} [{StreamQuality}]"

        with mock.patch.object(dedupe.TIDAL_API, "getPlaylist") as get_playlist:
            stats = dedupe.dedupeLibrary()

        get_playlist.assert_not_called()
        self.assertEqual(stats["linked"], 1)
        self.assertTrue(os.path.samefile(first, second))
        self.assertFalse((self.root / trackindex.TRACK_INDEX_FILE).exists())

    def test_dedupe_command_logs_in_then_runs(self):
        old_argv = sys.argv
        sys.argv = ["tidekeeper", "--dedupe", "--dry-run"]
        try:
            with mock.patch.object(tidal_dl, "loginByConfig", return_value=True), \
                 mock.patch.object(tidal_dl, "dedupeLibrary") as run, \
                 mock.patch.object(tidal_dl, "start") as start:
                tidal_dl.mainCommand()
        finally:
            sys.argv = old_argv

        run.assert_called_once_with(True)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
