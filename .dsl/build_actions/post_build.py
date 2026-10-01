"""DSL build action: prove the packaged Win64 game really contains the AirSim plugin.

Runs after the Unreal package step, before it is archived. Fails the build
unless the packaged output (DSL_OUTPUT_DIR) has:

* the game executable with the AirSim plugin module, AirLib's RPC server and
  rpclib linked in (a monolithic packaged game links the plugin into the
  executable, so each is proven by a string literal from it -- see
  airsim_deps.EXE_NEEDLES);
* the high-poly SUV car assets (VehicleAdv/SUV, from the external zip) cooked
  into the game's paks.

It then writes dsl_airsim_verification.json into DSL_OUTPUT_DIR -- so it ships
in the build's archive -- with what it found and pre_build's manifest.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import airsim_deps as deps  # noqa: E402

PROJECT_NAME = "Blocks"


def main() -> int:
    output = Path(os.environ.get("DSL_OUTPUT_DIR", ""))
    if not os.environ.get("DSL_OUTPUT_DIR") or not output.is_dir():
        raise SystemExit(f"DSL_OUTPUT_DIR is not a directory: {os.environ.get('DSL_OUTPUT_DIR')!r}")
    project = deps.find_project_dir()
    deps.log(f"post_build: output={output} project={project}")

    exes = sorted(p for p in output.rglob(f"{PROJECT_NAME}*.exe")
                  if p.parent.name == "Win64" and p.parent.parent.name == "Binaries")
    if not exes:
        listing = sorted(str(p.relative_to(output)) for p in output.rglob("*.exe"))
        raise SystemExit(f"no packaged {PROJECT_NAME} game executable under {output}; .exe files: {listing}")
    exe_found = {}
    for exe in exes:
        found = deps.scan(exe, deps.EXE_NEEDLES)
        exe_found[str(exe.relative_to(output))] = {"bytes": exe.stat().st_size, "found": sorted(found)}
        deps.log(f"{exe.relative_to(output)}: {exe.stat().st_size} bytes, AirSim code found: {sorted(found)}")
    linked = set().union(*(set(cur["found"]) for cur in exe_found.values()))
    missing_code = sorted(set(deps.EXE_NEEDLES) - linked)

    paks = sorted(p for p in output.rglob("*") if p.suffix.lower() in (".pak", ".utoc", ".ucas"))
    pak_found: set = set()
    for pak in paks:
        hit = deps.scan(pak, deps.PAK_NEEDLES)
        if hit:
            deps.log(f"{pak.relative_to(output)}: car assets found: {sorted(hit)}")
        pak_found |= hit
    missing_assets = sorted(set(deps.PAK_NEEDLES) - pak_found)

    manifest_path = project / "Plugins" / "AirSim" / deps.MANIFEST_NAME
    prebuild = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
    report = {
        "executables": exe_found,
        "paks": [str(p.relative_to(output)) for p in paks],
        "car_assets_found": sorted(pak_found),
        "missing_code": missing_code,
        "missing_assets": missing_assets,
        "prebuild_manifest": prebuild,
        "commit": os.environ.get("DSL_COMMIT"),
        "version": os.environ.get("DSL_VERSION"),  # absent on an unversioned branch
        "build_number": os.environ.get("DSL_BUILD_NUMBER"),
    }
    deps.write_json(output / "dsl_airsim_verification.json", report)

    problems = []
    if missing_code:
        problems.append(f"game executable lacks AirSim code from: {missing_code}")
    if not paks:
        problems.append("no .pak/.utoc/.ucas in the package")
    elif missing_assets:
        problems.append(f"car assets not cooked into the paks: {missing_assets}")
    if prebuild is None:
        problems.append(f"pre_build manifest missing: {manifest_path}")
    if problems:
        raise SystemExit("AirSim package verification FAILED: " + "; ".join(problems))
    deps.log("post_build: packaged game contains the AirSim plugin (plugin module, AirLib, rpclib) "
             "and the SUV car assets; wrote dsl_airsim_verification.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
