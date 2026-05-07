# UAssetGUI (bundled)

This directory contains a redistributed copy of [UAssetGUI](https://github.com/atenfyr/UAssetGUI)
by atenfyr, used by `BPShift` to extract Blueprint bytecode and
metadata from `.uasset` files.

- **Upstream**: <https://github.com/atenfyr/UAssetGUI>
- **License**: MIT (see [LICENSE](LICENSE) — Copyright (c) 2020-2026 atenfyr)
- **Bundled version / build date**: `UAssetGUI.exe` modified 2026-04-07
- **Engine target**: Tested with UE5_2 serialization. Other UE versions
  may work via the `--ue-version` flag passed to the `bpmigrate` CLI.

## Updating the bundled binary

1. Download the latest `UAssetGUI.exe` from the upstream
   [Releases page](https://github.com/atenfyr/UAssetGUI/releases).
2. Replace `UAssetGUI.exe` in this directory.
3. Pull the matching `LICENSE` from the upstream repository if it has
   changed.
4. Update the "Bundled version / build date" line above and commit.

## Why bundled?

The `BPShift` CLI invokes UAssetGUI in `tojson` mode. Bundling avoids
the friction of users having to download a separate tool, and pins a
known-working version against the rest of the toolchain. Since UAssetGUI
is MIT-licensed, redistribution requires only that the original copyright
and license text accompany the binary, which the [LICENSE](LICENSE) file
satisfies.
