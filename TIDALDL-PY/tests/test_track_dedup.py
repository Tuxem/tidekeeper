import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tidal_dl import download, trackindex
from tidal_dl.enums import AudioQuality
from tidal_dl.model import StreamUrl


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
    "saveAsFlac",
    "lyricFile",
    "multiThread",
    "showProgress",
    "showTrackInfo",
)


class TrackIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _file(self, relative, content=b"audio"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _lines(self):
        index = self.root / trackindex.TRACK_INDEX_FILE
        return [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]

    def test_paths_inside_the_library_are_stored_relative(self):
        path = self._file("Playlist/One/Artist - Track.flac")

        trackindex.registerTrack(str(self.root), 456, str(path), "LOSSLESS")

        self.assertEqual(self._lines(), [
            {"id": 456, "path": "Playlist/One/Artist - Track.flac", "quality": "LOSSLESS"},
        ])
        self.assertEqual(trackindex.findTrackCopies(str(self.root), 456), [(str(path), "LOSSLESS")])

    def test_index_survives_a_restart_and_a_moved_library(self):
        path = self._file("Playlist/One/Artist - Track.flac")
        trackindex.registerTrack(str(self.root), 456, str(path), "LOSSLESS")

        moved = Path(str(self.root) + "-moved")
        self.root.rename(moved)
        try:
            trackindex.track_index_cache.clear()
            copies = trackindex.findTrackCopies(str(moved), 456)
        finally:
            moved.rename(self.root)

        self.assertEqual(copies, [(str(moved / "Playlist/One/Artist - Track.flac"), "LOSSLESS")])

    def test_registering_a_known_copy_again_does_not_grow_the_index(self):
        path = self._file("a.flac")
        trackindex.registerTrack(str(self.root), 456, str(path), "LOSSLESS")
        trackindex.registerTrack(str(self.root), 456, str(path), "LOSSLESS")
        # A resume indexes the file without knowing its quality: keep what we had.
        trackindex.registerTrack(str(self.root), 456, str(path))

        self.assertEqual(len(self._lines()), 1)
        self.assertEqual(trackindex.findTrackCopies(str(self.root), 456), [(str(path), "LOSSLESS")])

    def test_every_copy_is_kept_and_missing_files_are_ignored(self):
        first = self._file("one/a.flac")
        second = self._file("two/a.flac")
        trackindex.registerTrack(str(self.root), 456, str(first), "LOSSLESS")
        trackindex.registerTrack(str(self.root), 456, str(second), "LOSSLESS")

        first.unlink()

        self.assertEqual(trackindex.findTrackCopies(str(self.root), 456), [(str(second), "LOSSLESS")])
        self.assertEqual(trackindex.findTrackCopies(str(self.root), 999), [])

    def test_damaged_lines_are_skipped(self):
        path = self._file("a.flac")
        index = self.root / trackindex.TRACK_INDEX_FILE
        index.write_text(
            '{"id": 456, "path": "a.flac", "quality": "HIGH"}\n'
            '\n'
            '[1, 2]\n'
            '{"id": 457, "pa',
            encoding="utf-8",
        )

        self.assertEqual(trackindex.findTrackCopies(str(self.root), 456), [(str(path), "HIGH")])
        self.assertEqual(trackindex.findTrackCopies(str(self.root), 457), [])

    def test_entries_pointing_outside_the_library_are_ignored(self):
        outside = tempfile.NamedTemporaryFile(delete=False)
        outside.write(b"secret")
        outside.close()
        self.addCleanup(os.unlink, outside.name)
        inside = self._file("a.flac")
        (self.root / "escape.flac").symlink_to(outside.name)
        index = self.root / trackindex.TRACK_INDEX_FILE
        index.write_text(
            json.dumps({"id": 456, "path": outside.name}) + "\n"
            + json.dumps({"id": 456, "path": "../" + os.path.basename(self.root) + "/../" + os.path.basename(outside.name)}) + "\n"
            + json.dumps({"id": 456, "path": "escape.flac"}) + "\n"
            + json.dumps({"id": 456, "path": "a.flac", "quality": "HIGH"}) + "\n",
            encoding="utf-8",
        )

        self.assertEqual(trackindex.findTrackCopies(str(self.root), 456), [(str(inside), "HIGH")])


class TrackDedupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.old_settings = {key: getattr(download.SETTINGS, key) for key in SETTING_KEYS}
        download.SETTINGS.downloadPath = str(self.root)
        download.SETTINGS.checkExist = True
        download.SETTINGS.linkDuplicateTracks = True
        download.SETTINGS.usePlaylistFolder = True
        download.SETTINGS.albumFolderFormat = "{ArtistName}/{AlbumTitle}"
        download.SETTINGS.playlistFolderFormat = "Playlist/{PlaylistName}"
        download.SETTINGS.trackFileFormat = "{ArtistName} - {TrackTitle}"
        download.SETTINGS.audioQuality = AudioQuality.HiFi
        download.SETTINGS.audioQualityPriority = []
        download.SETTINGS.saveAsFlac = False
        download.SETTINGS.lyricFile = False
        download.SETTINGS.multiThread = False
        download.SETTINGS.showProgress = False
        download.SETTINGS.showTrackInfo = False

        self.payload = b"flac-bytes"
        self.stream_quality = "LOSSLESS"
        self.lyrics = None
        self.messages = []

        patches = [
            mock.patch.object(download, "__getTrackStream__", side_effect=self._fake_stream),
            mock.patch.object(download, "__downloadUrls__", side_effect=self._fake_download),
            mock.patch.object(download, "__skipPath__", return_value=None),
            mock.patch.object(download, "__setMetaData__"),
            mock.patch.object(download, "__getLyricsForTrack__", side_effect=lambda track: self.lyrics),
            mock.patch.object(download.TIDAL_API, "getTrackContributors", return_value=None),
            mock.patch.object(download.Printf, "success", side_effect=self.messages.append),
            mock.patch.object(download.Printf, "err", side_effect=self.messages.append),
        ]
        self.get_stream = patches[0].start()
        self.download_urls = patches[1].start()
        self.skip_path = patches[2].start()
        for item in patches[3:]:
            item.start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        for key, value in self.old_settings.items():
            setattr(download.SETTINGS, key, value)
        self.temp_dir.cleanup()

    def _fake_stream(self, track_id):
        stream = StreamUrl()
        stream.url = "https://example.invalid/audio.flac"
        stream.urls = [stream.url]
        stream.codec = "flac"
        stream.soundQuality = self.stream_quality
        return stream

    def _fake_download(self, urls, path, *args, **kwargs):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(self.payload)
        return True, ""

    def _artist(self):
        return SimpleNamespace(name="Artist")

    def _track(self, track_id=456, title="Track"):
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

    def _playlist(self, name):
        return SimpleNamespace(uuid=f"uuid-{name}", title=name)

    def _path(self, playlist, name="Artist - Track.flac"):
        return self.root / "Playlist" / playlist / name

    def _download(self, playlist, track=None, album=None):
        ok, msg = download.downloadTrack(track or self._track(), album, self._playlist(playlist))
        self.assertTrue(ok, msg)

    def test_track_shared_by_two_playlists_is_downloaded_once_and_hard_linked(self):
        self._download("One")
        self.assertEqual(self.get_stream.call_count, 1)

        self._download("Two")

        first, second = self._path("One"), self._path("Two")
        self.assertEqual(self.get_stream.call_count, 1, "the second playlist must not hit the stream API")
        self.assertEqual(self.download_urls.call_count, 1)
        self.assertEqual(second.read_bytes(), self.payload)
        self.assertTrue(os.path.samefile(first, second))
        self.assertIn("Artist - Track.flac (linked to existing copy)", self.messages)

    def test_other_tracks_are_still_downloaded(self):
        self._download("One")
        self._download("Two", track=self._track(track_id=457, title="Other"))

        self.assertEqual(self.get_stream.call_count, 2)
        self.assertFalse(os.path.samefile(self._path("One"), self._path("Two", "Artist - Other.flac")))

    def test_album_copy_is_linked_into_a_playlist(self):
        ok, msg = download.downloadTrack(self._track(), self._album())
        self.assertTrue(ok, msg)
        album_file = self.root / "Artist" / "Album" / "Artist - Track.flac"
        self.assertTrue(album_file.exists())

        self._download("One", album=self._album())

        self.assertEqual(self.get_stream.call_count, 1)
        self.assertTrue(os.path.samefile(album_file, self._path("One")))

    def test_library_downloaded_before_the_index_existed_is_picked_up_on_resume(self):
        existing = self._path("One")
        existing.parent.mkdir(parents=True)
        existing.write_bytes(self.payload)

        # Re-running the old playlist skips the file and indexes it on the way.
        self._download("One")
        self._download("Two")

        self.get_stream.assert_not_called()
        self.assertTrue(os.path.samefile(existing, self._path("Two")))

    def test_any_surviving_copy_can_be_linked(self):
        self._download("One")
        self._download("Two")
        self._path("One").unlink()

        self._download("Three")

        self.assertEqual(self.get_stream.call_count, 1)
        self.assertTrue(os.path.samefile(self._path("Two"), self._path("Three")))

    def test_track_is_downloaded_again_when_every_copy_is_gone(self):
        self._download("One")
        self._path("One").unlink()

        self._download("Two")

        self.assertEqual(self.get_stream.call_count, 2)
        self.assertEqual(self._path("Two").read_bytes(), self.payload)

    def test_falls_back_to_a_copy_when_hard_links_are_not_supported(self):
        self._download("One")

        with mock.patch.object(download.os, "link", side_effect=OSError("Operation not permitted")):
            self._download("Two")

        first, second = self._path("One"), self._path("Two")
        self.assertEqual(self.get_stream.call_count, 1)
        self.assertEqual(second.read_bytes(), self.payload)
        self.assertFalse(os.path.samefile(first, second))
        self.assertIn("Artist - Track.flac (copied from existing copy)", self.messages)
        self.assertEqual(sorted(item.name for item in second.parent.iterdir()), ["Artist - Track.flac"])

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_copy_fallback_never_writes_through_a_link_planted_at_the_temporary_name(self):
        first, second = self._path("One"), self._path("Two")
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"audio")
        victim = self.root / "victim.txt"
        victim.write_text("keep me", encoding="utf-8")
        second.parent.mkdir(parents=True, exist_ok=True)
        planted = Path(f"{second}.link.{os.getpid()}.{download.get_ident()}")
        planted.symlink_to(victim)

        with mock.patch.object(download, "__removeFile__"), \
             mock.patch.object(download.os, "link", side_effect=OSError("Operation not permitted")):
            with self.assertRaises(OSError):
                download.__placeExistingCopy__(str(first), str(second))

        self.assertEqual(victim.read_text(encoding="utf-8"), "keep me")
        self.assertFalse(second.exists())

    def test_disabled_setting_downloads_again_and_writes_no_index(self):
        download.SETTINGS.linkDuplicateTracks = False

        self._download("One")
        self._download("Two")

        self.assertEqual(self.get_stream.call_count, 2)
        self.assertFalse(os.path.samefile(self._path("One"), self._path("Two")))
        self.assertFalse((self.root / trackindex.TRACK_INDEX_FILE).exists())

    def test_check_exist_off_forces_a_download_without_touching_other_links(self):
        self._download("One")
        self._download("Two")
        download.SETTINGS.checkExist = False
        self.payload = b"fresh-download"

        self._download("Two")

        self.assertEqual(self._path("Two").read_bytes(), b"fresh-download")
        self.assertEqual(self._path("One").read_bytes(), b"flac-bytes")

    def test_decrypting_over_a_linked_file_leaves_the_other_link_alone(self):
        self._download("One")
        self._download("Two")
        encrypted = self.root / "track.part"
        encrypted.write_bytes(b"encrypted")
        stream = SimpleNamespace(encryptionKey="key")

        def fake_decrypt(source, target, key, nonce):
            with open(target, "wb") as output:
                output.write(b"decrypted")

        with mock.patch.object(download, "decrypt_security_token", return_value=(b"k", b"n")), \
             mock.patch.object(download, "decrypt_file", side_effect=fake_decrypt):
            download.__encrypted__(stream, str(encrypted), str(self._path("Two")))

        self.assertEqual(self._path("Two").read_bytes(), b"decrypted")
        self.assertEqual(self._path("One").read_bytes(), b"flac-bytes")

    def test_lyrics_file_follows_the_linked_track(self):
        download.SETTINGS.lyricFile = True
        self.lyrics = SimpleNamespace(subtitles="[00:01.00] line", lyrics="line")
        self._download("One")
        self.assertEqual(self._path("One", "Artist - Track.lrc").read_text(encoding="utf-8"), "[00:01.00] line")

        with mock.patch.object(download, "__getLyricsForTrack__") as get_lyrics:
            self._download("Two")

        get_lyrics.assert_not_called()
        self.assertEqual(self._path("Two", "Artist - Track.lrc").read_text(encoding="utf-8"), "[00:01.00] line")

    def test_atmos_copy_is_not_linked_under_a_plain_name(self):
        atmos = self._path("One", "Artist - Track [Dolby Atmos].m4a")
        atmos.parent.mkdir(parents=True)
        atmos.write_bytes(b"atmos")
        trackindex.registerTrack(str(self.root), 456, str(atmos), "DOLBY_ATMOS")

        self._download("Two")

        self.assertEqual(self.get_stream.call_count, 1)
        self.assertEqual(self._path("Two").read_bytes(), self.payload)

    def test_stream_dependent_name_links_only_a_copy_of_the_same_quality(self):
        download.SETTINGS.trackFileFormat = "{ArtistName} - {TrackTitle} [{StreamQuality}]"
        self._download("One")
        first = self._path("One", "Artist - Track [HiFi].flac")
        self.assertTrue(first.exists())

        # The name needs the stream, so the request is made, but not the download.
        self._download("Two")
        self.assertEqual(self.get_stream.call_count, 2)
        self.assertEqual(self.download_urls.call_count, 1)
        self.assertTrue(os.path.samefile(first, self._path("Two", "Artist - Track [HiFi].flac")))

        self.stream_quality = "HI_RES_LOSSLESS"
        self.payload = b"hires-bytes"
        self._download("Three")
        self.assertEqual(self.download_urls.call_count, 2)
        self.assertEqual(self._path("Three", "Artist - Track [Max].flac").read_bytes(), b"hires-bytes")
        self.assertEqual(first.read_bytes(), b"flac-bytes")


if __name__ == "__main__":
    unittest.main()
