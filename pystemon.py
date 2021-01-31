#!/usr/bin/env python
# encoding: utf-8

'''
@author:     Christophe Vandeplas <christophe@vandeplas.com>
@copyright:  AGPLv3
             http://www.gnu.org/licenses/agpl.html

To be implemented:
- FIXME set all the config options in the class variables
- FIXME validate parsing of config file
- FIXME use syslog logging
- TODO runs as a daemon in background
- TODO save files in separate directories depending on the day/week/month. Try to avoid duplicate files
'''

try:
    from queue import Queue
except ImportError:
    from Queue import Queue
from collections import deque
from datetime import datetime
try:
    from email.mime.multipart import MIMEMultipart
except ImportError:
    from email.MIMEMultipart import MIMEMultipart

try:
    from email.mime.text import MIMEText
except ImportError:
    from email.MIMEText import MIMEText
import gzip
import hashlib
import logging.handlers
import optparse
import os
import random
import re
import smtplib
import socket
import sys
import traceback
import threading
import time
import urllib
import urllib2
import httplib
import ssl
from io import open
import requests

# try:
#     from urllib.error import HTTPError, URLError
# except ImportError:
#     from urllib2 import HTTPError, URLError

try:
    import redis
except ImportError:
    exit('ERROR: Cannot import the redis Python library. Are you sure it is installed?')

try:
    import yaml
except ImportError:
    exit('ERROR: Cannot import the yaml Python library. Are you sure it is installed?')

try:
    if sys.version_info < (2, 7):
        exit('You need python version 2.7 or newer.')
except Exception as exc:
    exit('You need python version 2.7 or newer.')

retries_paste = 3
retries_client = 5
retries_server = 100

socket.setdefaulttimeout(10)  # set a default timeout of 10 seconds to download the page (default = unlimited)
true_socket = socket.socket


def make_bound_socket(source_ip):
    def bound_socket(*a, **k):
        sock = true_socket(*a, **k)
        sock.bind((source_ip, 0))
        return sock
    return bound_socket


class PastieSite(threading.Thread):
    '''
    Instances of these threads are responsible for downloading the list of
    the most recent pastes and added those to the download queue.
    '''
    def __init__(self, name, download_url, archive_url, archive_regex):
        threading.Thread.__init__(self)
        self.kill_received = False

        self.name = name
        self.download_url = download_url
        self.archive_url = archive_url
        self.archive_regex = archive_regex
        try:
            self.ip_addr = yamlconfig['network']['ip']
            socket.socket = make_bound_socket(self.ip_addr)
        except Exception as exc:
            logger.debug("Using default IP address")

        self.save_dir = yamlconfig['archive']['dir'] + os.sep + name
        self.archive_dir = yamlconfig['archive']['dir-all'] + os.sep + name
        if yamlconfig['archive']['save'] and not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
        if yamlconfig['archive']['save-all'] and not os.path.exists(self.archive_dir):
            os.makedirs(self.archive_dir)
        self.archive_compress = yamlconfig['archive']['compress']
        self.update_max = 30  # TODO set by config file
        self.update_min = 10  # TODO set by config file
        self.pastie_classname = None
        self.seen_pasties = deque('', 1000)  # max number of pasties ids in memory

    def run(self):
        while not self.kill_received:
            sleep_time = random.randint(self.update_min, self.update_max)
            try:
                # grabs site from queue
                logger.info(
                    'Downloading list of new pastes from {name}. '
                    'Will check again in {time} seconds'.format(
                        name=self.name, time=sleep_time))
                # get the list of last pasties, but reverse it
                # so we first have the old entries and then the new ones
                last_pasties = self.get_last_pasties()
                if last_pasties:
                    for pastie in reversed(last_pasties):
                        queues[self.name].put(pastie)  # add pastie to queue
                    logger.info("Found {amount} new pasties for site {site}. There are now {qsize} pasties to be downloaded.".format(amount=len(last_pasties),
                                                                                                                                     site=self.name,
                                                                                                                                     qsize=queues[self.name].qsize()))
            # catch unknown errors
            except Exception as e:
                msg = 'Thread for {name} crashed unexpectectly, '\
                      'recovering...: {e}'.format(name=self.name, e=e)
                logger.error(msg)
                logger.debug(traceback.format_exc())
            time.sleep(sleep_time)

    def get_last_pasties(self):
        # reset the pasties list
        pasties = []
        # populate queue with data
        response = download_url(self.archive_url)
        htmlPage = response.text
        if not htmlPage:
            logger.warning("No HTML content for page {url}".format(url=self.archive_url))
            return False
        pasties_ids = re.findall(self.archive_regex, htmlPage)
        if pasties_ids:
            for pastie_id in pasties_ids:
                # check if the pastie was already downloaded
                # and remember that we've seen it
                if self.seen_pastie(pastie_id):
                    # do not append the seen things again in the queue
                    continue
                # pastie was not downloaded yet. Add it to the queue
                if self.pastie_classname:
                    class_name = globals()[self.pastie_classname]
                    pastie = class_name(self, pastie_id)
                else:
                    pastie = Pastie(self, pastie_id)
                pasties.append(pastie)
            return pasties
        if "DOES NOT HAVE ACCESS" in htmlPage.encode('utf8'):
            print("Problem with configured IP address")

        logger.error("No last pasties matches for regular expression site:{site} regex:{regex}. Error in your regex? Dumping htmlPage \n {html}".format(site=self.name, regex=self.archive_regex, html=htmlPage.encode('utf8')))
        return False

    def seen_pastie(self, pastie_id):
        ''' check if the pastie was already downloaded. '''
        # first look in memory if we have already seen this pastie
        if self.seen_pasties.count(pastie_id):
            return True
        # look on the filesystem.  # LATER remove this filesystem lookup as it will give problems on long term
        if yamlconfig['archive']['save-all']:
            # check if the pastie was already saved on the disk
            if os.path.exists(verify_directory_exists(self.archive_dir) + os.sep + self.pastie_id_to_filename(pastie_id)):
                return True
        # TODO look in the database if it was already seen

    def seen_pastie_and_remember(self, pastie):
        '''
        Check if the pastie was already downloaded
        and remember that we've seen it
        '''
        seen = False
        if self.seen_pastie(pastie.id):
            seen = True
        else:
            # We have not yet seen the pastie.
            # Keep in memory that we've seen it using
            # appendleft for performance reasons.
            # (faster later when we iterate over the deque)
            self.seen_pasties.appendleft(pastie.id)
        # add / update the pastie in the database
        if db:
            db.queue.put(pastie)
        return seen

    def pastie_id_to_filename(self, pastie_id):
        filename = pastie_id.replace('/', '_')
        if self.archive_compress:
            filename = filename + ".gz"
        return filename


def verify_directory_exists(directory):
    d = datetime.now()
    year = str(d.year)
    month = str(d.month)
    # prefix month and day with "0" if it is only one digit
    if len(month) < 2:
        month = "0" + month
    day = str(d.day)
    if len(day) < 2:
        day = "0" + day
    fullpath = directory + os.sep + year + os.sep + month + os.sep + day
    if not os.path.isdir(fullpath):
        os.makedirs(fullpath)
    return fullpath


class Pastie():
    def __init__(self, site, pastie_id):
        self.site = site
        self.id = pastie_id
        self.pastie_content = None
        self.matches = []
        self.md5 = None
        self.url = self.site.download_url.format(id=self.id)
        self.public = False

    def hash_pastie(self):
        if self.pastie_content:
            try:
                self.md5 = hashlib.md5(self.pastie_content).hexdigest()
                logger.debug('Pastie {site} {id} has md5: "{md5}"'.format(site=self.site.name, id=self.id, md5=self.md5))
            except Exception as e:
                logger.error('Pastie {site} {id} md5 problem: {e}'.format(site=self.site.name, id=self.id, e=e))

    def fetch_pastie(self):
        response = download_url(self.url)
        self.pastie_content = response.content
        return self.pastie_content

    def save_pastie(self, directory):
        if not self.pastie_content:
            raise SystemExit('BUG: Content not set, sannot save')
        full_path = verify_directory_exists(directory) + os.sep + self.site.pastie_id_to_filename(self.id)
        if yamlconfig['redis']['queue']:
            r = redis.StrictRedis(host=yamlconfig['redis']['server'], port=yamlconfig['redis']['port'], db=yamlconfig['redis']['database'])
        if self.site.archive_compress:
            f = gzip.open(full_path, 'w')
            f.write(self.pastie_content.encode('utf8'))
            f.flush()
            os.fsync(f.fileno())
            f.close()
        else:
            f = open(full_path, 'w')
            f.write(self.pastie_content.encode('utf8'))
            f.flush()
            os.fsync(f.fileno())
            f.close()
        if yamlconfig['redis']['queue']:
            time.sleep(3)
            r.lpush('pastes', full_path)
#            with gzip.open(full_path, 'wb') as f:
#                f.write(self.pastie_content)
#                if yamlconfig['redis']['queue']:
#                    r.lpush('pastes', full_path)
#        else:
#            with open(full_path, 'wb') as f:
#                f.write(self.pastie_content)
#                if yamlconfig['redis']['queue']:
#                    r.lpush('pastes', full_path)

    def fetch_and_process_pastie(self):
        # double check if the pastie was already downloaded,
        # and remember that we've seen it
        if self.site.seen_pastie(self.id):
            return None
        # download pastie
        self.fetch_pastie()
        # save the pastie on the disk
        if self.pastie_content:
            # take checksum
            self.hash_pastie()
            # keep in memory that the pastie was seen successfully
            self.site.seen_pastie_and_remember(self)
            # Save pastie to archive dir if configured
            if yamlconfig['archive']['save-all']:
                self.save_pastie(self.site.archive_dir)
            # search for data in pastie
            self.search_content()
        return self.pastie_content

    def search_content(self):
        if not self.pastie_content:
            raise SystemExit('BUG: Content not set, cannot search')
            return False
        # search for the regexes in the htmlPage
        for regex in yamlconfig['search']:
            # LATER first compile regex, then search using compiled version
            regex_flags = re.IGNORECASE
            if 'regex-flags' in regex:
                regex_flags = eval(regex['regex-flags'])
            m = re.findall(regex['search'].encode(), self.pastie_content, regex_flags)
            if m:
                # the regex matches the text
                # ignore if not enough counts
                if 'count' in regex and len(m) < int(regex['count']):
                    continue
                # ignore if exclude
                if 'exclude' in regex and re.search(regex['exclude'].encode(), self.pastie_content, regex_flags):
                    continue
                # we have a match, add to match list
                self.matches.append(regex)
                if 'public' in regex:
                    self.public = regex['public']
                else:
                    self.public = False
        if self.matches:
            self.action_on_match()

    def action_on_match(self):
        msg = 'Found hit for {matches} in pastie {url}'.format(
            matches=self.matches_to_text(), url=self.url)
        logger.info(msg)
        # store info in DB
        if db:
            db.queue.put(self)
        # Save pastie to disk if configured
        if yamlconfig['archive']['save']:
            self.save_pastie(self.site.save_dir)
        # Send email alert if configured
        if yamlconfig['email']['alert']:
            self.send_email_alert()

    def matches_to_text(self):
        descriptions = []
        for match in self.matches:
            if 'description' in match:
                descriptions.append(match['description'])
            else:
                descriptions.append(match['search'])
        if descriptions:
            return '[{}]'.format(', '.join(descriptions.decode('utf-8', 'ignore')))
        else:
            return ''

    def matches_to_regex(self):
        descriptions = []
        for match in self.matches:
            descriptions.append(match['search'])
        if descriptions:
            return '[{}]'.format(', '.join(descriptions.decode('utf-8', 'ignore')))
        else:
            return ''

    def send_email_alert(self):
        msg = MIMEMultipart()
        if self.public:
            alert = "Found hit for {matches} in pastie {url}".format(matches=self.matches_to_text(), url=self.url)
        else:
            alert = "Found hit in pastie {url}".format(url=self.url)
        # headers
        msg['Subject'] = yamlconfig['email']['subject'].format(subject=alert)
        msg['From'] = yamlconfig['email']['from']
        # build the list of recipients
        recipients = []
        recipients.append(yamlconfig['email']['to'])  # first the global alert email
        for match in self.matches:                    # per match, the custom additional email
            if 'to' in match and match['to']:
                recipients.extend(match['to'].split(","))
        msg['Bcc'] = ','.join(recipients)  # here the list needs to be comma separated
        # message body including full paste rather than attaching it
        message = '''
I found a hit for a regular expression on one of the pastebin sites.

The site where the paste came from :        {site}
The original paste was located here:        {url}
And the regular expressions that matched:   [redacted]

Below (after newline) is the content of the pastie:

{content}

        '''.format(site=self.site.name, url=self.url, content=self.pastie_content.encode('utf8'))
        # '''.format(site=self.site.name, url=self.url, matches=self.matches_to_regex(), content=self.pastie_content.encode('utf8'))
        msg.attach(MIMEText(message))
        # send out the mail
        try:
            s = smtplib.SMTP(yamlconfig['email']['server'], yamlconfig['email']['port'])
            # login to the SMTP server if configured
            if 'username' in yamlconfig['email'] and yamlconfig['email']['username']:
                s.login(yamlconfig['email']['username'], yamlconfig['email']['password'])
            # send the mail
            s.sendmail(yamlconfig['email']['from'], recipients, msg.as_string())
            s.close()
        except smtplib.SMTPException as e:
            logger.error("ERROR: unable to send email: {0}".format(e))
        except Exception as e:
            logger.error("ERROR: unable to send email. Are your email setting correct?: {e}".format(e=e))


class ThreadPasties(threading.Thread):
    '''
    Instances of these threads are responsible for downloading the pastes
    found in the queue.
    '''
    def __init__(self, queue, queue_name):
        threading.Thread.__init__(self)
        self.queue = queue
        self.name = queue_name
        self.kill_received = False

    def run(self):
        while not self.kill_received:
            try:
                # grabs pastie from queue
                pastie = self.queue.get()
                pastie_content = pastie.fetch_and_process_pastie()
                logger.debug("Queue {name} size: {size}".format(
                    size=self.queue.qsize(), name=self.name))
                if pastie_content:
                    logger.debug(
                        "Saved new pastie from {0} "
                        "with id {1}".format(self.name, pastie.id))
                else:
                    # pastie already downloaded OR error ?
                    pass
                # signals to queue job is done
                self.queue.task_done()
            # catch unknown errors
            except Exception as e:
                msg = "ThreadPasties for {name} crashed unexpectectly, "\
                      "recovering...: {e}".format(name=self.name, e=e)
                logger.error(msg)
                logger.debug(traceback.format_exc())


def main():
    global queues
    global threads
    global db
    queues = {}
    threads = []

    # start a thread to handle the DB data
    db = None
    if yamlconfig['db'] and yamlconfig['db']['sqlite3'] and yamlconfig['db']['sqlite3']['enable']:
        try:
            global sqlite3
            import sqlite3
        except Exception as exc:
            exit('ERROR: Cannot import the sqlite3 Python library. Are you sure it is compiled in python?')
        db = Sqlite3Database(yamlconfig['db']['sqlite3']['file'])
        db.setDaemon(True)
        threads.append(db)
        db.start()
    # test()
    # Build array of enabled sites.
    sites_enabled = []
    for site in yamlconfig['site']:
        if yamlconfig['site'][site]['enable']:
            print("Site: {} is enabled, adding to pool...".format(site))
            sites_enabled.append(site)
        elif not yamlconfig['site'][site]['enable']:
            print("Site: {} is disabled.".format(site))
        else:
            print("Site: {} is not enabled or disabled in config file. We just assume it disabled.".format(site))
    # spawn a pool of threads per PastieSite, and pass them a queue instance
    for site in sites_enabled:
        queues[site] = Queue()
        for i in range(yamlconfig['threads']):
            t = ThreadPasties(queues[site], site)
            t.setDaemon(True)
            threads.append(t)
            t.start()

    # build threads to download the last pasties
    for site_name in sites_enabled:
        t = PastieSite(site_name,
                       yamlconfig['site'][site_name]['download-url'],
                       yamlconfig['site'][site_name]['archive-url'],
                       yamlconfig['site'][site_name]['archive-regex'])
        if 'update-min' in yamlconfig['site'][site_name] and yamlconfig['site'][site_name]['update-min']:
            t.update_min = yamlconfig['site'][site_name]['update-min']
        if 'update-max' in yamlconfig['site'][site_name] and yamlconfig['site'][site_name]['update-max']:
            t.update_max = yamlconfig['site'][site_name]['update-max']
        if 'pastie-classname' in yamlconfig['site'][site_name] and yamlconfig['site'][site_name]['pastie-classname']:
            t.pastie_classname = yamlconfig['site'][site_name]['pastie-classname']
        threads.append(t)
        t.setDaemon(True)
        t.start()

    # wait while all the threads are running and someone sends CTRL+C
    while True:
        try:
            for t in threads:
                t.join(1)
        except KeyboardInterrupt:
            print('')
            print("Ctrl-c received! Sending kill to threads...")
            for t in threads:
                t.kill_received = True
            exit(0)  # quit immediately


user_agents_list = []


def load_user_agents_from_file(filename):
    global user_agents_list
    try:
        f = open(filename)
    except Exception as e:
        logger.error('Configuration problem: user-agent-file "{file}" not found or not readable: {e}'.format(file=filename, e=e))
    for line in f:
        line = line.strip()
        if line:
            user_agents_list.append(line)
    logger.debug('Found {count} UserAgents in file "{file}"'.format(file=filename, count=len(user_agents_list)))


def get_random_user_agent():
    global proxies_list
    if user_agents_list:
        return random.choice(user_agents_list)
    return None


proxies_failed = []
proxies_lock = threading.Lock()
proxies_list = []


def load_proxies_from_file(filename):
    global proxies_list
    try:
        f = open(filename)
    except Exception as e:
        logger.error('Configuration problem: proxyfile "{file}" not found or not readable: {e}'.format(file=filename, e=e))
    for line in f:
        line = line.strip()
        if line:  # LATER verify if the proxy line has the correct structure
            proxies_list.append(line)
    logger.debug('Found {count} proxies in file "{file}"'.format(file=filename, count=len(proxies_list)))


def get_random_proxy():
    global proxies_list
    proxy = None
    proxies_lock.acquire()
    if proxies_list:
        proxy = random.choice(proxies_list)
    proxies_lock.release()
    return proxy


def failed_proxy(proxy):
    proxies_failed.append(proxy)
    if proxies_failed.count(proxy) >= 2 and proxies_list.count(proxy) >= 1:
        logger.info("Removing proxy {0} from proxy list because of to many errors errors.".format(proxy))
        proxies_lock.acquire()
        proxies_list.remove(proxy)
        proxies_lock.release()


class NoRedirectHandler(urllib2.HTTPRedirectHandler):
    '''
    This class is only necessary to not follow HTTP redirects in webpages.
    It is used by the download_url() function
    '''
    def http_error_302(self, req, fp, code, msg, headers):
        infourl = urllib2.addinfourl(fp, headers, req.get_full_url())
        infourl.status = code
        infourl.code = code
        return infourl
    http_error_301 = http_error_303 = http_error_307 = http_error_302


class TLS1Connection(httplib.HTTPSConnection):
    """Like HTTPSConnection but more specific"""
    def __init__(self, host, **kwargs):
        httplib.HTTPSConnection.__init__(self, host, **kwargs)

    def connect(self):
        """Overrides HTTPSConnection.connect to specify TLS version"""
        # Standard implementation from HTTPSConnection, which is not
        # designed for extension, unfortunately
        sock = socket.create_connection(
            (self.host, self.port),
            self.timeout, self.source_address)
        if getattr(self, '_tunnel_host', None):
            self.sock = sock
            self._tunnel()

        # This is the only difference; default wrap_socket uses SSLv23
        self.sock = ssl.wrap_socket(
            sock, self.key_file, self.cert_file,
            ssl_version=ssl.PROTOCOL_TLSv1)


class TLS1Handler(urllib2.HTTPSHandler):
    """Like HTTPSHandler but more specific"""
    def __init__(self):
        urllib2.HTTPSHandler.__init__(self)

    def https_open(self, req):
        return self.do_open(TLS1Connection, req)


def download_url(url, data=None, cookie=None, loop_client=0, loop_server=0, loop_paste=0):
    # Client errors (40x): if more than 5 recursions, give up on URL (used for the 404 case)
    if loop_client >= retries_client:
        return None
    # Server errors (50x): if more than 100 recursions, give up on URL
    if loop_server >= retries_server:
        return None

    session = requests.Session()
    random_proxy = get_random_proxy()
    if random_proxy:
        session.proxies = {'http': random_proxy}
    user_agent = get_random_user_agent()
    session.headers.update({'User-Agent': get_random_user_agent(), 'Accept-Charset': 'utf-8'})
    if cookie:
        session.headers.update({'Cookie': cookie})
    if data:
        session.headers.update(data)
    logger.debug('Downloading url: {url} with proxy: {proxy} and user-agent: {ua}'.format(url=url, proxy=random_proxy, ua=user_agent))
    try:
        opener = None
        # urllib2.install_opener(urllib2.build_opener(TLS1Handler()))

        # Random Proxy if set in config
        random_proxy = get_random_proxy()
        if random_proxy:
            proxyh = urllib2.ProxyHandler({'http': random_proxy})
            opener = urllib2.build_opener(proxyh, NoRedirectHandler())
        # We need to create an opener if it didn't exist yet
        if not opener:
            opener = urllib2.build_opener(NoRedirectHandler())
        # Random User-Agent if set in config
        user_agent = get_random_user_agent()
        opener.addheaders = [('Accept-Charset', 'utf-8')]
        if user_agent:
            opener.addheaders.append(('User-Agent', user_agent))
        if cookie:
            opener.addheaders.append(('Cookie', cookie))
        logger.debug(
            'Downloading url: {url} with proxy: {proxy} and user-agent: {ua}'.format(
                url=url, proxy=random_proxy, ua=user_agent))
        if data:
            response = opener.open(url, data)
        else:
            response = opener.open(url)
        htmlPage = unicode(response.read(), errors='replace')
        if 'File is not ready for scraping yet. Try again in 1 minute.' in htmlPage:
            if loop_paste >= retries_paste:
                logger.warning("Tried to scrape too early for {url}, giving up and saving current content".format(url=url))
                return htmlPage, response.headers
            else:
                loop_paste += 1
                logger.warning("Tried to scrape too early for {url}, trying again in 60s ({nb}/{total})".format(url=url, nb=loop_paste, total=retries_paste))
                time.sleep(60)
                return download_url(url, loop_paste=loop_paste)
        return htmlPage, response.headers
    except urllib2.HTTPError, e:
        failed_proxy(random_proxy)
        logger.warning("!!Proxy error on {url} for proxy {proxy}.".format(url=url, proxy=random_proxy))
        if 404 == e.code:
            htmlPage = e.read()
            logger.warning("404 from proxy received for {url}. Waiting 1 minute".format(url=url))
            time.sleep(60)
            loop_client += 1
            logger.warning("Retry {nb}/{total} for {url}".format(nb=loop_client, total=retries_client, url=url))
            return download_url(url, loop_client=loop_client)
        if 500 == e.code:
            htmlPage = e.read()
            logger.warning("500 from proxy received for {url}. Waiting 1 minute".format(url=url))
            time.sleep(60)
            loop_server += 1
            logger.warning("Retry {nb}/{total} for {url}".format(nb=loop_server, total=retries_server, url=url))
            return download_url(url, loop_server=loop_server)
        if 504 == e.code:
            htmlPage = e.read()
            logger.warning("504 from proxy received for {url}. Waiting 1 minute".format(url=url))
            time.sleep(60)
            loop_server += 1
            logger.warning("Retry {nb}/{total} for {url}".format(nb=loop_server, total=retries_server, url=url))
            return download_url(url, loop_server=loop_server)
        if 502 == e.code:
            htmlPage = e.read()
            logger.warning("502 from proxy received for {url}. Waiting 1 minute".format(url=url))
            time.sleep(60)
            loop_server += 1
            logger.warning("Retry {nb}/{total} for {url}".format(nb=loop_server, total=retries_server, url=url))
            return download_url(url, loop_server=loop_server)
        if 403 == e.code:
            htmlPage = e.read()
            if 'Please slow down' in htmlPage or 'has temporarily blocked your computer' in htmlPage or 'blocked' in htmlPage:
                logger.warning("Slow down message received for {url}. Waiting 1 minute".format(url=url))
                time.sleep(60)
                loop_server += 1
                logger.warning("Retry {nb}/{total} for {url}".format(nb=loop_server, total=retries_server, url=url))
                return download_url(url, loop_server=loop_server)
            if 504 == e.code:
                htmlPage = e.read()
                logger.warning("504 from proxy received for {url}. Waiting 1 minute".format(url=url))
                time.sleep(60)
                loop_server += 1
                logger.warning("Retry {nb}/{total} for {url}".format(nb=loop_server, total=retries_server, url=url))
                return download_url(url, loop_server=loop_server)
            if 502 == e.code:
                htmlPage = e.read()
                logger.warning("502 from proxy received for {url}. Waiting 1 minute".format(url=url))
                time.sleep(60)
                loop_server += 1
                logger.warning("Retry {nb}/{total} for {url}".format(nb=loop_server, total=retries_server, url=url))
                return download_url(url, loop_server=loop_server)
            if 403 == e.code:
                htmlPage = e.read()
                if 'Please slow down' in htmlPage or 'has temporarily blocked your computer' in htmlPage or 'blocked' in htmlPage:
                    logger.warning("Slow down message received for {url}. Waiting 1 minute".format(url=url))
                    time.sleep(60)
                    return download_url(url)
            logger.warning("ERROR: HTTP Error ##### {e} ######################## {url}".format(e=e, url=url))
            return None
        return response
    except URLError as e:
        logger.debug("ERROR: URL Error ##### {e} ######################## ".format(e=e, url=url))
        if random_proxy:  # remove proxy from the list if needed
            failed_proxy(random_proxy)
            logger.warning("Failed to download the page {url} because of proxy error {proxy}. Trying again.".format(url=url, proxy=random_proxy))
            loop_server += 1
            logger.warning("Retry {nb}/{total} for {url}".format(nb=loop_server, total=retries_server, url=url))
            return download_url(url, loop_server=loop_server)
        if 'timed out' in e.reason:
            logger.warning("Timed out or slow down for {url}. Waiting 1 minute".format(url=url))
            loop_server += 1
            logger.warning("Retry {nb}/{total} for {url}".format(nb=loop_server, total=retries_server, url=url))
            time.sleep(60)
            return download_url(url, loop_server=loop_server)
        return None
    except socket.timeout:
        logger.debug("ERROR: timeout ############################# " + url)
        if random_proxy:  # remove proxy from the list if needed
            failed_proxy(random_proxy)
            logger.warning("Failed to download the page because of socket error {0} trying again.".format(url))
            loop_server += 1
            logger.warning("Retry {nb}/{total} for {url}".format(nb=loop_server, total=retries_server, url=url))
            return download_url(url, loop_server=loop_server)
        return None
    except Exception as e:
        failed_proxy(random_proxy)
        logger.warning("Failed to download the page because of other HTTPlib error proxy error {0} trying again.".format(url))
        loop_server += 1
        logger.warning("Retry {nb}/{total} for {url}".format(nb=loop_server, total=retries_server, url=url))
        return download_url(url, loop_server=loop_server)
        # logger.error("ERROR: Other HTTPlib error: {e}".format(e=e))
        # return None, None
    # do NOT try to download the url again here, as we might end in enless loop


class Sqlite3Database(threading.Thread):
    def __init__(self, filename):
        threading.Thread.__init__(self)
        self.kill_received = False
        self.queue = Queue()
        self.filename = filename
        self.db_conn = None
        self.c = None

    def run(self):
        self.db_conn = sqlite3.connect(self.filename)
        # create the db if it doesn't exist
        self.c = self.db_conn.cursor()
        try:
            # LATER maybe create a table per site. Lookups will be faster as less text-searching is needed
            self.c.execute('''
                CREATE TABLE IF NOT EXISTS pasties (
                    site TEXT,
                    id TEXT,
                    md5 TEXT,
                    url TEXT,
                    local_path TEXT,
                    timestamp DATE,
                    matches TEXT
                    )''')
            self.db_conn.commit()
        except sqlite3.DatabaseError as e:
            logger.error('Problem with the SQLite database {0}: {1}'.format(self.filename, e))
            return None
        # loop over the queue
        while not self.kill_received:
            try:
                # grabs pastie from queue
                pastie = self.queue.get()
                # add the pastie to the DB
                self.add_or_update(pastie)
                # signals to queue job is done
                self.queue.task_done()
            # catch unknown errors
            except Exception as e:
                logger.error("Thread for SQLite crashed unexpectectly, recovering...: {e}".format(e=e))
                logger.debug(traceback.format_exc())

    def add_or_update(self, pastie):
        data = {'site': pastie.site.name,
                'id': pastie.id
                }
        self.c.execute('SELECT count(id) FROM pasties WHERE site=:site AND id=:id', data)
        pastie_in_db = self.c.fetchone()
        # logger.debug('State of Database for pastie {site} {id} - {state}'.format(site=pastie.site.name, id=pastie.id, state=pastie_in_db))
        if pastie_in_db and pastie_in_db[0]:
            self.update(pastie)
        else:
            self.add(pastie)

    def add(self, pastie):
        try:
            data = {'site': pastie.site.name,
                    'id': pastie.id,
                    'md5': pastie.md5,
                    'url': pastie.url,
                    'local_path': pastie.site.archive_dir + os.sep + pastie.site.pastie_id_to_filename(pastie.id),
                    'timestamp': datetime.now(),
                    'matches': pastie.matches_to_text()
                    }
            self.c.execute('INSERT INTO pasties VALUES (:site, :id, :md5, :url, :local_path, :timestamp, :matches)', data)
            self.db_conn.commit()
        except sqlite3.DatabaseError as e:
            logger.error('Cannot add pastie {site} {id} in the SQLite database: {error}'.format(site=pastie.site.name, id=pastie.id, error=e))
        logger.debug('Added pastie {site} {id} in the SQLite database.'.format(site=pastie.site.name, id=pastie.id))

    def update(self, pastie):
        try:
            data = {'site': pastie.site.name,
                    'id': pastie.id,
                    'md5': pastie.md5,
                    'url': pastie.url,
                    'local_path': pastie.site.archive_dir + os.sep + pastie.site.pastie_id_to_filename(pastie.id),
                    'timestamp': datetime.now(),
                    'matches': pastie.matches_to_text()
                    }
            self.c.execute('''UPDATE pasties SET md5 = :md5,
                                            url = :url,
                                            local_path = :local_path,
                                            timestamp  = :timestamp,
                                            matches = :matches
                     WHERE site = :site AND id = :id''', data)
            self.db_conn.commit()
        except sqlite3.DatabaseError as e:
            logger.error('Cannot add pastie {site} {id} in the SQLite database: {error}'.format(site=pastie.site.name, id=pastie.id, error=e))
        logger.debug('Updated pastie {site} {id} in the SQLite database.'.format(site=pastie.site.name, id=pastie.id))


def parse_config_file(configfile):
    global yamlconfig
    try:
        yamlconfig = yaml.load(open(configfile))
    except yaml.YAMLError as exc:
        logger.error("Error in configuration file:")
        if hasattr(exc, 'problem_mark'):
            mark = exc.problem_mark
            logger.error("error position: (%s:%s)" % (mark.line + 1, mark.column + 1))
            exit(1)
    # TODO verify validity of config parameters
    for includes in yamlconfig.get("includes", []):
        yamlconfig.update(yaml.load(open(includes)))
    if yamlconfig['proxy']['random']:
        load_proxies_from_file(yamlconfig['proxy']['file'])
    if yamlconfig['user-agent']['random']:
        load_user_agents_from_file(yamlconfig['user-agent']['file'])
    # if yamlconfig['redis']['queue']:
    #    import redis


if __name__ == "__main__":
    global logger
    parser = optparse.OptionParser("usage: %prog [options]")
    parser.add_option("-c", "--config", dest="config",
                      help="load configuration from file", metavar="FILE")
    parser.add_option("-d", "--daemon", action="store_true", dest="daemon",
                      help="runs in background as a daemon (NOT IMPLEMENTED)")
    parser.add_option("-s", "--stats", action="store_true", dest="stats",
                      help="display statistics about the running threads (NOT IMPLEMENTED)")
    parser.add_option("-v", action="store_true", dest="verbose",
                      help="outputs more information")

    (options, args) = parser.parse_args()

    if not options.config:
        # try to read out the default configuration files if -c option is not set
        if os.path.isfile('/etc/pystemon.yaml'):
            options.config = '/etc/pystemon.yaml'
        if os.path.isfile('pystemon.yaml'):
            options.config = 'pystemon.yaml'
        filename = sys.argv[0]
        config_file = filename.replace('.py', '.yaml')
        if os.path.isfile(config_file):
            options.config = config_file
    if not os.path.isfile(options.config):
        parser.error('Configuration file not found. Please create /etc/pystemon.yaml, pystemon.yaml or specify a config file using the -c option.')
        exit(1)

    logger = logging.getLogger('pystemon')
    logger.setLevel(logging.DEBUG)
    hdlr = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('[%(asctime)s] %(message)s')
    hdlr.setFormatter(formatter)
    logger.addHandler(hdlr)
    if options.verbose:
        logger.setLevel(logging.DEBUG)

    if options.daemon:
        # send logging to syslog if using daemon
        logger.addHandler(logging.handlers.SysLogHandler(facility=logging.handlers.SysLogHandler.LOG_DAEMON))
        # FIXME run application in background

    parse_config_file(options.config)
    # run the software
    main()
