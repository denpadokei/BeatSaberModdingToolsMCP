# Beat Saber Modding Tools MCP

An MCP server and CLI that replace the **BeatSaberModdingTools** Visual Studio extension and
**BeatSaberModdingTools.Tasks** MSBuild tasks when you build Beat Saber PC mods with an AI agent
instead of Visual Studio.

The server detects the game, fixes `BeatSaberDir`, manages game references, validates and embeds
`manifest.json`, runs MSBuild/dotnet, creates BeatMods-style release zips, and deploys the plugin to
`Plugins` (or `IPA/Pending` while the game is running).

- Python 3.10+
- Windows (game detection, BSIPA deployment)
- No Visual Studio required: use **Visual Studio Build Tools** or the **.NET SDK** (`dotnet`)
- Works with existing BSIPA projects that use `BeatSaberModdingTools.Tasks`

```text
uvx --from git+https://github.com/denpadokei/BeatSaberModdingToolsMCP bsmt-mcp
```

---

## Quick start (MCP client)

Add the server to your MCP client configuration. The exact wrapper key depends on the client
(`mcpServers` for Claude Desktop/Codex-style clients, `context_servers`, etc.); the command is
always the same.

```json
{
  "mcpServers": {
    "beat-saber-modding-tools": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/denpadokei/BeatSaberModdingToolsMCP",
        "bsmt-mcp"
      ]
    }
  }
}
```

The first launch clones the repository, builds the wheel, and caches it. Later launches are fast.

### `uvx run git+<url>`

`uvx` takes the package with `--from` and the command as the following argument, so the literal
form `uvx run git+<url>` would ask uvx for a PyPI package named `run`. To keep the command shape
short, this package installs a `run` alias equivalent to `bsmt-mcp`:

```powershell
uvx --from git+https://github.com/denpadokei/BeatSaberModdingToolsMCP run
uvx --from git+https://github.com/denpadokei/BeatSaberModdingToolsMCP bsmt-mcp   # same server
```

For a local checkout:

```powershell
uvx --from "git+file:///C:/path/to/BeatSaberModdingToolsMCP" bsmt-mcp
```

---

## CLI quick start

The same code is available as the `bsmt` command, which is handy from a terminal or from an agent
that can run commands but not MCP.

```powershell
# 1. Find the game
uvx --from git+https://github.com/denpadokei/BeatSaberModdingToolsMCP bsmt detect

# 2. Inspect a project
uvx --from git+https://github.com/denpadokei/BeatSaberModdingToolsMCP bsmt inspect .\MyMod

# 3. Point the project at a game install
uvx --from git+https://github.com/denpadokei/BeatSaberModdingToolsMCP bsmt setup .\MyMod --target user

# 4. Add references (presets: core, bsipa, bsml, sira, harmony, newtonsoft, zenject)
uvx --from git+https://github.com/denpadokei/BeatSaberModdingToolsMCP bsmt refs add .\MyMod --preset core --preset bsipa

# 5. Build, zip and deploy
uvx --from git+https://github.com/denpadokei/BeatSaberModdingToolsMCP bsmt build .\MyMod -c Release --package --deploy

# 6. Scaffold a new project
uvx --from git+https://github.com/denpadokei/BeatSaberModdingToolsMCP bsmt create .\NewMod --name "New Mod"
```

After the first `uvx` run you can also install it once:

```powershell
uv tool install git+https://github.com/denpadokei/BeatSaberModdingToolsMCP
bsmt doctor
```

---

## What it replaces

| BeatSaberModdingTools feature | MCP tool / CLI |
|---|---|
| Beat Saber install detection (Steam, Oculus, BS Manager) | `detect_game` / `bsmt detect` |
| Set `BeatSaberDir` / `GameDirectory` | `set_game_directory` / `bsmt setup` |
| Beat Saber Reference Manager (add/remove DLLs) | `list_game_references`, `add_game_references`, `remove_game_references` |
| Add Reference Paths (`ReferencePath`) | `set_game_directory(set_reference_path=true)` |
| Reference copies for offline/CI builds | `copy_game_references` |
| Project templates | `create_project` |
| `GenerateManifest` | `generate_manifest` |
| Version bumps (manifest + assembly together) | `set_version` |
| `GetManifestInfo` / manifest validation | `read_manifest`, `validate_manifest` |
| `GetAssemblyInfo` / `CompareVersions` | `get_assembly_info`, `compare_versions` |
| `GetCommitInfo` | `get_git_info` |
| `ZipDir` / BeatMods release zip | `package_mod` |
| `BSMT_CopyToPlugins` / `IPA/Pending` | `deploy_mod` |
| `ReplaceInFile` | `replace_in_file` |
| `SetActionOutput` | `set_github_output` |
| Build without Visual Studio | `build_mod` |

The behavior is modeled on:

- [Zingabopp/BeatSaberModdingTools](https://github.com/Zingabopp/BeatSaberModdingTools)
- [Zingabopp/BeatSaberModdingTools.Tasks](https://github.com/Zingabopp/BeatSaberModdingTools.Tasks)
- [denpadokei/BeatSaberModdingTools](https://github.com/denpadokei/BeatSaberModdingTools) (fork with
  fixes; the detection code follows the fixed behavior)

### Build behavior

- **SDK-style projects** build with `dotnet build` when available, otherwise MSBuild.
- **Legacy (non-SDK) projects** build with `MSBuild.exe` from Visual Studio or Build Tools.
- When `BeatSaberModdingTools.Tasks` is present, the build passes
  `DisableCopyToGame=true`, `DisableCopyToPlugins=true` and `DisableZipRelease=true` so the package
  does not duplicate the server's packaging/deploy. Manifest generation by the package still works.
- `package_mod` honors `OutputCopy` items and otherwise copies `Plugins/<AssemblyName>.dll` (+ PDB).
  The zip contains `Plugins/...` at its root, exactly like `BSMT_ZipRelease`. Pass
  `include_manifest=true` if your distribution needs a root `manifest.json` too.
- `deploy_mod` writes to `Plugins` normally and to `IPA/Pending` while `Beat Saber.exe` is running,
  matching BSIPA's pending-file mechanism.

---

## MCP tools

Read-only tools: `detect_game`, `build_environment`, `game_process_status`,
`inspect_project`, `list_game_references`, `read_manifest`, `validate_manifest`,
`get_assembly_info`, `compare_versions`, `get_git_info`.

Mutating tools: `add_game_references`, `remove_game_references`, `set_game_directory`,
`copy_game_references`, `create_project`, `generate_manifest`, `set_version`, `build_mod`,
`package_mod`, `deploy_mod`, `replace_in_file`, `set_github_output`.

### Recommended workflow

1. `detect_game` — choose the install (the newest or the BS Manager last-launched instance).
2. `inspect_project` — see whether references, `BeatSaberDir`, and the manifest are sane.
3. `set_game_directory` — write `BeatSaberDir`/`GameDirectory` if the project does not resolve them.
4. `add_game_references` — add missing DLLs; start with `presets=["core","bsipa"]`.
5. `generate_manifest` — merge fields, validate, and embed the manifest. Use `set_version` when
   bumping the version so the manifest and `AssemblyInfo.cs` stay in sync.
6. `build_mod` with `package=true` and `deploy=true`.

Agents should check `ok` in the returned JSON. `build_mod` returns `ok=false` with parsed
`diagnostics` instead of hiding compiler errors.

---

## Requirements and setup

- **Windows 10/11** for game detection and deployment.
- **MSBuild** from Visual Studio Build Tools/VS (legacy projects), or the **.NET SDK** (SDK-style
  projects). `bsmt doctor` reports what was found.
- For `net472` projects, install the **.NET Framework 4.7.2 targeting pack** (included in the
  .NET desktop development workload).
- NuGet restore needs network access on the first build, unless the packages are already in the
  global NuGet cache.

No Visual Studio IDE is required. Build Tools are enough for legacy projects.

---

## How the tricky parts are handled

### `BeatSaberDir` and `.csproj.user`

MSBuild imports `<project>.csproj.user` through `Microsoft.Common.props`, so the extension's
`BeatSaberDir` convention keeps working from the command line. `set_game_directory` updates any
existing definition first (project, `.csproj.user`, nearest `Directory.Build.props`) and creates a
`.csproj.user` only when nothing defines it.

### Manifest embedding

BSIPA reads `<AssemblyName>.manifest.json` from the assembly's embedded resources. Projects using
`BeatSaberModdingTools.Tasks` either embed the source `manifest.json` (`RootNamespace` must equal
`AssemblyName`, the usual case) or let the package generate it. When the package is absent,
`generate_manifest(embed=true)` adds:

```xml
<ItemGroup>
  <EmbeddedResource Include="manifest.json">
    <LogicalName>$(AssemblyName).manifest.json</LogicalName>
  </EmbeddedResource>
</ItemGroup>
```

It deliberately skips this when `ImportBSMTTargets` is enabled, avoiding duplicate resource names
(`CS1508`).

### Offline references

`copy_game_references` mirrors `Beat Saber_Data/Managed`, `Libs`, and `Plugins` into a local
`Refs` folder (default: the parent of the project folder) and can point `BeatSaberDir` there. The
usual `$(BeatSaberDir)\Beat Saber_Data\Managed\Main.dll` HintPaths then resolve without the game
installed, which is useful for CI.

---

## CLI reference

```text
bsmt detect [--game PATH] [PATH ...]
bsmt doctor
bsmt inspect PROJECT [-c Debug]
bsmt refs list [PROJECT] [--game PATH] [--filter NAME] [--missing]
bsmt refs add PROJECT [--name DLL] [--preset core] [--game PATH] [--dry-run]
bsmt refs remove PROJECT --name DLL [--keep-non-game] [--dry-run]
bsmt refs copy PROJECT [--destination DIR] [--game PATH] [--no-set-dir]
bsmt setup PROJECT [--game PATH] [--target auto|user|project|props] [--reference-path]
bsmt manifest show PROJECT
bsmt manifest validate PROJECT
bsmt manifest generate PROJECT [--version 1.2.3] [--game-version 1.44.1] [--depends-on BSIPA=^4.3.0]
bsmt set-version PROJECT VERSION [--no-manifest] [--no-assembly] [--dry-run]
bsmt assembly PROJECT
bsmt compare MANIFEST_VERSION ASSEMBLY_VERSION
bsmt git PROJECT
bsmt build PROJECT [-c Release] [--package] [--deploy] [--property K=V]
bsmt package PROJECT [-c Release] [--extra SOURCE=DEST] [--include-manifest]
bsmt deploy PROJECT [-c Release] [--game PATH]
bsmt create PATH [--name NAME] [--id ID] [--author AUTHOR]
bsmt replace FILE PATTERN SUBSTITUTE [--regex]
bsmt github-output NAME VALUE [--path FILE]
bsmt mcp
```

All commands print JSON and exit non-zero when the operation reports `ok: false`.

---

## Troubleshooting

**`uvx` cannot acquire a lock / access denied (sandboxed environments).**
Set `UV_CACHE_DIR` to a writable directory:

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
uvx --from git+https://github.com/denpadokei/BeatSaberModdingToolsMCP bsmt doctor
```

**MSBuild.exe not found.**
Install Visual Studio Build Tools with the *.NET desktop development* workload, or use an
SDK-style project and the .NET SDK (`--prefer dotnet`).

**Game not detected.**
Pass `--game "D:\...\Beat Saber"` or set `BEATSABER_DIR`. BS Manager installs are auto-detected
from `%APPDATA%\bs-manager\config.json`.

**`CS0006` / missing `$(BeatSaberDir)` DLLs.**
Run `bsmt setup . --target user`, then `bsmt refs add . --preset core --preset bsipa`.

**Manifest version mismatch on Release.**
`bsmt compare <manifest version> <assembly version>` shows the BSMT rules (prerelease labels are
ignored; `1.2.3` matches `1.2.3.0`).

**NU1301 / NuGet TLS errors.**
Restore needs access to nuget.org (or a local cache/mirror). The build itself works offline once
the packages are cached.

---

## 日本語

Beat Saber の PC Mod（BSIPA）を **Visual Studio を使わずに AI エージェントでビルド**するための
MCP サーバー兼 CLI です。`BeatSaberModdingTools` 拡張機能と `BeatSaberModdingTools.Tasks` の
主要機能を Python で再実装しています。

- ゲームの検出（Steam / Oculus / BS Manager）、`BeatSaberDir` の自動設定
- 参照 DLL の追加・削除、ローカル `Refs` へのコピー（オフラインビルド用）
- `manifest.json` の生成・検証・アセンブリへの埋め込み
- MSBuild / dotnet でのビルド、BeatMods 形式 zip の作成、`Plugins` / `IPA/Pending` への配置
- `AssemblyInfo` のバージョン比較、git 情報、`ReplaceInFile` 相当

```json
{
  "mcpServers": {
    "beat-saber-modding-tools": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/denpadokei/BeatSaberModdingToolsMCP", "bsmt-mcp"]
    }
  }
}
```

`uvx` はパッケージ指定に `--from` が必要なので、`run` エイリアスも同梱しています。

```powershell
uvx --from git+https://github.com/denpadokei/BeatSaberModdingToolsMCP run
```

最初に `detect_game` → `inspect_project` → `set_game_directory` → `add_game_references` →
`generate_manifest` → `build_mod(package=true, deploy=true)` の順に呼ぶのがおすすめです。
`build_mod` は失敗時も `ok=false` とコンパイラ診断を返します。

Visual Studio 本体は不要ですが、レガシー csproj のビルドには **Build Tools の MSBuild**、
SDK スタイルなら **.NET SDK** が必要です。`bsmt doctor` で検出状況を確認できます。

---

## Development

```powershell
git clone https://github.com/denpadokei/BeatSaberModdingToolsMCP
cd BeatSaberModdingToolsMCP
uv venv
# Non-editable install: editable .pth files break on Windows paths containing
# non-ASCII characters (Python reads .pth with the ANSI code page).
uv pip install --group dev .
uv run pytest
uv run ruff check
```

Layout:

```text
src/bsmt_mcp/
  beatsaber.py    game detection, version parsing, reference inventory
  project.py      csproj inspection/editing, BeatSaberDir, reference copies
  manifest.py     manifest merge and schema validation
  assembly.py     AssemblyInfo/version comparison
  buildtools.py   MSBuild/dotnet, artifact, zip, deploy
  gitinfo.py      GetCommitInfo equivalent
  templates.py    project scaffolding
  ops.py          shared operations for MCP + CLI
  server.py       MCP tool definitions
  cli.py          bsmt CLI
```

## License

MIT. See [LICENSE](LICENSE). BeatSaberModdingTools and BeatSaberModdingTools.Tasks are MIT/
Unlicense projects by Zingabopp; this is an independent reimplementation and is not affiliated
with Beat Games, BSMG, or the original authors.
