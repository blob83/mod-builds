#!/usr/bin/env bash
# The player's mods: the sealed asset the Compile service (worker/) put in the requests release, opened
# with the key in this run's inputs. The key is read from the event, never from the workflow file, so it
# shows nowhere in the log; the mods are checked against the job they were asked as.
#
# build.yml's job is the whole game with a bundle of mods (mods.bin). mod.yml's (ABI set) is one .hmod
# for one ABI, its SHA-256 in the job: it goes to mods/<sha256>.hmod, with jobs.json for mod_lib.py.
set -euo pipefail
if [ -n "${ABI:-}" ]; then
  [[ "$ASK" =~ ^[0-9a-f]{32}$ && "$JOB" =~ ^[0-9a-f]{32}\.[a-z0-9_-]+$ && "${JOB#*.}" == "$ABI" ]] \
    || { echo "Not a request: $ASK $JOB $ABI"; exit 1; }
else
  [[ "$ASK" =~ ^[0-9a-f]{32}$ && "$JOB" =~ ^[0-9a-f]{24}$ ]] || { echo "Not a request: $ASK $JOB"; exit 1; }
fi
key=$(python3 -c 'import json, os; print(json.load(open(os.environ["GITHUB_EVENT_PATH"]))["inputs"]["key"])')
[[ "$key" =~ ^[0-9a-f]{96}$ ]] || { echo "Not a key."; exit 1; }
echo "::add-mask::$key"
echo "::add-mask::${key:0:64}"
curl -fsSL --retry 5 --retry-all-errors -o mods.sealed \
  "https://github.com/$GITHUB_REPOSITORY/releases/download/requests/$ASK.bin"
openssl enc -d -aes-256-ctr -K "${key:0:64}" -iv "${key:64:32}" -in mods.sealed -out mods.bin
rm mods.sealed
python3 - <<'EOF'
import hashlib, json, os, pathlib, sys
body = open("mods.bin", "rb").read()
if os.environ.get("ABI"):
    source = hashlib.sha256(body).hexdigest()
    if f"{source[:32]}.{os.environ['ABI']}" != os.environ["JOB"]:
        sys.exit("The mod is not the one that was asked for.")
    pathlib.Path("mods").mkdir(exist_ok=True)
    os.replace("mods.bin", f"mods/{source}.hmod")
    jobs = [{"job": os.environ["JOB"], "abi": os.environ["ABI"], "sha256": source}]
    pathlib.Path("jobs.json").write_text(json.dumps(jobs))
    print(f"{len(body)} bytes of mod")
    sys.exit()
asked = f"/v1/build?version={os.environ['VERSION']}&commit={os.environ['COMMIT']}".encode()
if hashlib.sha256(asked + body).hexdigest()[:24] != os.environ["JOB"]:
    sys.exit("The mods are not the ones that were asked for.")
print(f"{len(body)} bytes of mods")
EOF
