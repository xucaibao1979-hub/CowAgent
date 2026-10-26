import unittest
import uuid
from pathlib import Path
from email.message import EmailMessage
from unittest.mock import patch

from plugins.intelligence_center.email_secretary import EmailSecretaryService


class FakeImap:
    def __init__(self,*args,**kwargs): pass
    def login(self,*args): return "OK",[]
    def select(self,*args): return "OK",[]
    def search(self,*args): return "OK",[b"1"]
    def fetch(self,*args):
        msg=EmailMessage(); msg["Subject"]="紧急：服务故障"; msg["From"]="ops@example.com"; msg.set_content("请尽快处理线上故障")
        return "OK",[(b"RFC822",msg.as_bytes())]
    def logout(self): pass


class EmailSecretaryTests(unittest.TestCase):
    def setUp(self):
        self.root=Path('work/test-data')/uuid.uuid4().hex; self.root.mkdir(parents=True); self.service=EmailSecretaryService(str(self.root),{"imap_host":"mail.local","username":"user@example.com","password":"secret"})
    def tearDown(self):
        for path in sorted(self.root.rglob('*'),reverse=True): path.unlink() if path.is_file() else path.rmdir()
        self.root.rmdir()
    def test_read_and_prioritize(self):
        task=self.service.save_task({"name":"邮箱摘要","max_messages":10})
        with patch("imaplib.IMAP4_SSL",FakeImap): result=self.service.run(task["id"])
        self.assertEqual(result["messages"][0]["priority"],"高"); self.assertIn("优先处理",result["report"])
    def test_missing_settings_is_explained(self):
        service=EmailSecretaryService(str(self.root),{})
        task=service.save_task({"name":"邮箱摘要"})
        with self.assertRaisesRegex(ValueError,"邮箱服务器"): service.run(task["id"])


if __name__ == "__main__": unittest.main()
