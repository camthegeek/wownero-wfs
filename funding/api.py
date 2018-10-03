from datetime import datetime
from flask import request, redirect, Response, abort, render_template, url_for, flash, make_response, send_from_directory, jsonify
from flask_login import login_user , logout_user , current_user , login_required, current_user
from flask_yoloapi import endpoint, parameter
import settings
from funding.factory import app, db_session
from funding.orm.orm import Proposal, User

@app.route('/api/1/proposals')
@endpoint.api(
    parameter('status', type=int, location='args', default=1),
    parameter('cat', type=str, location='args'),
    parameter('limit', type=int, location='args', default=20),
    parameter('offset', type=int, location='args', default=0)
)
def api_proposals_get(status, cat, limit, offset):
    try:
        proposals = Proposal.find_by_args(status=status, cat=cat, limit=limit, offset=offset)
    except Exception as ex:
        print(ex)
        return 'error', 500
    return [p.json for p in proposals]


@app.route('/api/1/convert/' + settings.COINNAME + '-usd')
@endpoint.api(
    parameter('amount', type=int, location='args', required=True)
)
def api_coin_usd(amount):
    from funding.bin.utils import Summary, coin_to_usd
    prices = Summary.fetch_prices()
    return jsonify(usd=coin_to_usd(amt=amount, btc_per_coin=prices['coin-btc'], usd_per_btc=prices['btc-usd']))
