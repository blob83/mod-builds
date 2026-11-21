#!/usr/bin/env bash
# Compile v2's base for game build BUILD and ABI: the APK's own Cargo build, as Starling's android-sdk workflow makes
# it (tools/android_native_sdk.py `prepare`, then build_apk.py with -C prefer-dynamic), so a mod's library builds
# beside it in a minute (android_native_sdk.py `mods`) against the very libraries the APK ships. Only trusted code
# runs here, and only store-base.yml writes this cache. Its folders are the cache's, so they never change:
# ~/store (the checkout with its target, and Cargo's home), ~/android, ~/.rustup.
#
# Given APK (a Nightly's APK on GitHub), each of the game's libraries built here is checked against the APK's: a mod
# built beside libraries that differ from the phone's would not load there.
set -euo pipefail
[[ "${BUILD:-}" =~ ^[0-9a-f]{40}$ ]] || { echo "Not a build: ${BUILD:-}"; exit 1; }
# The profile players' Nightlies are built with (android.yml: `fast`, as releases), or `quick` for tests.
[[ "${PROFILE:-}" =~ ^(fast|quick)$ ]] || { echo "Not a profile: ${PROFILE:-}"; exit 1; }
triple=$(python3 -I -c 'import os, sys; sys.path.insert(0, "scripts"); import store; print(store.TARGETS[os.environ["ABI"]])')
root=$HOME/store
NDK=28.2.13676358

rm -rf "$root"
git init -q "$root/source"
git -C "$root/source" fetch -q --depth 1 https://github.com/starling-client/starling "$BUILD"
git -C "$root/source" checkout -q --detach FETCH_HEAD

# The NDK, the build tools and the platform build_apk.py pins, where the sandbox finds them. store-base.yml installs
# them before `room.sh clean` deletes the runner's SDK, sdkmanager with it.
if [ ! -d "$HOME/android/ndk/$NDK" ]; then
  (yes || true) | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$HOME/android" \
    --install "ndk;$NDK" "build-tools;35.0.0" "platforms;android-36" | tail -3
fi
export ANDROID_HOME=$HOME/android ANDROID_NDK_HOME=$HOME/android/ndk/$NDK

cd "$root/source"
# The Rust the game pins (tools/ci/rust-version), with the std's source build_apk.py builds the std from.
rm -rf "$HOME/.rustup" "$HOME/.cargo"
curl -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal --default-toolchain none
# shellcheck disable=SC1091
. "$HOME/.cargo/env"
GITHUB_ENV='' bash tools/ci/rustup_install.sh --profile minimal --component rust-src --target "$triple"
cargo install cargo-ndk --version 3.5.4 --locked
python3 tools/android_native_sdk.py prepare
# The game's build identity as each mod's build gives it (mod_lib.py): horizon-mod-host's build script reads it
# (rerun-if-env-changed), so a base built without it had Cargo build the whole game again for every mod, some 15
# minutes, though `git rev-parse HEAD` named the same commit (2026-11-20).
export HORIZON_GAME_BUILD_ID=$BUILD
started=$(date +%s)
RUSTFLAGS="-C prefer-dynamic" python3 packaging/android/build_apk.py --abi "$ABI" --profile "$PROFILE" --library-only
echo "The game's build: $(( $(date +%s) - started )) s"

# The game's libraries as the APK ships them (build_apk.py's entry library and `rust_dylibs`, stripped, with the
# emulated TLS runtime libemutls.so every one of them and every mod needs), by the names build_apk.py gives them:
# which ones they are is the game's to say.
shipped=$root/shipped
mkdir -p "$shipped"
RUSTFLAGS="-C prefer-dynamic" python3 - "$triple" "$PROFILE" "$shipped" <<'EOF'
import os, pathlib, shutil, sys
sys.path.insert(0, "packaging/android")
import build_apk
triple, profile, out = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
built = pathlib.Path("target") / triple / ("debug" if profile == "dev" else profile) / f"lib{build_apk.LIBRARY}.so"
shutil.copyfile(pathlib.Path("target/android") / f"lib{build_apk.LIBRARY}.so", out / f"lib{build_apk.LIBRARY}.so")
build_apk.rust_dylibs(pathlib.Path(os.environ["ANDROID_NDK_HOME"]), built.resolve(), out)
EOF
(cd "$shipped" && sha256sum ./*.so)

if [ -n "${APK:-}" ]; then
  [[ "$APK" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/ ]] \
    || { echo "An APK is a GitHub release's download, not $APK"; exit 1; }
  curl -fsSL --retry 5 -o "$RUNNER_TEMP/nightly.apk" "$APK"
  mkdir -p "$RUNNER_TEMP/apk"
  unzip -q -o "$RUNNER_TEMP/nightly.apk" "lib/$ABI/*.so" -d "$RUNNER_TEMP/apk"
  differ=0
  for library in "$shipped"/*.so; do
    name=$(basename "$library")
    theirs=$RUNNER_TEMP/apk/lib/$ABI/$name
    if [ ! -f "$theirs" ]; then
      echo "::error::$name is not in the APK"; differ=1
    elif ! cmp -s "$library" "$theirs"; then
      echo "::error::$name differs from the APK's: a mod built here would not load on the phone"; differ=1
    fi
  done
  [ "$differ" = 0 ] || exit 1
  echo "Every library of the game is the APK's."
fi

# Cargo's home goes with the base: the sandbox has no network to fetch crates.
mv "$HOME/.cargo" "$root/cargo"
du -sh "$root/source/target" "$root/cargo" "$HOME/android" "$HOME/.rustup"
