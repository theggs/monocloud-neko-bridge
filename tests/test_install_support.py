import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"deploy"))
import install_support

class InstallSupportTests(unittest.TestCase):
    def test_missing_checksum_is_explicit_and_asset_is_not_downloaded(self):
        class Opener:
            def __init__(self):self.names=[]
            def open(self,url,timeout):self.names.append(url);return io.BytesIO(b"\n")
        opener=Opener()
        with patch.object(install_support.urllib.request,"build_opener",return_value=opener):
            with self.assertRaisesRegex(ValueError,"checksum_entry_missing"):install_support.fetch_release("synthetic.tar.gz")
        self.assertEqual(len(opener.names),1)

    def test_concurrent_installers_cannot_share_journal(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)/"private"
            with install_support.InstallState(folder,"test"):
                with self.assertRaisesRegex(ValueError,"installation_locked"):
                    with install_support.InstallState(folder,"test"):pass

    def test_error_codes_never_include_private_or_remote_text(self):
        import urllib.error
        self.assertEqual(install_support.safe_error_code(ValueError("existing_installation_file_conflict")),"existing_installation_file_conflict")
        self.assertEqual(install_support.safe_error_code(ValueError("private path or token")),"ValueError")
        error=urllib.error.HTTPError("https://example.invalid",401,"secret body",{},io.BytesIO())
        try:self.assertEqual(install_support.safe_error_code(error),"http_401")
        finally:error.close()

    def test_atomic_publication_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"file";p.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):install_support.atomic_write(p,b"new",exclusive=True)
            self.assertEqual(p.read_bytes(),b"existing")
            self.assertEqual(list(Path(t).iterdir()),[p])

if __name__=="__main__":unittest.main()
