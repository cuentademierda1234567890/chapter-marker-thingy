import os
import subprocess
import re
import argparse
from rich.console import Console
from rich.table import Table

# ==== CONFIG ====
FFMPEG = "ffmpeg"  # o ruta absoluta en Windows si hace falta
# FFMPEG = "C:/ffmpeg/bin/ffmpeg.exe"

console = Console()

VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v')

# ================= UTILIDADES =================

def run_cmd(command):
    return subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

def get_video_duration(video_file):
    command = f'{FFMPEG} -i "{video_file}"'
    process = run_cmd(command)
    _, error = process.communicate()

    for line in error.decode(errors="ignore").splitlines():
        if "Duration:" in line:
            match = re.search(r'Duration: (\d+):(\d+):(\d+\.\d+)', line)
            if match:
                h, m, s = match.groups()
                return int(h) * 3600 + int(m) * 60 + float(s)
    return None

def get_files(directory, extensions):
    files = []
    for root, _, filenames in os.walk(directory):
        for f in filenames:
            if f.lower().endswith(extensions):
                files.append(os.path.join(root, f))
    return files

# ================= DETECTORES =================

def detect_black_spaces(video_file):
    command = (
        f'{FFMPEG} -i "{video_file}" '
        f'-vf blackdetect=d=0.1:pix_th=0.10 -an -f null -'
    )
    process = run_cmd(command)
    _, error = process.communicate()

    blacks = []
    for line in error.decode(errors="ignore").splitlines():
        if "black_start" in line:
            data = {}
            for part in line.split():
                if part.startswith("black_start:"):
                    data["start"] = float(part.split(":")[1])
                elif part.startswith("black_end:"):
                    data["end"] = float(part.split(":")[1])
            if "start" in data and "end" in data:
                data["center"] = (data["start"] + data["end"]) / 2
                blacks.append(data)
    return blacks

def detect_silence(video_file, noise="-30dB", min_dur=0.3):
    command = (
        f'{FFMPEG} -i "{video_file}" '
        f'-af silencedetect=noise={noise}:d={min_dur} -f null -'
    )
    process = run_cmd(command)
    _, error = process.communicate()

    silences = []
    silence_start = None

    for line in error.decode(errors="ignore").splitlines():
        if "silence_start" in line:
            silence_start = float(line.split("silence_start:")[1].split()[0])
        elif "silence_end" in line and silence_start is not None:
            silence_end = float(line.split("silence_end:")[1].split()[0])
            silences.append({
                "start": silence_start,
                "end": silence_end,
                "center": (silence_start + silence_end) / 2,
                "duration": silence_end - silence_start
            })
            silence_start = None
    return silences

def detect_scenes(video_file):
    command = (
        f'{FFMPEG} -i "{video_file}" '
        f'-vf "select=gt(scene\,0.4),showinfo" -f null -'
    )
    process = run_cmd(command)
    _, error = process.communicate()

    scenes = []
    for line in error.decode(errors="ignore").splitlines():
        if "pts_time:" in line:
            for part in line.split():
                if part.startswith("pts_time:"):
                    scenes.append({"timestamp": float(part.split(":")[1])})
    return scenes

# ================= LOGICA =================

def clean_black_spaces(blacks, duration, start=20, end=10):
    return [
        b for b in blacks
        if b["start"] > start and b["end"] < duration - end
    ]

def find_optimal_breaks(duration, blacks, silences, scenes, max_gap_minutes=12):
    breaks = [{"timestamp": b["center"], "type": "black", "confidence": "high"} for b in blacks]
    breaks.sort(key=lambda x: x["timestamp"])

    max_gap = max_gap_minutes * 60
    filled = breaks[:]

    gaps = []
    if filled:
        if filled[0]["timestamp"] > max_gap:
            gaps.append((0, filled[0]["timestamp"]))
        for i in range(len(filled) - 1):
            if filled[i+1]["timestamp"] - filled[i]["timestamp"] > max_gap:
                gaps.append((filled[i]["timestamp"], filled[i+1]["timestamp"]))
    else:
        gaps.append((0, duration))

    for gstart, gend in gaps:
        center = (gstart + gend) / 2
        candidates = [
            s for s in scenes
            if gstart + 30 < s["timestamp"] < gend - 30
        ]
        if candidates:
            best = min(candidates, key=lambda s: abs(s["timestamp"] - center))
            filled.append({
                "timestamp": best["timestamp"],
                "type": "scene",
                "confidence": "medium"
            })

    filled.sort(key=lambda x: x["timestamp"])
    return filled

# ================= CHAPTERS =================

def write_chapters_to_video(video_file, break_points, duration, overwrite=False):
    base, ext = os.path.splitext(video_file)
    output = video_file if overwrite else f"{base}.chapters{ext}"
    metadata = f"{base}.ffmetadata"

    with open(metadata, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        f.write("encoder=cmthingy\n")

        prev = 0
        for i, bp in enumerate(break_points, 1):
            f.write("\n[CHAPTER]\n")
            f.write("TIMEBASE=1/1000\n")
            f.write(f"START={int(prev * 1000)}\n")
            f.write(f"END={int(bp['timestamp'] * 1000)}\n")
            f.write(f"title=Chapter {i}\n")
            prev = bp["timestamp"]

        f.write("\n[CHAPTER]\n")
        f.write("TIMEBASE=1/1000\n")
        f.write(f"START={int(prev * 1000)}\n")
        f.write(f"END={int(duration * 1000)}\n")
        f.write(f"title=Chapter {len(break_points)+1}\n")

    command = (
        f'{FFMPEG} -i "{video_file}" -i "{metadata}" '
        f'-map 0 -map_metadata 0 -map_chapters 1 -c copy -y "{output}"'
    )

    process = run_cmd(command)
    _, err = process.communicate()

    os.remove(metadata)

    if process.returncode == 0:
        console.print("[green]✓ Chapters written successfully[/green]")
    else:
        console.print("[red]Error writing chapters[/red]")
        console.print(err.decode(errors="ignore")[:500])

# ================= MAIN =================

def process_video(video, write_chapters=False, overwrite=False, max_gap=12):
    console.print(f"\n[bold cyan]Processing:[/bold cyan] {video}")

    duration = get_video_duration(video)
    if not duration:
        console.print("[red]Could not get duration[/red]")
        return

    blacks = detect_black_spaces(video)
    silences = detect_silence(video)
    scenes = detect_scenes(video)

    blacks = clean_black_spaces(blacks, duration)
    breaks = find_optimal_breaks(duration, blacks, silences, scenes, max_gap)

    table = Table(title="Chapters")
    table.add_column("#")
    table.add_column("Timestamp")
    for i, b in enumerate(breaks, 1):
        m, s = divmod(int(b["timestamp"]), 60)
        table.add_row(str(i), f"{m}:{s:02d}")
    console.print(table)

    if write_chapters:
        write_chapters_to_video(video, breaks, duration, overwrite)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-f", "--file")
    parser.add_argument("--write-chapters", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--max-gap", type=int, default=12)

    args = parser.parse_args()

    if args.file:
        process_video(
            args.file,
            write_chapters=args.write_chapters,
            overwrite=args.overwrite,
            max_gap=args.max_gap
        )

if __name__ == "__main__":
    main()
