# Third-party code in slopetanium-ui

| What | Where | From | Licence |
|---|---|---|---|
| ROCKNIXDS Pixel engine (`RndsRaster`, `RndsUI`): layout, boxes, text, the shelf and its animations | `rnds/` | [BananaLinux](https://github.com/JorreFog/BananaLinux) `ui/rnds` at e158027 (imported there from ROCKNIXDS) | MIT (`LICENSE.BananaLinux`) |
| banana-ui's software compositor (`soft.*`, `pool.h`) and JSON reader (`json.*`) | `banana/` | BananaLinux `ui/src` at e158027 | MIT (`LICENSE.BananaLinux`) |
| stb_image, stb_image_write | `stb/` | [nothings/stb](https://github.com/nothings/stb) via BananaLinux | public domain / MIT |

Local changes, each marked `Slopetanium:` in the code:

- `rnds/RndsUI.*`: host hooks on the Source (`hostOpen`, `hostFaces`, `hostFace`), touchable hint buttons, per-entry
  stat labels and sheet tint.
- `banana/kms.h`: only the `Damage` struct (the display is SDL in Slopetanium).

The look is Slopetanium's own theme over the engine: palette, accent, background and logo (the `theme` section of `src/main.cpp`).
No BananaOS artwork (the banana mark, the console icons) is used.

Unequal-panel support: the software compositor now includes the independent screen strides, damage bounds and active-panel geometry from `rzrbld/bananaos_duo` commit `dfaf28ba85a0fa68e29498eb0a26a25eb852875d` (the same MIT-licensed BananaLinux engine). `rnds/RndsUI.*` additionally uses per-panel widths for the sheet, cover/details columns, shelf, face buttons and matching touch regions. Slopetanium's existing host hooks, palette and artwork remain its own.
