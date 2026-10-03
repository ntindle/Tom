# Changes offered upstream

The fork changes nothing outside `autogpt_platform/desktop/` except its one
caller workflow (`runtime/tests/test_fork_surface.py` holds it to that). Where
the desktop needs upstream's code to behave differently, the desktop works
around it from its own code, and the change upstream should take is kept here
as a patch against Significant-Gravitas/AutoGPT `dev`.

When upstream has merged a patch, delete it and the workaround it names.

| Patch | What it fixes | Workaround to delete once merged |
| --- | --- | --- |
| `runtime-config-windows.patch` | `single-container/runtime_config.py` calls `os.fchmod` and fsyncs a directory, neither of which Windows can do | `WindowsOs` in `runtime/autogpt_desktop/settings.py`, and its tests in `runtime/tests/test_fork_surface.py` |

To open the pull request:

```
git fetch origin dev                      # origin = Significant-Gravitas/AutoGPT
git switch -c fix/runtime-config-windows origin/dev
git show desktop:autogpt_platform/desktop/upstream/runtime-config-windows.patch | git apply
```

The first lines of each patch are its commit message.

`runtime/tests/test_fork_surface.py` checks on every run whether the patch still
applies to upstream's files, and says so when upstream has taken the fix. To
make a patch again after upstream changed the lines around it: apply the old
one by hand on a branch of upstream's `dev`, then

```
git diff origin/dev -- autogpt_platform/single-container > new.patch
```

and put the commit message back above the `---` line. `.gitattributes` here
keeps the patches' line endings LF on Windows; `git apply` refuses CRLF.
