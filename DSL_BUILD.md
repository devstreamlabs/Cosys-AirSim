# Building Blocks on the DSL build platform

Branch `dsl-build` makes `Unreal/Environments/Blocks` build as a customer project on the DSL
build platform (toolchain `unreal`, Win64), using **build actions**: Python scripts in
`.dsl/build_actions/` that the build agent runs at fixed points of the build.

## Project selection

The repository has exactly one `.uproject` (`Unreal/Environments/Blocks/Blocks.uproject`), so
Build Stream's project discovery forwards `build_config.project_rel_path` to it and the agent's
own `.uproject` walk finds the same file. Nothing needs configuring. `EngineAssociation` is
`5.8`; the agent resolves it to the newest installed 5.8 patch (5.8.2 on the farm).

## pre_build: pull the AirSim plugin from external locations

Blocks needs the AirSim plugin, which the project does not carry. `pre_build.py` does what
upstream `build.cmd` + `update_from_git.bat` do, Release only:

| step | source | pin |
|---|---|---|
| rpclib 2.3.1, built with CMake (VS 2022, x64) | github.com/WouterJansen/rpclib tag v2.3.1 | sha256 `3326bc0e...` |
| high-poly SUV car assets -> `Plugins/AirSim/Content/VehicleAdv/SUV` | Cosys-AirSim release `carassets` | sha256 `00fff48f...` |
| Eigen headers -> `AirLib/deps/eigen3` | github.com/WouterJansen/eigen tag 3.4.1r | sha256 `50ff45ad...` |
| `AirSim.sln` Release x64 (AirLib, MavLinkCom, ...) | this repository | built with the build machine's MSVC |
| plugin (`Unreal/Plugins/AirSim` + `AirLib`) -> `Blocks/Plugins/AirSim` | this repository | - |

- Every download is verified against the sha256 in `airsim_deps.py`; a mismatch fails the build.
- The native libraries are built with the **same MSVC toolset the engine uses** on that machine,
  so the prebuilt-lib/toolset mismatch of the release's prebuilt plugin zip cannot happen. Set
  `AIRSIM_VCTOOLSVERSION` to pin a toolset, as upstream allows.
- Tool output is streamed and a heartbeat line is printed every 60 s, so the build-action
  no-output limit (15 min) is never hit; the whole step takes a few minutes, well inside the
  30-minute per-script limit.
- It writes `Blocks/Plugins/AirSim/dsl_airsim_prebuild.json` (pins, file hashes, toolset).

## post_build: prove the package has the plugin

`post_build.py` fails the build unless the packaged game in `DSL_OUTPUT_DIR`

- has a `Blocks.exe` under `Binaries/Win64` with the AirSim plugin module, AirLib's RPC server
  and rpclib linked in (one distinctive string literal each; the prebuilt `rpc.lib` and
  `MavLinkCom.lib` are on the plugin's link line, so the link itself fails without them), and
- has the SUV car assets (`SuvCarPawn`, `Suv_Skel`) cooked into its paks.

It writes `dsl_airsim_verification.json` into the output, so the result ships in the archive.

## Turning it on

Build actions are operator-enabled per organization and project (Cloud Manager
`manage.py build_actions --org <org> [--project <proj>] --enable`). Without them the build
fails right after checkout with "build actions are not enabled for this project".

## Self-test

```
python .dsl/build_actions/airsim_deps.py --selftest
```
