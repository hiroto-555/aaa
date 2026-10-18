import io
import json
import unittest
from unittest.mock import mock_open, patch

import app as application


class ApplicationFlowTests(unittest.TestCase):
    def setUp(self):
        self.original_shelters = application.shelters
        application.shelters = [
            {
                'id': 1,
                'name': '青森中央小学校',
                'district': '中央地区',
                'address': '青森市中央1-1-1',
                'capacity': 100,
                'current_occupancy': 25,
                'hazards': ['地震', '津波'],
                'opening_status': 'open',
                'equipment': ['pet_allowed']
            },
            {
                'id': 2,
                'name': '青森東中学校',
                'district': '東地区',
                'address': '青森市東2-2-2',
                'capacity': None,
                'hazards': ['土砂崩れ'],
                'opening_status': 'closed'
            }
        ]
        self.client = application.app.test_client()

    def tearDown(self):
        application.shelters = self.original_shelters

    def log_in(self):
        with self.client.session_transaction() as session:
            session['logged_in'] = True

    def test_login_uses_configured_password_and_returns_to_safe_path(self):
        with patch.object(application, 'ADMIN_PASSWORD', 'configured-password'):
            response = self.client.post(
                '/login',
                data={
                    'password': 'configured-password',
                    'next': '/shelters/1/edit'
                }
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/shelters/1/edit')

    def test_login_rejects_external_redirect_targets(self):
        with patch.object(application, 'ADMIN_PASSWORD', 'configured-password'):
            response = self.client.post(
                '/login?next=https://attacker.example/',
                data={
                    'password': 'configured-password',
                    'next': 'https://attacker.example/'
                }
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/shelter_register')
        self.assertNotIn('attacker.example', response.headers['Location'])

    def test_login_is_disabled_until_admin_password_is_configured(self):
        with patch.object(application, 'ADMIN_PASSWORD', None):
            response = self.client.post('/login', data={'password': 'anything'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('管理者認証が設定されていません'.encode(), response.data)

    def test_login_failure_preserves_safe_next_target(self):
        with patch.object(application, 'ADMIN_PASSWORD', 'configured-password'):
            response = self.client.post(
                '/login',
                data={'password': 'incorrect', 'next': '/board'}
            )
        self.assertEqual(response.status_code, 401)
        self.assertIn('パスワードが正しくありません'.encode(), response.data)
        self.assertIn('value="/board"'.encode(), response.data)

    def test_edit_requires_login_and_prefills_existing_data(self):
        unauthenticated = self.client.get('/shelters/1/edit')
        self.assertEqual(unauthenticated.status_code, 302)
        self.assertIn('/login', unauthenticated.headers['Location'])

        self.log_in()
        response = self.client.get('/shelters/1/edit')
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="青森中央小学校"'.encode(), response.data)
        self.assertIn('開設中'.encode(), response.data)

    def test_registration_form_defaults_to_open_and_is_authenticated(self):
        unauthenticated = self.client.get('/shelter_register')
        self.assertEqual(unauthenticated.status_code, 302)

        self.log_in()
        response = self.client.get('/shelter_register')
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="open" selected', response.data.decode())

    def test_edit_persists_values_and_keeps_legacy_fields(self):
        self.log_in()
        application.shelters[0]['legacy_field'] = 'keep-me'
        data_file = mock_open()
        form = {
            'name': '青森中央小学校 改修校舎',
            'district': '中央地区',
            'address': '青森市中央9-9-9',
            'capacity': '120',
            'current_occupancy': '20',
            'hazards': ['地震', '土砂崩れ'],
            'opening_status': 'closed',
            'equipment': ['barrier_free'],
            'facilities': '非常用発電機あり'
        }
        with patch('builtins.open', return_value=data_file()):
            response = self.client.post('/shelters/1/edit', data=form)

        self.assertEqual(response.status_code, 200)
        self.assertIn('避難所情報を更新しました'.encode(), response.data)
        updated = application.shelters[0]
        self.assertEqual(updated['id'], 1)
        self.assertEqual(updated['capacity'], 120)
        self.assertEqual(updated['opening_status'], 'closed')
        self.assertEqual(updated['hazards'], ['地震', '土砂崩れ'])
        self.assertEqual(updated['legacy_field'], 'keep-me')
        saved_json = ''.join(call.args[0] for call in data_file().write.call_args_list)
        self.assertEqual(json.loads(saved_json)[0]['opening_status'], 'closed')

    def test_invalid_edit_values_are_rejected_without_saving(self):
        self.log_in()
        for form, expected_message in (
            ({'name': '', 'opening_status': 'open'}, '避難所名'),
            ({'name': '変更名', 'capacity': '-1', 'opening_status': 'open'}, '収容人数'),
            ({'name': '変更名', 'capacity': '1.2', 'opening_status': 'open'}, '整数'),
            ({'name': '変更名', 'opening_status': 'unknown'}, '開設状況')
        ):
            with patch('app.save_shelters') as save:
                response = self.client.post('/shelters/1/edit', data=form)
            self.assertEqual(response.status_code, 400)
            self.assertIn(expected_message.encode(), response.data)
            save.assert_not_called()
        self.assertEqual(application.shelters[0]['name'], '青森中央小学校')

    def test_duplicate_shelter_name_is_rejected(self):
        self.log_in()
        with patch('app.save_shelters') as save:
            response = self.client.post(
                '/shelter_register',
                data={'name': ' 青森中央小学校 '}
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn('同じ名前の避難所'.encode(), response.data)
        save.assert_not_called()

    def test_search_and_listing_show_opening_status_and_new_hazards(self):
        search = self.client.get('/shelter_search?district=中央地区')
        body = search.data.decode()
        self.assertIn('青森中央小学校', body)
        self.assertIn('地震、津波', body)
        self.assertIn('開設中', body)
        self.assertNotIn('青森東中学校', body)
        self.assertIn('地震'.encode(), search.data)

        listing = self.client.get('/all_shelters')
        self.assertIn('閉鎖中'.encode(), listing.data)
        self.assertIn('土砂崩れ'.encode(), listing.data)

    def test_home_has_aomori_region_and_resident_navigation(self):
        response = self.client.get('/')
        body = response.data.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn('青森市 防災・避難所案内', body)
        self.assertIn('避難所を検索', body)
        self.assertIn('全避難所一覧', body)
        self.assertIn('青森市周辺の地図', body)

    def test_home_and_board_only_show_resident_instructions(self):
        original_instructions = application.instructions
        application.instructions = [
            {'id': 1, 'target': '住民', 'content': '住民向け避難指示', 'status': '未対応'},
            {'id': 2, 'target': '消防団本部', 'content': '内部向け連絡', 'status': '未対応'}
        ]
        try:
            home = self.client.get('/')
            self.assertIn('住民向け避難指示'.encode(), home.data)
            self.assertNotIn('内部向け連絡'.encode(), home.data)

            self.log_in()
            board = self.client.get('/board')
            self.assertIn('住民向け避難指示'.encode(), board.data)
            self.assertNotIn('内部向け連絡'.encode(), board.data)
        finally:
            application.instructions = original_instructions

    def test_logout_clears_admin_session(self):
        self.log_in()
        response = self.client.get('/logout')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/')
        protected = self.client.get('/board')
        self.assertEqual(protected.status_code, 302)
        self.assertIn('/login', protected.headers['Location'])

    def test_weather_warnings_are_filtered_deduplicated_and_named(self):
        reports = [
            {
                'reportDatetime': '2026-10-18T00:00:00Z',
                'warning': {
                    'class20Items': [
                        {
                            'areaCode': '0220100',
                            'kinds': [
                                {'code': '04', 'status': '発表'},
                                {'code': '04', 'status': '継続'},
                                {'code': '99', 'status': '継続'},
                                {'code': '05', 'status': '解除'}
                            ]
                        },
                        {
                            'areaCode': '0220200',
                            'kinds': [{'code': '06', 'status': '発表'}]
                        }
                    ]
                }
            }
        ]
        warnings, report_time = application.parse_area_warnings(reports)
        self.assertEqual(
            [(item['code'], item['name']) for item in warnings],
            [('04', '洪水警報'), ('99', '不明な警報・注意報 (コード: 99)')]
        )
        self.assertEqual(report_time, '2026-10-18T00:00:00Z')
        self.assertEqual(
            application.format_report_time(report_time),
            '2026年10月18日 09:00'
        )

    def test_weather_uses_latest_matching_report_not_array_order(self):
        reports = [
            {
                'reportDatetime': '2026-10-18T02:00:00Z',
                'warning': {'class20Items': [{
                    'areaCode': '0220100',
                    'kinds': [{'code': '04', 'status': '解除'}]
                }]}
            },
            {
                'reportDatetime': '2026-10-18T01:00:00Z',
                'warning': {'class20Items': [{
                    'areaCode': '0220100',
                    'kinds': [{'code': '05', 'status': '発表'}]
                }]}
            }
        ]
        warnings, report_time = application.parse_area_warnings(reports)
        self.assertEqual(warnings, [])
        self.assertEqual(report_time, '2026-10-18T02:00:00Z')

    def test_disaster_information_api_sorts_events_and_excludes_inactive_or_staff_notices(self):
        original_instructions = application.instructions
        application.instructions = [
            {
                'id': 1, 'target': '住民', 'content': '古い住民向け指示',
                'status': '未対応', 'created_at': '2026-10-18T00:00:00Z'
            },
            {
                'id': 2, 'target': '住民', 'content': '解除済み指示',
                'status': '解除', 'created_at': '2026-10-18T04:00:00Z'
            },
            {
                'id': 3, 'target': '職員', 'content': '職員向け指示',
                'status': '発信中', 'created_at': '2026-10-18T05:00:00Z'
            },
            {
                'id': 4, 'target': '住民', 'content': '新しい住民向け指示',
                'status': '発信中', 'created_at': '2026-10-18T03:00:00Z',
                'shelter': '中央小学校'
            }
        ]
        weather = {
            'warnings': [{
                'name': '洪水警報', 'code': '04', 'status': '発表'
            }],
            'error': False,
            'report_time': '2026年10月18日 12:30',
            'last_fetch_time': '2026年10月18日 12:31'
        }
        try:
            with patch('app.get_weather_warnings', return_value=weather):
                response = self.client.get('/api/disaster_information')
            self.assertEqual(response.status_code, 200)
            body = response.get_json()
            self.assertEqual(
                [event['content'] for event in body['events']],
                ['洪水警報', '新しい住民向け指示', '古い住民向け指示']
            )
            self.assertEqual(body['events'][1]['shelter'], '中央小学校')
            self.assertEqual(body['sources']['道路・現地情報'], '情報源未接続')
            serialized = json.dumps(body, ensure_ascii=False)
            self.assertNotIn('解除済み指示', serialized)
            self.assertNotIn('職員向け指示', serialized)
        finally:
            application.instructions = original_instructions

    def test_shelter_management_and_geocode_require_login(self):
        management = self.client.get('/shelter_management')
        geocode = self.client.post('/api/geocode', data={
            'query': '中央小学校', 'prefecture': '青森県', 'city': '青森市'
        })
        self.assertEqual(management.status_code, 302)
        self.assertEqual(geocode.status_code, 302)

        self.log_in()
        management = self.client.get('/shelter_management')
        self.assertEqual(management.status_code, 200)
        self.assertIn('青森中央小学校'.encode(), management.data)
        self.assertIn('座標が未登録または不正な施設は地図に表示していません'.encode(), management.data)

    def test_map_data_only_includes_valid_coordinates_and_marks_unknown_status(self):
        mapped, unlocated = application.shelter_map_data([
            {
                'id': 1, 'name': '開設施設', 'latitude': 40.8, 'longitude': 140.7,
                'opening_status': 'open'
            },
            {
                'id': 2, 'name': '閉鎖施設', 'latitude': 40.9, 'longitude': 140.8,
                'opening_status': 'closed'
            },
            {
                'id': 3, 'name': '状態不明施設', 'latitude': 40.7, 'longitude': 140.6
            },
            {'id': 4, 'name': '座標不明施設'},
            {'id': 5, 'name': '不正座標施設', 'latitude': 91, 'longitude': 140.7}
        ])
        self.assertEqual(
            [item['marker_color'] for item in mapped],
            ['green', 'gray', 'yellow']
        )
        self.assertEqual([item['status'] for item in mapped], ['開設中', '未開設', '未登録'])
        self.assertEqual(unlocated, 2)

    def test_geocode_returns_limited_region_scoped_candidates_and_handles_failure(self):
        self.log_in()
        application.geocoder_cache.clear()
        application.geocoder_last_request = 0
        features = [
            {
                'geometry': {'coordinates': [140.7406 + index * 0.001, 40.8246]},
                'properties': {'title': f'候補{index}', 'address': f'青森市住所{index}'}
            }
            for index in range(7)
        ]
        features.append({
            'geometry': {'coordinates': [141.5, 40.8246]},
            'properties': {'title': '地域外候補', 'address': '遠方'}
        })
        payload = json.dumps(features, ensure_ascii=False).encode()
        with patch('app.urllib.request.urlopen', return_value=io.BytesIO(payload)):
            response = self.client.post('/api/geocode', data={
                'query': '中央小学校', 'prefecture': '青森県', 'city': '青森市'
            })
        self.assertEqual(response.status_code, 200)
        candidates = response.get_json()['candidates']
        self.assertEqual(len(candidates), 5)
        self.assertEqual(candidates[0]['name'], '候補0')
        self.assertTrue(all(item['distance_km'] < 50 for item in candidates))
        self.assertEqual(candidates[0]['source'], '国土地理院 地名検索')

        application.geocoder_cache.clear()
        application.geocoder_last_request = 0
        with patch('app.urllib.request.urlopen', side_effect=TimeoutError):
            failed = self.client.post('/api/geocode', data={
                'query': '別の施設', 'prefecture': '青森県', 'city': '青森市'
            })
        self.assertEqual(failed.status_code, 502)
        self.assertIn('地名検索サービスを利用できません', failed.get_json()['error'])

        application.geocoder_cache.clear()
        application.geocoder_last_request = 0
        with patch('app.urllib.request.urlopen', return_value=io.BytesIO(b'{')):
            malformed = self.client.post('/api/geocode', data={
                'query': '不正応答の施設', 'prefecture': '青森県', 'city': '青森市'
            })
        self.assertEqual(malformed.status_code, 502)
        self.assertIn('正しい形式', malformed.get_json()['error'])

    def test_shelter_coordinates_are_validated_and_persisted(self):
        self.log_in()
        with patch('builtins.open', mock_open()) as data_file:
            response = self.client.post('/shelters/1/edit', data={
                'name': '青森中央小学校',
                'opening_status': 'open',
                'latitude': '40.8246',
                'longitude': '140.7406'
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(application.shelters[0]['latitude'], 40.8246)
        self.assertEqual(application.shelters[0]['longitude'], 140.7406)
        saved_json = ''.join(
            call.args[0] for call in data_file().write.call_args_list
        )
        self.assertEqual(json.loads(saved_json)[0]['longitude'], 140.7406)

        with patch('app.save_shelters') as save:
            invalid = self.client.post('/shelters/1/edit', data={
                'name': '変更しない',
                'opening_status': 'open',
                'latitude': '91',
                'longitude': '140.7406'
            })
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('緯度'.encode(), invalid.data)
        save.assert_not_called()

    def test_weather_api_distinguishes_no_warnings_from_fetch_and_format_errors(self):
        with patch(
            'app.urllib.request.urlopen',
            return_value=io.BytesIO(b'[]')
        ):
            no_warnings = self.client.get('/api/weather_warnings').get_json()
        self.assertNotIn('error', no_warnings)
        self.assertEqual(no_warnings['warnings'], [])
        self.assertEqual(no_warnings['area_name'], '青森市')

        with patch('app.urllib.request.urlopen', side_effect=TimeoutError):
            failed = self.client.get('/api/weather_warnings').get_json()
        self.assertTrue(failed['error'])
        self.assertEqual(failed['report_time'], '取得失敗')

        with patch(
            'app.urllib.request.urlopen',
            return_value=io.BytesIO(b'{"unexpected": true}')
        ):
            malformed = self.client.get('/api/weather_warnings').get_json()
        self.assertTrue(malformed['error'])


if __name__ == '__main__':
    unittest.main()
