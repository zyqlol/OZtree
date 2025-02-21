import datetime
from gluon import current
from gluon.utils import web2py_uuid

from OZfunc import (
    child_leaf_query
)

"""HMAC expiry in seconds, NB: This is when they're rotated, so an HMAC will be valid for 2xHMAC_EXPIRY"""
SPONSOR_RENEW_HMAC_EXPIRY = 60 * 60 * 24 * 7


def sponsorship_config():
    """
    Return dict of sponsorship config options, returning defaults if not available
    """
    myconf = current.globalenv['myconf']
    out = dict()
    try:
        out['reservation_time_limit'] = float(myconf.take('sponsorship.reservation_time_limit_mins')) * 60.0
    except:
        out['reservation_time_limit'] = 360.0 #seconds
    try:
        out['unpaid_time_limit'] = float(myconf.take('sponsorship.unpaid_time_limit_mins')) * 60.0
    except:
        out['unpaid_time_limit'] = 2.0*24.0*60.0*60.0 #seconds
    try:
        out['renew_discount'] = float(myconf.take('sponsorship.renew_discount'))
    except:
        out['renew_discount'] = 0.2
    try:
        out['maintenance_mins'] = myconf.take('sponsorship.maintenance_mins')
        out['maintenance_mins'] = int(out['maintenance_mins'])
    except:
        out['maintenance_mins'] = 0
    return out


def sponsorship_enabled():
    """
    Return whether sponsorship should be allowed on this instance, deriving from:

    * URL "no_sponsoring" parameter
    * sponsorship.allow_sponsorship config option
    * Role of current user (if config is not 1/0/all/none)

    Default to not allowing sponsorships, unless actively turned on
    """
    myconf = current.globalenv['myconf']
    request = current.request
    auth = current.globalenv['auth']

    if request.vars.no_sponsoring:
        # Shut off sponsoring via. URL param (e.g. museum display on main OZ site)
        return False

    # Read sponsorship setting from config
    try:
        spons = myconf.take('sponsorship.allow_sponsorship')
    except:
        spons = '0'
    if spons.lower() in ['1', 'all']:
        return True
    if spons.lower() in ['0', 'none']:
        return False

    # If anything other than '1' or 'all', treat this as a role, e.g. "manager"
    return auth.has_membership(role=spons)


def clear_reservation(reservations_table_id):
    db = current.db
    keep_fields = ('id', 'OTT_ID', 'num_views', 'last_view')
    del_fields = {f: None for f in db.reservations.fields if f not in keep_fields}
    assert len(keep_fields) + len(del_fields) == len(db.reservations.fields)
    assert reservations_table_id is not None
    db(db.reservations.OTT_ID == reservations_table_id).update(**del_fields)


def reservation_get_all_expired():
    """
    Get all reservation rows that should be expired
    """
    db = current.db
    request = current.request

    return db(
        (db.reservations.verified_time != None) &
        (db.reservations.sponsorship_ends < request.now)
    ).select(db.reservations.ALL)

def reservation_expire(r):
    """
    Move reservations row (r) into expired_reservations,
    return expired_reservations.id

    NB: Does not check if the row should be expired (i.e. sponsorship_ends > now)
    """
    db = current.db
    expired_r = r.as_dict()
    del expired_r['id']
    expired_r['was_renewed'] = True
    expired_id = db.expired_reservations.insert(**expired_r)
    r.delete_record()
    return expired_id


def reservation_add_to_basket(basket_code, reservation_row, basket_fields):
    """Add (reservation_row) to a basket identified with (basket_code), update (basket_fields) dict of fields"""
    db = current.db
    if not reservation_row.user_sponsor_name and 'user_sponsor_name' not in basket_fields:
        if basket_fields.get('prev_reservation_id', False):
            # Try digging it out of the previous entry
            prev_row = db(db.expired_reservations.id == basket_fields['prev_reservation_id']).select().first()
            if prev_row is None:
                raise ValueError("Cannot find prev_reservation_id %d" % basket_fields['prev_reservation_id'])
            basket_fields['user_sponsor_name'] = prev_row.user_sponsor_name
        else:
            # This is what defines this row as potentially-bought, so has to be defined.
            raise ValueError("Missing user_sponsor_name")
    reservation_row.update_record(
        basket_code=basket_code,
        **basket_fields)
    return reservation_row


def reservation_confirm_payment(basket_code, total_paid_pence, basket_fields):
    """
    Update all reservations with (basket_code) as paid, spreading (total_paid_pence)
    across all reservations. Fill in (basket_fields) (i.e. Paypal info) for all rows.
    """
    db = current.db
    request = current.request
    sponsorship_renew_discount = sponsorship_config()['renew_discount']

    if 'PP_transaction_code' not in basket_fields:
        raise ValueError("basket_fields should at least have PP_transaction_code")
    if 'sale_time' not in basket_fields:
        raise ValueError("basket_fields should at least have sale_time")

    basket_rows = db(db.reservations.basket_code == basket_code).select()
    if len(basket_rows) == 0:
        raise ValueError("Unknown basket_code %s" % basket_code)

    for r in basket_rows:
        if basket_fields['PP_transaction_code'] == r.PP_transaction_code:
            # PP_transaction_code matches, so is a replay of a previous transaction, exit.
            # NB: It's possible for some of the basket to not have PP_t_c set, if they run out of funds.
            return
    if db(db.uncategorised_donation.PP_transaction_code == basket_fields['PP_transaction_code']).count() > 0:
        # Already an uncategorized donation, so this is a replay of a previous transaction
        # NB: We need to check here too in case all of the OTTs in this basket were unpaid.
        return

    remaining_paid_pence = total_paid_pence
    for r in basket_rows:
        fields_to_update = basket_fields.copy()
        if not r.user_giftaid:
            # Without giftaid, we shouldn't store a users' location
            fields_to_update['PP_house_and_street'] = None
            fields_to_update['PP_postcode'] = None

        # Fetch latest asking price
        ott_price_pence = db(db.ordered_leaves.ott==r.OTT_ID).select(db.ordered_leaves.price).first().price

        if r.verified_time is not None:  # i.e. is this an already paid for node?
            # Apply renewal discount for extension
            ott_price_pence = int(ott_price_pence * (1 - sponsorship_renew_discount))

            if remaining_paid_pence < ott_price_pence:
                # Not enough funds to extend sponsorship. Make a note and move on
                # NB: This isn't genuinely likely, but a potential attack if we don't preserve the old reservation
                #     (1) Get at their renewals page
                #     (2) Pay 0.01 for them, they all end up unpaid
                #     (3) Wait for them to timeout, claim for yourself
                r.update_record(admin_comment=("reservation_confirm_payment: Transaction %s insufficient for extension. Paid %d\n%s" % (
                    basket_fields['PP_transaction_code'],
                    total_paid_pence,
                    r.admin_comment or "",
                )).strip())
                continue

            # Remove old reservation and make a new one.
            prev_ott = r.OTT_ID
            prev_sponsorship_ends = r.sponsorship_ends
            prev_reservation_id = reservation_expire(r)
            status, _, r, _ = get_reservation(prev_ott, basket_code)
            assert status == 'available'  # We just expired the old one, this should work
            reservation_add_to_basket(basket_code, r, dict(
                prev_reservation_id=prev_reservation_id
            ))

            # Bump time to include renewal
            fields_to_update['sponsorship_duration_days'] = 365*4+1
            fields_to_update['sponsorship_ends'] = prev_sponsorship_ends + datetime.timedelta(days=365*4+1)  ## 4 Years

        else:
            # NB: This is different to existing paths, but feels a more correct place to set sponsorship_ends
            fields_to_update['sponsorship_duration_days'] = 365*4+1
            fields_to_update['sponsorship_ends'] = request.now + datetime.timedelta(days=365*4+1)  ## 4 Years

        # If there's a previous row, fill in any missing values using the old entry.
        # Set either as part of an extension above, or as part of a renewal (on paypal-start)
        # NB: This will include verified_time, if it was set before
        if r.prev_reservation_id:
            prev_row = db(db.expired_reservations.id == r.prev_reservation_id).select().first()
            for k in db.expired_reservations.fields:
                if db.expired_reservations[k].writable and k in db.reservations.fields and r[k] is None:
                    fields_to_update[k] = prev_row[k]

        # Update DB entry with recalculated asking price
        fields_to_update['asking_price'] = ott_price_pence / 100

        if remaining_paid_pence >= ott_price_pence:
            # Can pay for this node, so do so.
            remaining_paid_pence -= ott_price_pence
            # NB: Strictly speaking user_paid is "What they promised to pay", and should
            # have been set before the paypal trip. But with a basket of items we don't divvy up
            # their donation until now.
            fields_to_update['user_paid'] = ott_price_pence / 100
            fields_to_update['verified_paid'] = '{:.2f}'.format(ott_price_pence / 100)
        else:
            # Can't pay for this node, but make all other changes
            fields_to_update['user_paid'] = None
            fields_to_update['verified_paid'] = None
            fields_to_update['sale_time'] = None
            # NB: Both need to be NULL to be unpaid according to add_transaction()
            fields_to_update['verified_time'] = None
            fields_to_update['PP_transaction_code'] = None
            fields_to_update['admin_comment'] = ("reservation_confirm_payment: Transaction %s insufficient for purchase. Paid %d\n%s" % (
                basket_fields['PP_transaction_code'],
                total_paid_pence,
                r.admin_comment or "",
            )).strip()

        # Send all updates for this row
        r.update_record(**fields_to_update)
    if remaining_paid_pence > 0:
        # Money left over after this transaction, make an uncategorised donation as well
        fields_to_update = {
            k:basket_rows[0][k]  # NB: Pull from first row to fill in any details previously set
            for k in db.reservations.fields
            if k in db.uncategorised_donation.fields and k not in set(('id',))
        }
        # Make sure payment fields are set properly
        fields_to_update['user_paid'] = remaining_paid_pence / 100
        fields_to_update['verified_paid'] = '{:.2f}'.format(remaining_paid_pence / 100)
        fields_to_update['PP_transaction_code'] = basket_fields['PP_transaction_code']
        fields_to_update['sale_time'] = basket_fields['sale_time']
        db.uncategorised_donation.insert(**fields_to_update)


def get_reservation(OTT_ID_Varin, form_reservation_code, update_view_count=False):
    """
    Try and add a reservation for OTT_ID_Varin
    - form_reservation_code: Temporary identifier for current user
    - update_view_count: Should the view count for the OTT be incremented?

    Returns
    - status: String describing reservation status, One of
    -         banned / available / available only to user / reserved / sponsored / unverified / unverified waiting for payment
    - status_param: A parameter associated with the status, e.g. number of mins of maintenance, time until allowed to sponsor
    - reservation_row: row from reservations table
    - leaf_entry: row from ordered_leaves table
    """
    db = current.db
    request = current.request
    sp_conf = sponsorship_config()
    reservation_time_limit = sp_conf['reservation_time_limit']
    unpaid_time_limit = sp_conf['unpaid_time_limit']
    maintenance_mins = sp_conf['maintenance_mins']

    reservation_row = None
    allow_sponsorship = sponsorship_enabled()
    # initialise status flag (it will get updated if all is OK)
    status, status_param = "", None
    maintenance_mode = bool(maintenance_mins)
    if maintenance_mode:
        status, status_param = "maintenance", maintenance_mins
    # now search for OTTID in the leaf table
    try:
        leaf_entry = db(db.ordered_leaves.ott == OTT_ID_Varin).select().first()
    except:
        OTT_ID_Varin = None
        leaf_entry = {'name': 'invalid'}
    if ((not leaf_entry) or             # invalid if not in ordered_leaves
        (leaf_entry.ott is None) or     # invalid if no OTT ID
        (' ' not in leaf_entry.name)):  # invalid if not a sp. name (no space/underscore)
            status, status_param = "invalid", None # will override maintenance
    else: # should be able to get data
        # we might come into this with a partner set in request.vars (e.g. LinnSoc)
        """
        TO DO & CHECK - Allows specific parts of the tree to be associated with a partner
        if partner is None:
            #check through partner_taxa for leaves that might match this one
            partner = db((~db.partner_taxa.deactived) & 
                         (db.partner_taxa.is_leaf == True) &
                         (db.partner_taxa.ott == OTT_ID_Varin)
                        ).select(db.partner_taxa.partner_identifier).first()
        if partner is None:
            #pull out potential partner *nodes* that we might need to check
            #also check if this leaf lies within any of the node ranges
            #this should include a join with ordered_nodes to get the ranges, & a select
            partner = db((~db.partner_taxa.deactived) & 
                         (db.partner_taxa.is_leaf == False) &
                         (OTT_ID_Varin >= db.ordered_nodes.leaf_lft) &
                         (OTT_ID_Varin <= db.ordered_nodes.leaf_rgt) &
                         (db.ordered_nodes.ott == db.partner.ott).first()
                        ).select(db.partner_taxa.partner_identifier) 
        """
        # find out if the leaf is banned
        if db(db.banned.ott == OTT_ID_Varin).count() >= 1:
            status, status_param = "banned", None  # will override maintenance
        # we need to update the reservations table regardless of banned status)
        reservation_query = db(db.reservations.OTT_ID == OTT_ID_Varin)
        reservation_row = reservation_query.select().first()
        if reservation_row is None:
            if not maintenance_mode:
                # there is no row in the database for this case so add one
                if (status == ""):
                    status = "available"
                if status == "available" and allow_sponsorship:
                    db.reservations.insert(
                        OTT_ID = OTT_ID_Varin,
                        name=leaf_entry.name,
                        last_view=request.now,
                        num_views=1,
                        reserve_time=request.now,
                        user_registration_id=form_reservation_code)
                else:
                    # update with full viewing data but no reservation, even if e.g. banned
                    db.reservations.insert(
                        OTT_ID = OTT_ID_Varin,
                        name=leaf_entry.name,
                        last_view=request.now,
                        num_views=1)
        else:
            # there is already a row in the database so update if this is the main visit
            if update_view_count and not maintenance_mode:
                reservation_query.update(
                    last_view=request.now,
                    num_views=(reservation_row.num_views or 0)+1,
                    name=leaf_entry.name)

            # this may be available (because valid) but could be
            #  sponsored, unverified, reserved or still available  
            # easiest cases to rule out are relating sponsored and unverified cases.
            # In either case they would appear in the leger
            ledger_user_name = reservation_row.user_sponsor_name
            ledger_PP_transaction_code = reservation_row.PP_transaction_code
            ledger_verified_time = reservation_row.verified_time
            # We know something is fully sponsored if PP_transaction_code is filled out  
            # NB: this could be with us typing "yet to be paid" in which case
            #  verified_paid can be NULL, so "verified paid " should not be used as a
            #  test of whether something is available or not
            # For forked sites, we do not pass PP_transaction_code (confidential), so we
            #   have to check first if verified.
            # Need to have another option here if verified_time is too long ago - we
            #  should move this to the expired_reservations table and clear it.
            if (ledger_verified_time):
                status = "sponsored"  # will override maintenance
            elif status != "banned":
                if (ledger_user_name):
                # something has been filled in
                    if (ledger_PP_transaction_code):
                        #we have a code (or have reserved this taxon)
                        status = "unverified"
                    else:
                        # unverified and unpaid - test time
                        startTime = reservation_row.reserve_time
                        endTime = request.now
                        timesince = ((endTime-startTime).total_seconds())
                        # now we check if the time is too great
                        if (timesince < (unpaid_time_limit)):
                            status = "unverified waiting for payment"
                        elif not maintenance_mode:
                            # We've waited too long and can zap the personal data
                            # previously in the table then set available
                            clear_reservation(OTT_ID_Varin)
                            # Note that this e.g. clears deactivated taxa, etc etc. Even 
                            # if status == available, allow_sponsorship can be False
                            # status is then used to decide the text to show the user
                            status = "available"
                else:
                    # The page has no user name entered & is also valid (not banned etc)
                    # it could only be reserved or available
                    # First thing is to determine time difference since reserved
                    startTime = reservation_row.reserve_time   
                    endTime = request.now
                    if (startTime == None):
                        if not maintenance_mode:
                            status = "available"
                            # reserve the leaf because there is no reservetime on record
                            if allow_sponsorship:
                                reservation_query.update(
                                    name=leaf_entry.name,
                                    reserve_time=request.now,
                                    user_registration_id=form_reservation_code)
                    else:
                        # compare times to figure out if there is a time difference
                        timesince = ((endTime-startTime).total_seconds())
                        if (timesince < (reservation_time_limit)):
                            release_time = reservation_time_limit - timesince
                            # we may be reserved if it wasn't us
                            if(form_reservation_code == reservation_row.user_registration_id):
                                # it was the same user anyway so reset timer
                                if not maintenance_mode:
                                    status = "available only to user"
                                    if allow_sponsorship:
                                        reservation_query.update(
                                            name=leaf_entry.name,
                                            reserve_time=request.now)
                            else:
                                status, status_param = "reserved", release_time
                        elif not maintenance_mode:
                            # it's available still
                            status = "available"
                            # reserve the leaf because there is no reservetime on record
                            if allow_sponsorship:
                                reservation_query.update(
                                    name=leaf_entry.name,
                                    reserve_time = request.now,
                                    user_registration_id = form_reservation_code)
        #re-do the query since we might have added the row ID now
        reservation_row = reservation_query.select().first()
    return status, status_param, reservation_row, leaf_entry


def sponsorable_children_query(target_id, qtype="ott", check_reservations_table=True):
    """
    A function that returns a web2py query selecting the sponsorable children of a specific
    node (given by OTT if qtype="ott" or ID if qtype="id".
    
    Note a slight bug: this includes ones that have been reserved or sponsored but not paid for yet
    TO DO: change javascript so that nodes without an OTT use qtype='id'
    """
    db = current.db
    query = child_leaf_query(qtype, target_id)

    #nodes without an OTT are unsponsorable
    query = query & (db.ordered_leaves.ott != None) 

    #nodes without a space in the name are unsponsorable
    query = query & (db.ordered_leaves.name.contains(' ')) 

    if check_reservations_table:
        #check which have OTTs in the reservations table
        unavailable = db((db.reservations.verified_time != None))._select(db.reservations.OTT_ID)
        #the query above ony finds those with a name. We might prefer something like the below, but I can't get it to work
            #unavailable = db((db.reservations.user_sponsor_name != None) | ((db.reservations.reserve_time != None) & ((request.now - db.reservations.reserve_time).total_seconds() < reservation_time_limit)))._select(db.reservations.OTT_ID)
        query = query & (~db.ordered_leaves.ott.belongs(unavailable))

    return query

def sponsorable_children(target_id, qtype="ott", limit=None, in_reservations=None):
    """
    Return the result of a query to get the sponsorable children (otts) of an internal
    node.
    
    The in_reservations parameter specifies what to do with sponsorable OTTs that are in
    the reservations table (i.e. not actually sponsored, but with a row in reservations,
    which can occur if people lick to sponsor but don;t go through with it.
    If in_reservations is None, return OTTs including those in the reservations table 
    If in_reservations is True, return OTTs which must be in the reservations table 
    If in_reservations is False, return OTTs which are not in the reservations table 
    """
    db = current.db
    limitby = None if limit is None else (0, limit)
    
    if in_reservations is None:
        query = sponsorable_children_query(target_id, qtype)
        return db(query).select(db.ordered_leaves.ott, limitby=limitby)
    elif not in_reservations:
        # The reservations table could have a lot of OTT IDs, in which case the default
        # query, which uses .belongs() will be very slow, so we use a left join instead
        query = sponsorable_children_query(target_id, qtype, check_reservations_table=False)
        query = query & (db.reservations.OTT_ID == None)
        return db(query).select(
            db.ordered_leaves.ott,
            left=db.reservations.on(db.ordered_leaves.ott == db.reservations.OTT_ID),
            limitby=limitby,
            orderby="NULL",  # Suppress ordering, as otherwise we try to order on a joined index
        )
    else:
        # The reservations table could have a lot of OTT IDs, in which case the default
        # query, which uses .belongs() will be very slow, so we use a left join instead
        query = sponsorable_children_query(target_id, qtype, check_reservations_table=False)
        query = query & (db.reservations.OTT_ID != None)
        return db(query).select(
            db.ordered_leaves.ott,
            left=db.reservations.on(db.ordered_leaves.ott == db.reservations.OTT_ID),
            limitby=limitby,
            orderby="NULL",  # Suppress ordering, as otherwise we try to order on a joined index
        )
    

def sponsor_renew_hmac_key():
    """Get hmac_key, or error informatively"""
    myconf = current.globalenv['myconf']

    try:
        out = myconf.take("sponsorship.hmac_key")
    except:
        raise ValueError("sponsorship.hmac_key not set in appconfig.ini, we need this to generate renewal URLs")
    if len(out) < 10:
        raise ValueError("sponsorship.hmac_key is too short, or still set to the example value")
    return out


def sponsor_renew_url(email):
    """
    Generate a signed URL to renew e-mail (email), or None for an unknown user
    """
    db = current.db
    URL = current.globalenv['URL']

    if db(db.reservations.e_mail == email).count() == 0 and db(db.expired_reservations.e_mail == email).count() == 0:
        return None

    return URL(
        'sponsor_renew.html',
        args=[email],
        scheme=True,
        host=True,
        hmac_key=sponsor_renew_hmac_key(),
    )


def sponsor_renew_verify_url(request):
    """Verify current request has a valid signature"""
    URL = current.globalenv['URL']

    return URL.verify(request, hmac_key=sponsor_renew_hmac_key())
