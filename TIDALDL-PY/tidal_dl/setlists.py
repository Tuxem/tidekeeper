#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   setlists.py
@Desc    :   A list of TIDAL playlists (setlists) to keep up to date: register
             them once, then ``--sync`` downloads whatever is new in each one,
             and ``--schedule`` has cron run that every day.
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

from .enums import Type
from .events import start_playlist
from .paths import getSetlistsPath, getSyncLogPath
from .printf import Printf
from .settings import SETTINGS
from .tidal import TIDAL_API

UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
CRON_MARKER = "# tidekeeper-sync"
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


def addSetlist(text):
    """Register a playlist (needs a login to check it exists and read its title)."""
    uuid = parsePlaylistId(text)
    playlist = TIDAL_API.getPlaylist(uuid)
    items = loadSetlists()
    entry = next((item for item in items if item["uuid"] == uuid), None)
    if entry is None:
        entry = {"uuid": uuid, "added": __now__()}
        items.append(entry)
    entry["title"] = playlist.title
    saveSetlists(items)
    return entry


def removeSetlist(text):
    """Unregister a playlist given its URL, UUID or (unique) title. Downloaded files stay."""
    text = (text or "").strip()
    items = loadSetlists()
    try:
        uuid = parsePlaylistId(text)
    except ValueError:
        uuid = None
    matches = [item for item in items if item["uuid"] == uuid]
    if not matches:
        matches = [item for item in items if str(item.get("title", "")).casefold() == text.casefold()]
    if len(matches) != 1:
        raise ValueError(f"{'No' if not matches else 'Several'} registered setlist matches: {text}")
    saveSetlists([item for item in items if item["uuid"] != matches[0]["uuid"]])
    return matches[0]


def printSetlists():
    items = loadSetlists()
    if not items:
        Printf.info("No setlists registered. Add one with: tidekeeper --setlist-add URL")
        return items
    for item in items:
        status = item.get("lastStatus")
        last = f"last sync {item['lastSync']}: {status}" if item.get("lastSync") else "never synced"
        Printf.info(f"{item.get('title') or item['uuid']} [{item['uuid']}] - {last}")
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


def syncSetlists():
    """Download what is new in every registered setlist. Returns a process exit code."""
    items = loadSetlists()
    if not items:
        Printf.info("No setlists registered. Add one with: tidekeeper --setlist-add URL")
        return 0

    with __syncLock__() as locked:
        if not locked:
            Printf.err("Another sync is already running.")
            return 1

        # Unattended runs write to a log file, where progress bars are just noise.
        oldProgress = SETTINGS.showProgress
        if not sys.stdout.isatty():
            SETTINGS.showProgress = False
        failed = 0
        try:
            Printf.info(f"Sync started {__now__()}: {len(items)} setlists")
            for item in items:
                title = item.get("title") or item["uuid"]
                try:
                    playlist = TIDAL_API.getPlaylist(item["uuid"])
                    ok = start_playlist(playlist) is not False
                    status = "ok" if ok else "some tracks failed (see failed-tracks.txt)"
                    if playlist.title:
                        title = playlist.title
                        __updateSetlist__(item["uuid"], title=title)
                except Exception as e:
                    ok = False
                    status = f"failed: {e}"
                    Printf.err(f"{title}: {e}")
                __updateSetlist__(item["uuid"], lastSync=__now__(), lastStatus=status)
                failed += 0 if ok else 1
        finally:
            SETTINGS.showProgress = oldProgress

    summary = f"Sync finished {__now__()}: {len(items) - failed}/{len(items)} setlists up to date"
    (Printf.success if not failed else Printf.err)(summary)
    return 1 if failed else 0


def __syncCommand__():
    if getattr(sys, "frozen", False):
        return [sys.executable, "--sync"]
    return [sys.executable, "-m", "tidal_dl", "--sync"]


def __cronLine__(hour, minute):
    command = " ".join(shlex.quote(part) for part in __syncCommand__())
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


def scheduleSync(timeText):
    """Make cron run ``--sync`` every day at ``HH:MM`` (replaces an earlier schedule)."""
    match = CRON_TIME_PATTERN.match((timeText or "").strip())
    if match is None:
        raise ValueError(f"Time must look like 03:30, got: {timeText}")
    if os.name == "nt":
        raise RuntimeError("cron is not available on Windows; use Task Scheduler to run: "
                           + " ".join(__syncCommand__()))
    hour, minute = int(match.group(1)), int(match.group(2))
    line = __cronLine__(hour, minute)
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
