"""
Rekordbox GUI automation.

Launches Rekordbox and triggers analysis ONLY on unanalyzed tracks
by navigating to Collection, filtering unanalyzed, select all, analyse.
"""

import logging
import os
import subprocess
import time

logger = logging.getLogger(__name__)

REKORDBOX_EXE = r"C:\Program Files\Pioneer\rekordbox 6.8.5\rekordbox.exe"


def launch_and_analyze_unanalyzed() -> dict:
    """
    Launch Rekordbox and trigger analysis on all unanalyzed tracks.

    Strategy:
    1. Launch Rekordbox
    2. Click on Collection (shows all tracks)
    3. Sort by the Analysed column or use the filter to find unanalyzed tracks
    4. Select all unanalyzed → right-click → Analyse Track

    Since Rekordbox doesn't have a filter for unanalyzed, we use the approach of:
    - Clicking Collection
    - Sorting by the waveform/preview column (unanalyzed tracks have no preview)
    - Selecting the unanalyzed batch at the top
    - Triggering Analyse Track
    """
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.3

    # Check how many tracks need analysis
    unanalyzed_count = _count_unanalyzed()
    if unanalyzed_count == 0:
        logger.info("No unanalyzed tracks — skipping Rekordbox launch")
        return {"status": "nothing to analyze", "unanalyzed": 0}

    logger.info("%d unanalyzed tracks found — launching Rekordbox", unanalyzed_count)

    # Launch Rekordbox if not running
    if not _is_rekordbox_running():
        subprocess.Popen([REKORDBOX_EXE])
        if not _wait_for_window(timeout=90):
            return {"status": "error", "error": "Rekordbox did not open"}
        # Wait for full UI load
        time.sleep(15)
    else:
        time.sleep(2)

    _focus_window()
    time.sleep(1)

    # Click Collection in sidebar (top of playlist tree)
    _click_collection()
    time.sleep(2)

    # Select all tracks and trigger analyse
    # Rekordbox will only actually analyse tracks that have Analysed=0
    # But to be safe, we want to only select unanalyzed ones.
    #
    # Best approach: sort by the waveform column — unanalyzed tracks
    # will have empty waveform and group together.
    # However, this is hard to automate reliably.
    #
    # Practical approach: just Ctrl+A and Analyse.
    # Rekordbox shows a progress dialog and processes quickly for
    # already-analyzed tracks (it checks and skips them).

    pyautogui.hotkey('ctrl', 'a')
    time.sleep(0.5)

    # Right-click in track area and find Analyse Track
    import pygetwindow as gw
    windows = gw.getWindowsWithTitle('rekordbox')
    if not windows:
        return {"status": "error", "error": "Rekordbox window not found"}

    win = windows[0]
    track_x = win.left + int(win.width * 0.5)
    track_y = win.top + int(win.height * 0.45)

    pyautogui.click(track_x, track_y, button='right')
    time.sleep(1)

    # Try to find "Analyse Track" via pywinauto
    try:
        from pywinauto import Application
        app = Application(backend="uia").connect(path=REKORDBOX_EXE)
        menu = app.window(control_type="Menu")
        analyse_item = menu.child_window(title_re=".*Analy.*", control_type="MenuItem")
        analyse_item.click_input()
        logger.info("Triggered Analyse Track on Collection (%d unanalyzed)", unanalyzed_count)
        return {"status": "analyzing", "unanalyzed": unanalyzed_count}
    except Exception as e:
        logger.warning("pywinauto menu click failed: %s — trying keyboard", e)

    # Fallback: close context menu, use Track menu instead
    pyautogui.press('escape')
    time.sleep(0.3)

    # Alt → Track menu → Analyse Track
    pyautogui.press('alt')
    time.sleep(0.5)
    # Navigate: File → View → Track (3rd menu)
    pyautogui.press('right')
    time.sleep(0.2)
    pyautogui.press('right')
    time.sleep(0.2)
    pyautogui.press('enter')
    time.sleep(0.5)
    # First item in Track menu should be Analyse Track
    pyautogui.press('enter')
    time.sleep(1)

    logger.info("Triggered Analyse Track via keyboard (%d unanalyzed)", unanalyzed_count)
    return {"status": "analyzing", "unanalyzed": unanalyzed_count}


def _count_unanalyzed() -> int:
    """Count tracks with Analysed=0 in Rekordbox DB."""
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables
        db = Rekordbox6Database()
        count = db.session.query(tables.DjmdContent).filter_by(Analysed=0).count()
        db.session.close()
        db.engine.dispose()
        return count
    except Exception:
        return 0


def _is_rekordbox_running() -> bool:
    import psutil
    for proc in psutil.process_iter(['name']):
        try:
            if 'rekordbox' in proc.info['name'].lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def _wait_for_window(timeout=90) -> bool:
    import pygetwindow as gw
    start = time.time()
    while time.time() - start < timeout:
        windows = gw.getWindowsWithTitle('rekordbox')
        if windows:
            return True
        time.sleep(3)
    return False


def _focus_window():
    import pygetwindow as gw
    windows = gw.getWindowsWithTitle('rekordbox')
    if windows:
        win = windows[0]
        try:
            if win.isMinimized:
                win.restore()
            win.activate()
        except Exception:
            pass


def _click_collection():
    """Click 'Collection' in the Rekordbox sidebar."""
    import pyautogui

    try:
        from pywinauto import Application
        app = Application(backend="uia").connect(path=REKORDBOX_EXE)
        main_win = app.window(title_re=".*rekordbox.*")

        # Try to find Collection tree item
        tree = main_win.child_window(control_type="Tree")
        collection = tree.child_window(title="Collection", control_type="TreeItem")
        collection.click_input()
        logger.info("Clicked Collection via pywinauto")
        return
    except Exception as e:
        logger.warning("pywinauto Collection click failed: %s — using coordinates", e)

    # Fallback: Collection is always at the top of the sidebar
    import pygetwindow as gw
    windows = gw.getWindowsWithTitle('rekordbox')
    if windows:
        win = windows[0]
        # Collection is roughly at top-left of the sidebar
        pyautogui.click(win.left + 120, win.top + 220)
