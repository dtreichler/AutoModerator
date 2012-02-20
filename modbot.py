import re
import urllib2
from datetime import datetime, timedelta
from time import sleep, time

import reddit
from BeautifulSoup import BeautifulSoup
from sqlalchemy.sql import and_
from sqlalchemy.orm.exc import NoResultFound

from models import cfg_file, db, Subreddit, Condition, ActionLog, \
    AutoReapproval

# don't remove/approve any reports older than this (doesn't apply to alerts)
REPORT_BACKLOG_LIMIT = timedelta(days=2)


def perform_action(subreddit, item, condition):
    """Performs the action for the condition(s) and creates an ActionLog entry."""
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
        item.remove()
    elif condition.action == 'approve':
        item.approve()
    elif condition.action == 'alert':
        subreddit.session.reddit_session.compose_message(
            '#'+subreddit.name,
            'Reported Item Alert',
            'The following item has received a large number of reports, '+
            'please investigate:\n\n'+item)

    # log the action taken
    action_log = ActionLog()
    action_log.subreddit_id = subreddit.id
    action_log.action_time = datetime.utcnow()
    action_log.action = condition.action

    if isinstance(item, str):
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
    elif isinstance(item, reddit.objects.Comment):
        action_log.permalink = ('http://www.reddit.com/r/'+
                                item.subreddit.display_name+
                                '/comments/'+item.link_id.split('_')[1]+
                                '/a/'+item.id)

    db.session.add(action_log)
    db.session.commit()

    sleep(2)


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
        sleep(2)
        response['data']['things'][0].distinguish()
        sleep(2)


def check_reports_html(subreddit):
    """Does report alerts/reapprovals, requires loading HTML page."""
    # only check if a report alert threshold or auto-reapprove is set
    if not subreddit.report_threshold and not subreddit.auto_reapprove:
        return

    reports_page = subreddit.session.reddit_session._request(
        'http://www.reddit.com/r/'+subreddit.name+'/about/reports')
    soup = BeautifulSoup(reports_page)

    # check for report alerts
    if subreddit.report_threshold:
        for reported_item in soup.findAll(
                attrs={'class': 'rounded reported-stamp stamp'}):
            reports = re.search('(\d+)$', reported_item.text).group(1)
            if int(reports) >= subreddit.report_threshold:
                permalink = str(reported_item.parent.a['href'])
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
    if subreddit.auto_reapprove:
        for approved_item in soup.findAll(
                attrs={'class': 'approval-checkmark'}):
            report_stamp = approved_item.parent.parent.findAll(
                            attrs={'class': 'rounded reported-stamp stamp'})
            num_reports = re.search('(\d+)$', report_stamp[0].text).group(1)
            num_reports = int(num_reports)
            permalink = approved_item.parent.parent.findAll(
                            attrs={'class': re.compile('comments')}
                        )[0]['href']
            sub = (subreddit.session.reddit_session.get_submission(permalink))

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
                entry.total_reports += num_reports
                entry.last_approval_time = datetime.utcnow()

                db.session.add(entry)
                db.session.commit()
                sub.approve()
                sleep(2)


def check_items(subreddit, items, conditions, stop_time, modqueue=False):
    """Checks the items generator for any matching conditions.

    Returns the creation time of the newest item it checks.
    """
    if not conditions:
        return None

    newest_item_time = None

    for item in items:
        item_time = datetime.utcfromtimestamp(item.created_utc)
        if not newest_item_time and stop_time < item_time:
            newest_item_time = item_time
        elif item_time <= stop_time:
            break
        
        if not modqueue or in_modqueue(subreddit, item):
            # check removal conditions first
            if not check_conditions(subreddit, item,
                    [c for c in conditions if c.action == 'remove']):
                check_conditions(subreddit, item,
                        [c for c in conditions if c.action == 'approve'])

    return newest_item_time


def check_conditions(subreddit, item, conditions):
    """Checks an item against a set of conditions.

    Returns the first condition that matches, or a list of all conditions that
    match if check_all_conditions is set on the subreddit. Returns None if no
    conditions match.
    """
    if isinstance(item, reddit.objects.Submission):
        conditions = [c for c in conditions
                          if c.subject == 'submission']
    elif isinstance(item, reddit.objects.Comment):
        conditions = [c for c in conditions
                          if c.subject == 'comment']

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
    if condition.attribute == 'user':
        if item.author != '[deleted]':
            test_string = item.author.name
        else:
            test_string = item.author
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

    if re.search('^'+condition.value.lower()+'$',
            test_string.lower(),
            re.DOTALL|re.UNICODE):
        satisfied = True
    else:
        satisfied = False

    # check user conditions if necessary
    if satisfied:
        satisfied = check_user_conditions(item, condition)

    # flip the result it's an inverse condition
    if condition.inverse:
        satisfied = not satisfied

    # make sure all sub-conditions are satisfied as well
    if satisfied:
        for sub_condition in condition.additional_conditions:
            match = check_condition(item, sub_condition)
            if not match:
                satisfied = False
                break

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
    if item.author == '[deleted]':
        return fail_result

    # get user info
    user = item.reddit_session.get_redditor(item.author)

    # shadowbanned check
    if condition.is_shadowbanned is not None:
        try: # try to get user overview
            list(user.get_overview(limit=1))
        except: # if that failed, they're probably shadowbanned
            return fail_result

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
    

def in_modqueue(subreddit, item):
    """Checks if an item is in a subreddit's modqueue."""
    for i in subreddit.modqueue_cache:
        if i.created_utc < item.created_utc:
            return False
        if i.id == item.id:
            return True

    for i in subreddit.modqueue:
        subreddit.modqueue_cache.append(i)
        if i.created_utc < item.created_utc:
            return False
        if i.id == item.id:
            return True

    return False


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
            sleep(2)


def get_meme_name(item):
    """Gets the item's meme name, if relevant/possible."""
    # determine the URL of the page that will contain the meme name
    if item.domain in ['quickmeme.com', 'qkme.me']:
        url = item.url
    elif item.domain == 'i.qkme.me':
        matches = re.search('.+/(.+?)\.jpg$', item.url)
        url = 'http://qkme.me/'+matches.group(1)
    elif item.domain.endswith('memegenerator.net'):
        for regex in ['/instance/(\\d+)$', '(\\d+)\.jpg$']:
            matches = re.search(regex, item.url)
            if matches:
                url = 'http://memegenerator.net/instance/'+matches.group(1)
                break
    else:
        return ''

    # load the page and extract the meme name
    try:
        page = urllib2.urlopen(url)
        soup = BeautifulSoup(page)

        if item.domain in ['quickmeme.com', 'qkme.me', 'i.qkme.me']:
            return soup.findAll(id='meme_name')[0].text
        elif item.domain.endswith('memegenerator.net'):
            result = soup.findAll(attrs={'class': 'rank'})[0]
            matches = re.search('#\\d+ (.+)$', result.text)
            return matches.group(1)
    except:
        return ''


def main():
    start_time = datetime.utcnow()

    r = reddit.Reddit(user_agent=cfg_file.get('reddit', 'user_agent'))
    r.login(cfg_file.get('reddit', 'username'),
        cfg_file.get('reddit', 'password'))

    subreddits = Subreddit.query.filter(Subreddit.enabled == True).all()

    for subreddit in subreddits:
        try:
            subreddit.session = r.get_subreddit(
                                    subreddit.name.encode('ascii', 'ignore'))
            subreddit.modqueue = \
                    subreddit.session.get_modqueue()
            subreddit.modqueue_cache = list()
            all_conditions = (subreddit.conditions
                              .filter(Condition.parent_id == None)
                              .all())

            # check reports
            conditions = [c for c in all_conditions
                          if c.subject == 'comment' and
                             c.is_shadowbanned != True and
                             c.action == 'remove']
            items = subreddit.session.get_reports(limit=None)
            stop_time = datetime.utcnow() - REPORT_BACKLOG_LIMIT
            check_items(subreddit, items, conditions, stop_time)

            # check reports html
            check_reports_html(subreddit)

            # check spam
            items = subreddit.session.get_spam()
            newest_spam_time = check_items(subreddit, items, all_conditions,
                                           subreddit.last_spam, True)
            if newest_spam_time:
                subreddit.last_spam = newest_spam_time

            # check new submissions
            conditions = [c for c in all_conditions
                          if c.subject == 'submission' and
                             c.is_shadowbanned != True and
                             c.action == 'remove']
            items = subreddit.session.get_new_by_date()
            newest_submission_time = check_items(subreddit, items, conditions,
                                                 subreddit.last_submission)
            if newest_submission_time:
                subreddit.last_submission = newest_submission_time

            # check new comments
            if not subreddit.reported_comments_only:
                conditions = [c for c in all_conditions
                              if c.subject == 'comment' and
                                 c.is_shadowbanned != True and
                                 c.action == 'remove']
                items = subreddit.session.get_comments()
                newest_comment_time = check_items(subreddit, items,
                                                  conditions,
                                                  subreddit.last_comment)
                if newest_comment_time:
                    subreddit.last_comment = newest_comment_time

            db.session.commit()
        except Exception as e:
            print e

    respond_to_modmail(r.user.get_modmail(), start_time)


if __name__ == '__main__':
    main()
