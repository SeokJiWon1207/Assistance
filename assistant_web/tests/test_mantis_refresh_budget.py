"""비교 조회만 짧은 갱신 예산을 쓰며 원문·권한·취소 의미를 보존한다."""
import importlib
from unittest import TestCase
from unittest.mock import Mock,patch

import requests
from fastapi import HTTPException

from backend.sources import mantis


class MantisRefreshBudgetTests(TestCase):
    def scope(self,**kw):
        module=importlib.import_module('backend.sources.mantis_refresh')
        return module.short_refresh_scope(**kw)

    def settings(self):
        return patch.object(mantis,'get_mantis_settings',return_value={'url':'https://mantis.test/issues'})

    def test_reuses_current_request_rows_but_not_across_requests(self):
        response=Mock(status_code=200,json=lambda:{'issues':[{'id':7,'notes':[{'text':'원문'}]}]})
        with self.settings(),patch.object(mantis,'get_mantis_headers',return_value={}),patch.object(mantis.requests,'get',return_value=response) as get:
            with self.scope():
                a=mantis.fetch_mantis_issue_by_id(7)
                a['notes'][0]['text']='표시용 변경'
                self.assertEqual(mantis.fetch_mantis_issue_by_id(7)['notes'][0]['text'],'원문')
            with self.scope():mantis.fetch_mantis_issue_by_id(7)
        self.assertEqual(get.call_count,2)
        self.assertLessEqual(get.call_args.kwargs['timeout'],2)

    def test_slow_or_failed_refresh_does_not_retry_and_consumes_total_budget(self):
        refresh=importlib.import_module('backend.sources.mantis_refresh')
        clock=[0.0]
        def timeout(*args,**kw):
            clock[0]+=kw['timeout']
            raise requests.Timeout('slow')
        with self.settings(),patch.object(mantis,'get_mantis_headers',return_value={}),patch.object(mantis.requests,'get',side_effect=timeout) as get,patch.object(refresh,'monotonic',side_effect=lambda:clock[0]),patch.object(mantis,'interruptible_sleep',side_effect=AssertionError('재시도 불가')):
            with self.scope(total_seconds=3,request_seconds=2):
                for i in [1,2,3]:
                    with self.assertRaises(requests.Timeout):mantis.fetch_mantis_issue_by_id(i)
        self.assertEqual(get.call_count,2)
        self.assertEqual(clock[0],3)

    def test_access_denial_is_not_cached_as_missing_or_snapshot_success(self):
        response=Mock(status_code=403)
        with self.settings(),patch.object(mantis,'get_mantis_headers',return_value={}),patch.object(mantis.requests,'get',return_value=response):
            with self.scope():
                with self.assertRaises(HTTPException) as raised:mantis.fetch_mantis_issue_by_id(8)
        self.assertEqual(raised.exception.status_code,403)

    def test_normal_detail_fetch_retains_existing_retries(self):
        ok=Mock(status_code=200,json=lambda:{'issues':[{'id':8}]})
        with self.settings(),patch.object(mantis,'get_mantis_headers',return_value={}),patch.object(mantis.requests,'get',side_effect=[requests.Timeout,ok]) as get,patch.object(mantis,'interruptible_sleep'):
            self.assertEqual(mantis.fetch_mantis_issue_by_id(8),{'id':8})
        self.assertEqual(get.call_count,2)
        self.assertEqual(get.call_args.kwargs['timeout'],20)
