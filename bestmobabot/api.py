"""
Game API wrapper.
"""

import contextlib
import hashlib
import json
import random
import re
import string
from datetime import datetime
from time import sleep
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, TypeVar

import requests
from requests.adapters import HTTPAdapter
from tinydb import TinyDB, where

from bestmobabot.logger import logger
from bestmobabot.responses import *
from bestmobabot.types import *

T = TypeVar('T')


class ApiError(Exception):
    def __init__(self, name: Optional[str], description: Optional[str]):
        super().__init__(name, description)
        self.name = name
        self.description = description


class AlreadyError(ApiError):
    pass


class InvalidSessionError(ApiError):
    pass


class NotEnoughError(ApiError):
    pass


class NotAvailableError(ApiError):
    pass


class NotFoundError(ApiError):
    pass


class ArgumentError(ApiError):
    pass


class InvalidResponseError(ValueError):
    pass


class InvalidSignatureError(ValueError):
    pass


class ResponseError(ValueError):
    pass


class API(contextlib.AbstractContextManager):
    GAME_URL = 'https://vk.com/app5327745'
    IFRAME_URL = 'https://i-heroes-vk.nextersglobal.com/iframe/vkontakte/iframe.new.php'
    API_URL = 'https://heroes-vk.nextersglobal.com/api/'
    USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/63.0.3239.84 Safari/537.36'
    STATE_QUERY = (where('key') == 'api')

    def __init__(self, db: TinyDB, remixsid: str):
        self.db = db
        self.remixsid = remixsid
        self.auth_token: str = None
        self.user_id: str = None
        self.request_id: int = None
        self.session_id: str = None
        self.session = requests.Session()
        self.session.mount('https://', HTTPAdapter(max_retries=5))

        # Store last API results for debugging.
        self.last_responses: List[str] = []

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.__exit__(exc_type, exc_val, exc_tb)

    def start(self, invalidate_session: bool = False):
        state: Dict[str, Any] = self.db.get(self.STATE_QUERY)
        if not invalidate_session and state:
            logger.info('🔑 Using saved credentials.')
            self.user_id = state['user_id']
            self.auth_token = state['auth_token']
            self.request_id = state['request_id']
            self.session_id = state['session_id']
            return

        logger.info('🔑 Authenticating…')
        with requests.Session() as session:
            logger.debug('🌎 Loading game page on VK.com…')
            with session.get(API.GAME_URL, cookies={'remixsid': self.remixsid}) as response:
                response.raise_for_status()
                app_page = response.text

            # Look for params variable in the script.
            match = re.search(r'var params\s?=\s?({[^\}]+\})', app_page)
            assert match, 'params not found'
            params = json.loads(match.group(1))

            # Load the proxy page and look for Hero Wars authentication token.
            logger.debug('🌎 Authenticating in Hero Wars…')
            with session.get(API.IFRAME_URL, params=params) as response:
                response.raise_for_status()
                iframe_new = response.text
            match = re.search(r'auth_key=([a-zA-Z0-9.\-]+)', iframe_new)
            assert match, f'authentication key is not found: {iframe_new}'
            self.auth_token = match.group(1)

        logger.info(f'🔑 Authentication token: {self.auth_token}')
        self.user_id = str(params['viewer_id'])
        self.request_id = 0
        self.session_id = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(14))

        self.db.upsert({
            'key': 'api',
            'user_id': self.user_id,
            'auth_token': self.auth_token,
            'request_id': self.request_id,
            'session_id': self.session_id,
        }, self.STATE_QUERY)

    def call(self, name: str, arguments: Optional[Dict[str, Any]] = None, random_sleep=True) -> Result:
        try:
            return self._call(name, arguments=arguments, random_sleep=random_sleep)
        except (InvalidSessionError, InvalidSignatureError) as e:
            logger.warning('😱 Invalid session: %s.', e)
            self.start(invalidate_session=True)
            logger.info('🔔 Retrying the call…')
            return self._call(name, arguments=arguments, random_sleep=random_sleep)

    def _call(self, name: str, *, arguments: Optional[Dict[str, Any]] = None, random_sleep=True) -> Result:
        self.request_id += 1
        self.db.upsert({'request_id': self.request_id}, self.STATE_QUERY)

        # Emulate human behavior a little bit.
        sleep_time = random.uniform(5.0, 10.0) if random_sleep and self.request_id != 1 else 0.0
        logger.info(f'🔔 #{self.request_id} {name}({arguments or {}}) in {sleep_time:.1f} seconds…')
        sleep(sleep_time)

        calls = [{'ident': name, 'name': name, 'args': arguments or {}}]
        data = json.dumps({"session": None, "calls": calls})
        headers = {
            'User-Agent': self.USER_AGENT,
            'X-Auth-Application-Id': '5327745',
            'X-Auth-Network-Ident': 'vkontakte',
            'X-Auth-Session-Id': self.session_id,
            'X-Auth-Session-Key': '',
            'X-Auth-Token': self.auth_token,
            'X-Auth-User-Id': self.user_id,
            'X-Env-Library-Version': '1',
            'X-Env-Referrer': 'unknown',
            'X-Request-Id': str(self.request_id),
            'X-Requested-With': 'ShockwaveFlash / 28.0.0.126',
            'X-Server-Time': '0',
        }
        if self.request_id == 1:
            headers['X-Auth-Session-Init'] = '1'
        headers["X-Auth-Signature"] = self.sign_request(data, headers)

        with self.session.post(self.API_URL, data=data, headers=headers) as response:
            self.last_responses.append(response.text.strip())
            response.raise_for_status()
            try:
                item = response.json()
            except ValueError:
                if response.text == 'Invalid signature':
                    raise InvalidSignatureError(response.text)
                else:
                    raise InvalidResponseError(response.text)

        # noinspection PyUnboundLocalVariable
        if 'results' in item:
            result = Result(item['results'][0]['result'])
            if result.response and 'error' in result.response:
                raise ResponseError(result.response)
            return result
        if 'error' in item:
            raise self.make_exception(item['error'])
        raise ValueError(item)

    @staticmethod
    def sign_request(data: str, headers: dict) -> str:
        fingerprint = ''.join(
            f'{key}={value}'
            for key, value in sorted(
                (key[6:].upper(), value)
                for key, value in headers.items()
                if key.startswith('X-Env')
            )
        )
        data = ':'.join((
            headers['X-Request-Id'],
            headers['X-Auth-Token'],
            headers['X-Auth-Session-Id'],
            data,
            fingerprint,
        )).encode('utf-8')
        return hashlib.md5(data).hexdigest()

    exception_classes = {
        'Already': AlreadyError,
        'common\\rpc\\exception\\InvalidSession': InvalidSessionError,
        'NotEnough': NotEnoughError,
        'NotAvailable': NotAvailableError,
        'NotFound': NotFoundError,
        'ArgumentError': ArgumentError,
    }

    @classmethod
    def make_exception(cls, error: Dict) -> 'ApiError':
        name = error.get('name')
        description = error.get('description')
        return cls.exception_classes.get(name, ApiError)(name, description)

    # User.
    # ------------------------------------------------------------------------------------------------------------------

    def register(self):
        self.call('registration', {'user': {'referrer': {'type': 'menu', 'id': '0'}}})

    def get_user_info(self) -> User:
        return User(self.call('userGetInfo').response)

    def get_all_heroes(self) -> List[Hero]:
        return list_of(Hero, self.call('heroGetAll').response)

    # Daily bonus.
    # ------------------------------------------------------------------------------------------------------------------

    def farm_daily_bonus(self) -> Reward:
        return Reward(self.call('dailyBonusFarm', {'vip': 0}).response)

    # Expeditions.
    # ------------------------------------------------------------------------------------------------------------------

    def list_expeditions(self) -> List[Expedition]:
        return list_of(Expedition, self.call('expeditionGet').response)

    def farm_expedition(self, expedition_id: ExpeditionID) -> Reward:
        return Reward(self.call('expeditionFarm', {'expeditionId': expedition_id}).response)

    def send_expedition_heroes(self, expedition_id: ExpeditionID, hero_ids: List[HeroID]) -> Tuple[datetime, Quests]:
        response = self.call('expeditionSendHeroes', {'expeditionId': expedition_id, 'heroes': hero_ids})
        return datetime.fromtimestamp(response.response['endTime']).astimezone(), response.quests

    # Quests.
    # ------------------------------------------------------------------------------------------------------------------

    def get_all_quests(self) -> Quests:
        return list_of(Quest, self.call('questGetAll').response)

    def farm_quest(self, quest_id: QuestID) -> Reward:
        return Reward(self.call('questFarm', {'questId': quest_id}).response)

    # Mail.
    # ------------------------------------------------------------------------------------------------------------------

    def get_all_mail(self) -> List[Letter]:
        return list_of(Letter, self.call('mailGetAll').response['letters'])

    def farm_mail(self, letter_ids: Iterable[LetterID]) -> Dict[str, Reward]:
        result = self.call('mailFarm', {'letterIds': list(letter_ids)})
        return {letter_id: Reward(item or {}) for letter_id, item in result.response.items()}

    # Chests.
    # ------------------------------------------------------------------------------------------------------------------

    def buy_chest(self, is_free=True, chest='town', is_pack=False) -> List[Reward]:
        result = self.call('chestBuy', {'free': is_free, 'chest': chest, 'pack': is_pack})
        return list_of(Reward, result.response['rewards'])

    # Daily gift.
    # ------------------------------------------------------------------------------------------------------------------

    def send_daily_gift(self, ids: Iterable[UserID]) -> Quests:
        return self.call('friendsSendDailyGift', {'ids': list(ids)}).quests

    # Arena.
    # ------------------------------------------------------------------------------------------------------------------

    def find_arena_enemies(self) -> List[ArenaEnemy]:
        # Random sleep is turned off because model prediction takes some time already.
        return list_of(ArenaEnemy, self.call('arenaFindEnemies', random_sleep=False).response)

    def attack_arena(self, user_id: UserID, hero_ids: Iterable[HeroID]) -> Tuple[ArenaResult, Quests]:
        result = self.call('arenaAttack', {'userId': user_id, 'heroes': list(hero_ids)})
        return ArenaResult(result.response), result.quests

    def find_grand_enemies(self) -> List[GrandArenaEnemy]:
        # Random sleep is turned off because model prediction takes some time already.
        return list_of(GrandArenaEnemy, self.call('grandFindEnemies', random_sleep=False).response)

    def attack_grand(self, user_id: UserID, hero_ids: List[List[HeroID]]) -> Tuple[ArenaResult, Quests]:
        result = self.call('grandAttack', {'userId': user_id, 'heroes': hero_ids})
        return ArenaResult(result.response), result.quests

    def farm_grand_coins(self) -> Reward:
        return Reward(self.call('grandFarmCoins').response['reward'] or {})

    # Freebie.
    # ------------------------------------------------------------------------------------------------------------------

    def check_freebie(self, gift_id: str) -> Optional[Reward]:
        response = self.call('freebieCheck', {'giftId': gift_id}).response
        return Reward(response) if response else None

    # Zeppelin gift.
    # ------------------------------------------------------------------------------------------------------------------

    def farm_zeppelin_gift(self) -> Reward:
        return Reward(self.call('zeppelinGiftFarm').response)

    # Artifact chests.
    # ------------------------------------------------------------------------------------------------------------------

    def open_artifact_chest(self, amount=1, is_free=True) -> List[Reward]:
        response = self.call('artifactChestOpen', {'amount': amount, 'free': is_free}).response
        return list_of(Reward, response['chestReward'])

    # Battles.
    # ------------------------------------------------------------------------------------------------------------------

    def get_battle_by_type(self, battle_type: BattleType, offset=0, limit=20) -> List[Replay]:
        response = self.call('battleGetByType', {'type': battle_type.value, 'offset': offset, 'limit': limit}).response
        return list_of(Replay, response['replays'])

    # Raids.
    # https://github.com/eigenein/bestmobabot/wiki/Raids
    # ------------------------------------------------------------------------------------------------------------------

    def raid_mission(self, mission_id: MissionID, times=1) -> List[Reward]:
        response = self.call('missionRaid', {'times': times, 'id': mission_id}).response
        return list_of(Reward, response)

    # Boss.
    # https://github.com/eigenein/bestmobabot/wiki/Boss
    # ------------------------------------------------------------------------------------------------------------------

    # https://heroes.cdnvideo.ru/vk/v0312/lib/lib.json.gz
    RECOMMENDED_HEROES: Dict[BossID, Set[HeroID]] = {
        '1': {'1', '4', '5', '6', '7', '9', '10', '12', '13', '17', '18', '21', '22', '23', '26', '29', '32', '33', '34', '35', '36'},
        '2': {'8', '14', '15', '19', '20', '30', '31'},
        '3': {'2', '3', '11', '16', '25', '24', '27', '28', '37', '38', '39', '40'},
        '4': {'1'},
        '5': {'1'},
        '6': {'1'},
        '7': {'1'},
        '8': {'1'},
        '9': {'1'},
    }

    def get_current_boss(self) -> List[Boss]:
        return list_of(Boss, self.call('bossGetCurrent').response)

    def attack_boss(self, boss_id: BossID, hero_ids: Iterable[HeroID]) -> Battle:
        return Battle(self.call('bossAttack', {'bossId': boss_id, 'heroes': list(hero_ids)}).response)

    def open_boss_chest(self, boss_id: BossID) -> Tuple[Reward, Quests]:
        result = self.call('bossOpenChest', {'bossId': boss_id})
        return Reward(result.response['reward']), result.quests

    # Shop.
    # ------------------------------------------------------------------------------------------------------------------

    def get_shop(self, shop_id: ShopID) -> List[ShopSlot]:
        response = self.call('shopGet', {'shopId': shop_id}).response
        return list_of(ShopSlot, response['slots'])

    def shop(self, *, slot_id: SlotID, shop_id: ShopID) -> Reward:
        return Reward(self.call('shopBuy', {'slot': slot_id, 'shopId': shop_id}).response)

    # Tower.
    # ------------------------------------------------------------------------------------------------------------------

    def get_tower_info(self) -> Tower:
        return Tower(self.call('towerGetInfo').response)

    def skip_tower_floor(self) -> Tuple[Tower, Reward]:
        response = self.call('towerSkipFloor').response
        return Tower(response['tower']), Reward(response['reward'])

    def buy_tower_buff(self, buff_id: BuffID) -> Tower:
        return Tower(self.call('towerBuyBuff', {'buffId': buff_id}).response)

    def open_tower_chest(self, number: int) -> Tuple[Reward, Quests]:
        assert number in (0, 1, 2)
        result = self.call('towerOpenChest', {'num': number})
        return Reward(result.response['reward']), result.quests

    def next_tower_floor(self) -> Tower:
        return Tower(self.call('towerNextFloor').response)


def list_of(constructor: Callable[[Any], T], items: Iterable) -> List[T]:
    """
    Used to protect from changing a response from list to dictionary and vice versa.
    This often happens with the game updates.
    """
    if isinstance(items, dict):
        items = items.values()
    return [constructor(item) for item in items]
