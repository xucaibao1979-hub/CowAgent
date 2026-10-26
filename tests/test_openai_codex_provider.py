import base64
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from models.bot_factory import create_bot
from models.openai_codex import auth
from models.openai_codex.codex_bot import OpenAICodexBot


def jwt(payload):
    encoded=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f"header.{encoded}.sig"


class FakeResponse:
    status_code=200
    text=""
    def iter_lines(self,decode_unicode=False):
        events=[{"type":"response.output_text.delta","delta":"你好"},{"type":"response.completed"}]
        return [f"data: {json.dumps(x,ensure_ascii=False)}".encode("utf-8") for x in events]+[b"data: [DONE]"]


class OpenAICodexProviderTests(unittest.TestCase):
    def setUp(self):
        self.root=Path('work/test-data/codex-provider');self.root.mkdir(parents=True,exist_ok=True);self.auth_path=self.root/'auth.json'
        auth._pending.clear()
    def tearDown(self):
        for path in sorted(self.root.rglob('*'),reverse=True): path.unlink() if path.is_file() else path.rmdir()
        self.root.rmdir()

    def test_factory(self): self.assertIsInstance(create_bot('openai-codex'),OpenAICodexBot)

    def test_device_login_and_token_storage(self):
        access=jwt({"exp":9999999999,"email":"user@example.com","https://api.openai.com/auth":{"chatgpt_account_id":"acct-1"}})
        replies=[(200,{"user_code":"ABCD-EFGH","device_auth_id":"device-1","interval":3}),(200,{"authorization_code":"code","code_verifier":"verifier"}),(200,{"access_token":access,"refresh_token":"refresh"})]
        with patch.object(auth,'_path',return_value=self.auth_path),patch.object(auth,'_request',side_effect=replies):
            started=auth.start_device_login();auth._pending['last_poll']=0;result=auth.poll_device_login()
        self.assertEqual(started['user_code'],'ABCD-EFGH');self.assertTrue(result['configured']);self.assertEqual(json.loads(self.auth_path.read_text())['account_id'],'acct-1')

    def test_codex_responses_are_adapted(self):
        credentials={"access_token":"token","account_id":"acct"}
        with patch('models.openai_codex.codex_bot.get_access',return_value=credentials),patch('requests.Session.post',return_value=FakeResponse()) as post:
            result=OpenAICodexBot().call_with_tools([{"role":"user","content":"你好"}],model='gpt-5.3-codex')
        self.assertEqual(result['choices'][0]['message']['content'],'你好');self.assertIn('ChatGPT-Account-Id',post.call_args.kwargs['headers'])


if __name__=='__main__':unittest.main()
