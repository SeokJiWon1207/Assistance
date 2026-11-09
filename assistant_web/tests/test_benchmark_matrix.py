"""확대 평가 정답은 제품 매처가 아닌 원본 SQL에서 확정해야 한다."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.accuracy.benchmark_matrix import build_cases, read_oracle
from scripts.accuracy.benchmark import validate_case


class MatrixTests(unittest.TestCase):
    def test_oracle_uses_project_ids_open_states_and_actual_time_then_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'store.sqlite3'
            rows = [(1, 8, 'feedback', '2026-11-09T09:00:00+09:00'),
                    (2, 8, 'developed', '2026-11-09T00:00:00Z'),
                    (3, 8, 'new', '2026-11-09T01:00:00Z'),
                    (4, 8, 'resolved', '2026-11-09T02:00:00Z'),
                    (5, 16, 'new', '2026-11-09T02:00:00Z')]
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('create table issues (id integer primary key,payload text)')
                for identifier, project, status, created in rows:
                    raw={'id': identifier,'project': {'id':project},'status':{'name':status},
                         'created_at':created,'summary':f'이슈 {identifier}'}
                    db.execute('insert into issues values(?,?)',(identifier,json.dumps(raw)))
            oracle=read_oracle(path,8)
            self.assertEqual(oracle['count'],3)
            self.assertEqual(oracle['ids'],[3,2,1])
            self.assertEqual(oracle['groups'],{'신규 이슈':1,'개발완료':1,'정보 부족':1})
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("update issues set payload=json_set(payload,'$.status.name','unknown') where id=1")
            with self.assertRaises(ValueError):read_oracle(path,8)

    def test_three_customers_have_twelve_independently_scored_turns(self):
        profiles=[]
        for index,customer in enumerate(('한투','KB','NH')):
            profiles.append({'customer':customer,'key':str(index),'count':5,'ids':[index*10+n for n in (3,2,1)],
                'titles':{str(index*10+n):f'이슈 {n}' for n in (3,2,1)},'groups':{'확인된 이슈':5},
                'topic':'차트 설정','judgments':[], 'plan_customer': 'kb' if customer == 'KB' else customer})
        cases=build_cases(profiles)
        self.assertEqual(len(cases),6)
        self.assertEqual(sum(len(c['turns']) for c in cases),12)
        for index,case in enumerate(cases):validate_case(case,index+1)
        compound=cases[0]
        self.assertEqual(compound['turns'][1]['requirements'][0]['expect'][0]['ids'],[2])
        changed=compound['turns'][2]
        self.assertEqual(changed['expect'][1]['customer'],'kb')
        self.assertIn('KB', changed['question'])
        self.assertEqual(changed['expect'][1]['statuses'],['open'])
        self.assertEqual(changed['expect'][1]['issue_ids'],[])
        self.assertEqual(changed['requirements'][1]['expect'][0]['ids'],[13,12,11])

    def test_insufficient_or_duplicate_customer_samples_are_rejected(self):
        with self.assertRaises(ValueError):build_cases([])
