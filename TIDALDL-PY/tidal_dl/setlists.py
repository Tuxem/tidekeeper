#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   setlists.py
@Desc    :   A list of TIDAL playlists (setlists) to keep up to date: register
             them once (or write them one per line in a list file), then
             ``--sync`` downloads whatever is new in each one, and ``--schedule``
             has cron run that every day. A TIDAL folder stands for all the
             playlists it holds, looked up again on every sync.
'''
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from urllib.parse import unquote, urlparse

from .enums import Type
from .events import start_playlist
from .paths import getSetlistsFilePath, getSetlistsPath, getSyncLogPath
from .printf import Printf
from .settings import SETTINGS
from .tidal import TIDAL_API

UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
CRON_MARKER = "# tidekeeper-sync"
COMMENT_PATTERN = re.compile(r"(^|\s)#.*$")
CRON_TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def __now__():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def loadSetlists():
    """Return the registered setlists; an unreadable file counts as empty."""
    try:
        with open(getSetlistsPath(), "r", encoding="utf-8") as source:
            data = json.load(source)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        logging.warning("Unable to read %s: %s", getSetlistsPath(), e)
        return []
    items = data.get("setlists") if isinstance(data, dict) else None
    return [item for item in items or [] if isinstance(item, dict) and item.get("uuid")]


def saveSetlists(items):
    path = getSetlistsPath()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tempPath = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tempPath, "w", encoding="utf-8") as output:
            json.dump({"setlists": items}, output, indent=2, ensure_ascii=False)
            output.write("\n")
        os.replace(tempPath, path)
    except Exception:
        try:
            os.remove(tempPath)
        except OSError:
            pass
        raise


def __updateSetlist__(uuid, **fields):
    items = loadSetlists()
    for item in items:
        if item["uuid"] == uuid:
            item.update(fields)
    saveSetlists(items)


def parsePlaylistId(text):
    """Return the playlist UUID in a TIDAL playlist URL, or ``text`` when it already is one."""
    text = (text or "").strip()
    kind, itemId = TIDAL_API.parseUrl(text)
    if kind == Type.Playlist:
        return itemId
    if kind == Type.Null and UUID_PATTERN.match(text):
        return text
    raise ValueError(f"Not a TIDAL playlist URL or id: {text}")


def parseFolderId(text):
    """Return the folder UUID in a TIDAL folder URL (``tidal.com/folder/<uuid>``), or None."""
    text = (text or "").strip()
    if "tidal.com" not in text:
        return None
    parts = [unquote(part) for part in urlparse(text).path.split("/") if part]
    for index in range(len(parts) - 1):
        if parts[index].lower() == "folder" and UUID_PATTERN.match(parts[index + 1]):
            return parts[index + 1]
    return None


def parseSource(text):
    """Return ``("folder" | "playlist", uuid)`` for a TIDAL folder or playlist URL, or a bare playlist id."""
    folderId = parseFolderId(text)
    if folderId:
        return "folder", folderId
    return "playlist", parsePlaylistId(text)


def addSetlist(text):
    """Register a playlist, or a folder of playlists (needs a login to check it exists)."""
    kind, uuid = parseSource(text)
    if kind == "folder":
        title = None
        TIDAL_API.getFolderPlaylists(uuid)
    else:
        title = TIDAL_API.getPlaylist(uuid).title
    items = loadSetlists()
    entry = next((item for item in items if item["uuid"] == uuid), None)
    if entry is None:
        entry = {"uuid": uuid, "added": __now__()}
        if kind == "folder":
            entry["kind"] = "folder"
        items.append(entry)
    if title:
        entry["title"] = title
    saveSetlists(items)
    return entry


def removeSetlist(text):
    """Unregister a playlist given its URL, UUID or (unique) title. Downloaded files stay."""
    text = (text or "").strip()
    items = loadSetlists()
    try:
        uuid = parseSource(text)[1]
    except ValueError:
        uuid = None
    matches = [item for item in items if item["uuid"] == uuid]
    if not matches:
        matches = [item for item in items if str(item.get("title", "")).casefold() == text.casefold()]
    if len(matches) != 1:
        raise ValueError(f"{'No' if not matches else 'Several'} registered setlist matches: {text}")
    saveSetlists([item for item in items if item["uuid"] != matches[0]["uuid"]])
    return matches[0]


def readSetlistsFile(filePath=None):
    """Return ``(lineNumber, text)`` for each setlist of a list file, one per line.

    Blank lines and ``#`` comments (a whole line, or after the URL) are ignored.
    Without ``filePath`` the default list file is used, and may not exist; a file
    asked for by name must exist.
    """
    path = filePath or getSetlistsFilePath()
    try:
        with open(path, "r", encoding="utf-8-sig") as source:
            lines = source.read().splitlines()
    except FileNotFoundError:
        if filePath:
            raise
        return []
    entries = []
    for number, line in enumerate(lines, 1):
        text = COMMENT_PATTERN.sub("", line).strip()
        if text:
            entries.append((number, text))
    return entries


def __fileSetlists__(filePath=None):
    """Return the ``(number, kind, uuid)`` of the list file and its unusable lines as ``(number, text, error)``."""
    found, invalid = [], []
    for number, text in readSetlistsFile(filePath):
        try:
            found.append((number, *parseSource(text)))
        except ValueError as e:
            invalid.append((number, text, str(e)))
    return found, invalid


def printSetlists(filePath=None):
    items = loadSetlists()
    found, invalid = __fileSetlists__(filePath)
    if not items and not found and not invalid:
        Printf.info("No setlists registered. Add one with: tidekeeper --setlist-add URL "
                    f"(or one per line in {filePath or getSetlistsFilePath()})")
        return items
    for item in items:
        status = item.get("lastStatus")
        last = f"last sync {item['lastSync']}: {status}" if item.get("lastSync") else "never synced"
        label = "folder " if item.get("kind") == "folder" else ""
        Printf.info(f"{label}{item.get('title') or item['uuid']} [{item['uuid']}] - {last}")
    registered = {item["uuid"].lower() for item in items}
    for number, kind, uuid in found:
        if uuid.lower() not in registered:
            Printf.info(f"{'folder ' if kind == 'folder' else ''}[{uuid}] - from the list file, line {number}")
    for number, text, error in invalid:
        Printf.err(f"List file, line {number}: {error}")
    return items


@contextmanager
def __syncLock__():
    """Hold an exclusive lock so a slow run and the next cron run never overlap."""
    lockPath = getSetlistsPath() + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lockPath)), exist_ok=True)
    handle = open(lockPath, "a+")
    locked = False
    try:
        try:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except ImportError:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError:
            pass
        yield locked
    finally:
        handle.close()


def __syncTargets__(filePath=None):
    """Return the playlists and folders to sync and the number of unusable list file lines.

    The registered ones come first, then the ones that only appear in the
    list file. Only registered ones keep their last sync outcome.
    """
    targets = [{"uuid": item["uuid"], "title": item.get("title"), "registered": True,
                "kind": item.get("kind", "playlist")} for item in loadSetlists()]
    known = {target["uuid"].lower() for target in targets}
    found, invalid = __fileSetlists__(filePath)
    for number, kind, uuid in found:
        if uuid.lower() not in known:
            known.add(uuid.lower())
            targets.append({"uuid": uuid, "title": None, "registered": False, "kind": kind})
    for number, text, error in invalid:
        Printf.err(f"List file, line {number}: {error}")
    return targets, len(invalid)


def __syncPlaylist__(uuid, title, registered):
    """Download what is new in one playlist; returns ``(ok, status)`` and never raises."""
    title = title or uuid
    try:
        playlist = TIDAL_API.getPlaylist(uuid)
        ok = start_playlist(playlist) is not False
        status = "ok" if ok else "some tracks failed (see failed-tracks.txt)"
        if playlist.title and registered:
            __updateSetlist__(uuid, title=playlist.title)
    except Exception as e:
        ok = False
        status = f"failed: {e}"
        Printf.err(f"{title}: {e}")
    return ok, status


def syncSetlists(filePath=None):
    """Download what is new in every setlist (registered or listed in the list file).

    Returns a process exit code.
    """
    targets, invalidLines = __syncTargets__(filePath)
    if not targets:
        Printf.info("No setlists registered. Add one with: tidekeeper --setlist-add URL "
                    f"(or one per line in {filePath or getSetlistsFilePath()})")
        return 1 if invalidLines else 0

    with __syncLock__() as locked:
        if not locked:
            Printf.err("Another sync is already running.")
            return 1

        # Unattended runs write to a log file, where progress bars are just noise.
        oldProgress = SETTINGS.showProgress
        if not sys.stdout.isatty():
            SETTINGS.showProgress = False
        total, failed = 0, 0
        # A playlist named on its own is synced where it stands; a folder skips it, and any it already did.
        done = {target["uuid"].lower() for target in targets if target["kind"] != "folder"}
        try:
            Printf.info(f"Sync started {__now__()}: {len(targets)} setlists")
            for target in targets:
                title = target["title"] or target["uuid"]
                if target["kind"] == "folder":
                    try:
                        members = TIDAL_API.getFolderPlaylists(target["uuid"])
                        if not members:
                            # Better a loud failure than a silent "up to date" if TIDAL's reply was not understood.
                            raise ValueError("no playlist found in it")
                    except Exception as e:
                        total += 1
                        failed += 1
                        status = f"failed: {e}"
                        Printf.err(f"Folder {title}: {e}")
                    else:
                        members = [member for member in members if member["uuid"].lower() not in done]
                        done.update(member["uuid"].lower() for member in members)
                        Printf.info(f"Folder {title}: {len(members)} playlists to sync")
                        results = [__syncPlaylist__(member["uuid"], member["title"], False)[0] for member in members]
                        total += len(results)
                        bad = results.count(False)
                        failed += bad
                        status = "ok" if not bad else f"{bad} playlists failed"
                    if target["registered"]:
                        __updateSetlist__(target["uuid"], lastSync=__now__(), lastStatus=status)
                    continue
                ok, status = __syncPlaylist__(target["uuid"], target["title"], target["registered"])
                total += 1
                failed += 0 if ok else 1
                if target["registered"]:
                    __updateSetlist__(target["uuid"], lastSync=__now__(), lastStatus=status)
        finally:
            SETTINGS.showProgress = oldProgress

    summary = f"Sync finished {__now__()}: {total - failed}/{total} setlists up to date"
    failed += invalidLines
    (Printf.success if not failed else Printf.err)(summary)
    return 1 if failed else 0


def __syncCommand__(filePath=None):
    command = [sys.executable, "--sync"] if getattr(sys, "frozen", False) else [sys.executable, "-m", "tidal_dl", "--sync"]
    if filePath:
        command += ["--setlist-file", os.path.abspath(filePath)]
    return command


def __cronLine__(hour, minute, filePath=None):
    command = " ".join(shlex.quote(part) for part in __syncCommand__(filePath))
    return f"{minute} {hour} * * * {command} >> {shlex.quote(getSyncLogPath())} 2>&1 {CRON_MARKER}"


def __readCrontab__():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    # "no crontab for user" exits non-zero with nothing on stdout.
    return result.stdout if result.returncode == 0 else ""


def __writeCrontab__(content):
    result = subprocess.run(["crontab", "-"], input=content, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "crontab refused the new table").strip())


def __withoutSyncLines__(crontab):
    return [line for line in crontab.splitlines() if CRON_MARKER not in line]


def scheduleSync(timeText, filePath=None):
    """Make cron run ``--sync`` every day at ``HH:MM`` (replaces an earlier schedule).

    ``filePath`` is a setlist list file to sync along with the registered setlists.
    """
    match = CRON_TIME_PATTERN.match((timeText or "").strip())
    if match is None:
        raise ValueError(f"Time must look like 03:30, got: {timeText}")
    if os.name == "nt":
        raise RuntimeError("cron is not available on Windows; use Task Scheduler to run: "
                           + " ".join(__syncCommand__(filePath)))
    if filePath and not os.path.isfile(filePath):
        raise FileNotFoundError(f"Setlist list file not found: {filePath}")
    hour, minute = int(match.group(1)), int(match.group(2))
    line = __cronLine__(hour, minute, filePath)
    __writeCrontab__("\n".join(__withoutSyncLines__(__readCrontab__()) + [line]) + "\n")
    return line


def unscheduleSync():
    """Remove the cron entry; returns False when there was none."""
    current = __readCrontab__()
    kept = __withoutSyncLines__(current)
    if len(kept) == len(current.splitlines()):
        return False
    __writeCrontab__("\n".join(kept) + "\n" if kept else "")
    return True
