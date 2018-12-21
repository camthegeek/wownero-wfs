from datetime import datetime
from flask import request, redirect, Response, abort, render_template, url_for, flash, make_response, send_from_directory, jsonify
from flask_login import login_user , logout_user , current_user, login_required, current_user
from flask_yoloapi import endpoint, parameter
from itsdangerous import URLSafeTimedSerializer, BadData, SignatureExpired
import settings
from funding.factory import app, db_session
from funding.orm.orm import Proposal, User, Comment
from flask_mail import Message

@app.before_request
def before_request():
    if current_user.is_authenticated:
        try:
            current_user.last_online = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db_session.commit()
            db_session.flush()
        except: 
            db_session.rollback()

@app.route('/')
def index():
    return redirect(url_for('proposals'))


@app.route('/about')
def about():
    return make_response(render_template('about.html'))


@app.route('/api')
def api():
    return make_response(render_template('api.html'))


@app.route('/proposal/add/disclaimer')
def proposal_add_disclaimer():
    return make_response(render_template(('proposal/disclaimer.html')))


@app.route('/proposal/add')
def proposal_add():
    if current_user.is_anonymous:
        return make_response(redirect(url_for('login')))
    default_content = settings.PROPOSAL_CONTENT_DEFAULT
    return make_response(render_template('proposal/edit.html', default_content=default_content))


@app.route('/proposal/comment', methods=['POST'])
@endpoint.api(
    parameter('pid', type=int, required=True),
    parameter('text', type=str, required=True),
    parameter('cid', type=int, required=False)
)
def proposal_comment(pid, text, cid):
    if current_user.is_anonymous:
        flash('not logged in', 'error')
        return redirect(url_for('proposal', pid=pid))
    if len(text) <= 3:
        flash('comment too short', 'error')
        return redirect(url_for('proposal', pid=pid))
    try:
        Comment.add_comment(user_id=current_user.id, message=text, pid=pid, cid=cid)
    except Exception as ex:
        flash('Could not add comment: %s' % str(ex), 'error')
        return redirect(url_for('proposal', pid=pid))

    flash('Comment posted.')
    return redirect(url_for('proposal', pid=pid))


@app.route('/proposal/<int:pid>/comment/<int:cid>')
def propsal_comment_reply(cid, pid):
    from funding.orm.orm import Comment
    c = Comment.find_by_id(cid)
    if not c or c.replied_to:
        return redirect(url_for('proposal', pid=pid))
    p = Proposal.find_by_id(pid)
    if not p:
        return redirect(url_for('proposals'))
    if c.proposal_id != p.id:
        return redirect(url_for('proposals'))

    return make_response(render_template('comment_reply.html', c=c, pid=pid, cid=cid))

@app.route('/proposal/comment/edit', methods=['POST'])
@endpoint.api(
    parameter('pid', type=int, required=True),
    parameter('text', type=str, required=True),
    parameter('cid', type=int, required=False)
)
def proposal_comment_edit_post(pid, text, cid):
    if current_user.is_anonymous:
        flash('not logged in', 'error')
        return redirect(url_for('proposal', pid=pid))
    if len(text) <= 3:
        flash('comment too short', 'error')
        return redirect(url_for('proposal', pid=pid))
    try:
        Comment.edit(user_id=current_user.id, message=text, pid=pid, cid=cid)
    except Exception as ex:
        flash('Could not edit comment: %s' % str(ex), 'error')
        return redirect(url_for('proposal', pid=pid))

    flash('Comment updated.')
    return redirect(url_for('proposal', pid=pid))

@app.route('/proposal/<int:onpid>/remove-comment/<int:removecID>/<int:puid>')
def proposal_comment_remove(removecID, onpid, puid):
    if current_user.is_anonymous:
        flash('not logged in', 'error')
        return redirect(url_for('proposal', pid=onpid))
    try:
        Comment.remove(cid=removecID, pid=onpid, puid=puid)
    except Exception as ex:
        flash('Could not remove comment: %s' % str(ex), 'error')
        return redirect(url_for('proposal', pid=onpid))
    flash('Comment removed')
    return redirect(url_for('proposal', pid=onpid))

@app.route('/proposal/<int:pid>/comment-edit/<int:cid>')
def proposal_comment_edit(cid, pid):
    from funding.orm.orm import Comment
    c = Comment.find_by_id(cid)
    if c.locked:
        raise Exception('comment is locked, cannot edit or delete')
    p = Proposal.find_by_id(pid)
    if not p:
        return redirect(url_for('proposals'))
    if c.proposal_id != p.id:
        return redirect(url_for('proposals'))

    return make_response(render_template('comment_edit.html', c=c, pid=pid, cid=cid))

@app.route('/proposal/<int:pid>')
def proposal(pid):
    p = Proposal.find_by_id(pid=pid)
    p.get_comments()
    if not p:
        return make_response(redirect(url_for('proposals')))
    return make_response(render_template(('proposal/proposal.html'), proposal=p))


@app.route('/api/proposal/add', methods=['POST'])
@endpoint.api(
    parameter('title', type=str, required=True, location='json'),
    parameter('content', type=str, required=True, location='json'),
    parameter('pid', type=int, required=False, location='json'),
    parameter('funds_target', type=str, required=True, location='json'),
    parameter('addr_receiving', type=str, required=True, location='json'),
    parameter('category', type=str, required=True, location='json'),
    parameter('status', type=int, required=True, location='json', default=1)
)
def proposal_api_add(title, content, pid, funds_target, addr_receiving, category, status):
    import markdown2

    if current_user.is_anonymous:
        return make_response(jsonify('err'), 500)

    if len(title) <= 8:
        return make_response(jsonify('title too short'), 500)

    if len(content) <= 20:
        return make_response(jsonify('content too short'), 500)

    if category and category not in settings.FUNDING_CATEGORIES:
        return make_response(jsonify('unknown category'), 500)

    if status not in settings.FUNDING_STATUSES.keys():
        make_response(jsonify('unknown status'), 500)

    if status != 1 and not current_user.admin:
        return make_response(jsonify('no rights to change status'), 500)

    try:
        from funding.bin.anti_xss import such_xss
        content_escaped = such_xss(content)
        html = markdown2.markdown(content_escaped, safe_mode=True)
    except Exception as ex:
        return make_response(jsonify('markdown error'), 500)

    if pid:
        p = Proposal.find_by_id(pid=pid)
        if not p:
            return make_response(jsonify('proposal not found'), 500)

        if p.user.id != current_user.id and not current_user.admin:
            return make_response(jsonify('no rights to edit this proposal'), 500)

        p.headline = title
        p.content = content
        p.html = html
        if addr_receiving:
            p.addr_receiving = addr_receiving
        if category:
            p.category = category

        # detect if an admin moved a proposal to a new status and auto-comment
        if p.status != status and current_user.admin:
            msg = "Moved to status \"%s\"." % settings.FUNDING_STATUSES[status].capitalize()
            try:
                Comment.add_comment(user_id=current_user.id, message=msg, pid=pid, automated=True)
                if not p.generated_qr:
                    Proposal.generate_donation_addr_qr(donation_addr=p.addr_donation, pid=pid)
            except:
                pass

        p.status = status
        p.last_edited = datetime.now()

    else:
        try: 
            funds_target = float(funds_target) 
        except Exception as ex:
            return make_response(jsonify('letters detected'),500)
        if funds_target < 1:
                return make_response(jsonify('Proposal asking less than 1 error :)'), 500)
        if len(addr_receiving) != 97:
            return make_response(jsonify('Faulty address, should be of length 72'), 500)

        p = Proposal(headline=title, content=content, category='misc', user=current_user)
        proposalID = current_user
        addr_donation = Proposal.generate_proposal_subaccount(proposalID)
        p.addr_donation = addr_donation  
        p.html = html
        p.last_edited = datetime.now()
        p.funds_target = funds_target
        p.addr_receiving = addr_receiving
        p.category = category
        p.status = status
        db_session.add(p)
    
    db_session.commit()
    db_session.flush()

    if p.addr_donation:
        donation_addr = p.addr_donation
        Proposal.generate_donation_addr_qr(donation_addr, p.id)
    else:
        return

    # reset cached statistics
    from funding.bin.utils import Summary
    Summary.fetch_stats(purge=True)

    return make_response(jsonify({'url': url_for('proposal', pid=p.id)}))


@app.route('/proposal/<int:pid>/edit')
def proposal_edit(pid):
    p = Proposal.find_by_id(pid=pid)
    if not p:
        return make_response(redirect(url_for('proposals')))

    return make_response(render_template(('proposal/edit.html'), proposal=p))


@app.route('/search')
@endpoint.api(
    parameter('key', type=str, required=False)
)
def search(key=None):
    if not key:
        return make_response(render_template('search.html', results=None, key='Empty!'))
    results = Proposal.search(key=key)
    return make_response(render_template('search.html', results=results, key=key))


@app.route('/user/<path:name>')
def user(name):
    q = db_session.query(User)
    q = q.filter(User.username == name)
    user = q.first()
    return render_template('user.html', user=user)

@app.route('/proposals')
@endpoint.api(
    parameter('status', type=int, location='args', required=False),
    parameter('page', type=int, location='args', required=False),
    parameter('cat', type=str, location='args', required=False)
)
def proposals(status, page, cat):
    if not isinstance(status, int) and not isinstance(page, int) and not cat:
        # no args, render overview
        proposals = {
            'proposed': Proposal.find_by_args(status=1, limit=10),
            'funding': Proposal.find_by_args(status=2, limit=10),
            'wip': Proposal.find_by_args(status=3, limit=10),
            'completed': Proposal.find_by_args(status=4, limit=10)}
        return make_response(render_template('proposal/overview.html', proposals=proposals))

    try:
        if not isinstance(status, int):
            status = 1
        proposals = Proposal.find_by_args(status=status, cat=cat)
    except:
        return make_response(redirect(url_for('proposals')))

    return make_response(render_template('proposal/proposals.html',
                                         proposals=proposals, status=status, cat=cat))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if settings.USER_REG_DISABLED:
        return 'user reg disabled ;/'

    if request.method == 'GET':
        return make_response(render_template('register.html'))

    username = request.form['username']
    password = request.form['password']
    email = request.form['email']

    try:
        user = User.add(username, password, email)
        flash('Successfully registered. No confirmation email required. You can login!')
        return redirect(url_for('login'))
    except Exception as ex:
        flash('Could not register user. Probably a duplicate username or email that already exists.', 'error')
        return make_response(render_template('register.html'))


@app.route('/login', methods=['GET', 'POST'])
@endpoint.api(
    parameter('username', type=str, location='form'),
    parameter('password', type=str, location='form')
)
def login(username, password):
    if request.method == 'GET':
        return make_response(render_template('login.html'))

    from funding.factory import bcrypt
    user = User.query.filter_by(username=username).first()
    if user is None or not bcrypt.check_password_hash(user.password, password):
        flash('Username or Password is invalid', 'error')
        return make_response(render_template('login.html'))

    login_user(user)
    response = redirect(request.args.get('next') or url_for('index'))
    response.headers['X-Set-Cookie'] = True
    return response


@app.route('/logout', methods=['GET'])
def logout():
    logout_user()
    response = redirect(request.args.get('next') or url_for('login'))
    response.headers['X-Set-Cookie'] = True
    flash('Logout successfully')
    return response


@app.route('/static/<path:path>')
def static_route(path):
    return send_from_directory('static', path)

#password reset
@app.route('/account/password/reset', methods=['GET', 'POST'])
@endpoint.api(
    parameter('email', type=str, location='form')
)

def passResetStart(email):
    if request.method == 'GET':
        return make_response(render_template('reset.html'))

    xquery = db_session.query(User)
    searchQ = xquery.filter_by(email=email).first()
    if searchQ is None:
        return
    else: 
        key = URLSafeTimedSerializer(settings.SECRET,salt='passwordreset')
        token = key.dumps({'email': searchQ.email})
        msg = Message("Password Reset Request",
        sender="settings.USER_EMAIL_SENDER_EMAIL",
        recipients=[email])
        msg.body = "Hi, we received a request to reset your password on the {coincode} Funding System ({siteurl}).\n\n Please click this link to reset your password: {siteurl}account/password/reset/{token}".format(siteurl=settings.SITE_URL,coincode=settings.COINCODE, token=token)
        flash('Password reset email sent')
        mail.send(msg)

    return make_response(render_template('reset.html'))

@app.route('/account/password/reset/<token>', methods=['GET', 'POST'])
@endpoint.api(
    parameter('password', type=str, location='form')
)
def passwordReset(token, password, max_age=1200):
    s = URLSafeTimedSerializer(settings.SECRET, salt='passwordreset')
    try: 
        values = s.loads(token, max_age=max_age)
    except SignatureExpired:
        flash('Reset password URL link is too old.')
        return redirect(url_for('login'))
    except BadData as e:
        print('Bad login token "{}"', token)
        return redirect(url_for('login'))
    except SignatureExpired:
        return None

    userEmail = values['email']
    if (password):
        try:
            User.edit(email=userEmail, password=password)
            if (current_user.is_authenticated):
                flash('Password was changed.')
                return redirect(url_for('user', name=current_user))
            else:
                flash('Password was changed. You may log in now.')
                return redirect(url_for('login'))
        except Exception as ex:
            flash('Could not change password: %s' % str(ex), 'error')

    else:
        return make_response(render_template('password.html'))