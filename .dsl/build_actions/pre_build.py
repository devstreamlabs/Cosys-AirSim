"""DSL build action: make Unreal/Environments/Blocks buildable before the engine runs.

Blocks depends on the AirSim plugin, which this repository does NOT carry in
the project: upstream's build.cmd assembles it from external downloads plus a
native build of AirLib, and update_from_git.bat copies it into Blocks/Plugins.
This script does the same, Windows only (the farm's Win64 builds), faithfully
but Release-only. See DSL_BUILD.md.

Inputs (from the DSL build agent): DSL_REPO_DIR, DSL_PROJECT_DIR, DSL_HOST_OS,
DSL_PLATFORM, DSL_WORKSPACE_REUSED. Optional: AIRSIM_VCTOOLSVERSION pins the
MSVC toolset (docs/install_windows.md FAQ), as upstream build.cmd honours it.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import airsim_deps as deps  # noqa: E402

T0 = time.monotonic()


def vs_environment(toolset: str) -> dict:
    """The x64 Native Tools environment upstream build.cmd requires (AirSim.props
    reads WindowsSDKVersion from it), captured from vcvars64.bat."""
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
        "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        raise SystemExit(f"Visual Studio is not installed on this build machine ({vswhere} missing)")
    install = subprocess.run(
        [str(vswhere), "-latest", "-products", "*", "-requires",
         "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
        capture_output=True, text=True, check=True).stdout.strip().splitlines()
    if not install:
        raise SystemExit("no Visual Studio with the MSVC x64 tools found by vswhere")
    vs_root = Path(install[0])
    msvc_dir = vs_root / "VC" / "Tools" / "MSVC"
    toolsets = sorted(cur.name for cur in msvc_dir.iterdir()) if msvc_dir.is_dir() else []
    deps.log(f"Visual Studio: {vs_root}; MSVC toolsets installed: {toolsets}")
    vcvars = vs_root / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    call = f'call "{vcvars}"' + (f" -vcvars_ver={toolset}" if toolset else "")
    out = subprocess.run(f'cmd /d /s /c "{call} >nul && set"', capture_output=True, text=True,
                         shell=True)
    if out.returncode != 0:
        raise SystemExit(f"vcvars64.bat failed ({out.returncode}): {out.stdout[-2000:]}{out.stderr[-2000:]}")
    env = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line and not line.startswith("="))
    deps.log(f"VS environment: VisualStudioVersion={env.get('VisualStudioVersion')} "
             f"VCToolsVersion={env.get('VCToolsVersion')} WindowsSDKVersion={env.get('WindowsSDKVersion')}")
    if not env.get("VCToolsVersion"):
        raise SystemExit("vcvars64.bat did not set VCToolsVersion")
    return env


def tool(env: dict, name: str) -> str:
    for directory in env.get("PATH", env.get("Path", "")).split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    raise SystemExit(f"{name} not found on the Visual Studio environment's PATH")


def main() -> int:
    host = os.environ.get("DSL_HOST_OS", sys.platform)
    platform = os.environ.get("DSL_PLATFORM", "")
    repo = Path(os.environ.get("DSL_REPO_DIR") or Path(__file__).resolve().parents[2])
    project = deps.find_project_dir()
    deps.log(f"pre_build: host={host} platform={platform} repo={repo}")
    deps.log(f"pre_build: DSL_PROJECT_DIR={os.environ.get('DSL_PROJECT_DIR')!r} -> Blocks project {project}")
    if host != "win32":
        raise SystemExit(f"this repository's pre_build supports Windows (Win64) builds only; host is {host}. "
                         f"Linux would need upstream build.sh's clang toolchain.")

    toolset = os.environ.get("AIRSIM_VCTOOLSVERSION", "").strip()
    cache = repo / "external" / "dsl_download_cache"
    archives = {cur.name: deps.fetch(cur, cache) for cur in deps.DOWNLOADS}
    env = vs_environment(toolset)

    # ---- rpclib: download, build Release with cmake, copy into AirLib/deps ----
    rpc_root = repo / "external" / "rpclib"
    rpc_src = rpc_root / deps.RPCLIB.name
    shutil.rmtree(rpc_root, ignore_errors=True)
    n = deps.extract(archives[deps.RPCLIB.name], rpc_root)
    deps.log(f"rpclib: extracted {n} files to {rpc_src}")
    build_dir = rpc_src / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    cmake = tool(env, "cmake.exe")
    configure = [cmake, "-G", "Visual Studio 17 2022", "-A", "x64"]
    if toolset:
        configure += ["-T", f"version={toolset}"]
    deps.run(configure + [".."], build_dir, env, "cmake configure rpclib")
    deps.run([cmake, "--build", ".", "--config", "Release", "--", "/m", "/nologo", "/v:minimal"],
             build_dir, env, "cmake build rpclib")
    airlib = repo / "AirLib"
    deps.mirror(rpc_src / "include", airlib / "deps" / "rpclib" / "include")
    deps.mirror(build_dir / "Release", airlib / "deps" / "rpclib" / "lib" / "x64" / "Release")

    # ---- high-poly SUV car assets into the plugin's VehicleAdv ----
    vehicle_adv = repo / "Unreal" / "Plugins" / "AirSim" / "Content" / "VehicleAdv"
    shutil.rmtree(vehicle_adv / "SUV", ignore_errors=True)
    n = deps.extract(archives[deps.CAR_ASSETS.name], vehicle_adv, only_prefix="SUV/")
    deps.log(f"car assets: extracted {n} files to {vehicle_adv / 'SUV'}")

    # ---- Eigen headers ----
    eigen_dst = airlib / "deps" / "eigen3"
    shutil.rmtree(eigen_dst, ignore_errors=True)
    n = deps.extract(archives[deps.EIGEN.name], eigen_dst, strip_prefix=f"{deps.EIGEN.name}/",
                     only_prefix=f"{deps.EIGEN.name}/Eigen/")
    deps.log(f"eigen: extracted {n} files to {eigen_dst / 'Eigen'}")

    # ---- AirSim.sln (AirLib, MavLinkCom, ...) Release x64 ----
    msbuild = tool(env, "MSBuild.exe")
    argv = [msbuild, "AirSim.sln", "-maxcpucount", "-nologo", "-verbosity:minimal",
            "/p:Platform=x64", "/p:Configuration=Release"]
    if toolset:
        argv.append(f"/p:VCToolsVersion={toolset}")
    deps.run(argv, repo, env, "msbuild AirSim.sln Release")

    # ---- MavLinkCom into AirLib/deps; AirLib into the plugin ----
    deps.mirror(repo / "MavLinkCom" / "include", airlib / "deps" / "MavLinkCom" / "include")
    deps.mirror(repo / "MavLinkCom" / "lib", airlib / "deps" / "MavLinkCom" / "lib")
    plugin_src = repo / "Unreal" / "Plugins" / "AirSim"
    deps.mirror(airlib, plugin_src / "Source" / "AirLib")
    shutil.copy2(repo / "AirSim.props", plugin_src / "Source" / "AirLib" / "AirSim.props")

    # ---- update_from_git.bat: the plugin into Blocks/Plugins ----
    plugin_dst = project / "Plugins" / "AirSim"
    n = deps.mirror(plugin_src, plugin_dst)
    deps.log(f"plugin: copied {n} files to {plugin_dst}")

    # ---- verify the assembled plugin before the engine sees it ----
    lib_root = plugin_dst / "Source" / "AirLib"
    required = {
        "AirSim.uplugin": plugin_dst / "AirSim.uplugin",
        "AirLib.lib": lib_root / "lib" / "x64" / "Release" / "AirLib.lib",
        "rpc.lib": lib_root / "deps" / "rpclib" / "lib" / "x64" / "Release" / "rpc.lib",
        "MavLinkCom.lib": lib_root / "deps" / "MavLinkCom" / "lib" / "x64" / "Release" / "MavLinkCom.lib",
        "Eigen/Core": lib_root / "deps" / "eigen3" / "Eigen" / "Core",
        "SuvCarPawn.uasset": plugin_dst / "Content" / "VehicleAdv" / "SUV" / "SuvCarPawn.uasset",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        raise SystemExit(f"assembled AirSim plugin is incomplete, missing: {missing}")
    manifest = {
        "downloads": {cur.name: {"url": cur.url, "sha256": cur.sha256} for cur in deps.DOWNLOADS},
        "files": {name: {"path": str(path.relative_to(project)), "bytes": path.stat().st_size,
                         "sha256": deps.sha256_file(path)} for name, path in required.items()},
        "vc_tools_version": env.get("VCToolsVersion"),
        "windows_sdk_version": env.get("WindowsSDKVersion", "").strip("\\"),
        "commit": os.environ.get("DSL_COMMIT"),
        "build": f"{os.environ.get('DSL_VERSION')}.{os.environ.get('DSL_BUILD_NUMBER')}",
        "seconds": round(time.monotonic() - T0, 1),
    }
    deps.write_json(plugin_dst / deps.MANIFEST_NAME, manifest)
    for name, entry in manifest["files"].items():
        deps.log(f"verified {name}: {entry['bytes']} bytes sha256 {entry['sha256'][:16]}...")
    deps.log(f"pre_build: AirSim plugin ready in {manifest['seconds']} s "
             f"(MSVC {manifest['vc_tools_version']}, SDK {manifest['windows_sdk_version']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
