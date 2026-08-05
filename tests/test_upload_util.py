"""Тесты upload_util: санитизация имён файлов."""

from upload_util import sanitize_name


def test_sanitize_name():
    assert sanitize_name("song.mp3") == "song.mp3"
    assert sanitize_name('bad:name*with?chars<>.mp3') == "bad_name_with_chars__.mp3"
    assert sanitize_name("Upper.MP3") == "Upper.mp3"
    assert sanitize_name("noext") == "noext"
    assert sanitize_name("   .wav") == "audio.wav"
    assert sanitize_name("") == "audio"
    # расширение сохраняется, длинные имена обрезаются
    name = sanitize_name("a" * 100 + ".ogg")
    assert name.endswith(".ogg")
    assert len(name) <= 80
