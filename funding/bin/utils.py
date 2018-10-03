from datetime import datetime, date
import requests
from flask import g
from flask.json import JSONEncoder
import json
import settings

def json_encoder(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError ("Type %s not serializable" % type(obj))

class Summary:
    @staticmethod
    def fetch_prices():
        if hasattr(g, 'funding_prices') and g.coin_prices:
            return g.coin_prices
        from funding.factory import cache
        cache_key = 'funding_prices'
        data = cache.get(cache_key)
        if data:
            return data
        data = {
            'coin-btc': coin_btc_value(),
            'btc-usd': price_cmc_btc_usd()
        }
        cache.set(cache_key, data=data, expiry=7200)
        g.coin_prices = data
        return data

    @staticmethod
    def fetch_stats(purge=False):
        from funding.factory import db_session
        from funding.orm.orm import Proposal, User, Comment
        from funding.factory import cache
        cache_key = 'funding_stats'
        data = cache.get(cache_key)
        if data and not purge:
            return data

        categories = settings.FUNDING_CATEGORIES
        statuses = settings.FUNDING_STATUSES.keys()

        for cat in categories:
            q = db_session.query(Proposal)
            q = q.filter(Proposal.category == cat)
            res = q.count()
            data.setdefault('cats', {})
            data['cats'][cat] = res

        for status in statuses:
            q = db_session.query(Proposal)
            q = q.filter(Proposal.status == status)
            res = q.count()
            data.setdefault('statuses', {})
            data['statuses'][status] = res

        data.setdefault('users', {})
        data['users']['count'] = db_session.query(User.id).count()
        cache.set(cache_key, data=data, expiry=300)
        return data

def price_cmc_btc_usd():
    headers = {'User-Agent': 'Mozilla/5.0 (Android 4.4; Mobile; rv:41.0) Gecko/41.0 Firefox/41.0'}
    try:
        r = requests.get('https://api.coinmarketcap.com/v2/ticker/1/?convert=USD', headers=headers)
        r.raise_for_status()
        return r.json().get('data', {}).get('quotes', {}).get('USD', {}).get('price')
    except:
        return

def coin_btc_value():
    headers = {'User-Agent': 'Mozilla/5.0 (Android 4.4; Mobile; rv:41.0) Gecko/41.0 Firefox/41.0'}
    try:
        r = requests.get('https://bittrex.com/api/v1.1/public/getmarketsummary?market=btc-' +
                         settings.COINTICKER, headers=headers)
        value = r.json()['result'][0]['High']
        return value
    except:
        return

def coin_to_usd(amt: float, usd_per_btc: float, btc_per_coin: float):
    try:
        return round(usd_per_btc / (1.0 / (amt * btc_per_coin)), 2)
    except:
        pass