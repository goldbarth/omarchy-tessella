# Scenula release and migration notes

## Lifecycle findings

Checked against the installed Omarchy plugin commands and `shell/services/PluginRegistry.qml` on 2026-10-30.

- `omarchy-plugin-add` clones to a staging directory, validates, reads the manifest ID, rejects that ID if already installed, then moves the clone into `~/.config/omarchy/plugins/<id>`.
- `omarchy-plugin-update` fetches `origin HEAD`, fast-forwards in place, validates, and rescans the shell.
  It has no install/update migration hook and does not rename checkouts or rewrite `shell.json` for a changed ID.
- The registry scans each installed directory's root `manifest.json` and keys plugins by manifest ID.
  The directory name does not have to match the ID for discovery.
  Canonical widget aliases are host-defined; there is no third-party rename declaration that carries forward enabled state.
- Third-party enabled state comes from exact ID references in `bar.layout`, `plugins`, and `disabledPlugins`.
  Consequently a root manifest ID change prevents the new service from loading under an old bar entry.
  A migration implemented only in service startup cannot repair this.
- Update/removal commands locate checkouts by directory name.
  The checkout must move together with the shell references to support subsequent updates/removal using the new ID.

The release therefore needs the explicit one-time migration sequence in [README.md](README.md#upgrading-from-tessella).
It does **not** provide a zero-intervention upgrade for users who only run an ordinary Omarchy update.
Existing users must run the documented migration command once.

## Storage strategy

Fresh installs create native Scenula directories.
Migrated installs create new Scenula symlinks to the existing Tessella config, data and state directories.
Legacy directories are the active backing storage and are never deleted automatically.
No library schema, tag version, swatch version, download URL or selection algorithm changes accompany this rename.
Absolute library and state paths, the active background symlink, and in-flight render/sync locks remain valid.
The migration does not perform any image processing or network requests.
Normal daily sync and rotation retain their existing behavior after startup.

The shell config receives a byte-for-byte backup before changing identity fields, and updates atomically with its original permissions.
A symlinked shell config remains a symlink.
Conflicting independent data directories or installed checkouts are refused rather than merged, deleted or hidden.
An interrupted migration can be rerun from whichever checkout directory currently exists.
Old IPC targets route to the same handler code; no second plugin or service is installed.
Absolute paths in external scripts to the old plugin checkout require manual adjustment.

## Publication steps

1. Review and commit this change locally.
   Rename the existing GitHub repository to `goldbarth/omarchy-scenula` through Repository Settings.
   Keep its history; do not create a replacement repository.
2. Update the maintainer's local remote with `git remote set-url origin https://github.com/goldbarth/omarchy-scenula.git` and publish the reviewed commit.
   Confirm both the old and new URLs can fetch the same release before announcing it.
   Never reuse `goldbarth/omarchy-tessella`, because doing so removes GitHub's old-name redirects.
3. Coordinate the existing [marketplace listing](https://github.com/omacom/omarchy-plugin-marketplace/issues/12751) with its maintainers.
   Request a rename of the existing listing/source identity, repository URL, installation command, preview and reviewed snapshot, preserving the existing listing rather than publishing a duplicate.
   The marketplace's [normal update workflow](https://github.com/omacom/omarchy-plugin-marketplace/blob/main/VERIFICATION.md#promoting-a-plugin-update) requires the exact configured plugin ID set to remain unchanged, so it cannot promote this ID change as an ordinary update.
   Maintainer handling and fresh review of the exact published commit are required.
4. Announce the one-time upgrade commands from the README wherever existing users receive release notices.
   Explain the temporary missing-widget interval between update and migration, and the recovery command for users who already updated.
5. After publication, test installation from the renamed GitHub URL and an update through an old GitHub remote.
   GitHub-side redirect behavior cannot be exercised against this unpublished local change.

GitHub documents continued clone/fetch/push support for renamed repositories in its [rename guide](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository).
No GitHub repository, marketplace listing, installed plugin or real user data is changed by preparing this local release.

## Validation

```bash
python3 -m unittest discover -s tests -v
SCENULA_INTEGRATION=1 python3 -m unittest discover -s tests -v
SCENULA_INTEGRATION=1 SCENULA_QPA_PLATFORM=wayland python3 -m unittest discover -s tests -v
omarchy plugin validate .
git diff --check
```

Integration tests require the installed Omarchy tools, Quickshell and ImageMagick.
They run with a temporary HOME and isolated Quickshell instance using the installed registry and UI types.
The default offscreen backend verifies service/widget loading and IPC; a Wayland run also verifies the real panel can instantiate, with panels kept closed.
Git installation/update, manifest discovery, widget/service loading, IPC and ImageMagick rendering run for real.
Only monitor detection and the final wallpaper setter are replaced, so they cannot change the real desktop wallpaper.
The upgrade fixture starts with legacy identity/configuration and populated images, metadata, collages and state.
The tests reproduce the changed-ID discovery gap, then verify migration, legacy/new IPC, automatic rotation, restart persistence and subsequent updates.
Offline regressions cover idempotency, concurrent initialization, conflict refusal, malformed configuration, shell config symlinks, lock sharing, cancellation, remote preservation and data integrity.
Visible compositor placement and live GitHub redirects still need the post-publication checks above.

## Results on 2026-10-30

Validated on Omarchy 4.0.6-1 and Quickshell 0.3.2.r7.g2b4501a-1.
All 18 tests passed with the Wayland backend, including real panel instantiation with closed windows.
Manifest validation, Python compilation, SVG rendering and `git diff --check` passed.
A separate metadata snapshot of the existing user library reported 2,267 usable tagged images, 49 themes and zero pending color analyses; all copied JSON bytes remained unchanged after Scenula status inspection.
The real installation and its backing data were not migrated during validation.
