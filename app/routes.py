import flask
from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_socketio import emit
from werkzeug.urls import url_parse
from app import app, db
from app.models import Auction, Balance, Bid, Lot, Pool, User, cube_cards
from app.forms import (AdvanceForm, BidForm, CreateForm, CloseBiddingForm,
                       LoginForm, RegistrationForm)


@app.route("/")
@app.route("/index")
@login_required
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(username=form.username.data)
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash("Account created successfully. You are now logged in.")
        return redirect(url_for("index"))
    return render_template("register.html", title="Register", form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user is None or not user.check_password(form.password.data):
            flash("Invalid username or password")
            return redirect(url_for("login"))
        login_user(user, remember=form.remember_me.data)
        next_page = request.args.get("next")
        if not next_page or url_parse(next_page).netloc != "":
            next_page = url_for("index")
        return redirect(next_page)
    return render_template("login.html", title="Sign In", form=form)


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.route("/create", methods=["GET", "POST"])
@login_required
def create():
    form = CreateForm()
    all_users = User.query.order_by(db.func.lower(User.username))
    form.usernames.choices = [(u.username, u.username) for u in all_users]
    if form.validate_on_submit():
        users = [User.query.filter_by(username=username).first()
                 for username in form.usernames.data]
        pool = Pool()
        pool.new_from_cube()
        auction = Auction(starting_balance=form.starting_balance.data,
                          time_limit=form.time_limit.data,
                          default_lot=form.default_lot.data, pool=pool,
                          users=users, creator=current_user)
        if form.first_nom.data:
            auction.add_lot(card=form.first_nom.data)
        else:
            auction.add_lot(size=form.default_lot.data)
        db.session.add(auction)
        db.session.flush()
        for user in users:
            new_balance = Balance(holder=user, auction=auction,
                                  amount=form.starting_balance.data)
            db.session.add(new_balance)
        db.session.commit()
        return redirect(url_for("auction", auction_id=auction.id))
    return render_template("create.html", title="New Draft", form=form,
                           cube_cards=cube_cards)


@app.route("/auction/<auction_id>", methods=["GET", "POST"])
@login_required
def auction(auction_id):
    auction = Auction.query.filter_by(id=auction_id).first_or_404()
    users = auction.users.order_by(db.func.lower(User.username))
    allpicks = {}
    for user in users:
        currlots = Lot.query.filter_by(winner=user, auction_id=auction_id).all()
        all_cards_nested = [lot.content for lot in currlots]
        flat = [item for subitem in all_cards_nested for item in subitem]
        allpicks[user.username] = flat
    lot = auction.current_lot()
    if auction not in current_user.auctions:
        flash("You are not an authorized member of that auction.")
        return redirect(url_for("index"))
    balances = (
        auction.balances.join(User).order_by(db.func.lower(User.username))
    )
    if not lot:
        return render_template("auction.html", title=f"Auction {auction_id}",
                               auction=auction, users=users, balances=balances,
                               lot=None)
    advance_form = AdvanceForm()
    advance_form.auction_id.data = auction_id
    close_bidding_form = CloseBiddingForm()
    waiting_on = lot.waiting_on()
    if (current_user == auction.creator) and not waiting_on:
        if advance_form.submit_advance.data and advance_form.validate():
            lot.record_winner()
            if lot.content:
                auction.pool.remove_from_pool(lot.content)
            if not auction.is_complete():
                requested_lot = advance_form.next_lot.data
                if requested_lot:
                    try:
                        num_cards = int(requested_lot)
                        auction.add_lot(size=num_cards)
                    except ValueError:
                        auction.add_lot(card=advance_form.next_lot.data)
                else:
                    auction.add_lot(size=auction.default_lot)
            db.session.commit()
            emit("next lot", {"auction.id": auction.id}, namespace=None,
                 broadcast=True)
            if auction.is_complete():
                return render_template("auction.html",
                                       title=f"Auction {auction_id}",
                                       auction=auction, users=users, lot=None)
        if advance_form.submit_reset.data:
            lot.reset()
            db.session.commit()
            emit("reset", {"auction.id": auction.id}, namespace=None,
                 broadcast=True)
    if (current_user == auction.creator) and waiting_on:
        if close_bidding_form.submit_close.data:
            for user in waiting_on:
                bid = Bid(bidder=user, lot=auction.current_lot(), amount=0)
                db.session.add(bid)
            db.session.commit()
            skipped = ", ".join([user.username for user in waiting_on])
            flash(f"Proceeding without {skipped}.")
            emit("some bidders skipped", {"auction.id": auction.id},
                 namespace=None, broadcast=True)
    bid_form = BidForm()
    bid_form.auction_id.data = auction_id
    bid_form.lot_id.data = auction.current_lot().id
    try:
        bid_form.validate_on_submit()
    except:
        return ('', 204)
    if bid_form.validate_on_submit() and bid_form.submit_bid.data:
        if bid_form.lot_id.data != auction.current_lot().id or not waiting_on:
            # Don't take bids if user wasn't looking at current lot for some
            # reason, or if the bid was somehow submitted on a completed lot.
            # Not sure if the first part is actually working as implemented.
            return redirect(url_for("auction", auction_id=auction_id))
        amount = bid_form.amount.data
        if not amount:
            amount = 0
        bid = Bid(bidder=current_user, lot=auction.current_lot(),
                  amount=amount)
        db.session.add(bid)
        db.session.commit()
        emit("a bid", {"auction.id": auction.id}, namespace=None,
             broadcast=True)
        flash(f"Bid of {amount} recorded.")
        return redirect(url_for("auction", auction_id=auction_id))
    lot = auction.current_lot()
    waiting_on = lot.waiting_on()
    if waiting_on and current_user not in waiting_on:
        names = ", ".join([user.username for user in waiting_on])
        flash(f"Waiting on {names}.")
    return render_template("auction.html", title=f"Auction {auction_id}",
                           auction=auction, users=users, balances=balances,
                           lot=lot, waiting_on=waiting_on, bid_form=bid_form,
                           advance_form=advance_form,
                           allpicks=allpicks,
                           close_bidding_form=close_bidding_form)


@app.route("/picks/<auction_id>/")
@app.route("/picks/<auction_id>/<username>")
@login_required
def picks(auction_id, username=None):
    auction = Auction.query.filter_by(id=auction_id).first_or_404()
    if not username:
        users = auction.users.order_by(db.func.lower(User.username))
        return render_template("picks.html",
                               title=f"Auction {auction_id} picks",
                               auction=auction, users=users)
    user = User.query.filter_by(username=username).first()
    if user not in auction.users:
        flash(f"{username} is not part of Auction {auction_id}.")
        return redirect(url_for("picks", auction_id=auction_id))
    all_lots = Lot.query.filter_by(winner=user, auction_id=auction_id).all()
    all_cards_nested = [lot.content for lot in all_lots]
    all_cards = [card for lot in all_cards_nested for card in lot]
    # This assumes the cube list is sorted by color. This allows manual tweaks
    # to color identity (e.g., counting Vedalken Shackles as blue).
    # It also relies on dicts preserving order (python >=3.6).
    color_sorted = sorted(all_cards, key=cube_cards.index)
    return render_template("picks.html",
                           title=f"{username}'s Auction {auction_id} picks",
                           user=user, auction=auction, auction_id=auction_id, pick_sorted=all_cards,
                           color_sorted=color_sorted)

@app.route("/export/<auction_id>/<username>.txt")
@login_required
def export(auction_id, username):
    auction = Auction.query.filter_by(id=auction_id).first_or_404()
    user = User.query.filter_by(username=username).first()
    if user not in auction.users:
        flash(f"{username} is not part of Auction {auction_id}.")
        return redirect(url_for("picks", auction_id=auction_id))
    all_lots = Lot.query.filter_by(winner=user, auction_id=auction_id).all()
    all_cards_nested = [lot.content for lot in all_lots]
    all_cards = [card for lot in all_cards_nested for card in lot]
    all_cards = ["1 {}".format(card) for card in all_cards]
    return flask.Response("\n".join(all_cards), mimetype="text/plain")

@app.route("/auction/<auction_id>/status", methods=["GET"])
@login_required
def status(auction_id):
    auction = Auction.query.filter_by(id=auction_id).first_or_404()
    lot = auction.current_lot()
    return jsonify({
        "current_lot_id": lot.id,
        "waiting_on": lot.waiting_on_serialized()
    })
