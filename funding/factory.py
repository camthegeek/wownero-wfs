# -*- coding: utf-8 -*-
import settings
from werkzeug.contrib.fixers import ProxyFix
from flask import Flask

app = None
sentry = None
cache = None
db_session = None
bcrypt = None


def create_app():
    global app
    global db_session
    global sentry
    global cache
    global bcrypt

    from funding.orm.connect import create_session
    db_session = create_session()

    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app)
    app.config.from_object(settings)
    app.config['PERMANENT_SESSION_LIFETIME'] = 2678400
    app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 30
    app.secret_key = settings.SECRET

    # flask-login
    from flask_login import LoginManager
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'login'

    from flask_bcrypt import Bcrypt
    bcrypt = Bcrypt(app)

    @login_manager.user_loader
    def load_user(_id):
        from funding.orm.orm import User
        return User.query.get(int(_id))

    # session init
    from funding.cache import JsonRedis, WowCache
    app.session_interface = JsonRedis(key_prefix=app.config['SESSION_PREFIX'], use_signer=False)
    cache = WowCache()

    # import routes
    from funding import routes
    from funding import api
    from funding.bin import utils_request

    app.app_context().push()
    return app
