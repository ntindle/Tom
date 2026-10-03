# AutoGPT desktop builds

Experimental desktop builds of the [AutoGPT Platform](https://github.com/Significant-Gravitas/AutoGPT):
a Windows installer, a macOS disk image, and a Linux AppImage and `.deb`. No
Docker, no virtual machine, nothing to configure before the first start.

This repository is a parallel fork of `Significant-Gravitas/AutoGPT`. It is
not an official distribution.

## Branches

| Branch | What it holds |
| --- | --- |
| `desktop` | Upstream `dev` with the desktop distribution on top, in `autogpt_platform/desktop/`. This is where the code is. |
| `main` | This page and the automation that keeps `desktop` in step with upstream. Deliberately not a copy of upstream, so that none of upstream's scheduled workflows or dependency bots run here. |

Start with [`autogpt_platform/desktop/README.md`](https://github.com/ntindle/autogpt/blob/desktop/autogpt_platform/desktop/README.md)
on the `desktop` branch.
