# Official templates

These files are vendored from:

- https://github.com/denpadokei/UnityModdingTools.Templates.BeatSaber
- branch `denpadokei-dev`
- commit `71060a32f84aed4a37317f660dbfface3ab7dc9a`
- MIT License, Copyright (c) 2021 Zingabopp (see `LICENSE.txt`)

They are expanded by `bsmt_mcp.vstemplate`; the package does not depend on
Visual Studio. Project templates live in `bare/`, `core/`, `disableable/`;
item templates live in `items/` (`bsml-file`, `bsml-view`, `harmony`,
`monobehaviour`). To use a newer checkout of the template repository, pass
`templates_dir="C:\\path\\to\\UnityModdingTools.Templates.BeatSaber"` to
`create_project` / `add_item_template`, or set `BSMT_TEMPLATES_DIR`.
