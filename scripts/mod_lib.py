#!/usr/bin/env python3
"""Compile v2's build of one mod at a time: each .hmod's library beside a game build's own Cargo build (the base,
scripts/store_base.sh), as the game's loader reads it from its native-libs folder (Starling's android_lib.rs and
horizon-mod-host's native_libraries.rs): `lib<id>.so`, whose `horizon_native_build` says its identity,
"<build> <id> <version> <rustc release>", and a loader refuses a library of another build.

    python3 scripts/mod_lib.py build JOBS MODS RESULT   # BUILD, ABI, PROFILE, MOD_LIB_SOURCE

JOBS is a JSON list of {job, abi, sha256}; each job's .hmod is MODS/<sha256>.hmod. Its answer is
RESULT/<job>.status.json and, when it built, RESULT/<job>.so; scripts/store.py publishes them from a job that runs
nothing of the mods. This runs in the sandbox (scripts/sandbox.sh): no network, no token, nothing kept.

The build is Starling's own, from the base's checkout (MOD_LIB_SOURCE): `tools/android_native_sdk.py mods OUT
X.hmod --profile PROFILE --abi ABI`, with -C prefer-dynamic, so the game's crates are the ones the APK ships and only
the mod compiles. Only a mod that route takes is built: no `[native.*]` tables (mixins, bridges, modules), no build
script, no features, no crate but horizon-mod-api. Any other is told to use the whole-game Compile, for good.
"""
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import store  # noqa: E402

# What an unpacked .hmod may be at most, and how many files (a zip bomb stops here).
UNPACKED = 64 << 20
FILES = 4000
# A mod's build, at most.
SECONDS = int(os.environ.get("MOD_LIB_SECONDS", "600"))
# The game's crates a mod may depend on: android_native_sdk.py points this one at the base's checkout.
API_PACKAGES = {"horizon-mod-api"}
# What in a build's output says the mod's own code doesn't compile (for good), not the service.
OWN_ERROR = re.compile(r"error\[E\d{4}\]|could not compile `hmod-")
WHOLE_GAME = "needs the whole-game Compile"


class Refused(Exception):
    """A mod this route can't build, for good."""


def unpack(hmod, into):
    """The .hmod's files into `into`: plain files and folders inside it, within UNPACKED bytes."""
    with zipfile.ZipFile(hmod) as archive:
        entries = archive.infolist()
        if len(entries) > FILES:
            raise Refused(f"has more than {FILES} files")
        total = 0
        for entry in entries:
            name = entry.filename
            parts = pathlib.PurePosixPath(name).parts
            if not parts or name.startswith("/") or "\\" in name or any(p in ("..", "") for p in parts):
                raise Refused(f"has a file outside itself: {name!r}")
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise Refused(f"has a link: {name}")
            if entry.is_dir():
                (into / name).mkdir(parents=True, exist_ok=True)
                continue
            total += entry.file_size
            if total > UNPACKED:
                raise Refused(f"is more than {UNPACKED >> 20} MB unpacked")
            (into / name).parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, open(into / name, "wb") as target:
                shutil.copyfileobj(source, target, 1 << 20)
            if (into / name).stat().st_size != entry.file_size:
                raise Refused(f"{name}: not the size it says")


def mod_of(folder):
    """The mod's id and version (horizon-mod.toml), and its Cargo.toml."""
    try:
        manifest = tomllib.loads((folder / "horizon-mod.toml").read_text())
        cargo = tomllib.loads((folder / "Cargo.toml").read_text())
    except (OSError, ValueError) as e:
        raise Refused(f"is not a native mod: {e}")
    mod, version = manifest.get("id"), manifest.get("version")
    if not isinstance(manifest.get("native"), dict):
        raise Refused("is not a native mod")
    if not (isinstance(mod, str) and store.ID.fullmatch(mod)) or not (
        isinstance(version, str) and store.VERSION.fullmatch(version)
    ):
        raise Refused("has no plain id and version")
    return mod, version, manifest, cargo


def check(manifest, cargo, folder):
    """Refused, for good, when this route won't build it."""
    tables = sorted(k for k, v in (manifest.get("native") or {}).items() if isinstance(v, (list, dict)))
    why = f"has [native.{', native.'.join(tables)}]" if tables else refusal(cargo, folder)
    if why:
        raise Refused(f"{why}: it {WHOLE_GAME}")


def refusal(cargo, folder):
    """Why the API route won't build this crate, or None (the launcher's `project_refusal`)."""
    package = cargo.get("package") or {}
    build = package.get("build")
    if (build is not None and build is not False) or (build is None and (folder / "build.rs").is_file()):
        return "has a build script"
    if cargo.get("build-dependencies"):
        return "has build dependencies"
    if cargo.get("features"):
        return "has features"
    for table in dependency_tables(cargo):
        for key, value in table.items():
            krate = value.get("package", key) if isinstance(value, dict) else key
            if krate not in API_PACKAGES:
                return f"uses the crate {krate}"
            if isinstance(value, dict) and (value.get("features") or value.get("default-features") is False):
                return f"chooses features of {krate}"
    return None


def dependency_tables(cargo):
    yield cargo.get("dependencies") or {}
    for target in (cargo.get("target") or {}).values():
        if isinstance(target, dict):
            yield target.get("dependencies") or {}


def build_one(row, mods, source, result, build, profile):
    """One job's answer into `result`, built with the base's checkout `source`."""
    job, abi, digest = row["job"], row["abi"], row["sha256"]
    status = {"build": build, "source": digest, "abi": abi}
    work = result / "work" / job
    shutil.rmtree(work, ignore_errors=True)
    folder, out = work / "mod", work / "out"
    folder.mkdir(parents=True)
    try:
        hmod = mods / f"{digest}.hmod"
        if store.job_of(store.sha256_of(hmod), abi) != job:
            raise Refused("is not the file that was asked for")
        try:
            unpack(hmod, folder)
        except zipfile.BadZipFile:
            raise Refused("is not a .hmod")
        mod, version, manifest, cargo = mod_of(folder)
        status.update(id=mod, version=version)
        check(manifest, cargo, folder)
        command = [sys.executable, str(source / "tools" / "android_native_sdk.py"), "mods", str(out), str(hmod),
                   "--profile", profile, "--abi", abi]
        env = {**os.environ, "RUSTFLAGS": "-C prefer-dynamic", "HORIZON_GAME_BUILD_ID": build}
        try:
            done = subprocess.run(command, cwd=source, env=env, capture_output=True, text=True, timeout=SECONDS)
        except subprocess.TimeoutExpired:
            return said(result, job, {**status, "state": "failed", "error": "The build took too long.", "settled": False})
        if done.returncode != 0:
            said_by = done.stdout + "\n" + done.stderr
            print(said_by[-4000:], flush=True)
            # Cargo's own error lines first: the game shows the answer's first line, and the end of the output
            # is Python's traceback around the failed command (a phone showed ")))", 2026-11-20).
            errors = [line for line in said_by.splitlines() if line.startswith("error")]
            error = "\n".join(errors[:6])[:600] if errors else \
                (done.stderr or done.stdout or "The build failed.").strip()[-600:]
            return said(result, job, {**status, "state": "failed", "error": error,
                                      "settled": bool(OWN_ERROR.search(said_by))})
        record = json.loads((out / "native-mods.json").read_text())
        made = next((r for r in record.get("libraries", []) if isinstance(r, dict) and r.get("mod") == mod), None)
        if made is None:
            raise ValueError(f"the build made no library of {mod}")
        name, identity = made.get("file"), made.get("identity")
        if not (isinstance(name, str) and store.LIBRARY.fullmatch(name) and (out / name).is_file()):
            raise ValueError(f"the build named no plain library: {name!r}")
        words = str(identity).split(" ")
        if len(words) != 4 or words[:3] != [build, mod, version]:
            raise ValueError(f"the library's identity {identity!r} is not {build[:12]} {mod} {version}")
        library = result / f"{job}.so"
        shutil.copyfile(out / name, library)
        if store.sha256_of(library) != made.get("sha256"):
            raise ValueError("the library is not the one its record says")
        return said(result, job, {**status, "state": "done", "library": name, "identity": identity,
                                  "sha256": store.sha256_of(library), "size": library.stat().st_size})
    except Refused as e:
        return said(result, job, {**status, "state": "failed", "error": f"This mod {e}.", "settled": True})
    except (OSError, ValueError) as e:
        return said(result, job, {**status, "state": "failed", "error": f"The build went wrong: {e}", "settled": False})
    finally:
        shutil.rmtree(work, ignore_errors=True)


def said(result, job, status):
    (result / f"{job}.status.json").write_text(json.dumps(status))
    print(f"{job}: {status.get('id', '?')} {status['state']}"
          + (f": {status['error']}" if status["state"] == "failed" else ""), flush=True)
    return status


def main(argv):
    if len(argv) != 5 or argv[1] != "build":
        sys.exit(__doc__)
    build = os.environ.get("BUILD", "")
    if not store.BUILD.fullmatch(build):
        sys.exit(f"Not a build: {build!r}")
    source = pathlib.Path(os.environ["MOD_LIB_SOURCE"])
    profile = os.environ.get("PROFILE", "fast")
    jobs = json.loads(pathlib.Path(argv[2]).read_text())
    mods, result = pathlib.Path(argv[3]), pathlib.Path(argv[4])
    result.mkdir(parents=True, exist_ok=True)
    for row in jobs:
        if not (store.JOB.fullmatch(str(row.get("job"))) and store.SHA256.fullmatch(str(row.get("sha256")))):
            continue
        build_one(row, mods, source, result, build, profile)
    shutil.rmtree(result / "work", ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv)
