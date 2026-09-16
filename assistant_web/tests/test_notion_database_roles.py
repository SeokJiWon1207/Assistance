"""운영 DB의 속성 차이와 하위 문서 범위에 대한 회귀 검사."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from backend.notion import client, database_roles, mapping, read_scope, schema, search
from backend.notion.formatting import format_deterministic_notion_answer
from backend.query_planning import build_query_plan, validate_query_plan
from backend.intent.constraints import extract_query_constraints, merge_query_plan_constraints
from scripts.accuracy import rebaseline_notion_titles


def people(*names):
    return {"type": "people", "people": [{"id": f"user-{i}", "name": name} for i, name in enumerate(names)]}


def page(properties):
    return mapping.normalize_notion_page({"id": "p", "properties": {
        "제목": {"type": "title", "title": [{"plain_text": "배포 점검"}]}, **properties,
    }}, include_blocks=False)


def response(data, status=200):
    result = Mock(status_code=status)
    result.json.return_value = data
    return result


class DatabaseRoleTests(unittest.TestCase):
    def test_condition_only_issue_query_uses_issue_and_unknown_databases(self):
        known_issue, known_document, unknown = "a" * 32, "b" * 32, "c" * 32
        catalog = {
            known_issue: {"roles": ["issue"], "source": "configured"},
            known_document: {"roles": ["document"], "source": "configured"},
        }
        constraints = {"common_filter_clauses": [{
            "field": "created_at", "operator": "between", "value": ["2026-09-14", "2026-09-20"],
        }]}
        with patch.object(database_roles, "load_notion_database_roles", return_value=catalog):
            selected, role = database_roles.select_notion_database_ids(
                [known_issue, known_document, unknown],
                "이번주 등록된 이슈 알려줘", [], constraints,
            )
            all_for_keyword, keyword_role = database_roles.select_notion_database_ids(
                [known_issue, known_document, unknown],
                "도커 관련 이슈 찾아줘", ["도커"], constraints,
            )
        self.assertEqual(selected, [known_issue, unknown])
        self.assertEqual(role, "issue")
        self.assertEqual(all_for_keyword, [known_issue, known_document, unknown])
        self.assertEqual(keyword_role, "")

        with patch.object(database_roles, "load_notion_database_roles", return_value={
            known_issue: {"roles": ["issue"], "source": "inferred"},
            known_document: {"roles": ["document"], "source": "inferred"},
        }):
            inferred, inferred_role = database_roles.select_notion_database_ids(
                [known_issue, known_document], "이번주 등록된 이슈 알려줘", [], constraints,
            )
        self.assertEqual(inferred, [known_issue, known_document])
        self.assertEqual(inferred_role, "")

    def test_role_catalog_is_replaced_atomically_only_when_complete(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            database_roles, "RUNTIME_DIR", Path(directory),
        ):
            catalog = database_roles.build_notion_database_role_catalog(
                [("a" * 32, "이슈리스트", {
                    "이슈할당": {"type": "people"}, "등록일": {"type": "date"},
                    "고객사": {"type": "select"}, "매체": {"type": "select"},
                })],
                generated_at="2026-09-15T12:00:00+09:00",
                root_database_ids=["a" * 32],
            )
            path = database_roles.save_notion_database_role_catalog(catalog)
            self.assertEqual(json.loads(path.read_text())["databases"]["a" * 32]["roles"], ["issue"])
            with self.assertRaises(ValueError):
                database_roles.save_notion_database_role_catalog({"complete": False, "databases": {}})
    def test_issue_assignment_ignores_previous_team_and_people(self):
        item = page({"직전담당파트": {"type": "rich_text", "rich_text": [{"plain_text": "QC"}]},
                     "직전할당": people("김지원"), "이슈할당": people("김지은")})
        self.assertEqual(item['owner'], '김지은')
        self.assertTrue(search.notion_page_matches_constraints(item, {'owner': '김지은'}))
        self.assertFalse(search.notion_page_matches_constraints(item, {'owner': '김지원'}))
        self.assertEqual(item['owner_role'], '담당자')

    def test_team_rollup_is_not_a_person_and_contributor_role_is_preserved(self):
        for field in ('공동 작업자', '공헌', 'Contributors'):
            item = page({'담당부서': {'type': 'rollup', 'rollup': {'type': 'array', 'array': []}}, field: people('김지은')})
            self.assertEqual(item['owner'], '김지은')
            self.assertEqual(item['owner_role'], '기여자')
            self.assertEqual(item['owner_relation_ids'], [])
        item = page({'담당팀': {'type': 'relation', 'relation': [{'id': 'team-id'}]}})
        self.assertEqual(item['owner'], '담당자 미지정')
        self.assertEqual(item['owner_relation_ids'], [])

    def test_empty_assignee_is_not_replaced_with_contributor(self):
        item = page({'주담당자(입력)': people(), 'Contributors': people('김지은')})
        self.assertEqual(item['owner'], '담당자 미지정')
        self.assertEqual(item['owner_role'], '담당자')

    def test_due_date_uses_end_and_deployment_is_separate(self):
        item = page({'등록일': {'type': 'date', 'date': {'start': '2026-01-01'}},
                     '마감일': {'type': 'date', 'date': {'start': '2026-03-30', 'end': '2026-04-03'}},
                     '운영반영(입력)': {'type': 'date', 'date': {'start': '2026-04-10'}}})
        self.assertEqual(item['due_date'], '2026-04-03')
        self.assertEqual(item['deployment_date'], '2026-04-10')
        only_release = page({'운영반영(입력)': {'type': 'date', 'date': {'start': '2026-04-10'}}})
        self.assertEqual(only_release['due_date'], '')

    def test_deadline_plan_and_verification_do_not_use_registration_date(self):
        question = '노션 DB03_작업에서 2026년 4월 마감인 항목 보여줘'
        constraints = extract_query_constraints(question)
        plan = build_query_plan(question, {'source': 'notion'}, constraints)
        date_clause = {'field': 'due_at', 'operator': 'between', 'value': ['2026-04-01', '2026-04-30']}
        self.assertIn(date_clause, plan['filter_clauses'])
        merged = merge_query_plan_constraints(constraints, plan)
        self.assertFalse(merged.get('created_at'))
        self.assertFalse(merged.get('customer_key'))
        self.assertFalse(any(c['field'] == 'created_at' for c in merged['filter_clauses']))
        checks = validate_query_plan(question, plan)['checks']
        self.assertEqual(next(c['status'] for c in checks if c['id'] == 'date_intent'), 'pass')
        own = search.extract_notion_constraints(question)
        item = {'database_label': 'DB03_작업', 'due_date': '2026-04-03', 'created_time': '2025-01-01'}
        self.assertTrue(search.notion_page_matches_constraints(item, own))
        self.assertFalse(search.notion_page_matches_constraints({**item, 'due_date': ''}, own))
        self.assertFalse(search.notion_page_matches_constraints({**item, 'due_date': '2026-05-01'}, own))

    def test_release_date_plan_uses_operational_date(self):
        question = '노션 버전관리에서 2026년 8월 운영반영된 항목 보여줘'
        plan = build_query_plan(question, {'source': 'notion'})
        self.assertIn({'field': 'deployed_at', 'operator': 'between', 'value': ['2026-08-01', '2026-08-31']}, plan['filter_clauses'])

    def test_particle_in_common_owner_filter_matches_notion_name(self):
        own = search.extract_notion_constraints('노션 담당자 김지은 문서 찾아줘')
        common = {'filter_clauses': [{'field': 'owner', 'operator': 'in', 'value': ['김지은인']}]}
        merged = search.merge_common_notion_constraints(own, common)
        self.assertTrue(search.notion_page_matches_constraints({'owner': '김지은'}, merged))
        self.assertFalse(search.notion_page_matches_constraints({'owner': '김지원'}, merged))
        self.assertEqual(common['filter_clauses'][0]['value'], ['김지은인'])

    def test_server_due_filter_does_not_drop_ranges_starting_before_month(self):
        with patch.object(search, 'notion_property_schema', return_value={'마감일': {'type': 'date'}}):
            result = search.notion_database_date_filter('db', {'field': 'due_at', 'operator': 'between', 'value': ['2026-04-01', '2026-04-30']})
        self.assertEqual(result, {'property': '마감일', 'date': {'on_or_before': '2026-04-30'}})

    def test_display_does_not_call_contributor_assignee(self):
        item = page({'Contributors': people('김지은')})
        result = {'stages': [{'source': 'notion', 'status': 'ok', 'notion_pages': [item], 'meta': {'search_complete': True, 'candidate_count': 1}}]}
        text = format_deterministic_notion_answer('노션 문서 보여줘', result, 5)
        self.assertIn('기여자 김지은', text)
        self.assertNotIn('담당 김지은', text)

    def test_limited_created_list_does_not_claim_a_total_count(self):
        item = page({'등록일': {'type': 'date', 'date': {'start': '2026-09-15'}}})
        result = {'stages': [{'source': 'notion', 'status': 'ok', 'notion_pages': [item], 'meta': {
            'search_complete': True,
            'candidate_count': 1,
            'candidate_count_complete': False,
            'result_window_complete': True,
            'constraints': {'common_filter_clauses': [{
                'field': 'created_at', 'operator': 'between', 'value': ['2026-09-14', '2026-09-20'],
            }]},
        }}]}
        text = format_deterministic_notion_answer('이번주 등록된 이슈 알려줘', result, 5)
        self.assertIn('최근 등록순으로 1건 표시', text)
        self.assertNotIn('총 1건', text)
        self.assertNotIn('일부 항목', text)

    def test_limited_keyword_list_uses_modified_sort_without_incomplete_warning(self):
        item = page({'수정일': {'type': 'last_edited_time', 'last_edited_time': '2026-09-15'}})
        result = {'stages': [{'source': 'notion', 'status': 'ok', 'notion_pages': [item], 'meta': {
            'search_complete': True,
            'candidate_count': 1,
            'candidate_count_complete': False,
            'result_window_complete': True,
            'terms': ['needle'],
            'constraints': {},
        }}]}
        text = format_deterministic_notion_answer('needle 문서 보여줘', result, 5)
        self.assertIn('최근 수정순으로 1건 표시', text)
        self.assertNotIn('최근 등록순', text)
        self.assertNotIn('일부 항목', text)

    def test_created_date_sort_label_is_consistent_for_truncated_and_grouped_lists(self):
        item = page({'등록일': {'type': 'date', 'date': {'start': '2026-09-15'}}})
        constraints = {'common_filter_clauses': [{
            'field': 'created_at', 'operator': 'between', 'value': ['2026-09-14', '2026-09-20'],
        }]}
        stage = {'source': 'notion', 'status': 'ok', 'notion_pages': [item], 'meta': {
            'search_complete': True, 'candidate_count': 3, 'constraints': constraints,
        }}
        plain = format_deterministic_notion_answer('이번주 등록된 이슈 보여줘', {'stages': [stage]}, 1)
        grouped = format_deterministic_notion_answer(
            '이번주 등록된 이슈 담당자별로 보여줘',
            {'stages': [{**stage, 'meta': {**stage['meta'], 'constraints': {**constraints, 'group_by': 'owner'}}}]},
            1,
        )
        for text in (plain, grouped):
            self.assertIn('최근 등록순', text)
            self.assertNotIn('최근 수정순', text)

    def test_realistic_deadline_question_survives_the_full_filter_pipeline(self):
        question = '노션 DB03_작업에서 2026년 4월 마감인 "AP 가동" 항목 찾아줘'
        raw = {'id': 'item', 'properties': {
            '이름': {'type': 'title', 'title': [{'plain_text': 'AP 가동'}]},
            '마감일': {'type': 'date', 'date': {'start': '2026-04-03'}},
        }}
        constraints = extract_query_constraints(question)
        plan = build_query_plan(question, {'source': 'notion'}, constraints)
        merged = merge_query_plan_constraints(constraints, plan)
        with patch.object(search, 'get_notion_settings', return_value={'token': 'token', 'database_ids': ['a' * 32]}), patch.object(search, 'fetch_notion_database_title', return_value='DB03_작업'), patch.object(search, 'notion_property_schema', return_value={'이름': {'type': 'title'}, '마감일': {'type': 'date'}}), patch.object(search, 'notion_post_json', return_value=response({'results': [raw], 'has_more': False})):
            result = search.execute_notion_search(question, constraints=merged, include_blocks=False)
        self.assertEqual([p['id'] for p in result['notion_pages']], ['item'])
        self.assertEqual(result['notion_pages'][0]['due_date'], '2026-04-03')

    def test_model_cannot_invent_deadline_range_or_override_explicit_month(self):
        clause = {'field': 'due_at', 'operator': 'between', 'value': ['2025-01-01', '2025-01-31']}
        for question, expected in [('노션 마감일 있는 문서 보여줘', []), ('노션 2026년 4월 마감인 문서 보여줘', [{'field': 'due_at', 'operator': 'between', 'value': ['2026-04-01', '2026-04-30']}])]:
            plan = build_query_plan(question, {'source': 'notion'}, model_plan={'filter_clauses': [clause]})
            self.assertEqual([c for c in plan['filter_clauses'] if c['field'] == 'due_at'], expected)


class ChildScopeTests(unittest.TestCase):
    def setUp(self):
        read_scope._scope_cached.cache_clear()
        client._page_content_cached.cache_clear()
        self.addCleanup(read_scope._scope_cached.cache_clear)
        self.addCleanup(client._page_content_cached.cache_clear)

    def test_scope_follows_ownership_not_relations_and_cache_tracks_token(self):
        root, child, outside = 'a' * 32, 'b' * 32, 'c' * 32
        inventory = {'results': [
            {'id': child, 'object': 'database', 'parent': {'type': 'page_id', 'page_id': 'parent-page'}},
            {'id': outside, 'object': 'database', 'parent': {'type': 'workspace', 'workspace': True}},
        ], 'has_more': False}
        parent = {'parent': {'type': 'database_id', 'database_id': root}, 'properties': {'relation': {'relation': [{'id': outside}]}}}
        with patch.dict(os.environ, {'NOTION_INCLUDE_CHILD_DATABASES': 'true'}), patch.object(read_scope.requests, 'post', return_value=response(inventory)) as post, patch.object(read_scope.requests, 'get', return_value=response(parent)):
            scope, complete = read_scope.refresh_notion_read_scope([root], 'token-1')
            self.assertTrue(complete)
            self.assertEqual(scope, {root: root, child: root})
            read_scope.refresh_notion_read_scope([root], 'token-1')
            self.assertEqual(post.call_count, 1)
            read_scope.refresh_notion_read_scope([root], 'token-2')
            self.assertEqual(post.call_count, 2)

    def test_failed_discovery_keeps_roots_and_reports_partial_scope(self):
        with patch.dict(os.environ, {'NOTION_INCLUDE_CHILD_DATABASES': 'true'}), patch.object(read_scope.requests, 'post', return_value=response({}, 503)):
            scope, complete = read_scope.refresh_notion_read_scope(['a' * 32], 'token')
        self.assertFalse(complete)
        self.assertEqual(list(scope), ['a' * 32])

    def test_user_search_does_not_discover_scope_when_index_is_missing(self):
        root = 'a' * 32
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {'NOTION_INCLUDE_CHILD_DATABASES': 'true'}),
            patch.object(read_scope, 'RUNTIME_DIR', Path(directory)),
            patch.object(read_scope, '_scope_cached') as discover,
        ):
            scope, complete = read_scope.resolve_notion_read_scope([root], 'token')
        self.assertEqual(scope, {root: root})
        self.assertFalse(complete)
        discover.assert_not_called()

    def test_legacy_scope_is_reused_without_claiming_completeness(self):
        root, child = 'a' * 32, 'b' * 32
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / 'notion_title_index.json').write_text(json.dumps({
                'complete': True,
                'root_database_ids': [root],
                'read_scope': {root: root, child: root},
            }), encoding='utf-8')
            with patch.object(read_scope, 'RUNTIME_DIR', runtime):
                scope, complete = read_scope.persisted_notion_read_scope((root,))
        self.assertEqual(scope, {root: root, child: root})
        self.assertFalse(complete)

    def test_complete_scope_outlives_title_index_ttl(self):
        root, child = 'a' * 32, 'b' * 32
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            index_path = runtime / 'notion_title_index.json'
            index_path.write_text(json.dumps({
                'scope_complete': True,
                'catalog_complete': True,
                'root_database_ids': [root],
                'read_scope': {root: root, child: root},
            }), encoding='utf-8')
            old = time.time() - 90000
            os.utime(index_path, (old, old))
            with (
                patch.dict(os.environ, {
                    'NOTION_TITLE_INDEX_TTL_SECONDS': '86400',
                    'NOTION_READ_SCOPE_TTL_SECONDS': '2592000',
                }),
                patch.object(read_scope, 'RUNTIME_DIR', runtime),
            ):
                scope, complete = read_scope.persisted_notion_read_scope((root,))
        self.assertEqual(scope, {root: root, child: root})
        self.assertTrue(complete)

    def test_expired_complete_scope_is_reused_as_incomplete(self):
        root, child = 'a' * 32, 'b' * 32
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            scope_path = runtime / read_scope.READ_SCOPE_FILENAME
            scope_path.write_text(json.dumps({
                'scope_complete': True,
                'root_database_ids': [root],
                'read_scope': {root: root, child: root},
            }), encoding='utf-8')
            old = time.time() - 31 * 86400
            os.utime(scope_path, (old, old))
            with (
                patch.dict(os.environ, {'NOTION_READ_SCOPE_TTL_SECONDS': '2592000'}),
                patch.object(read_scope, 'RUNTIME_DIR', runtime),
            ):
                scope, complete = read_scope.persisted_notion_read_scope((root,))
        self.assertEqual(scope, {root: root, child: root})
        self.assertFalse(complete)

    def test_complete_scope_is_saved_separately_from_title_catalog(self):
        root, child = 'a' * 32, 'b' * 32
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            with patch.object(read_scope, 'RUNTIME_DIR', runtime):
                read_scope.save_complete_notion_read_scope(
                    [root], {root: root, child: root}, datetime.now(timezone.utc).isoformat()
                )
                scope, complete = read_scope.persisted_notion_read_scope((root,))
            saved = json.loads((runtime / read_scope.READ_SCOPE_FILENAME).read_text(encoding='utf-8'))
        self.assertEqual(scope, {root: root, child: root})
        self.assertTrue(complete)
        self.assertTrue(saved['scope_complete'])

    def test_legacy_migration_does_not_extend_successful_discovery_time(self):
        root, child = 'a' * 32, 'b' * 32
        with tempfile.TemporaryDirectory() as directory, patch.object(read_scope, 'RUNTIME_DIR', Path(directory)):
            path = Path(directory) / read_scope.TITLE_INDEX_FILENAME
            generated = datetime.fromtimestamp(time.time() - 29 * 86400, timezone.utc).isoformat()
            path.write_text(json.dumps({
                'generated_at': generated, 'scope_complete': True,
                'root_database_ids': [root], 'read_scope': {root: root, child: root},
            }), encoding='utf-8')
            read_scope.preserve_legacy_notion_read_scope([root])
            saved_path = Path(directory) / read_scope.READ_SCOPE_FILENAME
            self.assertEqual(json.loads(saved_path.read_text())['generated_at'], generated)
            before = saved_path.read_bytes()
            with patch.object(read_scope.time, 'time', return_value=time.time() + 2 * 86400):
                read_scope.preserve_legacy_notion_read_scope([root])
                scope, complete = read_scope.persisted_notion_read_scope((root,))
            self.assertEqual(scope, {root: root, child: root})
            self.assertFalse(complete)
            self.assertEqual(saved_path.read_bytes(), before)

    def test_old_generated_time_is_not_renewed_by_file_write(self):
        root = 'a' * 32
        with tempfile.TemporaryDirectory() as directory, patch.object(read_scope, 'RUNTIME_DIR', Path(directory)):
            read_scope.save_complete_notion_read_scope([root], {root: root}, '2020-01-01T00:00:00+09:00')
            self.assertFalse(read_scope.persisted_notion_read_scope((root,))[1])

    def test_rebaseline_index_completion_requires_catalog_and_scope(self):
        for scope_complete, catalog_complete, expected in (
            (True, True, True), (False, True, False), (True, False, False),
        ):
            with self.subTest(scope=scope_complete, catalog=catalog_complete):
                index = rebaseline_notion_titles.build_title_index(
                    'now', ['root'], {'root': 'root'}, {},
                    scope_complete=scope_complete,
                    catalog_complete=catalog_complete,
                )
                self.assertIs(index['complete'], expected)
                self.assertIs(index['scope_complete'], scope_complete)
                self.assertIs(index['catalog_complete'], catalog_complete)

    def test_rebaseline_outputs_follow_runtime_directory(self):
        self.assertEqual(rebaseline_notion_titles.REPORT_DIR.parent, rebaseline_notion_titles.RUNTIME_DIR)

    def test_child_read_permission_does_not_grant_write_permission(self):
        root, child = 'a' * 32, 'b' * 32
        with patch.object(schema, 'get_notion_settings', return_value={'token': 'token', 'database_ids': [root]}), patch.object(read_scope, 'resolve_notion_read_scope', return_value=({root: root, child: root}, True)):
            self.assertEqual(schema.ensure_readable_notion_database(child), child)
            with self.assertRaises(HTTPException):
                schema.ensure_configured_notion_database(child)
            with self.assertRaises(HTTPException):
                schema.ensure_readable_notion_database('c' * 32)

    def test_nested_content_and_child_database_provenance(self):
        root = {'results': [{'id': 'db', 'type': 'child_database', 'child_database': {'title': '가이드'}}]}
        db_schema = {'parent': {'type': 'page_id', 'page_id': 'root'}}
        row = {'id': 'child', 'parent': {'database_id': 'db'}, 'properties': {'제목': {'type': 'title', 'title': [{'plain_text': '복구 절차'}]}}}
        blocks = {'results': [{'id': 'toggle', 'type': 'toggle', 'has_children': True, 'toggle': {'rich_text': [{'plain_text': '상세'}]}}]}
        nested = {'results': [{'id': 'text', 'type': 'paragraph', 'paragraph': {'rich_text': [{'plain_text': '검증된 복구 명령'}]}}]}
        with patch.object(client.requests, 'get', side_effect=[response(root), response(db_schema), response(blocks), response(nested)]), patch.object(client.requests, 'post', return_value=response({'results': [row]})):
            text = client.fetch_notion_page_blocks('root', 100)
        self.assertIn('복구 절차', text)
        self.assertIn('https://www.notion.so/child', text)
        self.assertIn('검증된 복구 명령', text)
        self.assertNotIn(client.NOTION_CONTENT_INCOMPLETE, text)

    def test_unreadable_child_is_reported_without_claiming_empty_document(self):
        root = {'results': [{'id': 'db', 'type': 'child_database', 'child_database': {}}]}
        with patch.object(client.requests, 'get', side_effect=[response(root), response({}, 400)]), patch.object(client.requests, 'post') as post:
            text = client.fetch_notion_page_blocks('root', 100)
        self.assertIn(client.NOTION_CONTENT_INCOMPLETE, text)
        post.assert_not_called()

    def test_repeated_cursor_and_budget_stop_without_hanging(self):
        data = {'results': [{'id': 'text', 'type': 'paragraph', 'paragraph': {'rich_text': [{'plain_text': '내용'}]}}], 'has_more': True, 'next_cursor': 'same'}
        with patch.object(client.requests, 'get', return_value=response(data)) as get:
            text = client.fetch_notion_page_blocks('root', 100)
        self.assertEqual(get.call_count, 2)
        self.assertIn(client.NOTION_CONTENT_INCOMPLETE, text)
