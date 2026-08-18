import asyncio
import zipfile
import pytest

import akita_zmodem_meshcore as azm
from akita_zmodem_meshcore import (
    DEFAULT_CONFIG,
    _safe_extract_zip,
    calculate_md5,
    load_config,
)


@pytest.mark.asyncio
async def test_calculate_md5_runs_in_thread(tmp_path):
    # create a small temp file and compute md5 via calculate_md5
    p = tmp_path / "small.bin"
    data = b"abc123" * 10
    p.write_bytes(data)

    # run calculate_md5 in a background thread via asyncio.to_thread and
    # ensure result matches
    md = await asyncio.to_thread(calculate_md5, str(p))

    # verify known md5
    import hashlib
    h = hashlib.md5()
    h.update(data)
    assert md == h.hexdigest()


def test_safe_extract_zip_rejects_zip_slip(tmp_path):
    # create a zip containing a malicious member that tries to escape
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(str(zip_path), 'w') as z:
        # add a file with ../ path
        z.writestr('../evil.txt', 'pwned')

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with pytest.raises(Exception):
        _safe_extract_zip(str(zip_path), str(out_dir))

    # also ensure normal zip extracts fine
    good_zip = tmp_path / "good.zip"
    with zipfile.ZipFile(str(good_zip), 'w') as z:
        z.writestr('folder/good.txt', 'ok')

    _safe_extract_zip(str(good_zip), str(out_dir))
    assert (out_dir / 'folder' / 'good.txt').exists()


def test_safe_extract_zip_rejects_oversized_archive(tmp_path):
    zip_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(str(zip_path), "w") as z:
        z.writestr("big.txt", "x" * 100)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(azm.UnsafeZipError):
        azm._safe_extract_zip(str(zip_path), str(out), max_total_bytes=10)


def test_load_config_preserves_invalid_json(tmp_path):
    cfg = tmp_path / "broken.json"
    original = "{not json at all"
    cfg.write_text(original)
    loaded = load_config(str(cfg))
    assert loaded["timeout"] == DEFAULT_CONFIG["timeout"]
    assert cfg.read_text() == original


def test_load_config_creates_missing_file(tmp_path):
    cfg = tmp_path / "new.json"
    loaded = load_config(str(cfg))
    assert cfg.exists()
    assert loaded["auto_receive"] is True
    assert loaded["max_file_size_bytes"] == DEFAULT_CONFIG["max_file_size_bytes"]
