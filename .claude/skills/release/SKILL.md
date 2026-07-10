---
name: release
description: Cut a new pacer release — bump the version (major/minor/patch), commit and tag it, push, then update the Gentoo overlay ebuild and Manifest. Use when the user wants to release, publish, tag, or ship a new version of pacer.
---

# Release pacer

Automates a pacer release end to end: version bump in the Python sources, a
tagged commit pushed to `origin`, then the matching ebuild + Manifest update in
the Gentoo overlay.

## Ground rules (read first)

- **No AI attribution.** Do NOT add a `Co-Authored-By` trailer, "Generated with
  Claude", or any other AI/assistant attribution to ANY commit made by this
  skill, in either repository. Plain commit messages only. This overrides the
  usual co-author default.
- **Order matters.** Push the pacer git tag **before** regenerating the overlay
  Manifest — `pkgdev`/`ebuild manifest` fetches the release tarball from GitHub
  (`.../archive/refs/tags/vX.Y.Z.tar.gz`), which only exists once the tag is
  pushed.
- Do the work on the default branch (`main`) of each repo.

## Repositories

- **Source:** `/home/nlewis/Projects/stream-tools/pacer` — remote
  `git@github.com:teknoman117/pacer.git`. Tags are `vX.Y.Z` (lightweight).
- **Overlay:** `/home/nlewis/Projects/gentoo-overlay` — remote
  `git@github.com:teknoman117/gentoo-overlay` (ssh). Package lives at
  `sys-apps/pacer/`.

## Steps

### 1. Choose the bump

Ask the user whether the changes are **major**, **minor**, or **patch** (use
AskUserQuestion). Read the current version from `pyproject.toml`
(`version = "X.Y.Z"`) and compute the new one per semver:

- major → `(X+1).0.0`
- minor → `X.(Y+1).0`
- patch → `X.Y.(Z+1)`

Call the result `NEW` (e.g. `0.2.0`) and the tag `vNEW`.

### 2. Bump the version in the sources

Update **every** Python location:

- `pyproject.toml` → `version = "NEW"`
- `src/pacer/__init__.py` → `__version__ = "NEW"`

Then confirm nothing was missed:
`grep -rn "OLD_VERSION" pyproject.toml src/` should return no matches (ignore
the generated `*.egg-info/` if present).

### 3. Verify (recommended)

If practical, run the test suite and abort the release on failure:
`pip install -e '.[test]' && pytest` (in a venv). Skip only if pytest can't be
installed.

### 4. Commit, tag, and push (source repo)

From `/home/nlewis/Projects/stream-tools/pacer`:

```
git commit -am "release vNEW"        # NO co-author / AI attribution
git tag vNEW
git push origin main
git push origin vNEW
```

### 5. Update the overlay ebuild + Manifest

From `/home/nlewis/Projects/gentoo-overlay` (its origin is ssh):

```
git pull --ff-only
cd sys-apps/pacer
cp pacer-9999.ebuild pacer-NEW.ebuild     # dual-mode: files are byte-identical
git rm pacer-<PREV>.ebuild                # drop the superseded release ebuild
                                          # (keep pacer-9999.ebuild)
```

Regenerate the Manifest (the tag must already be pushed — see Ground rules):

- Preferred: `pkgdev manifest`
- If `pkgdev` (dev-util/pkgdev) isn't installed: `ebuild pacer-NEW.ebuild manifest`.
  This needs a writable `DISTDIR`; if it fails with "No write access to
  /var/cache/distfiles", re-run with `DISTDIR=$(mktemp -d) ebuild
  pacer-NEW.ebuild manifest` (or run as a user in the `portage` group).

Then commit and push:

```
git add -A
git commit -m "sys-apps/pacer: bump to NEW"   # NO co-author / AI attribution
git push origin main
```

## Notes

- The versioned ebuild and `pacer-9999.ebuild` are intentionally byte-identical
  (the `PV == 9999` conditional switches behavior), so copying `pacer-9999.ebuild`
  is the canonical way to mint a new version. Keywords stay stable
  (`amd64 arm64 x86`) via that copy — no edit needed.
- If a release should NOT supersede the previous one (e.g. keep both installable),
  skip the `git rm pacer-<PREV>.ebuild` step; the Manifest will then carry DIST
  entries for both tarballs.
- This skill lives under `.claude/`, which pacer's `.gitignore` excludes. To keep
  it in version control, un-ignore `.claude/skills/`.
