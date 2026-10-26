import unittest
from unittest.mock import patch, MagicMock

import requests
from models.availability import probe_model


class AvailabilityTests(unittest.TestCase):
    meta = {"openai": {"api_key_field": "key", "api_base_default": "https://api.openai.com/v1"}}

    def _response(self, status, data=None, text=""):
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = None
        response.status_code = status
        response.text = text
        response.json.return_value = data or {}
        return response

    def test_missing_key_never_connects(self):
        with patch('models.availability.requests.Session') as session:
            self.assertEqual(probe_model({}, self.meta, 'openai', 'gpt-test')['code'], 'missing_key')
            session.assert_not_called()

    def test_codex_is_not_api_login(self):
        with patch('models.openai_codex.auth.auth_status', return_value={'state': 'signed_out'}):
            self.assertEqual(probe_model({'key': 'secret'}, self.meta, 'openai-codex', 'test')['code'], 'codex_login_required')

    def test_reply_and_redacted_failure(self):
        with patch('models.availability.requests.Session') as factory:
            session = factory.return_value.__enter__.return_value
            session.get.return_value = self._response(200, {'id': 'test'})
            response = self._response(200, {'output_text': 'OK'})
            session.post.return_value = response
            config = {'key': 'private-test-key'}
            before = dict(config)
            self.assertTrue(probe_model(config, self.meta, 'openai', 'test')['available'])
            args = session.post.call_args.kwargs
            self.assertFalse(args['allow_redirects'])
            self.assertEqual(args['json']['input'], 'Reply only OK.')
            self.assertEqual(config, before)
            session.post.return_value = self._response(401, text='private-test-key')
            session.get.return_value = self._response(401, text='private-test-key')
            result = probe_model(config, self.meta, 'openai', 'test')
            self.assertEqual(result['code'], 'authentication_failed')
            self.assertNotIn('private-test-key', str(result))
            self.assertTrue(session.get.call_args.args[0].endswith('/models/test'))

    def test_openai_falls_back_to_chat_completion_for_compatible_proxies(self):
        with patch('models.availability.requests.Session') as factory:
            session = factory.return_value.__enter__.return_value
            session.get.return_value = self._response(200, {'id': 'test'})
            session.post.side_effect = [
                self._response(404),
                self._response(200, {'choices': [{'message': {'content': 'OK'}}]}),
            ]
            result = probe_model({'key': 'x'}, self.meta, 'openai', 'test')
            self.assertTrue(result['available'])
            self.assertTrue(session.post.call_args_list[0].args[0].endswith('/responses'))
            self.assertTrue(session.post.call_args_list[1].args[0].endswith('/chat/completions'))

    def test_empty_and_timeout(self):
        with patch('models.availability.requests.Session') as factory:
            session = factory.return_value.__enter__.return_value
            session.get.return_value = self._response(200, {'id': 'test'})
            response = self._response(200, {})
            session.post.return_value = response
            self.assertEqual(probe_model({'key': 'x'}, self.meta, 'openai', 'test')['code'], 'empty_reply')
            session.post.side_effect = requests.exceptions.Timeout('secret')
            self.assertEqual(probe_model({'key': 'x'}, self.meta, 'openai', 'test')['code'], 'timeout')


if __name__ == '__main__':
    unittest.main()
