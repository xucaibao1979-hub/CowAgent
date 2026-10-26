import unittest
from unittest.mock import patch

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.channel_factory import create_channel
from channel.email.email_channel import EmailChannel


class FakeSmtp:
    last_message=None
    def __init__(self,*args,**kwargs): pass
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def login(self,*args): pass
    def send_message(self,message): FakeSmtp.last_message=message


class EmailChannelTests(unittest.TestCase):
    def setUp(self):
        self.config={"email_smtp_host":"smtp.example.com","email_smtp_port":465,"email_smtp_username":"sender@example.com","email_smtp_password":"secret","email_smtp_ssl":True,"email_default_recipient":"default@example.com","email_from_name":"CowAgent"}

    def test_factory(self): self.assertIsInstance(create_channel("email"),EmailChannel)

    def test_send_to_task_receiver(self):
        context=Context(ContextType.TEXT,"",{"receiver":"task@example.com","subject":"日报"})
        with patch("channel.email.email_channel.conf",return_value=self.config),patch("smtplib.SMTP_SSL",FakeSmtp):
            EmailChannel().send(Reply(ReplyType.TEXT,"报告内容"),context)
        self.assertEqual(FakeSmtp.last_message["To"],"task@example.com"); self.assertEqual(FakeSmtp.last_message["Subject"],"日报")

    def test_default_recipient(self):
        with patch("channel.email.email_channel.conf",return_value=self.config),patch("smtplib.SMTP_SSL",FakeSmtp):
            EmailChannel().send(Reply(ReplyType.TEXT,"报告"),Context(ContextType.TEXT,"",{}))
        self.assertEqual(FakeSmtp.last_message["To"],"default@example.com")

    def test_missing_config(self):
        with patch("channel.email.email_channel.conf",return_value={}):
            with self.assertRaisesRegex(ValueError,"SMTP"): EmailChannel().startup()


if __name__ == "__main__": unittest.main()
