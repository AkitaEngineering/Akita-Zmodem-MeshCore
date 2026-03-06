import asyncio
import zipfile
import pytest

from akita_zmodem_meshcore import _safe_extract_zip, calculate_md5


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
