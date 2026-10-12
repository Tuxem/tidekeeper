#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   trackindex.py
@Desc    :   Index of the track files already present in a download folder,
             so a track that shows up again (another playlist, its album) can
             be linked to the existing copy instead of being downloaded twice.
'''
import json
import logging
import os
from threading import Lock

import aigpy

# One JSON object per line ({"id", "path", "quality"}), appended as tracks are
# downloaded. Lives in the download folder so it follows the library around;
# paths inside that folder are stored relative to it.
TRACK_INDEX_FILE = ".tidekeeper-tracks.jsonl"

track_index_lock = Lock()
track_index_cache = {}


def __root__(root):
    return os.path.abspath(root or ".")


def __indexPath__(root):
    return os.path.join(__root__(root), TRACK_INDEX_FILE)


def __toStored__(root, path):
    path = os.path.abspath(path)
    try:
        relative = os.path.relpath(path, __root__(root))
    except ValueError:
        # Windows: the file is on another drive than the download folder.
        return path
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return path
    return relative.replace(os.sep, "/")


def __fromStored__(root, stored):
    if os.path.isabs(stored):
        return stored
    return os.path.join(__root__(root), *stored.split("/"))


def __load__(root):
    indexPath = __indexPath__(root)
    entries = track_index_cache.get(indexPath)
    if entries is not None:
        return entries

    entries = {}
    try:
        with open(indexPath, "r", encoding="utf-8") as source:
            for line in source:
                # A truncated or hand-edited line only loses that one entry.
                try:
                    item = json.loads(line)
                    entries.setdefault(str(item["id"]), {})[str(item["path"])] = item.get("quality")
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    except FileNotFoundError:
        pass
    except OSError as e:
        logging.warning("Unable to read track index %s: %s", indexPath, e)
    track_index_cache[indexPath] = entries
    return entries


def findTrackCopies(root, trackId):
    """Return ``(path, quality)`` for every indexed copy of ``trackId`` still on disk.

    ``quality`` is the stream sound quality recorded at download time, or None
    when the file was indexed without fetching its stream.
    """
    with track_index_lock:
        known = dict(__load__(root).get(str(trackId)) or {})
    copies = []
    for stored, quality in known.items():
        path = __fromStored__(root, stored)
        if aigpy.file.getSize(path) > 0:
            copies.append((path, quality))
    return copies


def allTrackCopies(root):
    """Return ``{trackId: [(path, quality), ...]}`` for every indexed track still on disk."""
    with track_index_lock:
        trackIds = list(__load__(root))
    copies = {}
    for trackId in trackIds:
        found = findTrackCopies(root, trackId)
        if found:
            copies[trackId] = found
    return copies


def registerTrack(root, trackId, path, quality=None):
    """Record that ``path`` holds ``trackId``. A known quality is never erased by None."""
    stored = __toStored__(root, path)
    with track_index_lock:
        known = __load__(root).setdefault(str(trackId), {})
        if stored in known and (quality is None or known[stored] == quality):
            return
        known[stored] = quality
        indexPath = __indexPath__(root)
        os.makedirs(os.path.dirname(indexPath), exist_ok=True)
        entry = json.dumps({"id": trackId, "path": stored, "quality": quality}, ensure_ascii=False)
        with open(indexPath, "a", encoding="utf-8") as output:
            output.write(entry + "\n")
