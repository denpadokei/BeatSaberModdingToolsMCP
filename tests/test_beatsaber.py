from __future__ import annotations

from pathlib import Path

from bsmt_mcp import beatsaber


def make_fake_game(root: Path) -> Path:
    (root / "Beat Saber_Data" / "Managed").mkdir(parents=True)
    (root / "Libs").mkdir()
    (root / "Plugins").mkdir()
    (root / "Beat Saber.exe").write_bytes(b"")
    (root / "Beat Saber_Data" / "Managed" / "IPA.Loader.dll").write_bytes(b"MZ")
    (root / "Beat Saber_Data" / "Managed" / "Main.dll").write_bytes(b"MZ")
    (root / "Libs" / "Newtonsoft.Json.dll").write_bytes(b"MZ")
    (root / "Plugins" / "BSML.dll").write_bytes(b"MZ")
    return root


def test_is_beat_saber_dir(tmp_path) -> None:
    assert not beatsaber.is_beat_saber_dir(tmp_path)
    game = make_fake_game(tmp_path / "game")
    assert beatsaber.is_beat_saber_dir(game)


def test_parse_steam_library_folders() -> None:
    text = r'''
"libraryfolders"
{
    "0"
    {
        "path"        "C:\\Program Files (x86)\\Steam"
        "label"       ""
    }
    "1"
    {
        "path"        "D:\\SteamLibrary"
    }
}
'''
    paths = beatsaber.parse_steam_library_folders(text)
    assert paths == [r"C:\Program Files (x86)\Steam", r"D:\SteamLibrary"]


def test_version_from_version_file(tmp_path) -> None:
    game = make_fake_game(tmp_path / "game")
    (game / "BeatSaberVersion.txt").write_text("1.44.1_20239", encoding="utf-8")
    assert beatsaber.version_from_version_file(game) == ("1.44.1", "20239")


def test_version_from_globalgamemanagers(tmp_path) -> None:
    game = make_fake_game(tmp_path / "game")
    marker = b"public.app-category.games"
    data = b"prefix" + marker + b"\x00" * (136 - len(marker)) + b"1.44.1\x00rest"
    (game / "Beat Saber_Data" / "globalgamemanagers").write_bytes(data)
    assert beatsaber.version_from_globalgamemanagers(game) == "1.44.1"


def test_detect_explicit_install(tmp_path) -> None:
    game = make_fake_game(tmp_path / "game")
    installs = beatsaber.detect_installs([str(game)], include_steam=False, include_oculus=False, include_bsmanager=False)
    assert len(installs) == 1
    assert installs[0].source == "manual"
    assert installs[0].path == game.resolve()


def test_detect_deduplicates(tmp_path) -> None:
    game = make_fake_game(tmp_path / "game")
    installs = beatsaber.detect_installs(
        [str(game), str(game)],
        include_steam=False,
        include_oculus=False,
        include_bsmanager=False,
    )
    assert len(installs) == 1


def test_find_references(tmp_path) -> None:
    game = make_fake_game(tmp_path / "game")
    install = beatsaber.ensure_install(game)
    found, missing = beatsaber.find_references(install, ["IPA.Loader", "bsml", "Nope"])
    assert [ref.name for ref in found] == ["IPA.Loader", "BSML"]
    assert missing == ["nope"]
    assert found[1].relative_dir == "Plugins"


def test_available_references(tmp_path) -> None:
    game = make_fake_game(tmp_path / "game")
    install = beatsaber.ensure_install(game)
    references = beatsaber.available_references(install, include_versions=False)
    assert {ref.name for ref in references} == {"IPA.Loader", "Main", "Newtonsoft.Json", "BSML"}


def test_resolve_install_manifest_match(tmp_path) -> None:
    first = make_fake_game(tmp_path / "Game 1.44.1")
    second = make_fake_game(tmp_path / "Game 1.40.8")
    (first / "BeatSaberVersion.txt").write_text("1.44.1_20239", encoding="utf-8")
    (second / "BeatSaberVersion.txt").write_text("1.40.8_7379", encoding="utf-8")
    installs = beatsaber.detect_installs(
        [str(first), str(second)],
        include_steam=False,
        include_oculus=False,
        include_bsmanager=False,
    )
    chosen, candidates, reason = beatsaber.resolve_install(manifest_game_version="1.40.8", installs=installs)
    assert chosen.path == second.resolve()
    assert reason.startswith("manifest-game-version")
    assert len(candidates) == 2
