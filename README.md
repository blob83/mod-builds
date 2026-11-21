# Starling's mod builds

Native mods are compiled into [Starling](https://github.com/starling-client/starling), and a phone can't do that itself. When a player taps **Compile**, Starling sends their mods to the Compile service (`worker/`, on Cloudflare), which asks this repository to compile the game with them. The phone then downloads the result from this repository's `results` release.

- `worker/` is the service the game talks to, at the address in Starling's [`build-server.txt`](https://github.com/starling-client/starling/blob/starling/packaging/android/build-server.txt). It seals the mods (AES-256-CTR, a key for each build) into the `requests` release under a random name, and starts `build.yml` with the key in the run's inputs. The same mods on the same commit are the same job, so a build is never done twice. `npm test` there runs its tests, with no network.
- `build.yml` compiles one player's mods. A mod's code runs while it compiles, so that happens in a sandbox with no network, and that job's token can only read this repository. A second job, which runs nothing from the mods, puts the library and its status in `results`, and deletes the sealed mods.
- `clean.yml` deletes old builds, and sealed mods no run took, every six hours.
- `base.yml` makes the clean build cache for one version of the game. It only runs trusted code, and it's the only job that writes the cache.
- `versions.json` says which commit each version of the game is.

Runs and their logs are public, but the mods aren't: the key never shows up in a log. Builds are kept for two weeks.

The service's answer to a build names this repository (`repo`), and a Starling that knows it waits for `results-<job's first digit>/<job>.json` (or `.failed.json`) and downloads `<job>.so` straight from GitHub, asking the service only every ten minutes or so while it waits. A Starling before it asks `GET /v1/job/<job>` again two seconds after each answer, so the service holds that answer up to 25 s.

## Compile v2: a library per mod

With the game as a set of Rust libraries (a Nightly APK built with `-C prefer-dynamic`, Starling's `tools/android_native_sdk.py`), a native mod is a small library of its own, `lib<id>.so`, built once for each game build and ABI beside that build's own Cargo build, and kept in a store the game reads straight from GitHub into its `native-libs` folder:

- `store-base.yml` makes a build's base: Starling at the build's commit, `android_native_sdk.py prepare` and `build_apk.py --library-only` with `-C prefer-dynamic`, kept as the build cache `store-<build>-<abi>-<profile>` (`scripts/store_base.sh`). Given the Nightly's APK, it first checks that each library of the game built here (`libemutls.so` too) is the APK's, as a mod built beside other ones wouldn't load on the phone. `prepare` makes adding a mod's package change only `Cargo.lock`, so a mod's build doesn't build the game again. Then it adds the build to `store/builds.json`.
- `store/builds.json`: every game build the store serves, newest first: its commit, its Nightly, the Cargo profile of its APK, and its ABIs.
- `mods-<build>/index.json`: that build's mods, each id, version and `.hmod` SHA-256 to its library (`<job>.so`, named `lib<id>.so` on the phone), the library's SHA-256 and its identity, and the mods that failed there. A job is the `.hmod`'s SHA-256 (32 digits) and the ABI.
- `catalog.yml` builds every native mod of [Horizon's catalog](https://horizon-rust.github.io/horizon-mods/) for one build, ahead of the players.
- `mod.yml` builds one mod the store doesn't have, asked through the service (`POST /v2/mod?build=B&abi=A` with the `.hmod`). Its answer is `mods-<build>/<job>.json` or `.failed.json`, which the game waits for on GitHub.

Both build in a sandbox (`scripts/sandbox.sh`: no network, the base seen through a layer thrown away after) with a token that can only read, and publish from a job that runs nothing of the mods (`scripts/store.py`). Only a mod `android_native_sdk.py mods` takes is built: no `[native.*]` tables, build script, features, or crate but `horizon-mod-api`; any other is told it needs the whole-game Compile. `python -m unittest discover -s scripts` tests the scripts.
