from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from dotenv import load_dotenv
from urllib.parse import urlparse, urljoin
from functools import wraps
import hmac
import http.client
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')
load_dotenv(os.path.join(BASE_DIR, '.env'))

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)

# 管理者認証情報
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = os.environ.get('JMA_PREFECTURE_CODE', '020000')
AREA_NAME = os.environ.get('JMA_AREA_NAME', '青森市')

# 青森市の市区町村コード
AREA_CODE = os.environ.get('JMA_AREA_CODE', '0220100')

WARNING_URL = os.environ.get(
    'JMA_WARNING_URL',
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)
GEOCODER_URL = 'https://msearch.gsi.go.jp/address-search/AddressSearch'
GEOCODER_USER_AGENT = 'BousaiApp/1.0 (shelter location search)'
GEOCODER_TIMEOUT = 8
GEOCODER_CACHE_SECONDS = 900
GEOCODER_MIN_INTERVAL = 1.0
GEOCODER_MAX_DISTANCE_KM = 50
REGION_CENTERS = {
    ('青森県', '青森市'): (40.8246, 140.7406)
}

JST = timezone(timedelta(hours=9))
geocoder_cache = {}
geocoder_last_request = 0.0
geocoder_lock = threading.Lock()


class GeocoderResponseError(Exception):
    """地名検索サービスの応答が仕様どおりでない場合のエラー"""


# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')

SHELTER_EQUIPMENT = {
    'pet_allowed': ('🐾', 'ペット可'),
    'barrier_free': ('♿', 'バリアフリー'),
    'accessible_toilet': ('🚻', '多目的トイレ'),
    'power_supply': ('🔌', '電源あり'),
    'medical_support': ('⚕️', '医療支援'),
    'baby_support': ('🍼', '乳幼児対応')
}
SHELTER_HAZARDS = ('地震', '津波', '土砂崩れ', '洪水', '土砂災害')
SHELTER_OPENING_STATUSES = {
    'open': '開設中',
    'closed': '閉鎖中'
}

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])

def save_instructions():
    """指示ボードのデータをファイルに保存する"""
    try:
        with open(INSTRUCTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(instructions, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return (
        test_url.scheme in ('http', 'https')
        and ref_url.netloc == test_url.netloc
        and test_url.path.startswith('/')
        and not test_url.path.startswith('//')
        and not test_url.path.startswith('/\\')
    )

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        else:
            parsed = parsed.replace(tzinfo=JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError as error:
        raise ValueError('気象情報の発表時刻が不正です') from error


def parse_datetime(value):
    """API・既存データの日付をJST aware datetimeへ安全に変換する"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed = datetime.strptime(value.strip(), '%Y年%m月%d日 %H:%M')
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def format_japan_datetime(value):
    parsed = parse_datetime(value)
    return parsed.strftime('%Y年%m月%d日 %H:%M') if parsed else '日時不明'


def filter_shelters(
    district=None,
    q='',
    disaster='',
    pet=False,
    wheelchair=False,
    parking=False
):
    """検索条件をすべて適用して避難所を返す"""
    query = q.strip().casefold()
    disaster = disaster.strip()
    if disaster and disaster not in SHELTER_HAZARDS:
        return []
    required_attributes = {
        'pet_allowed': pet,
        'wheelchair_accessible': wheelchair,
        'parking': parking
    }

    results = []
    for shelter in shelters:
        if not isinstance(shelter, dict):
            continue
        if district and shelter.get('district') != district:
            continue
        if query:
            searchable_values = (
                shelter.get('name'),
                shelter.get('district'),
                shelter.get('address')
            )
            if not any(
                isinstance(value, str) and query in value.casefold()
                for value in searchable_values
            ):
                continue
        if disaster:
            hazards = shelter.get('hazards')
            if not isinstance(hazards, list) or disaster not in hazards:
                continue
        equipment = shelter.get('equipment')
        if not isinstance(equipment, list):
            equipment = []
        if any(
            required and (
                shelter.get(attribute) is not True
                and not (
                    attribute == 'pet_allowed' and 'pet_allowed' in equipment
                    or attribute == 'wheelchair_accessible' and 'barrier_free' in equipment
                )
            )
            for attribute, required in required_attributes.items()
        ):
            continue
        results.append(shelter)
    return results


def valid_coordinates(latitude, longitude):
    return (
        isinstance(latitude, (int, float))
        and not isinstance(latitude, bool)
        and -90 <= latitude <= 90
        and isinstance(longitude, (int, float))
        and not isinstance(longitude, bool)
        and -180 <= longitude <= 180
    )


def shelter_map_data(items):
    mapped = []
    unlocated_count = 0
    for shelter in items:
        if not isinstance(shelter, dict):
            continue
        latitude = shelter.get('latitude')
        longitude = shelter.get('longitude')
        if not valid_coordinates(latitude, longitude):
            unlocated_count += 1
            continue
        status = shelter.get('opening_status')
        if status in ('open', '開設中', '開設'):
            status_label, marker_color = '開設中', 'green'
        elif status in ('closed', '未開設', '閉鎖中'):
            status_label, marker_color = '未開設', 'gray'
        else:
            status_label, marker_color = '未登録', 'yellow'
        mapped.append({
            'id': shelter.get('id'),
            'name': shelter.get('name') or '名称未登録',
            'address': shelter.get('address') or '住所未登録',
            'latitude': latitude,
            'longitude': longitude,
            'status': status_label,
            'marker_color': marker_color
        })
    return mapped, unlocated_count


def distance_km(latitude_a, longitude_a, latitude_b, longitude_b):
    """2点間の大圏距離をkmで返す"""
    radius_km = 6371.0
    lat_a, lat_b = math.radians(latitude_a), math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return radius_km * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))


def geocode_place(query, prefecture, city):
    """国土地理院地名検索を利用し、地域内の候補を返す"""
    global geocoder_last_request
    query = query.strip()
    prefecture = prefecture.strip()
    city = city.strip()
    if not query or not prefecture or not city:
        raise ValueError('施設名または住所、都道府県、市区町村を入力してください。')
    if len(query) > 200 or len(prefecture) > 40 or len(city) > 40:
        raise ValueError('検索条件が長すぎます。')

    key = (query.casefold(), prefecture.casefold(), city.casefold())
    now = time.monotonic()
    cached = geocoder_cache.get(key)
    if cached and now - cached[0] < GEOCODER_CACHE_SECONDS:
        return cached[1]

    with geocoder_lock:
        now = time.monotonic()
        if now - geocoder_last_request < GEOCODER_MIN_INTERVAL:
            raise RuntimeError('検索間隔が短すぎます。少し待ってから再度お試しください。')
        geocoder_last_request = now

    url = f'{GEOCODER_URL}?{urlencode({"q": f"{prefecture}{city} {query}"})}'
    request_object = urllib.request.Request(
        url,
        headers={'User-Agent': GEOCODER_USER_AGENT}
    )
    with urllib.request.urlopen(request_object, timeout=GEOCODER_TIMEOUT) as response:
        try:
            data = json.loads(response.read())
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise GeocoderResponseError(
                '地名検索サービスから正しい形式の結果が返りませんでした。'
            ) from error
    if not isinstance(data, list):
        raise GeocoderResponseError(
            '地名検索サービスから正しい形式の結果が返りませんでした。'
        )

    center = REGION_CENTERS.get((prefecture, city))
    candidates = []
    for feature in data:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get('geometry')
        properties = feature.get('properties')
        if not isinstance(geometry, dict) or not isinstance(properties, dict):
            continue
        coordinates = geometry.get('coordinates')
        if (
            not isinstance(coordinates, list)
            or len(coordinates) < 2
            or not valid_coordinates(coordinates[1], coordinates[0])
        ):
            continue
        longitude, latitude = coordinates[:2]
        distance = (
            distance_km(center[0], center[1], latitude, longitude)
            if center else None
        )
        if distance is not None and distance > GEOCODER_MAX_DISTANCE_KM:
            continue
        candidates.append({
            'name': str(properties.get('title') or query),
            'address': str(properties.get('address') or properties.get('title') or ''),
            'latitude': latitude,
            'longitude': longitude,
            'distance_km': round(distance, 1) if distance is not None else None,
            'source': '国土地理院 地名検索'
        })
    candidates.sort(
        key=lambda candidate: (
            candidate['distance_km'] is None,
            candidate['distance_km'] if candidate['distance_km'] is not None else 0
        )
    )
    result = candidates[:5]
    geocoder_cache[key] = (time.monotonic(), result)
    if len(geocoder_cache) > 128:
        oldest = min(geocoder_cache, key=lambda item: geocoder_cache[item][0])
        del geocoder_cache[oldest]
    return result


def shelter_search_options(args):
    """検索画面とAPIで共通の避難所検索条件を読み取る"""
    def is_enabled(name):
        return args.get(name, '').strip().casefold() in ('1', 'true', 'on', 'yes')

    return {
        'q': args.get('q', '').strip(),
        'district': args.get('district', '').strip(),
        'disaster': args.get('disaster', '').strip(),
        'pet': is_enabled('pet'),
        'wheelchair': is_enabled('wheelchair'),
        'parking': is_enabled('parking')
    }


def search_shelters(args, district=None):
    """リクエスト条件を共通の避難所検索ロジックへ渡す"""
    options = shelter_search_options(args)
    if district is not None:
        options['district'] = district.strip()
    return filter_shelters(**options), options


def prepare_shelters_for_display(items):
    """旧データを含む避難所情報を一覧表示用に整形する"""
    prepared = []
    for shelter in items:
        if not isinstance(shelter, dict):
            continue
        item = dict(shelter)
        for field in ('district', 'address', 'facilities'):
            if not isinstance(item.get(field), str) or not item[field].strip():
                item[field] = None
        hazards = item.get('hazards')
        item['hazards_display'] = (
            [hazard for hazard in hazards if isinstance(hazard, str) and hazard.strip()]
            if isinstance(hazards, list) else []
        )
        item['opening_status_display'] = SHELTER_OPENING_STATUSES.get(
            item.get('opening_status'),
            '未登録'
        )

        selected_equipment = item.get('equipment')
        if not isinstance(selected_equipment, list):
            selected_equipment = []
        item['equipment_display'] = [
            {'icon': icon, 'label': label}
            for identifier, (icon, label) in SHELTER_EQUIPMENT.items()
            if identifier in selected_equipment
        ]
        for field in ('capacity', 'current_occupancy'):
            value = item.get(field)
            item[f'{field}_valid'] = (
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
            )

        if item['capacity_valid'] and item['current_occupancy_valid']:
            remaining = item['capacity'] - item['current_occupancy']
            if remaining > 0:
                item['availability'] = f'空きあり（残り{remaining}人）'
                item['availability_class'] = 'available'
            elif remaining == 0:
                item['availability'] = '満員'
                item['availability_class'] = 'full'
            else:
                item['availability'] = '定員超過'
                item['availability_class'] = 'full'
        else:
            item['availability'] = '不明'
            item['availability_class'] = 'unknown'
        prepared.append(item)
    return prepared


def parse_optional_count(raw_value):
    """空欄は未登録、入力済みなら0以上の整数に変換する"""
    value = raw_value.strip()
    if not value:
        return None
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError('人数は0以上の整数で入力してください。')
    try:
        return int(value)
    except ValueError as error:
        raise ValueError('人数：入力できる桁数を超えています。') from error


def parse_optional_coordinate(raw_value, field_name, minimum, maximum):
    value = raw_value.strip()
    if not value:
        return None
    try:
        coordinate = float(value)
    except ValueError as error:
        raise ValueError(f'{field_name}：有効な数値を入力してください。') from error
    if not math.isfinite(coordinate) or not minimum <= coordinate <= maximum:
        raise ValueError(f'{field_name}：{minimum}から{maximum}の範囲で入力してください。')
    return coordinate


def save_shelters(updated_shelters):
    """避難所JSONを保存し、成功時のみメモリ上のデータを更新する"""
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as file:
            json.dump(updated_shelters, file, ensure_ascii=False, indent=2)
    except OSError:
        return False
    shelters[:] = updated_shelters
    return True


def validate_shelter_form(form, opening_status_default=''):
    """避難所の登録・編集フォームを検証し、保存データを返す"""
    name = form.get('name', '').strip()
    if not name:
        raise ValueError('避難所名：必須項目です。入力してください。')

    try:
        capacity = parse_optional_count(form.get('capacity', ''))
        current_occupancy = parse_optional_count(
            form.get('current_occupancy', '')
        )
    except ValueError as error:
        raise ValueError(f'人数：{error}') from error

    opening_status = form.get('opening_status', opening_status_default).strip()
    if opening_status not in SHELTER_OPENING_STATUSES and opening_status != '':
        raise ValueError('開設状況：「開設中」または「閉鎖中」を選択してください。')
    latitude = parse_optional_coordinate(
        form.get('latitude', ''), '緯度', -90, 90
    )
    longitude = parse_optional_coordinate(
        form.get('longitude', ''), '経度', -180, 180
    )
    if (latitude is None) != (longitude is None):
        raise ValueError('位置情報：緯度と経度を両方入力してください。')

    getlist = getattr(form, 'getlist', None)
    hazards_input = getlist('hazards') if callable(getlist) else form.get('hazards', [])
    equipment_input = getlist('equipment') if callable(getlist) else form.get('equipment', [])
    if isinstance(hazards_input, str):
        hazards_input = [hazards_input]
    if isinstance(equipment_input, str):
        equipment_input = [equipment_input]
    hazards = [item for item in hazards_input if item in SHELTER_HAZARDS]
    equipment = [item for item in equipment_input if item in SHELTER_EQUIPMENT]
    return {
        'name': name,
        'district': form.get('district', '').strip(),
        'address': form.get('address', '').strip(),
        'capacity': capacity,
        'current_occupancy': current_occupancy,
        'hazards': hazards,
        'opening_status': opening_status or None,
        'latitude': latitude,
        'longitude': longitude,
        'equipment': equipment,
        'facilities': form.get('facilities', '').strip(),
        'pet_allowed': 'pet_allowed' in equipment,
        'wheelchair_accessible': 'barrier_free' in equipment,
        'parking': form.get('parking') == '1'
    }


def shelter_form_context(form=None, shelter=None, error=None, success=None):
    """避難所登録・編集画面の共通テンプレート変数を作る"""
    values = dict(shelter or {})
    if form is not None:
        for field in (
            'name', 'district', 'address', 'capacity', 'current_occupancy',
            'opening_status', 'latitude', 'longitude', 'facilities', 'parking'
        ):
            values[field] = form.get(field, '')
        getlist = getattr(form, 'getlist', None)
        if callable(getlist):
            selected_equipment = getlist('equipment')
            selected_hazards = getlist('hazards')
        else:
            selected_equipment = form.get('equipment', [])
            selected_hazards = form.get('hazards', [])
            if isinstance(selected_equipment, str):
                selected_equipment = [selected_equipment]
            if isinstance(selected_hazards, str):
                selected_hazards = [selected_hazards]
    else:
        if values.get('opening_status') not in SHELTER_OPENING_STATUSES:
            values['opening_status'] = {
                '開設中': 'open',
                '開設': 'open',
                '未開設': 'closed',
                '閉鎖中': 'closed'
            }.get(values.get('status'), '')
        selected_equipment = values.get('equipment', [])
        selected_hazards = values.get('hazards', [])
    return {
        'form': values,
        'selected_equipment': selected_equipment,
        'selected_hazards': selected_hazards,
        'equipment_options': SHELTER_EQUIPMENT,
        'hazard_options': SHELTER_HAZARDS,
        'opening_status_options': SHELTER_OPENING_STATUSES,
        'error': bool(error),
        'message': error or success,
        'success': bool(success),
        'is_edit': shelter is not None,
        'shelter_id': shelter.get('id') if shelter else None,
        'prefecture': os.environ.get('JMA_PREFECTURE_NAME', '青森県'),
        'city': AREA_NAME,
        'map_center': [40.8246, 140.7406]
    }


def parse_area_warnings(warning_data):
    """気象庁の新形式JSONから対象市区町村の発表・継続中の情報を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    matching_reports = []
    for report in warning_data:
        if not isinstance(report, dict):
            continue
        report_datetime = report.get("reportDatetime")
        parsed_report_datetime = parse_datetime(report_datetime)
        warning = report.get("warning")
        if not isinstance(warning, dict):
            continue
        class20_items = warning.get("class20Items", [])
        if not isinstance(class20_items, list):
            continue
        area = next(
            (
                item for item in class20_items
                if isinstance(item, dict)
                and item.get("areaCode") == AREA_CODE
            ),
            None
        )
        if not area or not parsed_report_datetime:
            continue
        kinds = area.get("kinds", [])
        if isinstance(kinds, list):
            matching_reports.append((parsed_report_datetime, report_datetime, kinds))

    if not matching_reports:
        return [], ''

    _, latest_report_datetime, kinds = max(
        matching_reports,
        key=lambda report: report[0]
    )
    warnings = []
    seen_codes = set()
    for kind in kinds:
        if not isinstance(kind, dict):
            continue
        status = kind.get("status", "")
        code = str(kind.get("code", ""))
        if status not in ("発表", "継続") or not code or code in seen_codes:
            continue

        warnings.append({
            "name": WARNING_CODES.get(
                code,
                f"不明な警報・注意報 (コード: {code})"
            ),
            "code": code,
            "status": status
        })
        seen_codes.add(code)
    return warnings, latest_report_datetime


def get_weather_warnings():
    """対象市区町村の警報・注意報を取得する"""
    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        return {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time()
        }

    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        ValueError,
        TypeError
    ):
        app.logger.exception('気象庁の警報・注意報データを取得できませんでした')
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


def active_resident_instructions():
    active_statuses = {'未対応', '対応中', '発信中'}
    return sorted(
        (
            item for item in instructions
            if isinstance(item, dict)
            and item.get('target') == '住民'
            and item.get('status') in active_statuses
        ),
        key=lambda item: (
            parse_datetime(item.get('updated_at') or item.get('created_at'))
            or datetime.min.replace(tzinfo=JST),
            str(item.get('id', ''))
        ),
        reverse=True
    )


def build_disaster_information():
    weather = get_weather_warnings()
    events = []
    for warning in weather.get('warnings', []):
        events.append({
            'kind': '気象',
            'icon': '🌧️',
            'content': warning['name'],
            'status': warning['status'],
            'source': '気象庁',
            'received_at': weather.get('report_time', ''),
            'timestamp': parse_datetime(weather.get('report_time'))
        })
    for instruction in active_resident_instructions():
        received_at = instruction.get('updated_at') or instruction.get('created_at')
        shelter = instruction.get('shelter', '')
        content = instruction.get('content', '')
        if shelter or '避難' in content:
            kind, icon = '避難', '🏠'
        elif '道路' in content or '通行' in content:
            kind, icon = '道路', '🚧'
        else:
            kind, icon = '対応', '📢'
        events.append({
            'kind': kind,
            'icon': icon,
            'content': content,
            'status': instruction.get('status') or '状態未登録',
            'source': '指示・発信ボード',
            'received_at': format_japan_datetime(received_at),
            'timestamp': parse_datetime(received_at),
            'shelter': shelter,
            'district': instruction.get('district', ''),
            'simple_content': instruction.get('simple_content', ''),
            'urgency': instruction.get('urgency', '')
        })
    events.sort(
        key=lambda item: item['timestamp'] or datetime.min.replace(tzinfo=JST),
        reverse=True
    )
    for event in events:
        event['timestamp'] = (
            event['timestamp'].isoformat() if event['timestamp'] else ''
        )
    return {
        'updated_at': get_japan_time(),
        'warnings': weather.get('warnings', []),
        'weather_error': bool(weather.get('error')),
        'weather_report_time': weather.get('report_time', '不明'),
        'weather_last_fetch_time': weather.get('last_fetch_time', '不明'),
        'events': events,
        'sources': {
            '気象庁': '取得失敗' if weather.get('error') else '接続中',
            '指示・発信ボード': '接続中',
            '道路・現地情報': '情報源未接続'
        }
    }


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    resident_notices = active_resident_instructions()
    mapped_shelters, unlocated_count = shelter_map_data(shelters)
    return render_template(
        'index.html',
        resident_notices=resident_notices,
        area_name=AREA_NAME,
        map_center=[40.8246, 140.7406],
        mapped_shelters=mapped_shelters,
        unlocated_count=unlocated_count,
        is_logged_in=session.get('logged_in', False)
    )

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        password = request.form.get('password', '')

        if ADMIN_PASSWORD and hmac.compare_digest(password, ADMIN_PASSWORD):
            session['logged_in'] = True
            session['username'] = ADMIN_USERNAME
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        message = (
            '管理者認証が設定されていません。管理者に連絡してください。'
            if not ADMIN_PASSWORD
            else 'パスワードが正しくありません。'
        )
        return render_template(
            'login.html',
            error=True,
            message=message,
            next=next_url
        ), 503 if not ADMIN_PASSWORD else 401

    # ログイン済みの場合は指定されたページにリダイレクト
    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# 避難所登録ページ
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    if request.method == 'POST':
        try:
            new_shelter = validate_shelter_form(
                request.form,
                opening_status_default='open'
            )
        except ValueError as error:
            return render_template(
                'shelter_register.html',
                **shelter_form_context(form=request.form, error=str(error))
            ), 400

        duplicate = next(
            (
                item for item in shelters
                if isinstance(item, dict)
                and isinstance(item.get('name'), str)
                and item['name'].strip().casefold() == new_shelter['name'].casefold()
            ),
            None
        )
        if duplicate:
            return render_template(
                'shelter_register.html',
                **shelter_form_context(
                    form=request.form,
                    error='避難所名：同じ名前の避難所がすでに登録されています。'
                )
            ), 400

        numeric_ids = [
            shelter.get('id') for shelter in shelters
            if isinstance(shelter, dict)
            and isinstance(shelter.get('id'), int)
            and not isinstance(shelter.get('id'), bool)
        ]
        new_shelter['id'] = max(numeric_ids, default=0) + 1
        updated_shelters = shelters + [new_shelter]

        if not save_shelters(updated_shelters):
            return render_template(
                'shelter_register.html',
                **shelter_form_context(
                    form=request.form,
                    error='避難所情報：保存できませんでした。時間をおいて再度お試しください。'
                )
            ), 500

        return render_template(
            'shelter_register.html',
            **shelter_form_context(success='避難所を登録しました。')
        )

    return render_template(
        'shelter_register.html',
        **shelter_form_context(form={'opening_status': 'open'})
    )


@app.route('/shelters/<int:shelter_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_shelter(shelter_id):
    shelter = next(
        (
            item for item in shelters
            if isinstance(item, dict) and item.get('id') == shelter_id
        ),
        None
    )
    if shelter is None:
        return '避難所が見つかりません。', 404

    if request.method == 'POST':
        try:
            updated_shelter = validate_shelter_form(request.form)
        except ValueError as error:
            return render_template(
                'shelter_register.html',
                **shelter_form_context(
                    form=request.form,
                    shelter=shelter,
                    error=str(error)
                )
            ), 400

        duplicate = next(
            (
                item for item in shelters
                if isinstance(item, dict)
                and item is not shelter
                and isinstance(item.get('name'), str)
                and item['name'].strip().casefold() == updated_shelter['name'].casefold()
            ),
            None
        )
        if duplicate:
            return render_template(
                'shelter_register.html',
                **shelter_form_context(
                    form=request.form,
                    shelter=shelter,
                    error='避難所名：同じ名前の避難所がすでに登録されています。'
                )
            ), 400

        updated_shelter['id'] = shelter_id
        updated_shelter = {**shelter, **updated_shelter}
        updated_shelters = [
            updated_shelter if item is shelter else item
            for item in shelters
        ]
        if not save_shelters(updated_shelters):
            return render_template(
                'shelter_register.html',
                **shelter_form_context(
                    form=request.form,
                    shelter=shelter,
                    error='避難所情報：保存できませんでした。時間をおいて再度お試しください。'
                )
            ), 500
        return render_template(
            'shelter_register.html',
            **shelter_form_context(
                shelter=updated_shelter,
                success='避難所情報を更新しました。'
            )
        )

    return render_template(
        'shelter_register.html',
        **shelter_form_context(shelter=shelter)
    )


@app.route('/shelter_management')
@login_required
def shelter_management():
    mapped_shelters, unlocated_count = shelter_map_data(shelters)
    return render_template(
        'shelter_management.html',
        shelters=shelters,
        mapped_shelters=mapped_shelters,
        unlocated_count=unlocated_count,
        area_name=AREA_NAME,
        prefecture=os.environ.get('JMA_PREFECTURE_NAME', '青森県'),
        city=AREA_NAME,
        map_center=[40.8246, 140.7406]
    )


@app.route('/api/geocode', methods=['POST'])
@login_required
def api_geocode():
    try:
        candidates = geocode_place(
            request.form.get('query', ''),
            request.form.get('prefecture', ''),
            request.form.get('city', '')
        )
    except ValueError as error:
        return jsonify({'error': str(error)}), 400
    except RuntimeError as error:
        return jsonify({'error': str(error)}), 429
    except GeocoderResponseError as error:
        app.logger.error('国土地理院の地名検索応答を解析できませんでした: %s', error)
        return jsonify({'error': str(error)}), 502
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        ValueError,
        TypeError
    ):
        app.logger.exception('国土地理院の地名検索に失敗しました')
        return jsonify({
            'error': '地名検索サービスを利用できません。地図クリックで位置を指定できます。'
        }), 502
    return jsonify({
        'source': '国土地理院 地名検索',
        'candidates': candidates,
        'message': (
            '候補地点が見つかりませんでした。地図をクリックして位置を指定してください。'
            if not candidates else ''
        )
    })


@app.route('/shelters/<int:shelter_id>/occupancy', methods=['POST'])
@login_required
def update_shelter_occupancy(shelter_id):
    shelter = next(
        (
            item for item in shelters
            if isinstance(item, dict) and item.get('id') == shelter_id
        ),
        None
    )
    if shelter is None:
        return '避難所が見つかりません。', 404

    try:
        capacity = parse_optional_count(request.form.get('capacity', ''))
        current_occupancy = parse_optional_count(
            request.form.get('current_occupancy', '')
        )
        if capacity is None or current_occupancy is None:
            raise ValueError('収容人数と現在の避難者数を入力してください。')
    except ValueError as error:
        results, options = search_shelters(request.form)
        return render_template(
            'search_results.html',
            results=prepare_shelters_for_display(results),
            total_count=len(shelters),
            heading='検索結果' if any(options.values()) else '登録されている避難所',
            options=options,
            is_logged_in=True,
            message=str(error),
            error=True
        ), 400

    updated_shelters = [
        dict(item, capacity=capacity, current_occupancy=current_occupancy)
        if item is shelter else item
        for item in shelters
    ]
    if not save_shelters(updated_shelters):
        results, options = search_shelters(request.form)
        return render_template(
            'search_results.html',
            results=prepare_shelters_for_display(results),
            total_count=len(shelters),
            heading='検索結果' if any(options.values()) else '登録されている避難所',
            options=options,
            is_logged_in=True,
            message='避難所情報を保存できませんでした。',
            error=True
        ), 500

    if request.form.get('return_to') == 'all_shelters':
        destination = url_for('all_shelters')
    else:
        destination_args = {
            name: request.form.get(name, '')
            for name in ('q', 'district', 'disaster')
        }
        destination_args.update({
            name: '1' for name in ('pet', 'wheelchair', 'parking')
            if request.form.get(name) == '1'
        })
        endpoint = (
            'shelter_search'
            if request.form.get('return_to') == 'shelter_search'
            else 'search_results'
        )
        destination = url_for(endpoint, **destination_args)
    return redirect(destination)

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    results, options = search_shelters(request.args)
    has_filters = bool(
        options['q'] or options['district'] or options['disaster'] or options['pet']
        or options['wheelchair'] or options['parking']
    )
    if options['q']:
        heading = f'「{options["q"]}」の検索結果'
    elif has_filters:
        heading = '検索結果'
    else:
        heading = '登録されている避難所'
    return render_template(
        'shelter_search.html',
        results=prepare_shelters_for_display(results),
        total_count=len(shelters),
        options=options,
        heading=heading,
        has_filters=has_filters,
        districts=sorted({
            item.get('district') for item in shelters
            if isinstance(item, dict) and isinstance(item.get('district'), str) and item.get('district').strip()
        }),
        hazard_options=SHELTER_HAZARDS,
        is_logged_in=session.get('logged_in', False)
    )

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return render_template(
        'search_results.html',
        results=prepare_shelters_for_display(shelters),
        total_count=len(shelters),
        heading='登録されている避難所',
        options={},
        is_logged_in=session.get('logged_in', False)
    )


# 指示・発信ボード
@app.route('/board', methods=['GET', 'POST'])
@login_required
def board():
    targets = ('住民', '防災課', '消防団本部', '道路管理課')
    districts = (
        '市内全域', '片瀬地区', '鵠沼地区', '辻堂地区', '村岡地区', '藤沢地区',
        '明治地区', '善行地区', '湘南大庭地区', '六会地区', '湘南台地区',
        '遠藤地区', '御所見地区'
    )
    urgencies = ('高', '中', '低')
    statuses = ('未対応', '対応中', '完了', '解除')
    sort_order = request.args.get('sort', 'newest')
    if sort_order not in ('newest', 'urgency'):
        sort_order = 'newest'

    if sort_order == 'urgency':
        sorted_instructions = sorted(
            instructions,
            key=lambda item: (
                urgencies.index(item.get('urgency')) if item.get('urgency') in urgencies else len(urgencies),
                -item.get('id', 0)
            )
        )
    else:
        sorted_instructions = sorted(
            instructions,
            key=lambda item: item.get('id', 0),
            reverse=True
        )
    resident_instructions = [
        item for item in sorted_instructions
        if item.get('target') == '住民'
    ]

    def render_board(message=None, error=False):
        return render_template(
            'board.html',
            instructions=resident_instructions,
            shelters=shelters,
            districts=districts,
            message=message,
            error=error,
            sort_order=sort_order
        )

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'create':
            content = request.form.get('content', '').strip()
            target = request.form.get('target', '')
            district = request.form.get('district', '')
            urgency = request.form.get('urgency', '')
            shelter = request.form.get('shelter', '').strip()
            if not content or target not in targets or district not in districts or urgency not in urgencies:
                return render_board('宛先・対象地区・緊急度・指示内容を確認してください。', error=True), 400
            if shelter and shelter not in {item.get('name') for item in shelters}:
                return render_board('避難先を選び直してください。', error=True), 400

            now = get_japan_time()
            numeric_ids = [item.get('id', 0) for item in instructions if isinstance(item.get('id'), int)]
            new_instruction = {
                'id': max(numeric_ids, default=0) + 1,
                'target': target,
                'district': district,
                'content': content,
                'simple_content': request.form.get('simple_content', '').strip(),
                'shelter': shelter,
                'urgency': urgency,
                'status': '未対応',
                'created_at': now,
                'updated_at': now
            }
            instructions.insert(0, new_instruction)
            if not save_instructions():
                instructions.pop(0)
                return render_board('指示を保存できませんでした。', error=True), 500
            return redirect(url_for('board', result='created'))

        if action == 'update_status':
            instruction_id = request.form.get('instruction_id', type=int)
            new_status = request.form.get('status', '')
            instruction = next((item for item in instructions if item.get('id') == instruction_id), None)
            if instruction is None or new_status not in statuses:
                return render_board('対応状況を更新できませんでした。', error=True), 400

            previous_status = instruction.get('status')
            previous_updated_at = instruction.get('updated_at')
            instruction['status'] = new_status
            instruction['updated_at'] = get_japan_time()
            if not save_instructions():
                instruction['status'] = previous_status
                instruction['updated_at'] = previous_updated_at
                return render_board('対応状況を保存できませんでした。', error=True), 500
            return redirect(url_for('board', result='updated'))

    result_messages = {
        'created': '指示を登録しました。',
        'updated': '対応状況を更新しました。'
    }
    return render_board(result_messages.get(request.args.get('result')))

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    results, options = search_shelters(
        request.args,
        district=request.args.get('district')
    )
    if options['q']:
        heading = f'「{options["q"]}」の検索結果'
    elif any((options['disaster'], options['pet'], options['wheelchair'], options['parking'], request.args.get('district'))):
        heading = '検索結果'
    else:
        heading = '登録されている避難所'
    return render_template(
        'search_results.html',
        results=prepare_shelters_for_display(results),
        total_count=len(shelters),
        heading=heading,
        options=options,
        is_logged_in=session.get('logged_in', False)
    )

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    results, _ = search_shelters(
        request.args,
        district=request.args.get('district')
    )

    if not results:
        # 見つからなければエラー JSON を返す
        return jsonify({'error': 'No shelters found'}), 404

    # 見つかったらリストを JSON で返す
    return jsonify(results)

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_weather_warnings())


@app.route('/api/disaster_information')
def api_disaster_information():
    """気象警報と有効な住民向け指示を新着順で返す"""
    return jsonify(build_disaster_information())

if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1', port=5000)
