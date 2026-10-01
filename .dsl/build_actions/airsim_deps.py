"""Shared helpers for this repository's DSL build actions (pre_build / post_build).

Everything the Blocks project needs but the repository does not carry is pulled
from its upstream location, pinned by sha256, exactly as upstream ``build.cmd``
does (minus the Debug configuration, which a packaged game never links):

* rpclib 2.3.1        -> built with CMake, copied into AirLib/deps/rpclib
* Eigen 3.4.1r        -> AirLib/deps/eigen3/Eigen
* high-poly SUV assets -> Unreal/Plugins/AirSim/Content/VehicleAdv/SUV
* AirSim.sln (AirLib + MavLinkCom ...) built Release x64 with the host's MSVC
* the AirSim plugin (Unreal/Plugins/AirSim + AirLib) copied into
  Unreal/Environments/Blocks/Plugins/AirSim, as update_from_git.bat does.

A pin that no longer matches what the URL serves FAILS the build: a changed
upstream artifact is never accepted silently.

Run ``python airsim_deps.py --selftest`` for the offline self-test.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Download:
    name: str
    url: str
    sha256: str
    size: int


RPCLIB = Download(
    "rpclib-2.3.1",
    "https://github.com/WouterJansen/rpclib/archive/refs/tags/v2.3.1.zip",
    "3326bc0ee2a20130bf3540c16a40e9da2b3fe25dafc16b743638f6e8e4f339c6", 2481250)
EIGEN = Download(
    "eigen-3.4.1r",
    "https://github.com/WouterJansen/eigen/archive/refs/tags/3.4.1r.zip",
    "50ff45ad90d3b0a4457065a70bd79b2d61ed02fdf75791befc73a9952ee414af", 3704353)
CAR_ASSETS = Download(
    "cosys_car_assets",
    "https://github.com/Cosys-Lab/Cosys-AirSim/releases/download/carassets/cosys_car_assets.zip",
    "00fff48fa303f0c54998146e3988815c28f12579782e741654ce41b52b916534", 40647246)
DOWNLOADS = (RPCLIB, EIGEN, CAR_ASSETS)

PROJECT_REL = Path("Unreal") / "Environments" / "Blocks"
UPROJECT = "Blocks.uproject"
MANIFEST_NAME = "dsl_airsim_prebuild.json"

# What the packaged Win64 game must contain (post_build): string literals
# compiled into the monolithic game executable. The plugin module's TEXT()
# literal proves the AirSim module was built into the game; the API name
# proves AirLib's RPC server (AirLib.lib, built by pre_build) was linked; the
# rpclib dispatcher literal proves rpclib code is in. (rpclib is header-heavy,
# so that literal is also inlined into AirLib.lib; the prebuilt rpc.lib and
# MavLinkCom.lib are on the plugin's link line, so a link without them fails.)
EXE_NEEDLES = {
    # AirSim plugin module source (TEXT() -> UTF-16 on Windows)
    "airsim_plugin": "Warning, WeatherAPI got invalid paramname!".encode("utf-16-le"),
    # AirLib.lib (RpcLibServerBase.cpp binds this API name)
    "airlib": b"simTestLineOfSightBetweenPoints",
    # rpclib dispatcher
    "rpclib": b"Function name already bound: '",
}
# Cooked into the paks from Plugins/AirSim/Content/VehicleAdv/SUV (the car
# assets zip); DefaultGame.ini always-cooks /AirSim/VehicleAdv.
PAK_NEEDLES = {"suv_car_pawn": b"SuvCarPawn", "suv_skeleton": b"Suv_Skel"}


def log(msg: str) -> None:
    print(f"[airsim {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Heartbeat:
    """Prints a line every `interval` seconds while a long step runs, so the
    build-action no-output bound (15 min) can never be hit by a quiet tool."""

    def __init__(self, label: str, interval: float = 60.0):
        self.label, self.interval = label, interval
        self._stop = threading.Event()
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            log(f"... still running: {self.label} ({time.monotonic() - self._t0:.0f} s)")

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(item: Download, cache_dir: Path) -> Path:
    """The verified archive for `item`, downloaded once into `cache_dir`.
    A cached file is re-verified; a mismatching one is discarded and fetched again;
    a fresh download that mismatches the pin fails the build."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{item.name}.zip"
    if dest.is_file():
        if sha256_file(dest) == item.sha256:
            log(f"{item.name}: cached archive verified (sha256 {item.sha256[:16]}...)")
            return dest
        log(f"{item.name}: cached archive does not match its pin; downloading again")
        dest.unlink()
    part = dest.with_suffix(".part")
    log(f"{item.name}: downloading {item.url} ({item.size / 1e6:.1f} MB)")
    t0 = time.monotonic()
    request = urllib.request.Request(item.url, headers={"User-Agent": "dsl-build-action"})
    digest = hashlib.sha256()
    received, next_report = 0, 0
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=120) as response, open(part, "wb") as out:
                digest, received, next_report = hashlib.sha256(), 0, 0
                while chunk := response.read(1 << 20):
                    out.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if received >= next_report:
                        log(f"{item.name}: {received / 1e6:.1f} / {item.size / 1e6:.1f} MB")
                        next_report += 8 << 20
            break
        except OSError as error:
            log(f"{item.name}: attempt {attempt} failed: {error!r}")
            if attempt == 3:
                raise
            time.sleep(5 * attempt)
    actual = digest.hexdigest()
    if actual != item.sha256:
        part.unlink(missing_ok=True)
        raise SystemExit(f"{item.name}: sha256 mismatch for {item.url}: expected {item.sha256}, "
                         f"got {actual} ({received} bytes). Upstream changed the file; "
                         f"review it and move the pin in airsim_deps.py.")
    part.replace(dest)
    log(f"{item.name}: verified sha256 {actual} ({received} bytes, {time.monotonic() - t0:.1f} s)")
    return dest


def extract(archive: Path, dest: Path, strip_prefix: str = "", only_prefix: str = "") -> int:
    """Extract `archive` into `dest`; members outside `only_prefix` are skipped,
    `strip_prefix` is removed from member names. Refuses members that would
    escape `dest`. Returns the number of files written."""
    dest = dest.resolve()
    count = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename
            if only_prefix and not name.startswith(only_prefix):
                continue
            if strip_prefix:
                if not name.startswith(strip_prefix):
                    continue
                name = name[len(strip_prefix):]
            if not name or name.endswith("/"):
                continue
            target = (dest / name).resolve()
            if dest not in target.parents:
                raise SystemExit(f"{archive.name}: member {info.filename!r} escapes {dest}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            count += 1
    return count


def mirror(src: Path, dst: Path, exclude_dirs=("temp",)) -> int:
    """robocopy /MIR equivalent: dst becomes an exact copy of src (minus excluded
    directory names). Returns the number of files copied."""
    if dst.exists():
        shutil.rmtree(dst)
    ignore = shutil.ignore_patterns(*exclude_dirs)
    shutil.copytree(src, dst, ignore=ignore)
    return sum(1 for cur in dst.rglob("*") if cur.is_file())


def run(argv: list, cwd: Path, env: dict | None = None, label: str = "") -> None:
    """Run a tool with its output streamed straight into the build log."""
    label = label or Path(str(argv[0])).name
    log(f"$ {' '.join(str(cur) for cur in argv)}   (cwd {cwd})")
    t0 = time.monotonic()
    with Heartbeat(label):
        completed = subprocess.run([str(cur) for cur in argv], cwd=str(cwd), env=env)
    log(f"{label}: exit {completed.returncode} after {time.monotonic() - t0:.0f} s")
    if completed.returncode != 0:
        raise SystemExit(f"{label} failed with exit code {completed.returncode}")


def find_project_dir() -> Path:
    """The Blocks project folder. DSL_PROJECT_DIR is the documented project
    folder; when it does not hold the .uproject (an agent that reports the
    repository root instead), fall back to the repository-relative path."""
    for key in ("DSL_PROJECT_DIR", "DSL_REPO_DIR"):
        value = os.environ.get(key)
        if value and (Path(value) / UPROJECT).is_file():
            return Path(value)
    repo = Path(os.environ.get("DSL_REPO_DIR") or Path(__file__).resolve().parents[2])
    candidate = repo / PROJECT_REL
    if not (candidate / UPROJECT).is_file():
        raise SystemExit(f"cannot find {UPROJECT}: not in DSL_PROJECT_DIR, nor at {candidate}")
    return candidate


def scan(path: Path, needles: dict, chunk: int = 64 << 20) -> set:
    """Names of `needles` ({name: bytes}) found in the file, read in overlapping chunks."""
    found: set = set()
    overlap = max(len(cur) for cur in needles.values())
    tail = b""
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            window = tail + block
            for name, needle in needles.items():
                if name not in found and needle in window:
                    found.add(name)
            if len(found) == len(needles):
                break
            tail = window[-overlap:]
    return found


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Offline self-test (no network): python airsim_deps.py --selftest
# --------------------------------------------------------------------------
def _selftest() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        archive = tmp / "a.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("top-1.0/inc/a.h", "a")
            zf.writestr("top-1.0/Eigen/Core", "core")
            zf.writestr("other/x", "x")
        assert extract(archive, tmp / "s", strip_prefix="top-1.0/") == 2
        assert (tmp / "s" / "Eigen" / "Core").read_text() == "core"
        assert extract(archive, tmp / "o", only_prefix="top-1.0/Eigen/") == 1
        evil = tmp / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../escape", "x")
        try:
            extract(evil, tmp / "e")
            raise AssertionError("zip-slip not refused")
        except SystemExit:
            pass
        cache = tmp / "cache"
        cache.mkdir()
        good = Download("good", "http://invalid.invalid/", sha256_file(archive), archive.stat().st_size)
        shutil.copy(archive, cache / "good.zip")
        assert fetch(good, cache) == cache / "good.zip"          # cached + verified, no network
        blob = tmp / "blob.bin"
        blob.write_bytes(b"\0" * 100 + "Warning, WeatherAPI got invalid paramname!".encode("utf-16-le")
                         + b"\1" * 10 + b"simTestLineOfSightBetweenPoints")
        assert scan(blob, EXE_NEEDLES, chunk=7) == {"airsim_plugin", "airlib"}
        src = tmp / "src"
        (src / "temp").mkdir(parents=True)
        (src / "temp" / "t").write_text("t")
        (src / "keep").write_text("k")
        assert mirror(src, tmp / "dst") == 1 and not (tmp / "dst" / "temp").exists()
    print("airsim_deps selftest: ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        _selftest()
    else:
        print(__doc__)
