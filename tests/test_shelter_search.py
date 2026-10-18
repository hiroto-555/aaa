import json
import unittest
from unittest.mock import mock_open, patch

import app as application


class ShelterSearchTests(unittest.TestCase):
    def setUp(self):
        self.original_shelters = application.shelters
        application.shelters = [
            {
                'id': 1,
                'name': '中央小学校',
                'district': '中央地区',
                'address': '青森市中央1-1-1',
                'hazards': ['洪水', '土砂災害'],
                'pet_allowed': True,
                'wheelchair_accessible': True,
                'parking': True
            },
            {
                'id': 2,
                'name': '海岸中学校',
                'district': '東地区',
                'address': '青森市海岸2-2-2',
                'hazards': ['津波'],
                'pet_allowed': False,
                'wheelchair_accessible': True,
                'parking': False
            },
            {'id': 3, 'name': '山の集会所'}
        ]
        self.client = application.app.test_client()

    def tearDown(self):
        application.shelters = self.original_shelters

    def test_unfiltered_search_shows_all_and_total(self):
        response = self.client.get('/shelter_search')
        self.assertEqual(response.status_code, 200)
        self.assertIn('登録されている避難所'.encode(), response.data)
        self.assertIn('3件 / 全3件'.encode(), response.data)
        self.assertIn('中央地区'.encode(), response.data)
        self.assertIn('青森市中央1-1-1'.encode(), response.data)
        for name in ('中央小学校', '海岸中学校', '山の集会所'):
            self.assertIn(name.encode(), response.data)

    def test_q_searches_name_district_and_address_case_insensitively(self):
        for query, expected in (
            ('小学校', '中央小学校'),
            ('東地区', '海岸中学校'),
            ('海岸2-2-2', '海岸中学校'),
            ('CENTRAL', None),
        ):
            if query == 'CENTRAL':
                application.shelters[0]['name'] = 'Central School'
                expected = 'Central School'
            response = self.client.get('/shelter_search', query_string={'q': query})
            self.assertEqual(response.status_code, 200)
            self.assertIn(expected.encode(), response.data)
            self.assertIn('1件 / 全3件'.encode(), response.data)

    def test_q_is_trimmed_and_retained(self):
        response = self.client.get(
            '/shelter_search',
            query_string={
                'q': '  中央  ',
                'disaster': '洪水',
                'pet': '1',
                'wheelchair': '1',
                'parking': '1'
            }
        )
        self.assertIn('「中央」の検索結果'.encode(), response.data)
        self.assertIn('value="中央"'.encode(), response.data)
        self.assertIn('value="洪水" selected'.encode(), response.data)
        for filter_name in ('pet', 'wheelchair', 'parking'):
            self.assertIn(f'name="{filter_name}" value="1" checked'.encode(), response.data)

    def test_disaster_filter(self):
        response = self.client.get('/shelter_search', query_string={'disaster': '洪水'})
        self.assertIn('中央小学校'.encode(), response.data)
        self.assertNotIn('海岸中学校'.encode(), response.data)
        self.assertIn('value="洪水" selected'.encode(), response.data)

    def test_pet_wheelchair_and_parking_filters(self):
        expected_results = {
            'pet': ('中央小学校',),
            'wheelchair': ('中央小学校', '海岸中学校'),
            'parking': ('中央小学校',)
        }
        for filter_name, expected_names in expected_results.items():
            response = self.client.get('/shelter_search', query_string={filter_name: '1'})
            for name in expected_names:
                self.assertIn(name.encode(), response.data)
            if '海岸中学校' not in expected_names:
                self.assertNotIn('海岸中学校'.encode(), response.data)
            self.assertNotIn('山の集会所'.encode(), response.data)
            self.assertIn(f'name="{filter_name}" value="1" checked'.encode(), response.data)

    def test_multiple_filters_are_combined_with_and(self):
        response = self.client.get(
            '/shelter_search',
            query_string={'disaster': '津波', 'pet': '1', 'wheelchair': '1'}
        )
        self.assertIn('条件に合う避難所がありません'.encode(), response.data)
        self.assertIn('0件 / 全3件'.encode(), response.data)

    def test_optional_fields_may_be_missing(self):
        response = self.client.get('/shelter_search')
        self.assertEqual(response.status_code, 200)
        self.assertIn('山の集会所'.encode(), response.data)

    def test_data_is_html_escaped(self):
        application.shelters[0]['name'] = '<script>alert(1)</script>'
        response = self.client.get('/shelter_search')
        self.assertIn(b'&lt;script&gt;alert(1)&lt;/script&gt;', response.data)
        self.assertNotIn(b'<script>alert(1)</script>', response.data)

    def test_clear_link_and_all_shelters_route(self):
        response = self.client.get('/shelter_search', query_string={'q': 'no match'})
        self.assertIn('href="/shelter_search">条件をクリア</a>', response.data.decode())
        listing = self.client.get('/all_shelters')
        self.assertEqual(listing.status_code, 200)
        self.assertIn('3件 / 全3件'.encode(), listing.data)
        self.assertIn('中央小学校'.encode(), listing.data)

    def test_legacy_district_api_filter_and_not_found_status_are_preserved(self):
        response = self.client.get('/shelters', query_string={'district': '東地区'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['name'] for item in response.get_json()], ['海岸中学校'])

        not_found = self.client.get('/shelters', query_string={'district': '存在しない地区'})
        self.assertEqual(not_found.status_code, 404)
        self.assertEqual(not_found.get_json(), {'error': 'No shelters found'})

    def test_api_supports_search_filters_and_combines_them_with_district(self):
        response = self.client.get(
            '/shelters',
            query_string={'q': '青森市', 'disaster': '洪水', 'pet': 'true'}
        )
        self.assertEqual([item['name'] for item in response.get_json()], ['中央小学校'])

        no_match = self.client.get(
            '/shelters',
            query_string={'district': '東地区', 'parking': '1'}
        )
        self.assertEqual(no_match.status_code, 404)

    def test_legacy_search_results_route_uses_shared_search(self):
        response = self.client.get(
            '/search_results',
            query_string={'district': '東地区', 'q': '海岸'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('海岸中学校'.encode(), response.data)
        self.assertNotIn('中央小学校'.encode(), response.data)

    def test_registered_shelter_is_available_to_search_and_list(self):
        with self.client.session_transaction() as session:
            session['logged_in'] = True
        real_open = open
        data_file = mock_open()
        with patch(
            'builtins.open',
            side_effect=lambda path, *args, **kwargs: (
                data_file() if path == application.DATA_FILE
                else real_open(path, *args, **kwargs)
            )
        ):
            response = self.client.post(
                '/shelter_register',
                data={
                    'name': '新規避難所',
                    'district': '西地区',
                    'address': '青森市西1-2-3',
                    'capacity': '0',
                    'current_occupancy': '0',
                    'hazards': ['洪水'],
                    'equipment': ['pet_allowed', 'power_supply', 'unknown_equipment'],
                    'parking': '1',
                    'facilities': '備蓄倉庫あり'
                }
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('避難所を登録しました。'.encode(), response.data)
        registered = application.shelters[-1]
        self.assertEqual(registered['capacity'], 0)
        self.assertEqual(registered['current_occupancy'], 0)
        self.assertEqual(registered['equipment'], ['pet_allowed', 'power_supply'])
        self.assertEqual(registered['facilities'], '備蓄倉庫あり')
        saved_json = ''.join(
            call.args[0] for call in data_file().write.call_args_list
        )
        self.assertEqual(json.loads(saved_json)[-1]['name'], '新規避難所')
        search = self.client.get('/shelter_search', query_string={'q': '新規避難所'})
        listing = self.client.get('/all_shelters')
        self.assertIn('新規避難所'.encode(), search.data)
        self.assertIn('4件 / 全4件'.encode(), listing.data)

    def test_occupancy_states_and_equipment_are_displayed(self):
        application.shelters[0].update({
            'capacity': 10,
            'current_occupancy': 3,
            'equipment': ['pet_allowed', 'power_supply', 'unknown_equipment'],
            'facilities': '備蓄倉庫'
        })
        application.shelters[1].update({'capacity': 5, 'current_occupancy': 5})

        response = self.client.get('/all_shelters')
        body = response.data.decode()
        self.assertIn('空きあり（残り7人）', body)
        self.assertIn('満員', body)
        self.assertIn('不明', body)
        self.assertIn('🐾', body)
        self.assertIn('ペット可', body)
        self.assertIn('🔌', body)
        self.assertIn('電源あり', body)
        self.assertIn('備蓄倉庫', body)
        self.assertNotIn('unknown_equipment', body)

        application.shelters[0].update({'capacity': 0, 'current_occupancy': 0})
        zero_response = self.client.get('/all_shelters')
        self.assertIn('0人'.encode(), zero_response.data)
        self.assertIn('満員'.encode(), zero_response.data)

        application.shelters[2].update({'capacity': 2, 'current_occupancy': 3})
        over_capacity = self.client.get('/all_shelters')
        self.assertIn('定員超過'.encode(), over_capacity.data)

    def test_empty_results_offer_research_with_district_condition(self):
        application.shelters = []
        response = self.client.get('/all_shelters')
        body = response.data.decode()
        self.assertIn('条件に合う避難所がありません', body)
        self.assertIn('条件を変えて再検索', body)

        application.shelters = self.original_shelters
        filtered = self.client.get(
            '/search_results',
            query_string={'district': '存在しない地区'}
        )
        filtered_body = filtered.data.decode()
        self.assertIn('href="/shelter_search?', filtered_body)
        research = self.client.get(
            '/shelter_search',
            query_string={'district': '存在しない地区'}
        )
        self.assertIn('value="存在しない地区"'.encode(), research.data)

    def test_occupancy_update_requires_login(self):
        page = self.client.get('/all_shelters')
        self.assertNotIn('人数の更新'.encode(), page.data)

        response = self.client.post(
            '/shelters/1/occupancy',
            data={'capacity': '8', 'current_occupancy': '2'}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])
        self.assertNotIn('capacity', application.shelters[0])

        with self.client.session_transaction() as session:
            session['logged_in'] = True
        authorized_page = self.client.get('/all_shelters')
        self.assertIn('管理操作'.encode(), authorized_page.data)

    def test_occupancy_update_persists_and_recalculates_availability(self):
        with self.client.session_transaction() as session:
            session['logged_in'] = True
        data_file = mock_open()
        with patch('builtins.open', return_value=data_file()):
            response = self.client.post(
                '/shelters/1/occupancy',
                data={
                    'capacity': '8',
                    'current_occupancy': '2',
                    'return_to': 'all_shelters'
                }
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/all_shelters')
        self.assertEqual(application.shelters[0]['capacity'], 8)
        self.assertEqual(application.shelters[0]['current_occupancy'], 2)
        saved_json = ''.join(
            call.args[0] for call in data_file().write.call_args_list
        )
        self.assertEqual(json.loads(saved_json)[0]['capacity'], 8)
        listing = self.client.get('/all_shelters')
        self.assertIn('空きあり（残り6人）'.encode(), listing.data)

    def test_invalid_occupancy_values_are_rejected_without_saving(self):
        with self.client.session_transaction() as session:
            session['logged_in'] = True
        for capacity, current_occupancy in (
            ('-1', '2'),
            ('1.5', '2'),
            ('5', ''),
        ):
            with patch('app.save_shelters') as save:
                response = self.client.post(
                    '/shelters/1/occupancy',
                    data={
                        'capacity': capacity,
                        'current_occupancy': current_occupancy
                    }
                )
            self.assertEqual(response.status_code, 400)
            self.assertIn('人数', response.data.decode())
            save.assert_not_called()
        self.assertNotIn('capacity', application.shelters[0])

    def test_invalid_registration_number_is_not_saved(self):
        with self.client.session_transaction() as session:
            session['logged_in'] = True
        with patch('app.save_shelters') as save:
            response = self.client.post(
                '/shelter_register',
                data={'name': '不正データ', 'capacity': '-4'}
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn('0以上の整数', response.data.decode())
        save.assert_not_called()
        self.assertEqual(len(application.shelters), 3)


if __name__ == '__main__':
    unittest.main()
