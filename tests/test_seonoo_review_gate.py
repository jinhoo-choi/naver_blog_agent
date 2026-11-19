import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / 'scripts/seonoo-review-check.py'
spec = importlib.util.spec_from_file_location('review', TARGET)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class ReviewTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        import io

        from PIL import Image

        stream = io.BytesIO()
        Image.new('RGB', (4, 4), 'white').save(stream, format='PNG')
        raw = stream.getvalue()
        (self.root / 'cover.jpg').write_bytes(raw)

        def h(b):
            return hashlib.sha256(b).hexdigest()
        source = '승인 예문 위치: 사용자 제공 메모. 패딩을 거꾸로 입고 다니는 줄 알았어요.'
        (self.root / 'reference.txt').write_text(source)
        saved = {'title': '검증용 원고', 'body': '첫 답과 추가 조건을 설명한 검증용 본문입니다.', 'representative': {'sha256': h(raw)}, 'images': [{'sha256': h(raw)}], 'tables': [], 'cards': [], 'tags': []}
        self.saved = copy.deepcopy(saved)
        (self.root / 'snapshot.json').write_text(json.dumps(saved, ensure_ascii=False, sort_keys=True))
        self.sha = h((self.root / 'snapshot.json').read_bytes())
        artifacts = {k: {'path': p, 'sha256': h((self.root / p).read_bytes()), 'kind': kind} for k, p, kind in [('saved', 'snapshot.json', 'text'), ('cover', 'cover.jpg', 'image'), ('reference', 'reference.txt', 'text')]}
        steps = {}
        for i, stage in enumerate(m.STAGES):
            s = {'at': f'2026-11-18T10:00:0{i}+09:00', 'evidence': [{'location': '시험 장면', 'observation': '명시적 수행 기록을 가정한 스키마 시험', 'artifact': 'saved', 'quote': '첫 답과 추가 조건'}]}
            if stage in m.REVIEWS:
                s.update(changes=[], draft_id='test-draft')
            if stage in m.REVIEWS or stage == 'naver_save':
                s['revision'] = self.sha
            if stage in m.PREVIOUS_REVISIONS:
                s.update(draft_id='test-draft', input_revision=self.sha)
            if stage == 'mobile_review':
                s['reopened_at'] = '2026-11-18T10:00:07+09:00'
            steps[stage] = s
        ev = {'artifact': 'saved', 'quote': '첫 답과 추가 조건', 'observation': '시험용 저장본의 실제 구절을 인용함'}
        image_ev = {'artifact': 'cover', 'observation': '시험 이미지가 존재함. 시각 품질을 검증한 것은 아님'}
        quality = {axis: {'status': 'pass', 'score': 85, 'subscores': [17] * 5, 'deductions': [], 'immediate_fix': '', 'evidence': [ev.copy()]} for axis in m.AXES}
        for axis in ('design', 'structure'):
            quality[axis]['evidence'].append(image_ev.copy())
        quality['voice']['reference_evidence'] = [{'artifact': 'reference', 'quote': '패딩을 거꾸로', 'observation': '시험용 참고 구절의 파일 존재 확인'}]
        for stage in m.REVIEWS:
            steps[stage]['scores'] = {axis: 85 for axis in m.AXES}
        self.record = {'hard_checks': {k: {'status': 'pass', 'evidence': [ev.copy()]} for k in m.HARD_CHECKS}, 'standard_version': m.STANDARD, 'request_id': 'test', 'draft_id': 'test-draft', 'scored_revision': self.sha, 'snapshot_artifact': 'saved', 'artifacts': artifacts, 'steps': steps, 'quality': quality, 'unresolved': []}

        self.record['approved_cover'] = 'cover'
        for role, width, color, check in (
            ('pc_representative', 800, 'blue', 'representative'),
            ('list_140', 140, 'red', 'representative'),
            ('mobile_360', 360, 'green', 'mobile'),
        ):
            path = self.root / f'{role}.png'
            Image.new('RGB', (width, 400), color).save(path)
            artifacts[role] = {
                'kind': 'image', 'role': role, 'path': path.name, 'sha256': h(path.read_bytes()),
                'draft_id': 'test-draft', 'revision': self.sha,
                'captured_at': '2026-11-18T10:00:06+09:00',
            }
            self.record['hard_checks'][check]['evidence'].append(
                {'artifact': role, 'observation': 'Synthetic screen fixture'})

    def refresh_media(self):
        saved = copy.deepcopy(self.saved)
        media_sha = self.record['artifacts']['cover']['sha256']
        saved['representative']['sha256'] = media_sha
        saved['images'][0]['sha256'] = media_sha
        p = self.root / 'snapshot.json'
        p.write_text(json.dumps(saved, ensure_ascii=False))
        self.sha = hashlib.sha256(p.read_bytes()).hexdigest()
        self.record['artifacts']['saved']['sha256'] = self.sha
        self.record['scored_revision'] = self.sha
        for step in self.record['steps'].values():
            if 'revision' in step:
                step['revision'] = self.sha
            if 'input_revision' in step:
                step['input_revision'] = self.sha

    def tearDown(self):
        self.temp.cleanup()

    def check(self, record=None, root=True):
        return m.validate(self.record if record is None else record, 'test', self.sha, self.root if root else None)

    def test_valid_record_does_not_claim_quality_review(self):
        r = self.check()
        self.assertEqual(r['status'], 'record_valid')
        self.assertEqual(r['quality_decision'], 'pass')
        self.assertEqual(r['total'], 85)
        self.assertEqual(r['scope'], 'record_and_evidence_only')

    def test_reject_bad_records(self):
        cases = {'missing_snapshot': lambda r: r.pop('snapshot_artifact'), 'null_snapshot': lambda r: r.update(snapshot_artifact=None), 'unknown_snapshot': lambda r: r.update(snapshot_artifact='absent'), 'list_snapshot': lambda r: r.update(snapshot_artifact=[]), 'removed_snapshot_file': lambda r: r['artifacts'].pop('saved'), 'wrong_first_draft': lambda r: r['steps']['first_review'].update(draft_id='another'), 'missing_stage_artifact': lambda r: r['steps']['media']['evidence'][0].pop('artifact'), 'alias_current_reference': lambda r: (r['artifacts'].update(alias=r['artifacts']['saved'].copy()), r['quality']['voice'].update(reference_evidence=[{'artifact': 'alias', 'quote': '첫 답과 추가 조건', 'observation': '시험'}])), 'old_standard': lambda r: r.pop('standard_version'), 'wrong_id': lambda r: r.update(request_id='other'), 'missing_stage': lambda r: r['steps'].pop('first_review'), 'reverse_time': lambda r: r['steps']['second_review'].update(at='2026-11-17T10:00:00+09:00'), 'timezone_missing': lambda r: r['steps']['media'].update(at='2026-11-18T10:00:02'), 'wrong_draft': lambda r: r['steps']['mobile_review'].update(draft_id='different'), 'stale_input': lambda r: r['steps']['mobile_review'].update(input_revision='b' * 64), 'stale_score': lambda r: r.update(scored_revision='b' * 64), 'stale_mobile': lambda r: r['steps']['mobile_review'].update(revision='b' * 64), 'no_reopen': lambda r: r['steps']['mobile_review'].pop('reopened_at'), 'changes_mismatch': lambda r: r['steps']['second_review'].update(changes=[{'location': 'x', 'observation': 'changed'}]), 'old_score_only': lambda r: (r.pop('quality'), r.update(scores={a: {'score': 5, 'evidence': [{'location': '확인', 'observation': '확인'}]} for a in m.AXES})), 'no_artifacts': lambda r: r.pop('artifacts'), 'missing_file': lambda r: r['artifacts']['cover'].update(path='absent.jpg'), 'wrong_hash': lambda r: r['artifacts']['cover'].update(sha256='b' * 64), 'absolute_path': lambda r: r['artifacts']['cover'].update(path='/etc/hosts'), 'escape_path': lambda r: r['artifacts']['cover'].update(path='../escape.jpg'), 'bad_image': lambda r: r['artifacts']['reference'].update(kind='image'), 'quote_not_found': lambda r: r['quality']['topic']['evidence'][0].update(quote='존재하지 않는 말'), 'generic_confirmation': lambda r: r['quality']['voice'].update(evidence=[{'location': '확인', 'observation': '확인'}]), 'no_voice_reference': lambda r: r['quality']['voice'].pop('reference_evidence'), 'draft_as_reference': lambda r: r['quality']['voice'].update(reference_evidence=r['quality']['voice']['evidence']), 'no_visual_evidence': lambda r: r['quality']['design']['evidence'].pop(), 'nonstring_artifact': lambda r: r['quality']['topic']['evidence'][0].update(artifact={}), 'revise_without_defect': lambda r: r['quality']['topic'].update(status='revise'), 'snapshot_not_linked': lambda r: r.update(snapshot_artifact='reference'), 'missing_axis': lambda r: r['quality'].pop('reader'), 'bad_status': lambda r: r['quality']['topic'].update(status='great')}
        for name, mutate in cases.items():
            with self.subTest(name=name):
                r = copy.deepcopy(self.record)
                mutate(r)
                self.assertEqual(self.check(r)['status'], 'unscored')

    def test_direct_malformed_input(self):
        for value in (None, [], {}, 42, 'bad'):
            with self.subTest(value=value):
                self.assertEqual(m.validate(value, 'test', self.sha, self.root)['status'], 'unscored')

    def test_truncated_image(self):
        p = self.root / 'cover.jpg'
        p.write_bytes(bytes([255, 216, 255]))
        self.record['artifacts']['cover']['sha256'] = hashlib.sha256(p.read_bytes()).hexdigest()
        self.refresh_media()
        result = self.check()
        self.assertEqual(result['status'], 'unscored')
        self.assertTrue(any(x.startswith('artifacts.cover.') for x in result['reasons']))

    def test_bad_snapshot_elements(self):
        for field, value in [('tables', [None, 42]), ('cards', [{'unexpected': 'x'}]), ('cards', [{'title': 'x', 'url': 'https://'}]), ('tags', [None, {'bad': True}])]:
            with self.subTest(field=field):
                r = copy.deepcopy(self.record)
                p = self.root / 'snapshot.json'
                saved = copy.deepcopy(self.saved)
                saved[field] = value
                p.write_text(json.dumps(saved, ensure_ascii=False))
                sha = hashlib.sha256(p.read_bytes()).hexdigest()
                r['artifacts']['saved']['sha256'] = sha
                r['scored_revision'] = sha
                for step in r['steps'].values():
                    if 'revision' in step:
                        step['revision'] = sha
                    if 'input_revision' in step:
                        step['input_revision'] = sha
                result = m.validate(r, 'test', sha, self.root)
                self.assertEqual(result['status'], 'unscored')
                self.assertTrue(any(x.startswith('snapshot.' + field) for x in result['reasons']))

    def test_corrupt_png_checksum(self):
        import io

        from PIL import Image

        stream = io.BytesIO()
        Image.new('RGB', (2, 2)).save(stream, format='PNG')
        raw = bytearray(stream.getvalue())
        offset = raw.index(b'IDAT') + 4
        raw[offset] ^= 1
        (self.root / 'cover.jpg').write_bytes(raw)
        self.record['artifacts']['cover']['sha256'] = hashlib.sha256(raw).hexdigest()
        self.refresh_media()
        result = self.check()
        self.assertEqual(result['status'], 'unscored')
        self.assertTrue(any(x.startswith('artifacts.cover.') for x in result['reasons']))

    def test_no_root(self):
        self.assertEqual(self.check(root=False)['status'], 'unscored')

    def test_actual_tamper(self):
        (self.root / 'cover.jpg').write_bytes(b'changed')
        self.assertEqual(self.check()['status'], 'unscored')

    def test_unverified_stays_unverified(self):
        self.record['quality']['design'] = {'status': 'unverified', 'score': None, 'subscores': None, 'reason': '이미지 미제공', 'evidence': []}
        self.record['steps']['mobile_review']['scores']['design'] = None
        r = self.check()
        self.assertEqual(r['status'], 'record_valid')
        self.assertEqual(r['quality_decision'], 'unverified')

    def test_defect_stays_revise(self):
        self.record['quality']['topic'].update(status='revise', score=75, subscores=[15] * 5, deductions=[{'location': '본문', 'artifact': 'saved', 'quote': '첫 답과 추가 조건', 'observation': '중복'}], immediate_fix='중복 구간을 합칩니다.')
        self.record['steps']['mobile_review']['scores']['topic'] = 75
        self.record['unresolved'] = [{'location': '본문', 'observation': '중복 문장 남음'}]
        self.assertEqual(self.check()['quality_decision'], 'revise')

    def test_unresolved_overrides_pass(self):
        self.record['unresolved'] = [{'location': '본문', 'observation': '중복 문장 남음'}]
        self.assertEqual(self.check()['quality_decision'], 'revise')

    def test_file_not_json(self):
        p = self.root / 'snapshot.json'
        p.write_text('bad JSON')
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        self.record['artifacts']['saved']['sha256'] = h
        self.sha = h
        self.record['scored_revision'] = h
        self.assertEqual(self.check()['status'], 'unscored')

class ThresholdTests(ReviewTests):

    def set_scores(self, values):
        for axis, score in zip(m.AXES, values):
            item = self.record['quality'][axis]
            item.update(score=score, subscores=[score // 5] * 4 + [score - 4 * (score // 5)])
            for stage in m.REVIEWS:
                self.record['steps'][stage]['scores'][axis] = score
            if score < 80:
                item.update(status='revise', deductions=[{'location': '본문', 'artifact': 'saved', 'quote': '첫 답과 추가 조건', 'observation': '보완'}], immediate_fix='본문을 수정합니다.')
                self.record['unresolved'] = [{'location': '본문', 'observation': '보완'}]

    def test_84_8_does_not_round_up(self):
        self.set_scores([85, 85, 85, 85, 84])
        self.assertEqual(self.check()['total'], 84.8)
        self.assertEqual(self.check()['quality_decision'], 'revise')

    def test_high_mean_low_persona_blocked(self):
        self.set_scores([100, 100, 79, 100, 100])
        self.assertEqual(self.check()['quality_decision'], 'revise')

    def test_caption_error_blocks_high_scores(self):
        self.set_scores([100] * 5)
        self.record['hard_checks']['media_match']['status'] = 'fail'
        self.record['unresolved'] = [{'location': '사진', 'observation': '캡션 불일치'}]
        self.assertEqual(self.check()['quality_decision'], 'revise')

    def test_missing_mobile_blocks(self):
        self.record['hard_checks']['mobile'] = {'status': 'unverified', 'reason': '모바일 접근 불가'}
        r = self.check()
        self.assertEqual(r['quality_decision'], 'unverified')
        self.assertIsNone(r['total'])

    def test_score_sum_and_bool_invalid(self):
        for value in (True, 101, -1):
            with self.subTest(value=value):
                r = copy.deepcopy(self.record)
                r['quality']['design']['score'] = value
                self.assertEqual(self.check(r)['status'], 'unscored')
        self.record['quality']['design']['subscores'] = [20] * 5
        self.assertEqual(self.check()['status'], 'unscored')

    def test_malformed_persona_rejected(self):
        for value in (None, [], 5):
            r = copy.deepcopy(self.record)
            r['quality']['design'] = value
            self.assertEqual(self.check(r)['status'], 'unscored')

    def test_old_6_axes_rejected(self):
        self.record['quality']['accuracy'] = {}
        self.assertEqual(self.check()['status'], 'unscored')

    def test_missing_hard_check_rejected(self):
        self.record['hard_checks'].pop('mobile')
        self.assertEqual(self.check()['status'], 'unscored')

    def test_disclosure_only_may_be_na(self):
        self.record['hard_checks']['disclosure'] = {'status': 'not_applicable', 'reason': '광고 없음'}
        self.assertEqual(self.check()['quality_decision'], 'pass')
        self.record['hard_checks']['mobile'] = {'status': 'not_applicable', 'reason': '화면 없음'}
        self.assertEqual(self.check()['status'], 'unscored')

    def test_incomplete_reviews(self):
        for stage in ('first_review', 'second_review'):
            with self.subTest(stage=stage):
                record = copy.deepcopy(self.record)
                record['steps'][stage]['scores'] = dict.fromkeys(m.AXES)
                self.assertIn(stage + '.incomplete_scores', self.check(record)['reasons'])

    def test_no_score_increase_without_new_evidence(self):
        for stage in ('first_review', 'second_review'):
            self.record['steps'][stage]['scores'] = dict.fromkeys(m.AXES, 60)
        self.assertIn('mobile_review.unsupported_increase.voice', self.check()['reasons'])

    def test_score_correction_on_new_observation(self):
        self.record['steps']['second_review']['scores']['design'] = 82
        correction = {'artifact': 'mobile_360', 'location': 'first screen', 'observation': 'new observation'}
        self.record['steps']['mobile_review']['score_corrections'] = {'design': [correction]}
        self.assertEqual(self.check()['quality_decision'], 'pass')
        correction.update(artifact='saved', quote='첫 답과 추가 조건')
        self.assertIn('mobile_review.unsupported_increase.design', self.check()['reasons'])

    def test_screenless_and_stale_records_blocked(self):
        for role, key in [('pc_representative', 'representative'), ('list_140', 'representative'),
                          ('mobile_360', 'mobile')]:
            with self.subTest(role=role):
                record = copy.deepcopy(self.record)
                record['hard_checks'][key]['evidence'] = [{'artifact': 'cover', 'observation': 'cover'}]
                self.assertIn('screen.' + role, self.check(record)['reasons'])
                record = copy.deepcopy(self.record)
                record['artifacts'][role]['revision'] = 'd' * 64
                self.assertIn('screen.' + role, self.check(record)['reasons'])
        self.record['approved_cover'] = 'mobile_360'
        self.assertIn('representative.approved_cover', self.check()['reasons'])

    def test_duplicate_role_bytes_blocked(self):
        self.record['artifacts']['mobile_360'].update(
            {k: self.record['artifacts']['pc_representative'][k] for k in ('path', 'sha256')})
        self.assertIn('screen.mobile_360', self.check()['reasons'])

    def test_table_requires_matching_mobile_evidence(self):
        self.saved['tables'] = [{'rows': [['월령', '수유량']]}]
        self.refresh_media()
        for item in self.record['artifacts'].values():
            if item.get('role'):
                item['revision'] = self.sha
        self.assertEqual(self.check()['reasons'], ['screen.mobile_table'])

    def test_rewrite_grade(self):
        self.set_scores([85, 55, 85, 85, 85])
        self.record['quality']['voice']['status'] = 'rewrite'
        self.assertEqual(self.check()['quality_decision'], 'revise')
        self.record['quality']['voice']['status'] = 'revise'
        self.assertIn('quality.voice.score_status', self.check()['reasons'])

if __name__ == '__main__':
    unittest.main(verbosity=2)

class GuardedReviewTests(unittest.TestCase):
    commit = 'a' * 40
    target = 'https://example.gov/application'

    def setUp(self):
        self.fixture = ReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.record = self.fixture.record
        self.saved = self.fixture.saved
        self.root = self.fixture.root
        self.record['standard_commit'] = self.commit
        self.steps = self.record['steps']
        self.steps['second_review']['at'] = '2026-11-18T10:02:00+09:00'
        self.steps['mobile_review']['at'] = '2026-11-18T10:03:00+09:00'
        self.steps['mobile_review']['reopened_at'] = '2026-11-18T10:03:10+09:00'
        self.steps['candidate']['purpose_urls'] = []
        self.saved.update(quotes=[], links=[])
        self.dom = {
            'draft_id': 'test-draft',
            'observed_at': '2026-11-18T10:00:06+09:00',
            'title': self.saved['title'],
            'media_settled': True,
            'body_images': [
                {'src': 'https://blogfiles.pstatic.net/image.png', 'loaded': True,
                 'sha256': self.saved['images'][0]['sha256']},
            ],
            'links': [],
            'quotes': [],
            'purpose_required': False,
            'purpose_urls': [],
        }
        self.record['saved_dom_artifact'] = 'saved_dom'
        self.record['blind_review'] = {
            'input_artifact': 'blind_input',
            'result_artifact': 'blind_result',
            'input_prepared_at': '2026-11-18T10:00:08+09:00',
            'compared_first_at': '2026-11-18T10:02:30+09:00',
        }
        from PIL import Image

        cover = self.root / 'list_140.png'
        Image.new('RGB', (140, 140), 'red').save(cover)
        self.record['artifacts']['list_140'].update(
            sha256=hashlib.sha256(cover.read_bytes()).hexdigest(),
            source_sha256=self.saved['representative']['sha256'],
        )
        self.refresh()

    def artifact(self, key, value):
        path = self.root / (key + '.json')
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if key == 'snapshot':
            self.record['artifacts']['saved']['sha256'] = digest
        else:
            self.record['artifacts'][key] = {'path': path.name, 'kind': 'text', 'sha256': digest}
        return digest

    def refresh(self):
        self.sha = self.artifact('snapshot', self.saved)
        self.record['scored_revision'] = self.sha
        for step in self.steps.values():
            if 'revision' in step:
                step['revision'] = self.sha
            if 'input_revision' in step:
                step['input_revision'] = self.sha
        for artifact in self.record['artifacts'].values():
            if 'revision' in artifact:
                artifact['revision'] = self.sha
        self.dom['revision'] = self.sha
        self.artifact('saved_dom', self.dom)
        self.blind_input = {
            'standard_commit': self.commit, 'draft_id': 'test-draft', 'revision': self.sha,
            'title': self.saved['title'], 'body': self.saved['body'], 'official_references': [],
            'screens': {role: self.record['artifacts'][role]['sha256']
                        for role in ('list_140', 'mobile_360')},
        }
        self.blind_result = {
            'call_id': 'fixture-call-id', 'isolation': 'stateless_no_tools',
            'started_at': '2026-11-18T10:01:00+09:00',
            'finished_at': self.steps['second_review']['at'],
            'input_sha256': self.artifact('blind_input', self.blind_input),
            'scores': dict(self.steps['second_review']['scores']),
        }
        result_sha = self.artifact('blind_result', self.blind_result)
        self.history = [
            {'stage': stage, 'request_id': 'test', 'draft_id': 'test-draft', 'revision': self.sha,
             'at': self.steps[stage]['at'], 'scores': dict(self.steps[stage]['scores'])}
            for stage in m.REVIEWS
        ]
        self.history[1].update(result_sha256=result_sha, frozen_at='2026-11-18T10:02:10+09:00')

    def check(self):
        return m.validate(self.record, 'test', self.sha, self.root,
                          standard_commit=self.commit, review_history=self.history)

    def test_valid_information_post_has_no_mandatory_link(self):
        self.assertEqual(self.check()['quality_decision'], 'pass')

    def test_current_record_cannot_omit_strict_commit(self):
        self.record.pop('standard_commit')
        self.assertIn('standard_commit', self.check()['reasons'])

    def test_payload_builder_copies_only_allowed_snapshot_fields(self):
        self.saved.update(first_review={'design': 85}, changes=['internal'])
        self.refresh()
        payload = m.blind_input(
            self.root / 'snapshot.json', 'test-draft', self.commit,
            {role: self.root / (role + '.png') for role in ('list_140', 'mobile_360')},
            [],
        )
        self.assertEqual(payload, self.blind_input)

    def test_second_score_must_be_frozen_before_comparison(self):
        self.history[1]['frozen_at'] = self.record['blind_review']['compared_first_at']
        self.assertIn('second_review.freeze_order', self.check()['reasons'])

    def test_first_review_and_changes_do_not_enter_second_input(self):
        self.blind_input.update(first_review={'design': 85}, changes=[])
        self.blind_result['input_sha256'] = self.artifact('blind_input', self.blind_input)
        self.history[1]['result_sha256'] = self.artifact('blind_result', self.blind_result)
        self.assertIn('blind_review.input', self.check()['reasons'])

    def test_compare_before_second_score_freeze_is_rejected(self):
        self.record['blind_review']['compared_first_at'] = '2026-11-18T10:01:30+09:00'
        self.assertIn('blind_review.result', self.check()['reasons'])

    def test_unverified_isolation_blocks_pass(self):
        self.blind_result.pop('call_id')
        self.history[1]['result_sha256'] = self.artifact('blind_result', self.blind_result)
        self.assertEqual(self.check()['quality_decision'], 'unverified')

    def test_frozen_scores_reject_rewrite_of_all_stages_and_rehashed_result(self):
        for stage in m.REVIEWS:
            self.steps[stage]['scores']['design'] = 95
        self.record['quality']['design'].update(score=95, subscores=[19] * 5)
        self.blind_result['scores']['design'] = 95
        self.artifact('blind_result', self.blind_result)
        self.assertIn('mobile_review.frozen_scores_changed', self.check()['reasons'])

    def test_independent_second_round_preserves_its_own_score(self):
        self.steps['first_review']['scores']['design'] = 80
        self.refresh()
        self.assertEqual(self.check()['quality_decision'], 'pass')

    def test_missing_or_wrong_purpose_link_blocks_pass(self):
        self.steps['candidate']['purpose_urls'] = [self.target]
        self.dom.update(purpose_urls=[self.target], purpose_required=True)
        self.refresh()
        self.assertIn('saved_dom.purpose_link_missing_or_wrong', self.check()['reasons'])

        self.saved['links'] = [self.target]
        self.dom['links'] = [{'href': self.target, 'destination': 'https://example.gov/home'}]
        self.refresh()
        self.assertEqual(self.check()['quality_decision'], 'revise')

        self.dom['links'][0]['destination'] = self.target
        self.refresh()
        self.assertEqual(self.check()['quality_decision'], 'pass')

    def test_placeholder_before_settle_is_unverified_and_after_is_defect(self):
        self.dom['body_images'][0].update(src='data:image/svg+xml,fixture', loaded=True)
        self.dom['media_settled'] = False
        self.refresh()
        self.assertEqual(self.check()['quality_decision'], 'unverified')
        self.dom['media_settled'] = True
        self.refresh()
        self.assertEqual(self.check()['quality_decision'], 'revise')
        self.assertIn('saved_dom.image_not_loaded', self.check()['reasons'])

    def test_duplicate_body_image_even_with_different_urls(self):
        self.saved['images'].append(self.saved['images'][0].copy())
        duplicate = dict(self.dom['body_images'][0], src='https://postfiles.pstatic.net/image.png')
        self.dom['body_images'].append(duplicate)
        self.refresh()
        self.assertIn('saved_dom.duplicate_image', self.check()['reasons'])

    def test_title_and_quote_defects(self):
        self.dom['title'] = self.saved['title'] = '검증용 원고 '
        self.dom['quotes'] = self.saved['quotes'] = [
            {'text': '지급 금액', 'source_placeholder_visible': True,
             'adjacent_headings': ['지급   금액']},
        ]
        self.refresh()
        result = self.check()
        self.assertEqual(result['quality_decision'], 'revise')
        self.assertTrue({'saved_dom.title_whitespace', 'saved_dom.quote_placeholder',
                         'saved_dom.duplicate_heading'} <= set(result['reasons']))

    def test_stale_screens_and_mismatched_140_source_rejected(self):
        self.record['artifacts']['list_140']['source_sha256'] = 'f' * 64
        self.assertIn('screen.list_140_source', self.check()['reasons'])
        self.record['artifacts']['mobile_360']['revision'] = 'f' * 64
        self.assertIn('screen.mobile_360', self.check()['reasons'])


class SlotStateTests(unittest.TestCase):
    def test_late_start_can_only_resume_known_draft_and_preserves_slot_deadlines(self):
        def at(value):
            return m.timestamp(f'2026-11-19T{value}:00+09:00')

        scheduled, started = at('07:15'), at('07:30')
        self.assertEqual(m.slot_state(scheduled, started, at('07:54'))['action'], 'create_draft')
        self.assertEqual(m.slot_state(scheduled, started, at('07:55'))['action'], 'handoff')
        first = m.slot_state(scheduled, started, at('07:55'), 'draft-1')
        self.assertEqual(first, {'phase': 'closeout', 'action': 'verify_saved_draft', 'draft_id': 'draft-1'})
        self.assertEqual(m.slot_state(scheduled, started, at('08:10'), 'draft-1')['phase'], 'record_only')
        self.assertEqual(m.slot_state(scheduled, started, at('08:15'), 'draft-1')['action'], 'stop')
        next_slot = m.slot_state(at('08:15'), at('08:15'), at('08:16'), first['draft_id'])
        self.assertEqual(next_slot['action'], 'resume_draft')
        self.assertEqual(next_slot['draft_id'], first['draft_id'])
        self.assertEqual(
            m.slot_state(at('08:15'), at('08:15'), at('08:16'), save_uncertain=True)['action'],
            'reconcile_save',
        )
