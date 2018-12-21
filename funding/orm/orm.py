from datetime import datetime
import sqlalchemy as sa
from sqlalchemy.orm import scoped_session, sessionmaker, relationship
from sqlalchemy.ext.declarative import declarative_base
import settings
import pyqrcode
import os
import png
from hashlib import md5

base = declarative_base(name="Model")

class User(base):
    __tablename__ = "users"
    id = sa.Column('user_id', sa.Integer, primary_key=True)
    username = sa.Column(sa.String(20), unique=True, index=True)
    password = sa.Column(sa.String(60))
    email = sa.Column(sa.String(50), unique=True)
    registered_on = sa.Column(sa.DateTime)
    admin = sa.Column(sa.Boolean, default=False)
    last_online = sa.Column(sa.DateTime, default=datetime.utcnow)
    proposals = relationship('Proposal', back_populates="user")
    comments = relationship("Comment", back_populates="user")

    def __init__(self, username, password, email):
        from funding.factory import bcrypt
        self.username = username
        self.password = bcrypt.generate_password_hash(password).decode('utf8')
        self.email = email
        self.registered_on = datetime.utcnow()

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        return False

    @property
    def is_admin(self):
        return self.admin

    def get_id(self):
        return self.id

    def __repr__(self):
        return self.username

    @classmethod
    def add(cls, username, password, email):
        from funding.factory import db_session
        from funding.validation import val_username, val_email

        try:
            # validate incoming username/email
            val_username(username)
            val_email(email)

            user = User(username, password, email)
            db_session.add(user)
            db_session.commit()
            db_session.flush()
            return user
        except Exception as ex:
            db_session.rollback()
            raise
    
    def avatar(self, size):
        digest = md5(self.email.lower().encode('utf-8')).hexdigest()
        return 'https://www.gravatar.com/avatar/{}?d=identicon&s={}'.format(digest, size)

    @staticmethod
    def edit(email: str, password: str): 
        from funding.factory import db_session
        from funding.factory import bcrypt
        try:
            db_session.query(User).filter(User.email == email).update({'password': bcrypt.generate_password_hash(password).decode('utf8')})
            db_session.commit()
            db_session.flush()
        except Exception as ex:
            db_session.rollback()
            raise


class Proposal(base):
    __tablename__ = "proposals"
    id = sa.Column(sa.Integer, primary_key=True)
    headline = sa.Column(sa.VARCHAR, nullable=False)
    content = sa.Column(sa.VARCHAR, nullable=False)
    category = sa.Column(sa.VARCHAR, nullable=False)
    date_added = sa.Column(sa.TIMESTAMP, default=datetime.now)
    html = sa.Column(sa.VARCHAR)
    last_edited = sa.Column(sa.TIMESTAMP)
    generated_qr = sa.Column(sa.Boolean, default=False)

    # the FFS target
    funds_target = sa.Column(sa.Float, nullable=False)

    # the FFS progress (cached)
    funds_progress = sa.Column(sa.Float, nullable=False, default=0)

    # the FFS withdrawal amount (paid to the author)
    funds_withdrew = sa.Column(sa.Float, nullable=False, default=0)

    # the FFS receiving and withdrawal addresses
    addr_donation = sa.Column(sa.VARCHAR)
    addr_receiving = sa.Column(sa.VARCHAR)

    # proposal status:
    # 0: disabled
    # 1: proposed
    # 2: funding required
    # 3: wip
    # 4: completed
    status = sa.Column(sa.INTEGER, default=1)

    user_id = sa.Column(sa.Integer, sa.ForeignKey('users.user_id'))
    user = relationship("User", back_populates="proposals")

    payouts = relationship("Payout", back_populates="proposal")
    comments = relationship("Comment", back_populates="proposal", lazy='select')

    def __init__(self, headline, content, category, user: User):
        if not headline or not content:
            raise Exception('faulty proposal')
        self.headline = headline
        self.content = content
        self.user_id = user.id
        if category not in settings.FUNDING_CATEGORIES:
            raise Exception('wrong category')
        self.category = category

    @property
    def json(self):
        return {
            'date_posted_epoch': self.date_added.strftime('%s'),
            'date_posted': self.date_added.strftime('%b %d %Y %H:%M:%S'),
            'headline': self.headline,
            'content_markdown': self.content,
            'category': self.category,
            'funds_target': self.funds_target,
            'funded_pct': self.funds_progress,
            'addr_donation': self.addr_donation,
            'status': self.status,
            'user': self.user.username,
            'id': self.id
        }

    @classmethod
    def find_by_id(cls, pid: int):
        from funding.factory import db_session
        q = cls.query
        q = q.filter(Proposal.id == pid)
        result = q.first()
        if not result:
            return

        # check if we have a valid addr_donation generated. if not, make one.
        if not result.addr_donation and result.status >= 2:
            Proposal.generate_donation_addr(result)

        return result

    @property
    def funds_target_usd(self):
        from funding.bin.utils import Summary, coin_to_usd
        prices = Summary.fetch_prices()
        if not prices:
            return
        return coin_to_usd(amt=self.funds_target, btc_per_coin=prices['coin-btc'], usd_per_btc=prices['btc-usd'])

    @property
    def comment_count(self):
        from funding.factory import db_session
        q = db_session.query(sa.func.count(Comment.id))
        q = q.filter(Comment.proposal_id == self.id)
        return q.scalar()

    def get_comments(self):
        from funding.factory import db_session
        q = db_session.query(Comment)
        q = q.filter(Comment.proposal_id == self.id)
        q = q.filter(Comment.replied_to == None)
        q = q.order_by(Comment.date_added.desc())
        comments = q.all()

        for c in comments:
            q = db_session.query(Comment)
            q = q.filter(Comment.proposal_id == self.id)
            q = q.filter(Comment.replied_to == c.id)
            _c = q.all()
            setattr(c, 'comments', _c)

        setattr(self, '_comments', comments)
        return self

    @property
    def balance(self):
        """This property retrieves the current funding status
        of this proposal. It uses Redis cache to not spam the
        daemon too much. Returns a nice dictionary containing
        all relevant proposal funding info"""
        from funding.bin.utils import Summary, coin_to_usd
        from funding.factory import cache, db_session
        rtn = {'sum': 0.0, 'txs': [], 'pct': 0.0}

        cache_key = 'coin_balance_pid_%d' % self.id
        data = cache.get(cache_key)
        if not data:
            from funding.bin.daemon import Daemon
            try:
                data = Daemon().get_transfers_in(index=self.id, proposal_id=self.id)
                if not isinstance(data, dict):
                    print('error; get_transfers_in; %d' % self.id)
                    return rtn
                cache.set(cache_key, data=data, expiry=300)
            except:
                print('error; get_transfers_in; %d' % self.id)
                return rtn

        prices = Summary.fetch_prices()
        for tx in data['txs']:
            if prices:
                tx['amount_usd'] = coin_to_usd(amt=tx['amount_human'], btc_per_coin=prices['coin-btc'], usd_per_btc=prices['btc-usd'])
            tx['datetime'] = datetime.fromtimestamp(tx['timestamp'])

        if data.get('sum', 0.0):
            data['pct'] = 100 / float(self.funds_target / data.get('sum', 0.0))
            data['available'] = data['sum']
        else:
            data['pct'] = 0.0
            data['available'] = 0.0

        if data['pct'] != self.funds_progress:
            self.funds_progress = data['pct']
            db_session.commit()
            db_session.flush()

        if data['available']:
            data['remaining_pct'] = 100 / float(data['sum'] / data['available'])
        else:
            data['remaining_pct'] = 0.0

        return data

    @property
    def spends(self):
        from funding.bin.utils import Summary, coin_to_usd
        from funding.factory import cache, db_session
        rtn = {'sum': 0.0, 'txs': [], 'pct': 0.0}

        cache_key = 'coin_spends_pid_%d' % self.id
        data = cache.get(cache_key)
        if not data:
            from funding.bin.daemon import Daemon
            try:
                data = Daemon().get_transfers_out(index=self.id, proposal_id=self.id)
                if not isinstance(data, dict):
                    print('error; get_transfers_out; %d' % self.id)
                    return rtn
                cache.set(cache_key, data=data, expiry=300)
            except:
                print('error; get_transfers_out; %d' % self.id)
                return rtn

        prices = Summary.fetch_prices()
        for tx in data['txs']:
            if prices:
                tx['amount_usd'] = coin_to_usd(amt=tx['amount_human'], btc_per_coin=prices['coin-btc'], usd_per_btc=prices['btc-usd'])
            tx['datetime'] = datetime.fromtimestamp(tx['timestamp'])

        if data.get('sum', 0.0):
            data['pct'] = 100 / float(self.funds_target / data.get('sum', 0.0))
            data['spent'] = data['sum']
        else:
            data['pct'] = 0.0
            data['spent'] = 0.0

        if data['spent']:
            data['remaining_pct'] = 100 / float(data['sum'] / data['spent'])
        else:
            data['remaining_pct'] = 0.0

        return data

    @staticmethod
    def generate_donation_addr(cls):
        from funding.factory import db_session
        from funding.bin.daemon import Daemon
        if cls.addr_donation:
            return cls.addr_donation

        try:
            addr_donation = Daemon().get_address(index=cls.id, proposal_id=cls.id)
            if not isinstance(addr_donation, dict):
                raise Exception('get_address, needs dict; %d' % cls.id)
        except Exception as ex:
            print('error: %s' % str(ex))
            return

        if addr_donation.get('address'):
            cls.addr_donation = addr_donation['address']
            db_session.commit()
            db_session.flush()
            return addr_donation['address']

    #super simple possibly faulty way of generating qr codes. but it works /shrug
    #qr codes are saved to funding/static/qr
    #a logo can be added directly to the middle -- see variable: logod below
    #will turn that into a global setting later on.
    @staticmethod
    def generate_donation_addr_qr(donation_addr, pid):
        from funding.factory import db_session
        from PIL import Image
        url = pyqrcode.create(donation_addr, error='L')
        filename = os.path.abspath('funding/static/qr/'+donation_addr+'.png')
        logod = os.path.abspath('funding/static/aeon.jpg')
        url.png(filename,scale=10)
        im = Image.open(filename)
        im = im.convert("RGBA")
        width, height = im.size
        logo_size = 76
        logo = Image.open(logod)
        xmin = ymin = int((width / 2) - (logo_size / 2))
        xmax = ymax = int((width / 2) + (logo_size / 2))
        logo = logo.resize((xmax - xmin, ymax - ymin))
        im.paste(logo, (xmin, ymin, xmax, ymax))
        im.save(filename)
        p = Proposal.find_by_id(pid)
        p.generated_qr = True
        db_session.commit()
        db_session.flush()

    @staticmethod  
    def generate_proposal_subaccount(pid):
        from funding.bin.daemon import Daemon

        try:
            Daemon().create_account(pid)
        except Exception as ex:
            print('error: %s' % str(ex))
            return

    @classmethod
    def find_by_args(cls, status: int = None, cat: str = None, limit: int = 20, offset=0):
        from funding.factory import db_session
        if isinstance(status, int) and status not in settings.FUNDING_STATUSES.keys():
            raise NotImplementedError('invalid status')
        if isinstance(cat, str) and cat not in settings.FUNDING_CATEGORIES:
            raise NotImplementedError('invalid cat')

        q = cls.query
        if isinstance(status, int):
            q = q.filter(Proposal.status == status)
        if cat:
            q = q.filter(Proposal.category == cat)
        q = q.order_by(Proposal.date_added.desc())
        q = q.limit(limit)
        if isinstance(offset, int):
            q = q.offset(offset)

        return q.all()

    @classmethod
    def search(cls, key: str):
        key_ilike = '%' + key.replace('%', '') + '%'
        q = Proposal.query
        q = q.filter(sa.or_(
            Proposal.headline.ilike(key_ilike),
            Proposal.content.ilike(key_ilike)))
        return q.all()


class Payout(base):
    __tablename__ = "payouts"
    id = sa.Column(sa.Integer, primary_key=True)

    proposal_id = sa.Column(sa.Integer, sa.ForeignKey('proposals.id'))
    proposal = relationship("Proposal", back_populates="payouts")

    amount = sa.Column(sa.Integer, nullable=False)
    to_address = sa.Column(sa.VARCHAR, nullable=False)

    ix_proposal_id = sa.Index("ix_proposal_id", proposal_id)

    @classmethod
    def add(cls, proposal_id, amount, to_address):
        # @TODO: validate that we can make this payout; check previous payouts
        from flask_login import current_user
        if not current_user.admin:
            raise Exception("user must be admin to add a payout")
        from funding.factory import db_session

        try:
            payout = Payout(propsal_id=proposal_id, amount=amount, to_address=to_address)
            db_session.add(payout)
            db_session.commit()
            db_session.flush()
            return payout
        except Exception as ex:
            db_session.rollback()
            raise

    @staticmethod
    def get_payouts(proposal_id):
        return db_session.query(Payout).filter(Payout.proposal_id == proposal_id).all()


class Comment(base):
    __tablename__ = "comments"
    id = sa.Column(sa.Integer, primary_key=True)

    proposal_id = sa.Column(sa.Integer, sa.ForeignKey('proposals.id'))
    proposal = relationship("Proposal", back_populates="comments")

    user_id = sa.Column(sa.Integer, sa.ForeignKey('users.user_id'))
    user = relationship("User", back_populates="comments")

    date_added = sa.Column(sa.TIMESTAMP, default=datetime.now)

    message = sa.Column(sa.VARCHAR, nullable=False)
    replied_to = sa.Column(sa.ForeignKey("comments.id"))

    locked = sa.Column(sa.Boolean, default=False)

    automated = sa.Column(sa.Boolean, default=False)

    ix_comment_replied_to = sa.Index("ix_comment_replied_to", replied_to)
    ix_comment_proposal_id = sa.Index("ix_comment_proposal_id", proposal_id)

    @property
    def ago(self):
        from funding.bin.utils_time import TimeMagic
        return TimeMagic().ago(self.date_added)

    @staticmethod
    def find_by_id(cid: int):
        from funding.factory import db_session
        return db_session.query(Comment).filter(Comment.id == cid).first()

    @staticmethod
    def remove(cid: int, pid: int, puid: int):
        from funding.factory import db_session
        from flask_login import current_user
        if current_user.id != puid and not current_user.admin:
            raise Exception("no rights to remove this comment")
        comment = Comment.find_by_id(cid=cid)
        if not comment.replied_to:
            comment.message = "comment removed by %s" % current_user
            comment.locked = True
            try: 
                db_session.commit()
                db_session.flush()
            except:
                db_session.rollback()
        else:
            try:
                db_session.delete(comment)
                db_session.commit()
                db_session.flush()
            except:
                db_session.rollback()
                raise

    @staticmethod
    def lock(cid: int):
        from funding.factory import db_session
        from flask_login import current_user
        if not current_user.admin:
            raise Exception("admin required")
        comment = Comment.find_by_id(cid=cid)
        if not comment:
            raise Exception("comment by that id not found")
        comment.locked = True
        try:
            db_session.commit()
            db_session.flush()
            return comment
        except:
            db_session.rollback()
            raise

    @classmethod
    def add_comment(cls, pid: int, user_id: int, message: str, cid: int = None, message_id: int = None, automated=False):
        from flask_login import current_user
        from funding.factory import db_session
        if not message:
            raise Exception("empty message")

        if current_user.id != user_id and not current_user.admin:
            raise Exception("no rights to add or modify this comment")

        if not message_id:
            proposal = Proposal.find_by_id(pid=pid)
            if not proposal:
                raise Exception("no proposal by that id")
            comment = Comment(user_id=user_id, proposal_id=proposal.id, automated=automated)
            if cid:
                parent = Comment.find_by_id(cid=cid)
                if not parent:
                    raise Exception("cannot reply to a non-existent comment")
                comment.replied_to = parent.id
        else:
            try:
                user = db_session.query(User).filter(User.id == user_id).first()
                if not user:
                    raise Exception("no user by that id")
                comment = next(c for c in user.comments if c.id == message_id)
                if comment.locked and not current_user.admin:
                    raise Exception("your comment has been locked/removed")
            except StopIteration:
                raise Exception("no message by that id")
            except:
                raise Exception("unknown error")
        try:
            comment.message = message
            db_session.add(comment)
            db_session.commit()
            db_session.flush()
        except Exception as ex:
            db_session.rollback()
            raise Exception(str(ex))
        return comment

    @staticmethod
    def edit(cid: int, pid: int, message: str, user_id: int):
        from funding.factory import db_session
        from flask_login import current_user
        if current_user.id != user_id and not current_user.admin:
            raise Exception("no rights to remove this comment")
        comment = Comment.find_by_id(cid=cid)
        if not comment.locked:
            try:
                db_session.query(Comment).filter(Comment.id == cid).update({'message': message})
                db_session.commit()
                db_session.flush()
            except:
                db_session.rollback()
                raise
        else:
            raise Exception("Comment is locked and can not be edited")