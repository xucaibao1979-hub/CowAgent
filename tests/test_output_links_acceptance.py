import unittest
from plugins.intelligence_center.output_formatting import format_automation_output


class OutputLinkTests(unittest.TestCase):
    def test_url_heading_does_not_nest(self):
        value = '### [https://example.com](https://example.com)\n\n状态：首次建立基线'
        expected = '### [链接](https://example.com)\n\n状态：首次建立基线'
        self.assertEqual(expected, format_automation_output(value))
        self.assertEqual(expected, format_automation_output(expected))

    def test_existing_link_preserved(self):
        value = '新闻 [链接](https://example.com/a?q=1)\n正文'
        self.assertEqual(value, format_automation_output(value))

    def test_bare_url_converted(self):
        self.assertEqual('[链接](https://example.com)', format_automation_output('https://example.com'))
