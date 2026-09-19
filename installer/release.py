"""Cut a release:  .venv\\Scripts\\python installer\\release.py 1.1.0 --repo owner/name "What changed"

Without --publish it stops after building, so the result can be looked at first:
  1. sets givvy/version.py (__version__, and UPDATE_REPO the first time)
  2. runs the test suite; a failure stops everything
  3. commits, tags v<version>, builds dist/WhatnotGivvySetup.exe (+ .sha256), Defender-scanned

With --publish it then puts it on GitHub, which is what the app's "Check for updates" reads:
  4. pushes a CLEAN SNAPSHOT of the tracked files to the public repo's main branch. Not the
     local history: old commits contain the author's phone serial and Discord id.
  5. creates the GitHub release v<version> and uploads the installer and its checksum.

Credentials: the GitHub login already stored by Git Credential Manager (the one GitHub Desktop /
git push use) is asked for at that moment via `git credential fill`. It is never printed or saved.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PUBLIC_PATHS = ["README.md", "config.example.toml", "requirements.txt", ".gitignore", "run.py", "givvy", "installer"]


def sh(*args, cwd=ROOT, check=True, **kw):
    return subprocess.run(list(args), cwd=cwd, check=check, text=True, capture_output=True, **kw)


def set_version(version: str, repo: str) -> None:
    p = ROOT / "givvy" / "version.py"
    t = p.read_text(encoding="utf-8")
    t = re.sub(r'(?m)^__version__ = "[^"]*"', f'__version__ = "{version}"', t)
    if repo:
        t = re.sub(r'(?m)^UPDATE_REPO = "[^"]*"', f'UPDATE_REPO = "{repo}"', t)
    p.write_text(t, encoding="utf-8")


def github_token() -> str:
    r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                       text=True, capture_output=True, timeout=120)
    m = re.search(r"(?m)^password=(.+)$", r.stdout)
    if not m:
        raise SystemExit("No stored GitHub login found. Sign in once with GitHub Desktop or `git push`, then retry.")
    return m.group(1).strip()


def api(method: str, url: str, token: str, data: bytes | None = None, content_type: str = "application/json"):
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "User-Agent": "givvy-release", "Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=300) as r:
        body = r.read()
    return json.loads(body) if body else {}


def push_snapshot(repo: str, version: str) -> None:
    """Tracked files only, as one fresh commit. The guard test has already proven nothing personal is in them."""
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "snap"
        snap.mkdir()
        archive = Path(td) / "src.zip"
        # Only what someone needs to read or build the program. NOT tests/ (the fixtures are real screen
        # captures with other people's usernames and chat), NOT docs/ (internal notes), NOT scratch files.
        sh("git", "archive", "--format=zip", "-o", str(archive), "HEAD", "--", *PUBLIC_PATHS)
        shutil.unpack_archive(str(archive), str(snap))
        sh("git", "init", "-q", "-b", "main", cwd=snap)
        sh("git", "add", "-A", cwd=snap)
        sh("git", "-c", "user.name=Whatnot Givvy", "-c", "user.email=noreply@users.noreply.github.com",
           "commit", "-q", "-m", f"Whatnot Givvy v{version}", cwd=snap)
        sh("git", "push", "--force", f"https://github.com/{repo}.git", "HEAD:main", cwd=snap)


def publish(repo: str, version: str, notes: str) -> str:
    token = github_token()
    tag = f"v{version}"
    push_snapshot(repo, version)
    rel = api("POST", f"https://api.github.com/repos/{repo}/releases", token,
              json.dumps({"tag_name": tag, "target_commitish": "main", "name": f"Whatnot Givvy {version}",
                          "body": notes}).encode())
    upload = rel["upload_url"].split("{", 1)[0]
    for name, ctype in (("WhatnotGivvySetup.exe", "application/octet-stream"), ("WhatnotGivvySetup.exe.sha256", "text/plain")):
        api("POST", f"{upload}?name={name}", token, (DIST / name).read_bytes(), ctype)
    return rel.get("html_url", "")


def main(argv: list[str]) -> None:
    args = [a for a in argv if not a.startswith("--")]
    if not args or not re.fullmatch(r"\d+\.\d+\.\d+", args[0]):
        raise SystemExit(__doc__)
    version, notes = args[0], (args[1] if len(args) > 1 else f"Version {args[0]}")
    repo = next((a.split("=", 1)[1] for a in argv if a.startswith("--repo=")), "")
    if "--repo" in argv:
        repo = argv[argv.index("--repo") + 1]
        notes = args[2] if len(args) > 2 and args[1] == repo else notes
    set_version(version, repo)
    from importlib import reload
    sys.path.insert(0, str(ROOT))
    import givvy.version as V
    reload(V)
    if not V.UPDATE_REPO:
        raise SystemExit("No GitHub repo configured yet: pass --repo owner/name once.")
    print(f"version {version}, updates from github.com/{V.UPDATE_REPO}")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit("tests failed; nothing was built or published")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=Whatnot Givvy", "-c", "user.email=noreply@users.noreply.github.com", "commit", "-q", "-m", f"Release v{version}", check=False)
    sys.path.insert(0, str(ROOT / "installer"))
    import build
    build.main()
    if "--publish" not in argv:
        print("\nBuilt, NOT published. Re-run with --publish to put it on GitHub.")
        return
    print("published:", publish(V.UPDATE_REPO, version, notes))


if __name__ == "__main__":
    main(sys.argv[1:])
