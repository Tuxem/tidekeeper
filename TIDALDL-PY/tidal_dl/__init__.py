#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   __init__.py
@Time    :   2020/11/08
@Author  :   Yaronzz
@Version :   3.0
@Contact :   yaronhuang@foxmail.com
@Desc    :
'''
import sys
import getopt
import aigpy

from .events import *
from .settings import *
from .dedupe import dedupeLibrary
from .diagnostics import runDoctor
from .paths import getProfilePath, getTokenPath, openPath
from .printf import Printf
from .setlists import (addSetlist, printSetlists, removeSetlist, scheduleSync, syncSetlists,
                       unscheduleSync)
from .updater import run_update


def startGui():
    import importlib

    try:
        gui_module = importlib.import_module("tidal_dl.gui_app.__main__")
    except ImportError:
        Printf.err("GUI is not bundled with this executable. Run tidekeeper-gui or install tidekeeper[gui].")
        return 1
    return gui_module.main()


def mainCommand():
    try:
        opts, args = getopt.getopt(sys.argv[1:],
                                   "hvgl:o:q:r:",
                                   [
                                       "help", "version", "gui", "doctor",
                                       "update", "update-gui",
                                       "paths", "open-output",
                                       "dedupe", "dry-run",
                                       "setlist-add=", "setlist-remove=", "setlist-list",
                                       "sync", "schedule=", "unschedule",
                                       "video-only", "videos-only",
                                       "link=", "output=", "quality=", "quality-priority=", "resolution="
                                   ])
    except getopt.GetoptError as errmsg:
        Printf.err(vars(errmsg)['msg'] + ". Use 'tidekeeper -h' for usage.")
        return

    link = None
    showGui = False
    showDoctor = False
    updateInstall = False
    updateGuiInstall = False
    showPaths = False
    openOutput = False
    videoOnly = False
    dedupe = False
    dryRun = False
    setlistAdd = []
    setlistRemove = []
    setlistList = False
    sync = False
    schedule = None
    unschedule = False

    for opt, val in opts:
        if opt in ('-h', '--help'):
            Printf.usage()
            return
        if opt in ('-v', '--version'):
            Printf.logo()
            return
        if opt in ('-g', '--gui'):
            showGui = True
            continue
        if opt == '--doctor':
            showDoctor = True
            continue
        if opt == '--paths':
            showPaths = True
            continue
        if opt == '--open-output':
            openOutput = True
            continue
        if opt in ('--video-only', '--videos-only'):
            videoOnly = True
            continue
        if opt == '--dedupe':
            dedupe = True
            continue
        if opt == '--dry-run':
            dryRun = True
            continue
        if opt == '--setlist-add':
            setlistAdd.append(val)
            continue
        if opt == '--setlist-remove':
            setlistRemove.append(val)
            continue
        if opt == '--setlist-list':
            setlistList = True
            continue
        if opt == '--sync':
            sync = True
            continue
        if opt == '--schedule':
            schedule = val
            continue
        if opt == '--unschedule':
            unschedule = True
            continue
        if opt == '--update':
            updateInstall = True
            continue
        if opt == '--update-gui':
            updateInstall = True
            updateGuiInstall = True
            continue
        if opt in ('-l', '--link'):
            link = val
            continue
        if opt in ('-o', '--output'):
            SETTINGS.downloadPath = val
            SETTINGS.save()
            continue
        if opt in ('-q', '--quality'):
            SETTINGS.audioQuality = SETTINGS.getAudioQuality(val)
            SETTINGS.audioQualityPriority = []
            SETTINGS.save()
            continue
        if opt in ('--quality-priority',):
            SETTINGS.audioQualityPriority = SETTINGS.getAudioQualityPriority(val)
            if SETTINGS.audioQualityPriority:
                SETTINGS.audioQuality = SETTINGS.audioQualityPriority[0]
            SETTINGS.save()
            continue
        if opt in ('-r', '--resolution'):
            SETTINGS.videoQuality = SETTINGS.getVideoQuality(val)
            SETTINGS.save()
            continue

    if showDoctor:
        runDoctor()
        return

    if showPaths:
        Printf.paths()
        return

    if openOutput:
        try:
            opened = openPath(SETTINGS.downloadPath)
            Printf.success("Opened download folder: " + opened)
        except OSError as exc:
            Printf.err("Could not open download folder: " + str(exc))
        return

    if updateInstall:
        updateTidekeeper(updateGuiInstall)
        return

    if setlistRemove or setlistList or schedule is not None or unschedule:
        code = 0
        for item in setlistRemove:
            try:
                Printf.success("Removed setlist: " + str(removeSetlist(item).get("title") or item))
            except ValueError as e:
                Printf.err(str(e))
                code = 1
        if setlistList:
            printSetlists()
        if schedule is not None:
            try:
                Printf.success("Daily sync scheduled: " + scheduleSync(schedule))
            except (ValueError, RuntimeError, OSError) as e:
                Printf.err(str(e))
                code = 1
        if unschedule:
            try:
                if unscheduleSync():
                    Printf.success("Daily sync unscheduled.")
                else:
                    Printf.info("No daily sync was scheduled.")
            except (RuntimeError, OSError) as e:
                Printf.err(str(e))
                code = 1
        return code

    if not aigpy.path.mkdirs(SETTINGS.downloadPath):
        Printf.err(LANG.select.MSG_PATH_ERR + SETTINGS.downloadPath)
        return

    if showGui:
        startGui()
        return

    if sync:
        # Runs from cron: never wait for a web login, just say how to fix it.
        if not loginByConfig():
            Printf.err("Not logged in. Run tidekeeper once interactively to log in, then retry.")
            return 1
        Printf.info(LANG.select.SETTING_DOWNLOAD_PATH + ':' + SETTINGS.downloadPath)
        return syncSetlists()

    if setlistAdd:
        if not loginByConfig() and not loginByWeb():
            return 1
        code = 0
        for item in setlistAdd:
            try:
                entry = addSetlist(item)
                Printf.success(f"Registered setlist: {entry['title']} [{entry['uuid']}]")
            except Exception as e:
                Printf.err(f"{item}: {e}")
                code = 1
        return code

    if dedupe:
        if not loginByConfig() and not loginByWeb():
            return
        Printf.info(LANG.select.SETTING_DOWNLOAD_PATH + ':' + SETTINGS.downloadPath)
        dedupeLibrary(dryRun)
        return

    if link is not None:
        if not loginByConfig() and not loginByWeb():
            return
        Printf.info(LANG.select.SETTING_DOWNLOAD_PATH + ':' + SETTINGS.downloadPath)
        start(link, videoOnly)


def normalizeChoice(choice):
    aliases = {
        "": "",
        "q": "0",
        "quit": "0",
        "exit": "0",
        "login": "1",
        "signin": "1",
        "sign-in": "1",
        "refresh": "1",
        "logout": "2",
        "signout": "2",
        "sign-out": "2",
        "token": "3",
        "access-token": "3",
        "path": "4",
        "paths": "4",
        "folder": "4",
        "quality": "5",
        "options": "6",
        "settings": "6",
        "client": "7",
        "apikey": "7",
        "api-key": "7",
        "show": "8",
        "status": "8",
        "help": "8",
        "all": "8",
        "update": "9",
        "upgrade": "9",
        "clear": "clear",
        "cls": "clear",
    }
    clean = choice.strip()
    return aliases.get(clean.lower(), clean)


def updateTidekeeper(include_gui=False):
    Printf.info("Updating Tidekeeper...")
    result = run_update(include_gui)
    if result.command:
        Printf.info("Command: " + " ".join(result.command))
    if result.output.strip():
        print(result.output.strip())
    if result.ok:
        Printf.success(result.message)
    elif result.standalone:
        Printf.info(result.message)
    else:
        Printf.err(result.message)
    return result.ok


def main():
    SETTINGS.read(getProfilePath())
    TOKEN.read(getTokenPath())
    if not apiKey.isItemValid(SETTINGS.apiKeyIndex):
        SETTINGS.apiKeyIndex = apiKey.getDefaultIndex()
        SETTINGS.save()
    TIDAL_API.apiKey = apiKey.getItem(SETTINGS.apiKeyIndex)

    if len(sys.argv) > 1:
        return mainCommand()

    if not loginByConfig():
        loginByWeb()

    Printf.checkVersion()

    while True:
        Printf.choices()
        choice = normalizeChoice(Printf.enter("Paste URL or choice > "))
        if choice == "":
            continue
        if choice == "clear":
            Printf.clearScreen()
            continue
        if choice == "0":
            return
        elif choice == "1":
            if not loginByConfig():
                loginByWeb()
        elif choice == "2":
            logout()
        elif choice == "3":
            loginByAccessToken()
        elif choice == "4":
            changePathSettings()
        elif choice == "5":
            changeQualitySettings()
        elif choice == "6":
            changeSettings()
        elif choice == "7":
            if changeApiKey():
                loginByWeb()
        elif choice == "8":
            Printf.settings()
        elif choice == "9":
            updateTidekeeper(False)
        else:
            start(choice)


def test():
    SETTINGS.read(getProfilePath())
    TOKEN.read(getTokenPath())

    if not loginByConfig():
        loginByWeb()

    SETTINGS.audioQuality = AudioQuality.Master
    SETTINGS.videoFileFormat = VideoQuality.P240
    SETTINGS.checkExist = False
    SETTINGS.includeEP = True
    SETTINGS.saveCovers = True
    SETTINGS.lyricFile = True
    SETTINGS.showProgress = True
    SETTINGS.showTrackInfo = True
    SETTINGS.saveAlbumInfo = True
    SETTINGS.downloadVideos = True
    SETTINGS.downloadPath = "./download/"
    SETTINGS.usePlaylistFolder = True
    SETTINGS.albumFolderFormat = R"{ArtistName}/{Flag} {AlbumTitle} [{AlbumID}] [{AlbumYear}]"
    SETTINGS.playlistFolderFormat = R"Playlist/{PlaylistName} [{PlaylistUUID}]"
    SETTINGS.trackFileFormat = R"{TrackNumber} - {ArtistName} - {TrackTitle}{ExplicitFlag}"
    SETTINGS.videoFileFormat = R"{VideoNumber} - {ArtistName} - {VideoTitle}{ExplicitFlag}"
    SETTINGS.multiThread = False
    SETTINGS.apiKeyIndex = 4
    SETTINGS.checkExist = False

    Printf.settings()

    TIDAL_API.getPlaylistSelf()
    # test example
    # https://tidal.com/browse/track/70973230
    # track 70973230  77798028 212657
    start('242700165')
    # album 58138532  77803199  21993753   79151897  56288918
    # start('58138532')
    # playlist 98235845-13e8-43b4-94e2-d9f8e603cee7
    # start('98235845-13e8-43b4-94e2-d9f8e603cee7')
    # video 155608351 188932980 https://tidal.com/browse/track/55130637
    # start("155608351")https://tidal.com/browse/track/199683732


if __name__ == '__main__':
    # test()
    main()
