import os
import unittest
from unittest.mock import patch
import requests
from backend import config

class NotionCooldownTests(unittest.TestCase):
    def test_exhausted_429_defers_the_next_database_request(self):
        limited=requests.Response(); limited.status_code=429; limited.headers['Retry-After']='30'
        ok=requests.Response(); ok.status_code=200
        now=[100.0]; calls=[]
        def sleep(seconds):
            now[0]+=seconds
        def post(*args,**kwargs):
            calls.append(now[0]); return limited if len(calls)==1 else ok
        with patch.dict(os.environ,{'NOTION_TOOL_MAX_RETRIES':'0','NOTION_MIN_REQUEST_INTERVAL_SECONDS':'0'}), \
             patch.object(config.time,'monotonic',side_effect=lambda:now[0]), \
             patch.object(config,'interruptible_sleep',side_effect=sleep), \
             patch.object(config.requests,'post',side_effect=post), \
             patch.object(config,'_notion_next_request_at',0.0):
            config.notion_post_json('https://api.notion.test/one',{})
            config.notion_post_json('https://api.notion.test/two',{})
        self.assertGreaterEqual(calls[1]-calls[0],30)
