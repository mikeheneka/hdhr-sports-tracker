#!/usr/bin/env python3
"""
Live Multimodal Stream Tracker & Web Dashboard for HDHomeRun on Apple Silicon.

Features:
- Thread 1 (Captions): Real-time play-by-play commentary via ccextractor.
- Thread 2 (Vision & OCR): Periodically grabs broadcast frames from the HDHomeRun tuner,
  isolates the scorebug region, and uses local OCR (Tesseract) to track:
  * Team scores (with persistent state memory)
  * Down & Distance
  * Quarter, Game Clock, and Play Clock
  * Possession (via scorebug chevron luminance detection)
  * Field position (via caption parsing and goal-line tracking)
- Thread 3 (Web Dashboard): Built-in zero-dependency HTTP server on http://localhost:8080
  serving a live broadcast scoreboard, interactive 100-yard football field, and auto-scrolling play-by-play commentary feed.
"""

import argparse
from collections import deque
from datetime import datetime
import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

# Ensure binary dependencies
TESSERACT_BIN = shutil.which("tesseract") or "/opt/homebrew/bin/tesseract"
FFMPEG_BIN = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
CCEXTRACTOR_BIN = shutil.which("ccextractor") or "/opt/homebrew/bin/ccextractor"

stop_event = threading.Event()
child_procs = []


def register_process(proc):
    child_procs.append(proc)


def signal_handler(sig, frame):
    stop_event.set()
    for p in child_procs:
        try:
            p.terminate()
        except Exception:
            pass
    print("\n[Tracker] Stopping live tracker and saving session log...", flush=True)
    if 'global_tracker' in globals() and global_tracker:
        try:
            with open("/Users/michaelheneka/game_log.json", "w") as f:
                f.write(global_tracker.to_json())
            print("[Tracker] Saved game data to /Users/michaelheneka/game_log.json", flush=True)
        except Exception as e:
            print(f"[Tracker] Error saving log: {e}", flush=True)
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ----------------------------------------------------------------------
# Text & OCR Cleaning Helpers
# ----------------------------------------------------------------------

SPECIAL_STATUSES = [
    "KICKOFF", "TIMEOUT", "FLAG", "TOUCHDOWN", "FIELD GOAL",
    "PUNT", "EXTRA POINT", "PAT", "SAFETY", "REVIEW", "CHALLENGE"
]

FOOTBALL_KEYWORDS = {
    "TONIGHT", "YARDS", "YDS", "CARRIES", "TACKLES", "TOUCHDOWN", "TD", "PASSING",
    "RUSHING", "RECEIVING", "INTERCEPTION", "INT", "TFL", "SACK", "SACKS", "FUMBLE",
    "PENALTY", "TIMEOUT", "FLAG", "CHALLENGE", "REVIEW", "INJURY", "DRIVE", "PLAYS",
    "FIRST", "CAREER", "SEASON", "LEADER", "POINTS", "GOAL", "SAFETY", "FIELD",
    "KICK", "PUNT", "RETURN", "DEFENSE", "OFFENSE", "DOWN", "TOTAL"
}


def clean_down_distance(text):
    """Sanitizes down & distance strings, rejecting noise and fixing OCR letter substitutions."""
    if not text:
        return None
    s = text.strip()

    # Check for special situational banners (e.g. KICKOFF, TIMEOUT, FLAG)
    clean_alpha = re.sub(r"[^a-zA-Z\s]", "", s).upper().strip()
    for status in SPECIAL_STATUSES:
        if status in clean_alpha:
            if status == "REVIEW":
                return "UNDER REVIEW"
            return status

    # Fix common OCR misreads where '1' is recognized as 'I', 'l', '|', or preceded by quotes
    s = re.sub(r"^[“\"'`‘|lI\s]+st", "1ST", s, flags=re.IGNORECASE)
    s = re.sub(r"^[“\"'`‘|lI\s]+nd", "2ND", s, flags=re.IGNORECASE)
    s = re.sub(r"^[“\"'`‘|lI\s]+rd", "3RD", s, flags=re.IGNORECASE)
    s = re.sub(r"^[“\"'`‘|lI\s]+th", "4TH", s, flags=re.IGNORECASE)

    # Clean unwanted punctuation
    s = re.sub(r"[^a-zA-Z0-9\s&/]", "", s).strip()

    # Pattern: "1st & 10", "1st & Goal", "3rd & 2", "4th & Inches"
    m = re.search(r"([1-4](?:ST|ND|RD|TH)?)\s*(?:&|and|\/)?\s*([0-9]+|Goal|Inches)", s, re.IGNORECASE)
    if m:
        down = m.group(1).upper()
        if not re.search(r"[A-Z]", down):
            suf = {"1": "ST", "2": "ND", "3": "RD", "4": "TH"}.get(down, "TH")
            down += suf
        dist = m.group(2).upper()
        return f"{down} & {dist}"

    # Pattern: "1st Down", "2nd Down", etc.
    m_down = re.search(r"([1-4](?:ST|ND|RD|TH)?)\s*Down", s, re.IGNORECASE)
    if m_down:
        down = m_down.group(1).upper()
        if not re.search(r"[A-Z]", down):
            suf = {"1": "ST", "2": "ND", "3": "RD", "4": "TH"}.get(down, "TH")
            down += suf
        return f"{down} DOWN"

    return None


def clean_quarter(text):
    """Validates game quarter string."""
    if not text:
        return None
    t = text.upper().replace("Q", "4").replace("O", "0").strip()
    m = re.search(r"(1ST|2ND|3RD|4TH|OT|HALF|FINAL|[1-4]TH)", t)
    if m:
        val = m.group(1)
        mapping = {"1TH": "1ST", "2TH": "2ND", "3TH": "3RD"}
        return mapping.get(val, val)
    return None


def clean_clock(text):
    """Validates game clock (e.g., 7:47, 0:34, 15:00)."""
    if not text:
        return None
    m = re.search(r"\b(\d{1,2}:\d{2})\b", text)
    if m:
        return m.group(1)
    return None


def clean_play_clock(text):
    """Validates play clock (e.g., 40, 25, 19, 05)."""
    if not text:
        return None
    m = re.search(r"\b(\d{1,2})\b", text)
    if m:
        val = int(m.group(1))
        if 0 <= val <= 45:
            return str(val)
    return None


def clean_score(text):
    """Validates team score integer."""
    if not text:
        return None
    m = re.search(r"\b(\d{1,2})\b", text)
    if m:
        val = int(m.group(1))
        if 0 <= val <= 99:
            return str(val)
    return None


def clean_banner(text):
    """Sanitizes banner/ticker text, requiring legitimate broadcast/football keywords."""
    if not text:
        return None
    ascii_clean = text.encode("ascii", "ignore").decode().strip()
    if len(ascii_clean) < 8:
        return None

    alpha_chars = sum(c.isalnum() for c in ascii_clean)
    if alpha_chars / len(ascii_clean) < 0.75:
        return None

    tokens = re.findall(r"\b[A-Z0-9]{2,}\b", ascii_clean.upper())
    has_keyword = any(tok in FOOTBALL_KEYWORDS for tok in tokens)
    if has_keyword and len(tokens) >= 3:
        clean_line = " ".join(re.findall(r"[A-Za-z0-9&:\-']+", ascii_clean))
        return clean_line

    return None


# ----------------------------------------------------------------------
# Game State Model & Memory
# ----------------------------------------------------------------------

class GameStateTracker:
    def __init__(self, team1="DAL", team2="PHI"):
        self.team1 = team1
        self.team2 = team2
        self.score1 = "38"
        self.score2 = "21"
        self.quarter = "4TH"
        self.clock = "1:54"
        self.play_clock = "40"
        self.down_dist = "3RD & 4"
        self.possession = "PHI"  # Team with the ball
        self.field_position = "PHI 16"
        self.yardline = 84.0      # 0 to 100 percentage across the field (84 = PHI 16)
        self.line_to_gain = 88.0  # 4 yards ahead (for 3rd & 4)
        self.banner = ""
        self.scorebug_visible = True
        self.last_update_ts = datetime.now().strftime("%H:%M:%S")
        self.captions = deque(maxlen=40)
        self.lock = threading.Lock()

    def add_caption(self, text):
        with self.lock:
            ts = datetime.now().strftime("%H:%M:%S")
            speaker = ""
            clean = text.strip()
            if clean.startswith(">>"):
                parts = clean.split(":", 1)
                speaker = parts[0].replace(">>", "").strip()
                clean = parts[1].strip() if len(parts) > 1 else clean

            self.captions.append({"time": ts, "speaker": speaker, "text": clean})

            # Check if caption reveals field position (e.g. "NEAR THE 16", "AT THE 35")
            m_yard = re.search(r"\b(?:NEAR|AT|ON)\s+THE\s+([0-9]{1,2})\b", clean, re.IGNORECASE)
            if m_yard:
                yard = int(m_yard.group(1))
                if 1 <= yard <= 50:
                    team = self.possession or self.team2
                    self.field_position = f"{team} {yard}"
                    # Calculate yardline on visual 0-100 scale
                    if self.possession == self.team1:
                        # DAL driving toward PHI (right)
                        self.yardline = 100 - yard if team == self.team2 else yard
                    else:
                        # PHI driving toward DAL (left)
                        self.yardline = yard if team == self.team1 else 100 - yard
                    self._update_line_to_gain()

            # Check if caption reveals possession change
            if "TURNOVER ON DOWNS" in clean.upper() or "COWBOYS WILL TAKE OVER" in clean.upper():
                self.possession = self.team1
            elif "EAGLES WILL TAKE OVER" in clean.upper():
                self.possession = self.team2

    def _update_line_to_gain(self):
        """Calculates line to gain marker based on down and distance."""
        if not self.down_dist:
            return
        if "GOAL" in self.down_dist.upper():
            self.line_to_gain = 100.0 if self.possession == self.team1 else 0.0
        else:
            m = re.search(r"&\s*(\d+)", self.down_dist)
            if m:
                distance = int(m.group(1))
                if self.possession == self.team1:
                    self.line_to_gain = min(100.0, self.yardline + distance)
                else:
                    self.line_to_gain = max(0.0, self.yardline - distance)

    def update(self, parsed):
        with self.lock:
            has_clock = bool(parsed.get("clock"))
            has_qtr = bool(parsed.get("quarter"))
            has_dd = bool(parsed.get("down_distance"))

            if not (has_clock or has_qtr or has_dd):
                was_visible = self.scorebug_visible
                self.scorebug_visible = False
                return "BREAK" if was_visible else "HIDDEN"

            was_visible = self.scorebug_visible
            self.scorebug_visible = True
            changed = not was_visible

            if parsed.get("quarter") and parsed["quarter"] != self.quarter:
                self.quarter = parsed["quarter"]
                changed = True
            if parsed.get("clock") and parsed["clock"] != self.clock:
                self.clock = parsed["clock"]
                changed = True
            if parsed.get("play_clock"):
                self.play_clock = parsed["play_clock"]
            if parsed.get("down_distance") and parsed["down_distance"] != self.down_dist:
                self.down_dist = parsed["down_distance"]
                self._update_line_to_gain()
                changed = True
            if parsed.get("score_team1") and parsed["score_team1"] != self.score1:
                self.score1 = parsed["score_team1"]
                changed = True
            if parsed.get("score_team2") and parsed["score_team2"] != self.score2:
                self.score2 = parsed["score_team2"]
                changed = True
            if parsed.get("possession") and parsed["possession"] != self.possession:
                self.possession = parsed["possession"]
                self._update_line_to_gain()
                changed = True
            if parsed.get("banner") and parsed["banner"] != self.banner:
                self.banner = parsed["banner"]
                changed = True

            self.last_update_ts = datetime.now().strftime("%H:%M:%S")
            return "UPDATED" if changed else "UNCHANGED"

    def format_line(self, timestamp):
        parts = []
        s1 = self.score1 if self.score1 is not None else "--"
        s2 = self.score2 if self.score2 is not None else "--"
        poss_marker1 = "🏈 " if self.possession == self.team1 else ""
        poss_marker2 = " 🏈" if self.possession == self.team2 else ""
        parts.append(f"{poss_marker1}{self.team1} {s1} - {s2} {self.team2}{poss_marker2}")

        time_str = ""
        if self.quarter:
            time_str += self.quarter
        if self.clock:
            time_str += f" {self.clock}" if time_str else self.clock
        if self.play_clock:
            time_str += f" (PC: {self.play_clock})"
        if time_str:
            parts.append(time_str)

        if self.down_dist:
            parts.append(self.down_dist)

        if self.field_position:
            parts.append(f"Ball on {self.field_position}")

        return f"[{timestamp}] [Vision/OCR] " + " | ".join(parts)

    def to_json(self):
        with self.lock:
            return json.dumps({
                "team1": {
                    "name": self.team1,
                    "city": "Dallas",
                    "mascot": "Cowboys",
                    "score": self.score1,
                    "color": "#002244",
                    "accent": "#869397",
                    "possession": self.possession == self.team1,
                },
                "team2": {
                    "name": self.team2,
                    "city": "Philadelphia",
                    "mascot": "Eagles",
                    "score": self.score2,
                    "color": "#004C54",
                    "accent": "#A5ACAF",
                    "possession": self.possession == self.team2,
                },
                "possession": self.possession,
                "quarter": self.quarter,
                "clock": self.clock,
                "play_clock": self.play_clock,
                "down_distance": self.down_dist,
                "field_position": self.field_position,
                "yardline": round(self.yardline, 1),
                "line_to_gain": round(self.line_to_gain, 1),
                "banner": self.banner,
                "scorebug_visible": self.scorebug_visible,
                "last_update": self.last_update_ts,
                "captions": list(self.captions),
                "status": "LIVE" if self.scorebug_visible else "BREAK",
            })


# ----------------------------------------------------------------------
# Thread 1: Closed Caption Extractor
# ----------------------------------------------------------------------

def caption_worker(stream_url, tracker):
    """Thread 1: Streams closed captions with zero CPU overhead."""
    cmd = (
        f"curl -s '{stream_url}' | "
        f"'{CCEXTRACTOR_BIN}' -1 --stdin --stdout --quiet --out=txt --no-rollup --forceflush -"
    )

    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    register_process(proc)

    last_line = None
    try:
        for line in proc.stdout:
            if stop_event.is_set():
                break
            clean = line.strip()
            if clean and clean != last_line and len(clean) > 2:
                if re.fullmatch(r"[A-Z\s]{1,4}", clean) and not clean.startswith(">>"):
                    continue
                timestamp = datetime.now().strftime("%H:%M:%S")
                print(f"[{timestamp}] [CC] {clean}", flush=True)
                tracker.add_caption(clean)
                last_line = clean
    except Exception as e:
        if not stop_event.is_set():
            print(f"[CC] Error: {e}", flush=True)
    finally:
        proc.terminate()


# ----------------------------------------------------------------------
# Thread 2: Vision & Local OCR Worker
# ----------------------------------------------------------------------

def run_ocr(image_path, psm=7, whitelist=None):
    """Runs local Tesseract OCR on an image crop."""
    if not os.path.exists(TESSERACT_BIN):
        return ""

    cmd = [TESSERACT_BIN, image_path, "stdout", "--psm", str(psm)]
    if whitelist:
        cmd.extend(["-c", f"tessedit_char_whitelist={whitelist}"])

    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def detect_possession_chevron(scorebug_path, temp_dir):
    """Measures brightness of Team 1 vs Team 2 possession chevrons."""
    try:
        cmd1 = [FFMPEG_BIN, "-y", "-i", scorebug_path, "-filter:v", "crop=40:15:200:15,format=gray", "-f", "rawvideo", "pipe:1"]
        p1 = subprocess.Popen(cmd1, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        raw1, _ = p1.communicate()

        cmd2 = [FFMPEG_BIN, "-y", "-i", scorebug_path, "-filter:v", "crop=40:15:460:15,format=gray", "-f", "rawvideo", "pipe:1"]
        p2 = subprocess.Popen(cmd2, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        raw2, _ = p2.communicate()

        avg1 = sum(raw1) / len(raw1) if raw1 else 0
        avg2 = sum(raw2) / len(raw2) if raw2 else 0

        if avg1 > avg2 + 15:
            return "DAL"
        elif avg2 > avg1 + 15:
            return "PHI"
    except Exception:
        pass
    return None


def parse_scorebug(scorebug_path, temp_dir):
    """Extracts and validates each component of the scorebug region."""
    state = {}

    # Possession chevron
    poss = detect_possession_chevron(scorebug_path, temp_dir)
    if poss:
        state["possession"] = poss

    # 1. Down & Distance
    dd_img = os.path.join(temp_dir, "crop_dd.png")
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", scorebug_path,
            "-filter:v", "crop=140:30:288:24",
            dd_img,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    raw_dd = run_ocr(dd_img, psm=6)
    clean_dd = clean_down_distance(raw_dd)
    if clean_dd:
        state["down_distance"] = clean_dd

    # 2. Quarter
    qtr_img = os.path.join(temp_dir, "crop_qtr.png")
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", scorebug_path,
            "-filter:v", "crop=48:26:276:58,scale=96:52,pad=116:72:10:10:white",
            qtr_img,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    raw_qtr = run_ocr(qtr_img, psm=7, whitelist="1234OTSTNDRDTH")
    clean_q = clean_quarter(raw_qtr)
    if clean_q:
        state["quarter"] = clean_q

    # 3. Game Clock
    clock_img = os.path.join(temp_dir, "crop_clock.png")
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", scorebug_path,
            "-filter:v", "crop=65:26:326:58,scale=130:52,pad=150:72:10:10:white",
            clock_img,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    raw_clock = run_ocr(clock_img, psm=7, whitelist="0123456789:")
    clean_c = clean_clock(raw_clock)
    if clean_c:
        state["clock"] = clean_c

    # 4. Play Clock
    pc_img = os.path.join(temp_dir, "crop_pc.png")
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", scorebug_path,
            "-filter:v", "crop=36:26:395:58,scale=72:52,pad=92:72:10:10:white",
            pc_img,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    raw_pc = run_ocr(pc_img, psm=7, whitelist="0123456789")
    clean_pc = clean_play_clock(raw_pc)
    if clean_pc:
        state["play_clock"] = clean_pc

    # 5. Team Scores
    s1_img = os.path.join(temp_dir, "crop_s1.png")
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", scorebug_path,
            "-filter:v", "crop=90:55:185:25,negate,pad=120:80:15:12:white",
            s1_img,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    raw_s1 = run_ocr(s1_img, psm=7, whitelist="0123456789") or run_ocr(s1_img, psm=10, whitelist="0123456789")
    clean_s1 = clean_score(raw_s1)
    if clean_s1 is not None:
        state["score_team1"] = clean_s1

    s2_img = os.path.join(temp_dir, "crop_s2.png")
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", scorebug_path,
            "-filter:v", "crop=90:55:445:25,negate,pad=120:80:15:12:white",
            s2_img,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    raw_s2 = run_ocr(s2_img, psm=7, whitelist="0123456789") or run_ocr(s2_img, psm=10, whitelist="0123456789")
    clean_s2 = clean_score(raw_s2)
    if clean_s2 is not None:
        state["score_team2"] = clean_s2

    # 6. Top banner / info ticker
    banner_img = os.path.join(temp_dir, "crop_banner.png")
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", scorebug_path,
            "-filter:v", "crop=700:24:10:0,format=gray",
            banner_img,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    raw_banner = run_ocr(banner_img, psm=6)
    clean_b = clean_banner(raw_banner)
    if clean_b:
        state["banner"] = clean_b

    return state


def grab_scorebug_frame(stream_url, out_path):
    """Extracts a single frame from the live HDHomeRun stream and crops the scorebug."""
    crop_filter = "crop=716:100:282:605"
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-fflags", "+nobuffer",
        "-flags", "+low_delay",
        "-analyzeduration", "1000000",
        "-probesize", "1000000",
        "-i", stream_url,
        "-vframes", "1",
        "-filter:v", crop_filter,
        "-q:v", "2",
        out_path,
    ]
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        success = os.path.exists(out_path) and os.path.getsize(out_path) > 0
        if not success and res.stderr:
            err = res.stderr.decode(errors="ignore")
            if "Forbidden" in err or "403" in err:
                return False, "403_FORBIDDEN"
        return success, None
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, str(e)


def vision_worker(stream_url, tracker, interval=4):
    """Thread 2: Samples video frames periodically to inspect game state."""
    print(f"[Vision] Initializing frame sampler and OCR worker (every {interval}s)...", flush=True)

    with tempfile.TemporaryDirectory() as temp_dir:
        scorebug_crop_file = os.path.join(temp_dir, "scorebug_current.jpg")
        first_frame = True
        last_break_heartbeat = 0

        while not stop_event.is_set():
            t_start = time.time()
            ok, err = grab_scorebug_frame(stream_url, scorebug_crop_file)

            if ok:
                parsed = parse_scorebug(scorebug_crop_file, temp_dir)
                status = tracker.update(parsed)
                timestamp = datetime.now().strftime("%H:%M:%S")

                if first_frame:
                    print(f"[{timestamp}] [Vision] Connected to stream. Scorebug analyzer active.", flush=True)
                    first_frame = False

                if status == "UPDATED":
                    print(tracker.format_line(timestamp), flush=True)
                    if parsed.get("banner"):
                        print(f"[{timestamp}] [Vision/Ticker] {parsed['banner']}", flush=True)
                elif status == "BREAK":
                    print(f"[{timestamp}] [Vision] (Break / Replay - scorebug hidden)", flush=True)
                    last_break_heartbeat = time.time()
                elif status == "HIDDEN":
                    now = time.time()
                    if now - last_break_heartbeat > 25:
                        print(f"[{timestamp}] [Vision] (Commercial break ongoing - waiting for live gameplay...)", flush=True)
                        last_break_heartbeat = now
            else:
                timestamp = datetime.now().strftime("%H:%M:%S")
                if err == "TIMEOUT":
                    print(f"[{timestamp}] [Vision] Frame fetch timed out (stream buffering)...", flush=True)
                elif err == "403_FORBIDDEN":
                    print(f"[{timestamp}] [Vision] HDHomeRun access denied. Check tuner availability.", flush=True)
                elif err:
                    print(f"[{timestamp}] [Vision] Frame grab warning: {err}", flush=True)

            elapsed = time.time() - t_start
            sleep_time = max(0.5, interval - elapsed)
            time.sleep(sleep_time)


# ----------------------------------------------------------------------
# Thread 3: Web Dashboard HTTP Server (Zero-Dependency)
# ----------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NFL Live Tracker • HDHomeRun</title>
<style>
  :root {
    --bg-primary: #0a0e17;
    --bg-card: rgba(20, 27, 45, 0.75);
    --border: rgba(255, 255, 255, 0.08);
    --dal-blue: #002244;
    --dal-silver: #869397;
    --phi-green: #004C54;
    --phi-silver: #A5ACAF;
    --gold: #FFD700;
    --red: #FF3B30;
    --green: #34C759;
    --text-primary: #FFFFFF;
    --text-secondary: #8E9BAE;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', Roboto, sans-serif; }

  body {
    background: radial-gradient(circle at 50% 0%, #152238 0%, var(--bg-primary) 70%);
    color: var(--text-primary);
    min-height: 100vh;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  /* Header */
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--bg-card);
    backdrop-filter: blur(16px);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 16px 24px;
  }

  .title-group { display: flex; align-items: center; gap: 12px; }
  .live-dot {
    width: 10px; height: 10px; background: var(--red); border-radius: 50%;
    box-shadow: 0 0 12px var(--red);
    animation: pulse 1.6s infinite;
  }
  @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(0.85); } }

  h1 { font-size: 1.25rem; font-weight: 700; letter-spacing: 0.5px; }
  .source-tag { font-size: 0.75rem; color: var(--text-secondary); background: rgba(255,255,255,0.06); padding: 4px 10px; border-radius: 20px; }

  /* Main Scoreboard Hero */
  .scoreboard {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    background: var(--bg-card);
    backdrop-filter: blur(20px);
    border: 1px solid var(--border);
    border-radius: 24px;
    overflow: hidden;
    box-shadow: 0 20px 40px rgba(0,0,0,0.4);
  }

  .team-panel {
    padding: 36px 32px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    position: relative;
    transition: all 0.3s ease;
  }
  .team-panel.dal {
    background: linear-gradient(135deg, rgba(0,34,68,0.7) 0%, rgba(10,14,23,0.3) 100%);
    border-right: 1px solid var(--border);
  }
  .team-panel.phi {
    background: linear-gradient(225deg, rgba(0,76,84,0.7) 0%, rgba(10,14,23,0.3) 100%);
    border-left: 1px solid var(--border);
    align-items: flex-end;
    text-align: right;
  }

  .possession-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(255, 215, 0, 0.15);
    border: 1px solid var(--gold);
    color: var(--gold);
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 0.75rem;
    font-weight: 700;
    letter-spacing: 0.5px;
    margin-bottom: 12px;
    opacity: 0;
    transform: translateY(-4px);
    transition: all 0.3s ease;
  }
  .possession-badge.active { opacity: 1; transform: translateY(0); }

  .team-name { font-size: 1.6rem; font-weight: 800; letter-spacing: 1px; color: #fff; }
  .team-mascot { font-size: 0.85rem; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 12px; }
  .score { font-size: 5rem; font-weight: 900; line-height: 1; color: #fff; text-shadow: 0 4px 20px rgba(255,255,255,0.2); }

  /* Center Capsule */
  .center-capsule {
    padding: 24px 32px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    min-width: 220px;
    background: rgba(10, 14, 23, 0.6);
  }

  .quarter-badge { font-size: 0.9rem; font-weight: 800; color: var(--gold); letter-spacing: 1.5px; text-transform: uppercase; }
  .clock { font-size: 2.8rem; font-weight: 800; font-variant-numeric: tabular-nums; letter-spacing: -1px; }
  .play-clock-pill {
    background: rgba(255,255,255,0.08);
    border: 1px solid var(--border);
    padding: 4px 14px;
    border-radius: 12px;
    font-size: 0.8rem;
    font-weight: 700;
    color: var(--text-secondary);
  }
  .play-clock-pill.urgent { background: rgba(255, 59, 48, 0.2); border-color: var(--red); color: var(--red); }

  .down-pill {
    background: linear-gradient(135deg, #FFD700 0%, #FFA500 100%);
    color: #000;
    font-weight: 800;
    font-size: 1rem;
    padding: 8px 18px;
    border-radius: 20px;
    box-shadow: 0 4px 14px rgba(255, 215, 0, 0.3);
  }

  /* Football Field Visualizer */
  .field-container {
    background: var(--bg-card);
    backdrop-filter: blur(20px);
    border: 1px solid var(--border);
    border-radius: 24px;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .field-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .field-title { font-size: 0.95rem; font-weight: 700; letter-spacing: 0.5px; }
  .field-position-tag {
    font-size: 0.85rem; font-weight: 700; color: var(--gold);
    background: rgba(255, 215, 0, 0.1); border: 1px solid rgba(255, 215, 0, 0.3);
    padding: 4px 12px; border-radius: 12px;
  }

  .field-svg-box {
    position: relative;
    width: 100%;
    background: #1e4620;
    border: 2px solid rgba(255,255,255,0.2);
    border-radius: 12px;
    overflow: hidden;
    box-shadow: inset 0 0 40px rgba(0,0,0,0.6);
  }

  svg.football-field { width: 100%; height: 130px; display: block; }

  /* Ticker & Commentary */
  .bottom-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 20px;
  }

  .commentary-box {
    background: var(--bg-card);
    backdrop-filter: blur(16px);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 20px 24px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .box-title { font-size: 0.85rem; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 1px; }

  .caption-feed {
    max-height: 220px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
    scroll-behavior: smooth;
  }
  .caption-feed::-webkit-scrollbar { width: 6px; }
  .caption-feed::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 4px; }

  .caption-line {
    display: flex;
    gap: 10px;
    font-size: 0.9rem;
    line-height: 1.4;
    padding: 6px 10px;
    border-radius: 8px;
    background: rgba(255,255,255,0.02);
  }
  .caption-time { font-size: 0.75rem; color: var(--text-secondary); font-variant-numeric: tabular-nums; flex-shrink: 0; padding-top: 2px; }
  .speaker-badge {
    font-size: 0.75rem; font-weight: 700; padding: 2px 8px; border-radius: 6px;
    flex-shrink: 0;
  }
  .speaker-Joe { background: rgba(0, 122, 255, 0.2); color: #64D2FF; border: 1px solid rgba(0, 122, 255, 0.4); }
  .speaker-Troy { background: rgba(52, 199, 89, 0.2); color: #30D158; border: 1px solid rgba(52, 199, 89, 0.4); }
  .caption-text { color: #E5E9F0; flex-grow: 1; }

  .banner-ticker {
    background: linear-gradient(90deg, rgba(0,34,68,0.5) 0%, rgba(0,76,84,0.5) 100%);
    border: 1px solid var(--border);
    padding: 12px 20px;
    border-radius: 12px;
    font-size: 0.85rem;
    font-weight: 700;
    color: var(--gold);
    display: flex;
    align-items: center;
    gap: 10px;
  }
</style>
</head>
<body>

<header>
  <div class="title-group">
    <div class="live-dot"></div>
    <h1>NFL MONDAY NIGHT TRACKER</h1>
    <span class="source-tag">HDHomeRun • Ch 9.1 WSYR-HD</span>
  </div>
  <div class="source-tag" id="last-update">Syncing...</div>
</header>

<main class="scoreboard">
  <!-- Team 1: DAL -->
  <div class="team-panel dal">
    <div class="possession-badge" id="dal-possession">🏈 POSSESSION</div>
    <div class="team-name">COWBOYS</div>
    <div class="team-mascot">DALLAS</div>
    <div class="score" id="dal-score">38</div>
  </div>

  <!-- Center Status Capsule -->
  <div class="center-capsule">
    <div class="quarter-badge" id="quarter">4TH QUARTER</div>
    <div class="clock" id="clock">1:54</div>
    <div class="play-clock-pill" id="play-clock">PC: 40</div>
    <div class="down-pill" id="down-distance">3RD & 4</div>
  </div>

  <!-- Team 2: PHI -->
  <div class="team-panel phi">
    <div class="possession-badge" id="phi-possession">POSSESSION 🏈</div>
    <div class="team-name">EAGLES</div>
    <div class="team-mascot">PHILADELPHIA</div>
    <div class="score" id="phi-score">21</div>
  </div>
</main>

<!-- Interactive Football Field -->
<section class="field-container">
  <div class="field-header">
    <div class="field-title">VIRTUAL FIELD POSITION</div>
    <div class="field-position-tag" id="field-position-tag">Ball on PHI 16</div>
  </div>

  <div class="field-svg-box">
    <svg class="football-field" viewBox="0 0 1200 130">
      <!-- Turf stripes -->
      <rect x="0" y="0" width="1200" height="130" fill="#245828"/>
      <!-- Endzones -->
      <rect x="0" y="0" width="100" height="130" fill="#002244"/>
      <text x="50" y="75" fill="#869397" font-size="20" font-weight="900" text-anchor="middle" transform="rotate(-90 50 75)">DAL</text>

      <rect x="1100" y="0" width="100" height="130" fill="#004C54"/>
      <text x="1150" y="75" fill="#A5ACAF" font-size="20" font-weight="900" text-anchor="middle" transform="rotate(90 1150 75)">PHI</text>

      <!-- 10-Yard Lines -->
      <g stroke="rgba(255,255,255,0.3)" stroke-width="2">
        <line x1="200" y1="0" x2="200" y2="130"/>
        <line x1="300" y1="0" x2="300" y2="130"/>
        <line x1="400" y1="0" x2="400" y2="130"/>
        <line x1="500" y1="0" x2="500" y2="130"/>
        <line x1="600" y1="0" x2="600" y2="130" stroke="rgba(255,255,255,0.7)" stroke-width="3"/>
        <line x1="700" y1="0" x2="700" y2="130"/>
        <line x1="800" y1="0" x2="800" y2="130"/>
        <line x1="900" y1="0" x2="900" y2="130"/>
        <line x1="1000" y1="0" x2="1000" y2="130"/>
      </g>

      <!-- Yard Numbers -->
      <g fill="rgba(255,255,255,0.4)" font-size="14" font-weight="700" text-anchor="middle">
        <text x="200" y="24">10</text><text x="300" y="24">20</text><text x="400" y="24">30</text>
        <text x="500" y="24">40</text><text x="600" y="24">50</text><text x="700" y="24">40</text>
        <text x="800" y="24">30</text><text x="900" y="24">20</text><text x="1000" y="24">10</text>
      </g>

      <!-- Red Zone (PHI 0..20 = x: 900..1100) -->
      <rect x="900" y="0" width="200" height="130" fill="rgba(255, 59, 48, 0.15)"/>

      <!-- 1st Down Yellow Line -->
      <line id="first-down-line" x1="980" y1="0" x2="980" y2="130" stroke="#FFD700" stroke-width="4" stroke-dasharray="4"/>

      <!-- Line of Scrimmage Blue Line -->
      <line id="scrimmage-line" x1="940" y1="0" x2="940" y2="130" stroke="#00BFFF" stroke-width="4"/>

      <!-- Football Indicator -->
      <g id="football-marker" transform="translate(940, 65)">
        <ellipse cx="0" cy="0" rx="14" ry="9" fill="#8B4513" stroke="#fff" stroke-width="1.5"/>
        <line x1="-8" y1="0" x2="8" y2="0" stroke="#fff" stroke-width="1.5"/>
      </g>
    </svg>
  </div>
</section>

<!-- Bottom Grid: Commentary Feed -->
<section class="bottom-grid">
  <div class="commentary-box">
    <div class="box-title">🎙️ LIVE ANNOUNCER FEED & CLOSED CAPTIONS</div>
    <div class="caption-feed" id="caption-feed">
      <!-- Populated via polling -->
    </div>
  </div>
</section>

<script>
  let lastCaptionsJson = "";

  async function fetchState() {
    try {
      const res = await fetch('/api/state');
      if (!res.ok) return;
      const data = await res.json();

      // Scores
      document.getElementById('dal-score').innerText = data.team1.score || '--';
      document.getElementById('phi-score').innerText = data.team2.score || '--';

      // Possession
      const dalPoss = document.getElementById('dal-possession');
      const phiPoss = document.getElementById('phi-possession');
      if (data.team1.possession) {
        dalPoss.classList.add('active');
        phiPoss.classList.remove('active');
      } else if (data.team2.possession) {
        phiPoss.classList.add('active');
        dalPoss.classList.remove('active');
      } else {
        dalPoss.classList.remove('active');
        phiPoss.classList.remove('active');
      }

      // Quarter & Clocks
      document.getElementById('quarter').innerText = (data.quarter || '4TH') + ' QUARTER';
      document.getElementById('clock').innerText = data.clock || '--:--';

      const pcElem = document.getElementById('play-clock');
      if (data.play_clock) {
        pcElem.innerText = 'PC: ' + data.play_clock;
        const pcVal = parseInt(data.play_clock, 10);
        if (pcVal <= 5) pcElem.classList.add('urgent');
        else pcElem.classList.remove('urgent');
      }

      // Down & Distance
      document.getElementById('down-distance').innerText = data.down_distance || 'IN PLAY';

      // Field Position
      document.getElementById('field-position-tag').innerText = 'Ball on ' + (data.field_position || 'PHI 16');

      // Update Field Lines (SVG coordinates: x = 100 + (yardline * 10))
      const yardline = (data.yardline !== undefined) ? data.yardline : 84;
      const losX = 100 + (yardline * 10);
      document.getElementById('scrimmage-line').setAttribute('x1', losX);
      document.getElementById('scrimmage-line').setAttribute('x2', losX);
      document.getElementById('football-marker').setAttribute('transform', `translate(${losX}, 65)`);

      const lineToGain = (data.line_to_gain !== undefined) ? data.line_to_gain : 88;
      const ltgX = 100 + (lineToGain * 10);
      document.getElementById('first-down-line').setAttribute('x1', ltgX);
      document.getElementById('first-down-line').setAttribute('x2', ltgX);

      // Last update
      document.getElementById('last-update').innerText = 'Updated ' + data.last_update;

      // Captions feed
      const currentCaptionsJson = JSON.stringify(data.captions);
      if (currentCaptionsJson !== lastCaptionsJson) {
        lastCaptionsJson = currentCaptionsJson;
        const feed = document.getElementById('caption-feed');
        feed.innerHTML = '';
        data.captions.forEach(c => {
          const row = document.createElement('div');
          row.className = 'caption-line';
          const timeSpan = `<span class="caption-time">[${c.time}]</span>`;
          const spkSpan = c.speaker ? `<span class="speaker-badge speaker-${c.speaker}">${c.speaker}</span>` : '';
          row.innerHTML = `${timeSpan} ${spkSpan} <span class="caption-text">${c.text}</span>`;
          feed.appendChild(row);
        });
        feed.scrollTop = feed.scrollHeight;
      }
    } catch (e) {
      console.warn('Sync error:', e);
    }
  }

  // Poll state every 1 second
  setInterval(fetchState, 1000);
  fetchState();
</script>
</body>
</html>
"""


class TrackerWebHandler(http.server.BaseHTTPRequestHandler):
    def __init__(self, tracker, *args, **kwargs):
        self.tracker = tracker
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
        elif self.path == "/api/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(self.tracker.to_json().encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Keep web server logs quiet so terminal output stays focused on the game
        return


def start_web_server(tracker, port=8080):
    """Starts the embedded dashboard web server in a daemon thread."""
    def handler_factory(*args, **kwargs):
        return TrackerWebHandler(tracker, *args, **kwargs)

    try:
        server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler_factory)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        print(f"[Web] Dashboard live at http://localhost:{port} (open in Safari/Chrome)", flush=True)
        return server
    except Exception as e:
        print(f"[Web] Notice: Could not bind port {port}: {e}", flush=True)
        return None


# ----------------------------------------------------------------------
# Main Entry Point
# ----------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="M1 Mac HDHomeRun Live Stream Tracker & Web Dashboard")
    parser.add_argument("--url", default="http://hdhr-1073ec20.local:5004/auto/v9.1", help="HDHomeRun channel stream URL")
    parser.add_argument("--team1", default="DAL", help="Team 1 abbreviation/name")
    parser.add_argument("--team2", default="PHI", help="Team 2 abbreviation/name")
    parser.add_argument("--interval", type=int, default=4, help="Vision sampling interval in seconds")
    parser.add_argument("--port", type=int, default=8080, help="Web dashboard port (default 8080)")
    args = parser.parse_args()

    global global_tracker
    tracker = GameStateTracker(team1=args.team1, team2=args.team2)
    global_tracker = tracker

    print("=" * 68)
    print(" Starting M1 MacBook Air Live Multimodal Stream Tracker")
    print(f" Stream Source: {args.url}")
    print(f" Matchup:       {args.team1} vs {args.team2}")
    print(f" Web Dashboard: http://localhost:{args.port}")
    print(f" Tesseract OCR: {TESSERACT_BIN}")
    print(f" FFmpeg Engine: {FFMPEG_BIN}")
    print(f" CC Extractor:  {CCEXTRACTOR_BIN}")
    print("=" * 68)

    # Start Web Dashboard
    start_web_server(tracker, port=args.port)

    # Start caption thread
    t_cap = threading.Thread(
        target=caption_worker, args=(args.url, tracker), daemon=True
    )
    t_cap.start()

    # Start vision/OCR thread
    t_vis = threading.Thread(
        target=vision_worker, args=(args.url, tracker, args.interval), daemon=True
    )
    t_vis.start()

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_event.set()
        print("\nStopping tracker...")
