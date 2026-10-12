#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   dedupe.py
@Desc    :   Index the tracks already in the download folder and replace the
             duplicate copies of a track with hard links to a single file.
'''
import os
import re
import unicodedata

import aigpy

from .download import __placeExistingCopy__
from .enums import Type
from .paths import (TRACK_EXTENSION_CANDIDATES, getAlbumPath, getPlaylistPath, getTrackPathCandidates,
                    trackPathNeedsStream)
from .printf import Printf
from .settings import SETTINGS
from .tidal import TIDAL_API
from .trackindex import allTrackCopies, registerTrack

COMPARE_CHUNK_SIZE = 1024 * 1024

# Track file formats that used to be the default. Libraries downloaded with
# them still carry those names, so they are tried after the configured format.
FORMER_TRACK_FORMATS = (R"{TrackNumber} - {ArtistName} - {TrackTitle}{ExplicitFlag}",)


def __folderPatterns__(folderFormat, idToken, idPattern):
    """Turn a folder format into one regex per path segment.

    ``idToken`` (``{PlaylistUUID}``/``{AlbumID}``) becomes the ``id`` group, the
    other tokens match anything. Returns None when the format has no id, since
    folders can then not be traced back to TIDAL.
    """
    patterns = []
    hasId = False
    for segment in (folderFormat or "").replace("\\", "/").split("/"):
        segment = segment.strip()
        if segment in ("", "."):
            continue
        regex = ""
        for part in re.split(r"(\{[A-Za-z]+\}|\s+)", segment):
            if part == idToken and not hasId:
                regex += f"(?P<id>{idPattern})"
                hasId = True
            elif part.startswith("{") and part.endswith("}"):
                regex += ".*"
            elif part.strip() == "":
                regex += r"\s*" if part else ""
            else:
                regex += re.escape(part)
        patterns.append(re.compile(r"\s*" + regex + r"\s*", re.DOTALL))
    return patterns if hasId else None


def __findFolders__(root, patterns):
    """Return ``(id, folder)`` for the folders under ``root`` named after ``patterns``."""
    found = []

    def walk(folder, depth, foundId):
        if depth == len(patterns):
            found.append((foundId, folder))
            return
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            match = patterns[depth].fullmatch(entry.name)
            if match is None or not entry.is_dir():
                continue
            walk(entry.path, depth + 1, match.groupdict().get("id") or foundId)

    walk(root, 0, None)
    return found


def __tidalFolders__(root):
    """Return ``(type, id, folder)`` for the playlist and album folders in the library.

    Those folders carry their TIDAL id in their name, which is what tells them
    apart from anything else stored in the download folder.
    """
    folders = []
    for kind, folderFormat, idToken, idPattern in (
        (Type.Playlist, SETTINGS.playlistFolderFormat, "{PlaylistUUID}", r"[0-9a-fA-F-]{36}"),
        (Type.Album, SETTINGS.albumFolderFormat, "{AlbumID}", r"\d+"),
    ):
        patterns = __folderPatterns__(folderFormat or SETTINGS.getDefaultPathFormat(kind), idToken, idPattern)
        if patterns is None:
            Printf.info(f"{kind.name} folders are not scanned: their folder format has no {idToken}.")
            continue
        folders += [(kind, itemId, folder) for itemId, folder in __findFolders__(root, patterns)]
    return folders


def __folderTracks__(kind, itemId, albums):
    """Return ``(track, album, playlist, expectedFolder)`` for the tracks TIDAL lists in a folder."""
    if kind == Type.Album:
        album = TIDAL_API.getAlbum(itemId)
        tracks, _ = TIDAL_API.getItems(itemId, Type.Album)
        return [(track, album, None, getAlbumPath(album)) for track in tracks]

    playlist = TIDAL_API.getPlaylist(itemId)
    tracks, _ = TIDAL_API.getItems(itemId, Type.Playlist)
    trackFormat = SETTINGS.trackFileFormat or SETTINGS.getDefaultPathFormat(Type.Track)
    needAlbum = "{AlbumTitle}" in trackFormat or "{AlbumYear}" in trackFormat
    items = []
    for index, track in enumerate(tracks):
        track.trackNumberOnPlaylist = index + 1
        album = None
        if needAlbum:
            albumId = track.album.id
            if albumId not in albums:
                albums[albumId] = TIDAL_API.getAlbum(albumId)
            album = albums[albumId]
        items.append((track, album, playlist, getPlaylistPath(playlist)))
    return items


def __existingTrackFile__(track, album, playlist, expectedFolder, folder):
    """Return the file holding ``track`` in ``folder``, named as a download would name it.

    ``folder`` is the folder found on disk for the playlist/album id. It can
    differ from ``expectedFolder`` when the playlist or album was renamed on
    TIDAL after it was downloaded, so the expected file name is looked up there.
    """
    for trackFormat in (None,) + FORMER_TRACK_FORMATS:
        for path in getTrackPathCandidates(track, album, playlist, trackFormat) or []:
            local = os.path.join(folder, os.path.relpath(path, expectedFolder))
            if aigpy.file.getSize(local) > 0:
                return local
    return None


def scanLibrary():
    """Match the files already in the download folder to TIDAL tracks.

    Each playlist and album folder is looked up on TIDAL and every track is
    searched under the file name a download would give it, which is the same
    test the downloader uses to skip existing files. Returns the TIDAL folders
    found on disk and the ``(trackId, path)`` matches.
    """
    root = os.path.abspath(SETTINGS.downloadPath or ".")
    folders = __tidalFolders__(root)
    found = []
    albums = {}
    matchNames = not trackPathNeedsStream()
    if not matchNames:
        Printf.info("Track file names depend on the stream ({StreamQuality}/{Codec} in the track format, or Atmos "
                    "quality), so existing files cannot be matched to TIDAL tracks. Identical files are still merged.")

    for kind, itemId, folder in folders:
        # Without playlist folders a playlist's tracks are named after their album folder.
        if not matchNames or (kind == Type.Playlist and not SETTINGS.usePlaylistFolder):
            continue
        name = os.path.relpath(folder, root)
        try:
            items = __folderTracks__(kind, itemId, albums)
        except Exception as e:
            Printf.info(f"{name}: not looked up on TIDAL ({e})")
            continue
        matched = 0
        for track, album, playlist, expectedFolder in items:
            path = __existingTrackFile__(track, album, playlist, expectedFolder, folder)
            if path is not None:
                found.append((track.id, path))
                matched += 1
        Printf.info(f"{name}: {matched}/{len(items)} tracks on disk")
    return [folder for _, _, folder in folders], found


def __nameOnDisk__(path, listings):
    """Return ``path`` spelled as its folder lists it.

    On a case-insensitive filesystem (SMB shares, macOS, Windows) a file also
    answers to other spellings, e.g. the one TIDAL uses since it re-capitalized
    a title. Left alone, the two spellings would pass for two hard links.
    """
    parent, name = os.path.split(path)
    if parent not in listings:
        try:
            listings[parent] = {__spelling__(item): item for item in os.listdir(parent)}
        except OSError:
            listings[parent] = {}
    return os.path.join(parent, listings[parent].get(__spelling__(name), name))


def __spelling__(name):
    return unicodedata.normalize("NFC", name).casefold()


def __audioFiles__(folder):
    files = []
    for parent, _, names in os.walk(folder):
        files += [os.path.join(parent, name) for name in names
                  if os.path.splitext(name)[1].lower() in TRACK_EXTENSION_CANDIDATES]
    return files


def __sameContent__(first, second):
    with open(first, "rb") as left, open(second, "rb") as right:
        while True:
            chunk = left.read(COMPARE_CHUNK_SIZE)
            if chunk != right.read(COMPARE_CHUNK_SIZE):
                return False
            if not chunk:
                return True


def __sizeText__(size):
    return f"{size / (1024 ** 3):.2f} GiB" if size >= 1024 ** 3 else f"{size / (1024 ** 2):.1f} MiB"


def linkDuplicates(paths, dryRun=False):
    """Replace the byte-identical files among ``paths`` with hard links to one of them.

    Only identical files are merged, so every path keeps exactly the content it
    had; a file that cannot be hard-linked is left untouched. Returns the
    counters and ``{path: identical file kept}`` for every duplicate found,
    whether it was linked now, already was, or could not be.
    """
    root = os.path.abspath(SETTINGS.downloadPath or ".")
    stats = {"linked": 0, "freed": 0, "alreadyLinked": 0, "different": 0, "failed": 0}
    originals = {}

    bySize = {}
    for path in sorted({os.path.abspath(path) for path in paths}):
        size = aigpy.file.getSize(path)
        if size > 0:
            bySize.setdefault(size, []).append(path)

    for size, group in sorted(bySize.items(), key=lambda item: item[1]):
        kept = []
        for path in group:
            name = os.path.relpath(path, root)
            try:
                original = None
                alreadyLinked = False
                for candidate in kept:
                    alreadyLinked = os.path.samefile(candidate, path)
                    if alreadyLinked or __sameContent__(candidate, path):
                        original = candidate
                        break
                if original is None:
                    kept.append(path)
                    continue
                originals[path] = original
                if alreadyLinked:
                    stats["alreadyLinked"] += 1
                    continue
                if not dryRun:
                    __placeExistingCopy__(original, path, allowCopy=False)
            except Exception as e:
                stats["failed"] += 1
                Printf.err(f"{name}: {e}")
                continue
            stats["linked"] += 1
            stats["freed"] += size
            Printf.success(f"{name} -> {os.path.relpath(original, root)}" + (" (dry run)" if dryRun else ""))
    return stats, originals


def dedupeLibrary(dryRun=False):
    """Index the tracks already downloaded, then hard-link their duplicate copies.

    With ``dryRun`` nothing is written: neither the index nor the links.
    """
    root = os.path.abspath(SETTINGS.downloadPath or ".")
    folders, found = scanLibrary()

    trackIds = {}
    listings = {}
    known = [(trackId, path) for trackId, copies in allTrackCopies(root).items() for path, _ in copies]
    for trackId, path in known + found:
        trackIds.setdefault(__nameOnDisk__(os.path.abspath(path), listings), set()).add(str(trackId))

    files = set(trackIds)
    for folder in folders:
        files.update(os.path.abspath(path) for path in __audioFiles__(folder))
    stats, originals = linkDuplicates(files, dryRun)

    # A file identical to an indexed one holds the same track, even where TIDAL
    # no longer lists it (deleted playlist, track removed from a playlist).
    identical = {}
    for path, original in originals.items():
        identical.setdefault(original, {original}).add(path)
    for group in identical.values():
        known = set().union(*(trackIds.get(path, set()) for path in group))
        if known:
            for path in group:
                trackIds[path] = known

    if not dryRun:
        for path in sorted(trackIds):
            for trackId in sorted(trackIds[path]):
                registerTrack(root, int(trackId) if trackId.isdigit() else trackId, path)

    copies = {}
    for path, known in trackIds.items():
        for trackId in known:
            copies.setdefault(trackId, set()).add(originals.get(path, path))
    for trackId in sorted(copies):
        if len(copies[trackId]) > 1:
            stats["different"] += len(copies[trackId]) - 1
            Printf.info("Same track, different content, left as is: "
                        + " | ".join(os.path.relpath(path, root) for path in sorted(copies[trackId])))

    Printf.info(f"{len(trackIds)} files indexed for {len(copies)} tracks"
                + (" (dry run, index not written)" if dryRun else ""))
    verb = "would be replaced" if dryRun else "replaced"
    Printf.success(f"{stats['linked']} duplicate files {verb} with hard links ({__sizeText__(stats['freed'])})")
    if stats["alreadyLinked"]:
        Printf.info(f"{stats['alreadyLinked']} files were already hard links")
    if stats["different"]:
        Printf.info(f"{stats['different']} copies differ from another copy of their track and were left as is")
    if stats["failed"]:
        Printf.err(f"{stats['failed']} files could not be linked and were left as is")
    return stats
