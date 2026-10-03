from __future__ import annotations

import io
import os

import pytest

from nyanpasu.safe_files import open_regular_file


def test_reader_supports_descriptor_size_checks_and_streaming_text(tmp_path):
    root = tmp_path / "home"
    path = root / ".claude" / "projects" / "session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"message":"one"}\n{"message":"two"}\n')
    with open_regular_file(root, path.relative_to(root)) as stream:
        assert os.fstat(stream.fileno()).st_size == path.stat().st_size
        text = io.TextIOWrapper(stream, encoding="utf-8")
        assert next(text) == '{"message":"one"}\n'
        assert next(text) == '{"message":"two"}\n'
    assert stream.closed


@pytest.mark.parametrize("relative", ["/etc/passwd", "../private", "folder/../../private", "."])
def test_reader_rejects_escape_paths(tmp_path, relative):
    with pytest.raises(ValueError, match="relative"):
        with open_regular_file(tmp_path, relative):
            pytest.fail("escaped its root")


@pytest.mark.parametrize("position", ["root", "directory", "file"])
def test_reader_rejects_symlinks_at_every_component(tmp_path, position):
    private = tmp_path / "private"
    private.mkdir()
    (private / "note").write_text("secret")
    root = tmp_path / "root"
    if position == "root":
        root.symlink_to(private, target_is_directory=True)
        relative = "note"
    else:
        root.mkdir()
        if position == "directory":
            (root / "nested").symlink_to(private, target_is_directory=True)
            relative = "nested/note"
        else:
            (root / "note").symlink_to(private / "note")
            relative = "note"
    with pytest.raises(OSError):
        with open_regular_file(root, relative):
            pytest.fail("followed an agent symlink")


@pytest.mark.parametrize("file_kind", ["directory", "fifo"])
def test_reader_rejects_nonregular_files_without_blocking(tmp_path, file_kind):
    path = tmp_path / "input"
    if file_kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(ValueError, match="regular"):
        with open_regular_file(tmp_path, "input"):
            pytest.fail("accepted a nonregular file")


def test_directory_swap_cannot_redirect_a_later_lookup(tmp_path, monkeypatch):
    root = tmp_path / "root"
    folder = root / "nested"
    folder.mkdir(parents=True)
    (folder / "note").write_text("allowed")
    private = tmp_path / "private"
    private.mkdir()
    (private / "note").write_text("secret")
    original_open = os.open

    def swap_directory(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "nested":
            folder.rename(root / "original")
            folder.symlink_to(private, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(os, "open", swap_directory)
    with open_regular_file(root, "nested/note") as stream:
        assert stream.read() == b"allowed"
    assert (folder / "note").read_text() == "secret"


def test_final_file_swap_cannot_redirect_an_opened_stream(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "note"
    target.write_text("allowed")
    private = tmp_path / "private"
    private.write_text("secret")
    original_open = os.open

    def swap_file(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "note":
            target.unlink()
            target.symlink_to(private)
        return descriptor

    monkeypatch.setattr(os, "open", swap_file)
    with open_regular_file(root, "note") as stream:
        assert stream.read() == b"allowed"
    assert target.read_text() == "secret"


def test_file_changed_to_symlink_before_open_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "note"
    target.write_text("allowed")
    private = tmp_path / "private"
    private.write_text("secret")
    original_open = os.open

    def swap_before_open(path, flags, *args, **kwargs):
        if path == "note":
            target.unlink()
            target.symlink_to(private)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(OSError):
        with open_regular_file(root, "note"):
            pytest.fail("followed swapped symlink")
