import re
import logging, logging.config
import urllib2
from datetime import datetime, timedelta
from time import time

import reddit
from BeautifulSoup import BeautifulSoup
from sqlalchemy import func
from sqlalchemy.sql import and_
from sqlalchemy.orm.exc import NoResultFound

from models import cfg_file, path_to_cfg, db, Subreddit, Condition, \
    ActionLog, AutoReapproval

# global reddit session
r = None

# don't remove/approve any reports older than this (doesn't apply to alerts)
REPORT_BACKLOG_LIMIT = timedelta(days=2)


def perform_action(subreddit, item, condition):
    """Performs the action for the condition(s) and creates an ActionLog entry."""
    global r

    # post the comment if one is set
    if isinstance(condition, list):
        if any([c.comment for c in condition]):
            comment = ('This has been '+condition[0].action+'d for the '
                       'following reasons:\n\n')
            for c in condition:
                if c.comment:
                    comment += '* '+c.comment+'\n'
            post_comment(item, comment)

        # bit of a hack and only logs first action matched
        # should find a better method
        condition = condition[0]
    elif condition.comment:
        post_comment(item, condition.comment)

    # perform the action
    if condition.action == 'remove':
        item.remove(condition.spam)
    elif condition.action == 'approve':
        item.approve()
    elif condition.action == 'alert':
        r.compose_message(
            '#'+subreddit.name,
            'Reported Item Alert',
            'The following item has received a large number of reports, '+
            'please investigate:\n\n'+item)

    # log the action taken
    action_log = ActionLog()
    action_log.subreddit_id = subreddit.id
    action_log.action_time = datetime.utcnow()
    action_log.action = condition.action

    if isinstance(item, str) or isinstance(item, unicode):
        # for report threshold alert, we only know permalink to item
        action_log.permalink = item
    else:
        action_log.user = item.author.name
        action_log.created_utc = datetime.utcfromtimestamp(item.created_utc)
        action_log.matched_condition = condition.id

    if isinstance(item, reddit.objects.Submission):
        action_log.title = item.title
        action_log.permalink = item.permalink
        action_log.url = item.url
        action_log.domain = item.domain
        logging.info('  /r/%s: %sd submission "%s"',
                        subreddit.name,
                        condition.action,
                        item.title.encode('ascii', 'ignore'))
    elif isinstance(item, reddit.objects.Comment):
        action_log.permalink = ('http://www.reddit.com/r/'+
                                item.subreddit.display_name+
                                '/comments/'+item.link_id.split('_')[1]+
                                '/a/'+item.id)
        logging.info('        %sd comment by user %s',
                        condition.action,
                        item.author.name)

    db.session.add(action_log)
    db.session.commit()


def post_comment(item, comment):
    """Posts a distinguished comment as a reply to an item.

    Currently only supports this for submissions.
    """
    disclaimer = ('\n\n*I am a bot, and this action was performed '
                    'automatically. Please [contact the moderators of this '
                    'subreddit](http://www.reddit.com/message/compose?'
                    'to=%23'+item.subreddit.display_name+') if you have any '
                    'questions or concerns.*')
    if isinstance(item, reddit.objects.Submission):
        response = item.add_comment(comment+disclaimer)
        response.distinguish()


def check_reports_html(sr_dict):
    """Does report alerts/reapprovals, requires loading HTML page."""
    global r

    logging.info('Checking reports html page')
    reports_page = r._request('http://www.reddit.com/r/mod/about/reports')
    soup = BeautifulSoup(reports_page)

    # check for report alerts
    for reported_item in soup.findAll(
            attrs={'class': 'rounded reported-stamp stamp'}):
        permalink = (reported_item.parent
                     .findAll('li', attrs={'class': 'first'})[0].a['href'])
        sub_name = re.search('^http://www.reddit.com/r/([^/]+)',
                    permalink).group(1).lower()
        try:
            subreddit = sr_dict[sub_name]
        except KeyError:
            continue

        if not subreddit.report_threshold:
            continue

        reports = re.search('(\d+)$', reported_item.text).group(1)
        if int(reports) >= subreddit.report_threshold:
            try:
                # check log to see if this item has already had an alert
                ActionLog.query.filter(
                    and_(ActionLog.subreddit_id == subreddit.id,
                         ActionLog.permalink == permalink,
                         ActionLog.action == 'alert')).one()
            except NoResultFound:
                c = Condition()
                c.action = 'alert'
                perform_action(subreddit, permalink, c)

    # do auto-reapprovals
    for approved_item in soup.findAll(
            attrs={'class': 'approval-checkmark'}):
        report_stamp = approved_item.parent.parent.findAll(
                        attrs={'class': 'rounded reported-stamp stamp'})[0]

        permalink = (report_stamp.parent
                     .findAll('li', attrs={'class': 'first'})[0].a['href'])
        sub_name = re.search('^http://www.reddit.com/r/([^/]+)',
                    permalink).group(1).lower()
        try:
            subreddit = sr_dict[sub_name]
        except KeyError:
            continue

        if not subreddit.auto_reapprove:
            continue

        num_reports = re.search('(\d+)$', report_stamp.text).group(1)
        num_reports = int(num_reports)

        try:
            # see if this item has already been auto-reapproved
            entry = (AutoReapproval.query.filter(
                        and_(AutoReapproval.subreddit_id == subreddit.id,
                             AutoReapproval.permalink == permalink))
                        .one())
            in_db = True
        except NoResultFound:
            entry = AutoReapproval()
            entry.subreddit_id = subreddit.id
            entry.permalink = permalink
            entry.original_approver = (re.search('approved by (.+)$',
                                                 approved_item['title'])
                                       .group(1))
            entry.total_reports = 0
            entry.first_approval_time = datetime.utcnow()
            in_db = False

        if (in_db or
                approved_item['title'].lower() != \
                'approved by '+cfg_file.get('reddit', 'username').lower()):
            sub = r.get_submission(permalink)
            sub.approve()
            entry.total_reports += num_reports
            entry.last_approval_time = datetime.utcnow()

            db.session.add(entry)
            db.session.commit()
            logging.info('    Re-approved %s', entry.permalink)


def check_items(name, items, sr_dict, stop_time):
    """Checks the items generator for any matching conditions."""
    item_count = 0
    skip_count = 0
    skip_subs = set()
    start_time = time()
    seen_subs = set()

    logging.info('Checking new %ss', name)

    try:
        for item in items:
            item_time = datetime.utcfromtimestamp(item.created_utc)
            if item_time <= stop_time:
                break

            try:
                subreddit = sr_dict[item.subreddit.display_name.lower()]
            except KeyError:
                skip_count += 1
                skip_subs.add(item.subreddit.display_name.lower())
                continue

            conditions = (subreddit.conditions
                            .filter(Condition.parent_id == None)
                            .all())
            conditions = filter_conditions(name, conditions)

            if name != 'spam' or in_modqueue(item):
                if not check_conditions(subreddit, item,
                        [c for c in conditions if c.action == 'remove']):
                    check_conditions(subreddit, item,
                            [c for c in conditions if c.action == 'approve'])
                    
            item_count += 1

            if subreddit.name not in seen_subs:
                setattr(subreddit, 'last_'+name, item_time)
                seen_subs.add(subreddit.name)

        db.session.commit()
    except Exception as e:
        logging.error('  ERROR: %s', e)
        db.session.rollback()

    logging.info('  Checked %s items, skipped %s items in %s (skips: %s)',
            item_count, skip_count, elapsed_since(start_time),
            ', '.join(skip_subs))


def filter_conditions(name, conditions):
    """Filters a list of conditions based on the queue's needs."""
    if name == 'spam':
        return conditions
    elif name == 'report':
        return [c for c in conditions if c.subject == 'comment' and
                c.is_shadowbanned != True]
    elif name == 'submission':
        return [c for c in conditions if c.action == 'remove' and
                c.is_shadowbanned != True]
    elif name == 'comment':
        return [c for c in conditions if c.action == 'remove' and
                c.is_shadowbanned != True]


def check_conditions(subreddit, item, conditions):
    """Checks an item against a set of conditions.

    Returns the first condition that matches, or a list of all conditions that
    match if check_all_conditions is set on the subreddit. Returns None if no
    conditions match.
    """
    if isinstance(item, reddit.objects.Submission):
        conditions = [c for c in conditions
                          if c.subject == 'submission']
        logging.debug('      Checking submission titled "%s"',
                        item.title.encode('ascii', 'ignore'))
    elif isinstance(item, reddit.objects.Comment):
        conditions = [c for c in conditions
                          if c.subject == 'comment']
        logging.debug('      Checking comment by user %s',
                        item.author.name)

    # sort the conditions so the easiest ones are checked first
    conditions.sort(key=condition_complexity)
    matched = list()

    for condition in conditions:
        try:
            match = check_condition(item, condition)
        except:
            match = False

        if match:
            if subreddit.check_all_conditions:
                matched.append(condition)
            else:
                perform_action(subreddit, item, condition)
                return condition

    if subreddit.check_all_conditions and len(matched) > 0:
        perform_action(subreddit, item, matched)
        return matched
    return None


def check_condition(item, condition):
    """Checks an item against a single condition (and sub-conditions).
    
    Returns True if it matches, or False if not
    """
    start_time = time()
    if condition.attribute == 'user':
        if item.author:
            test_string = item.author.name
    elif (condition.attribute == 'body' and
            isinstance(item, reddit.objects.Submission)):
        test_string = item.selftext
    elif condition.attribute.startswith('media_'):
        if item.media:
            try:
                if condition.attribute == 'media_user':
                    test_string = item.media['oembed']['author_name']
                elif condition.attribute == 'media_title':
                    test_string = item.media['oembed']['description']
                elif condition.attribute == 'media_description':
                    test_string = item.media['oembed']['description']
            except KeyError:
                test_string = ''
        else:
            test_string = ''
    elif condition.attribute == 'meme_name':
        test_string = get_meme_name(item)
    else:
        test_string = getattr(item, condition.attribute)
    if not test_string:
        test_string = ''

    if condition.inverse:
        logging.debug('        Check #%s: "%s" NOT match ^%s$',
                        condition.id,
                        test_string.encode('ascii', 'ignore'),
                        condition.value.encode('ascii', 'ignore').lower())
    else:
        logging.debug('        Check #%s: "%s" match ^%s$',
                        condition.id,
                        test_string.encode('ascii', 'ignore'),
                        condition.value.encode('ascii', 'ignore').lower())

    if re.search('^'+condition.value.lower()+'$',
            test_string.lower(),
            re.DOTALL|re.UNICODE):
        satisfied = True
    else:
        satisfied = False

    # flip the result it's an inverse condition
    if condition.inverse:
        satisfied = not satisfied

    # check user conditions if necessary
    if satisfied:
        satisfied = check_user_conditions(item, condition)
        logging.debug('          User condition result = %s', satisfied)

    # make sure all sub-conditions are satisfied as well
    if satisfied:
        if condition.additional_conditions:
            logging.debug('        Checking sub-conditions:')
        for sub_condition in condition.additional_conditions:
            match = check_condition(item, sub_condition)
            if not match:
                satisfied = False
                break
        if condition.additional_conditions:
            logging.debug('        Sub-condition result = %s', satisfied)

    logging.debug('        Result = %s in %s',
                    satisfied, elapsed_since(start_time))
    return satisfied


def check_user_conditions(item, condition):
    """Checks an item's author against the age/karma/has-gold requirements."""
    # if no user conditions are set, no need to check at all
    if (condition.is_gold is None and
            condition.is_shadowbanned is None and
            condition.link_karma is None and
            condition.comment_karma is None and
            condition.combined_karma is None and
            condition.account_age is None):
        return True

    # returning True will result in the action being performed
    # so when removing, return True if they DON'T meet user reqs
    # but for approving we return True if they DO meet it
    if condition.action == 'remove':
        fail_result = True
    elif condition.action == 'approve':
        fail_result = False

    # if they deleted the post, fail user checks
    if not item.author:
        return fail_result

    # shadowbanned check
    if condition.is_shadowbanned is not None:
        user = item.reddit_session.get_redditor(item.author, fetch=False)
        try: # try to get user overview
            list(user.get_overview(limit=1))
        except: # if that failed, they're probably shadowbanned
            return fail_result

    # get user info
    user = item.reddit_session.get_redditor(item.author)

    # reddit gold check
    if condition.is_gold is not None:
        if condition.is_gold != user.is_gold:
            return fail_result

    # karma checks
    if condition.link_karma is not None:
        if user.link_karma < condition.link_karma:
            return fail_result
    if condition.comment_karma is not None:
        if user.comment_karma < condition.comment_karma:
            return fail_result
    if condition.combined_karma is not None:
        if (user.link_karma + user.comment_karma) \
                < condition.combined_karma:
            return fail_result

    # account age check
    if condition.account_age is not None:
        if (datetime.utcnow() \
                - datetime.utcfromtimestamp(user.created_utc)).days \
                < condition.account_age:
            return fail_result

    # user passed all checks
    return not fail_result
    

def in_modqueue(item):
    """Checks if an item is in the modqueue (hasn't been acted on yet)."""
    global r
    if not in_modqueue.modqueue:
        mod_subreddit = r.get_subreddit('mod')
        in_modqueue.modqueue = mod_subreddit.get_modqueue()
        in_modqueue.cache = list()

    for i in in_modqueue.cache:
        if i.created_utc < item.created_utc:
            return False
        if i.id == item.id:
            return True

    for i in in_modqueue.modqueue:
        in_modqueue.cache.append(i)
        if i.created_utc < item.created_utc:
            return False
        if i.id == item.id:
            return True

    return False
in_modqueue.modqueue = None


def respond_to_modmail(modmail, start_time):
    """Responds to modmail if any submitters sent one before approval."""
    cache = list()
    approvals = ActionLog.query.filter(
                    and_(ActionLog.action == 'approve',
                         ActionLog.action_time >= start_time)).all()

    for item in approvals:
        found = None
        done = False

        for i in cache:
            if datetime.utcfromtimestamp(i.created_utc) < item.created_utc:
                done = True
                break
            if (i.dest.lower() == '#'+item.subreddit.name.lower() and
                    i.author.name == item.user and
                    not i.replies):
                found = i
                break

        if not found and not done:
            for i in modmail:
                cache.append(i)
                if datetime.utcfromtimestamp(i.created_utc) < item.created_utc:
                    break
                if (i.dest.lower() == '#'+item.subreddit.name.lower() and
                        i.author.name == item.user and
                        not i.replies):
                    found = i
                    break

        if found:
            found.reply('Your submission has been approved automatically by '+
                cfg_file.get('reddit', 'username')+'. For future submissions '
                'please wait at least 5 minutes before messaging the mods, '
                'this post would have been approved automatically even '
                'without you sending this message.')


def get_meme_name(item):
    """Gets the item's meme name, if relevant/possible."""
    # determine the URL of the page that will contain the meme name
    if item.domain in ['quickmeme.com', 'qkme.me']:
        url = item.url
    elif item.domain.endswith('.qkme.me'):
        matches = re.search('.+/(.+?)\.jpg$', item.url)
        url = 'http://qkme.me/'+matches.group(1)
    elif item.domain.endswith('memegenerator.net'):
        for regex in ['/instance/(\\d+)$', '(\\d+)\.jpg$']:
            matches = re.search(regex, item.url)
            if matches:
                url = 'http://memegenerator.net/instance/'+matches.group(1)
                break
    elif item.domain == 'troll.me':
        url = item.url
    else:
        return None

    # load the page and extract the meme name
    try:
        page = urllib2.urlopen(url)
        soup = BeautifulSoup(page)

        if (item.domain in ['quickmeme.com', 'qkme.me'] or
                item.domain.endswith('.qkme.me')):
            return soup.findAll(id='meme_name')[0].text
        elif item.domain.endswith('memegenerator.net'):
            result = soup.findAll(attrs={'class': 'rank'})[0]
            matches = re.search('#\\d+ (.+)$', result.text)
            return matches.group(1)
        elif item.domain == 'troll.me':
            matches = re.search('^.+?\| (.+?) \|.+?$', soup.title.text)
            return matches.group(1)
    except:
        pass
    return None


def elapsed_since(start_time):
    """Returns a timedelta for how much time has passed since start_time."""
    elapsed = time() - start_time
    return timedelta(seconds=round(elapsed))


def condition_complexity(condition):
    """Returns a value representing how difficult a condition is to check."""
    complexity = 0

    # meme_name requires an external site page load
    if condition.attribute == 'meme_name':
        complexity += 1

    # checking user requires a page load
    if (condition.is_gold is not None or
            condition.is_shadowbanned is not None or
            condition.link_karma is not None or
            condition.comment_karma is not None or
            condition.combined_karma is not None or
            condition.account_age is not None):
        complexity += 1

    # checking shadowbanned requires an extra page load
    if condition.is_shadowbanned is not None:
        complexity += 1

    # commenting+distinguishing requires 2 requests
    if condition.comment is not None:
        complexity += 2

    # add complexities of all sub-conditions too
    for sub in condition.additional_conditions:
        complexity += condition_complexity(sub)

    return complexity


def main():
    logging.config.fileConfig(path_to_cfg)
    start_utc = datetime.utcnow()
    start_time = time()

    global r
    try:
        r = reddit.Reddit(user_agent=cfg_file.get('reddit', 'user_agent'))
        logging.info('Logging in as %s', cfg_file.get('reddit', 'username'))
        r.login(cfg_file.get('reddit', 'username'),
            cfg_file.get('reddit', 'password'))

        subreddits = Subreddit.query.filter(Subreddit.enabled == True).all()
        sr_dict = dict()
        for subreddit in subreddits:
            sr_dict[subreddit.name.lower()] = subreddit
        mod_subreddit = r.get_subreddit('mod')
    except Exception as e:
        logging.error('  ERROR: %s', e)

    # check reports
    items = mod_subreddit.get_reports(limit=1000)
    stop_time = datetime.utcnow() - REPORT_BACKLOG_LIMIT
    check_items('report', items, sr_dict, stop_time)

    # check spam
    items = mod_subreddit.get_spam(limit=1000)
    stop_time = (db.session.query(func.max(Subreddit.last_spam))
                 .filter(Subreddit.enabled == True).one()[0])
    check_items('spam', items, sr_dict, stop_time)

    # check new submissions
    items = mod_subreddit.get_new_by_date(limit=1000)
    stop_time = (db.session.query(func.max(Subreddit.last_submission))
                 .filter(Subreddit.enabled == True).one()[0])
    check_items('submission', items, sr_dict, stop_time)

    # check new comments
    comment_multi = '+'.join([s.name for s in subreddits
                              if not s.reported_comments_only])
    if comment_multi:
        comment_multi_sr = r.get_subreddit(comment_multi)
        items = comment_multi_sr.get_comments(limit=1000)
        stop_time = (db.session.query(func.max(Subreddit.last_comment))
                     .filter(Subreddit.enabled == True).one()[0])
        check_items('comment', items, sr_dict, stop_time)

    # respond to modmail
    try:
        respond_to_modmail(r.user.get_modmail(), start_utc)
    except Exception as e:
        logging.error('  ERROR: %s', e)

    # check reports html
    try:
        check_reports_html(sr_dict)
    except Exception as e:
        logging.error('  ERROR: %s', e)

    logging.info('Completed full run in %s', elapsed_since(start_time))


if __name__ == '__main__':
    main()
