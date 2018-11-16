#! /usr/bin/python

import os
import time
import logging
import re
import json
import xml.etree.ElementTree as ET
import threading
from queue import Queue, Empty

from watchdog.observers import Observer
from watchdog.events import RegexMatchingEventHandler
import schedule
import twitter
from requests.exceptions import RequestException


os.chdir(os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s [%(thread)d] %(levelname)s: %(message)s')

fh = logging.FileHandler('bot.log')
fh.setLevel(logging.DEBUG)
fh.setFormatter(formatter)
logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
logger.addHandler(ch)


BASE_DIR = 'c:/Games'
SECRET_FILE = 'secret.json'
TARGET_FILE = 'version.xml'
CACHE_FILE = 'cache.json'

SCRIPT_NAME = os.path.basename(__file__)

FILE_PATTERN = (BASE_DIR + r'/(World_of_Tanks(_\w+)?)/' + TARGET_FILE.replace('.', r'\.')).replace('/', r'\\')

WOT_FOLDERS = {
    'World_of_Tanks':       'SG',
    'World_of_Tanks_EU':    'EU',
    'World_of_Tanks_RU':    'RU',
    'World_of_Tanks_NA':    'NA',
    'World_of_Tanks_CT':    'CT',
    'World_of_Tanks_SB':    'SB'
}

#TWEET_MESSAGE_TEMPLATE = 'BOT TEST MESSAGE: ({region}: {version} {build})'
TWEET_MESSAGE_TEMPLATE = 'WoT client is updated.\n{region}: {version} {build}\n#WoT #wotversion'


class Cache(object):
    def __init__(self, file):
        self.__cacheFileName = file
        self.__cache = None

    def fetch(self):
        try:
            with open(self.__cacheFileName, 'r') as fp:
                self.__cache = json.load(fp)
            logger.info(f'cache fetch: {json.dumps(self.__cache)}')
        except FileNotFoundError:
            logger.info(f'cache fetch: cache is not found')

    def update(self, region, version, build, done):
        if self.__cache is None:
            self.__cache = {}
        self.__cache[region] = [version, build, done]
        with open(self.__cacheFileName, 'w') as fp:
            json.dump(self.__cache, fp)
        logger.info(f'cache update: {json.dumps(self.__cache)}')

    def compare(self, region, version, build):
        if self.__cache is None or region not in self.__cache:
            return True
        cache = self.__cache[region]
        if cache and cache[0:2] == [version, build]:
            return False
        return True

    def isDone(self, region):
        if self.__cache is None or region not in self.__cache:
            return False
        cache = self.__cache[region]
        if cache and cache[2]:
            return True
        return False


class Token(object):
    type = None
    
    def __init__(self, data):
        self._data = data

    def isEvent(self):
        return self.type == 'event'


class PathToken(Token):
    type = 'path'

    @property
    def data(self):
        return self._data


class EventToken(Token):
    type = 'event'

    @property
    def data(self):
        return self._data.__class__.__name__


class MyHandler(RegexMatchingEventHandler):
    def __init__(self, queue, regexes):
        super(MyHandler, self).__init__(regexes=regexes)
        self.queue = queue

    def on_created(self, event):
        path = event.src_path
        logger.debug(f'on_created: {path}')
        self.queue.put(PathToken(path))

    def on_modified(self, event):
        path = event.src_path
        logger.debug(f'on_modified: {path}')
        self.queue.put(PathToken(path))


class WotVersionBot(object):
    def __init__(self):
        self.__queue = Queue()
        self.__cache = Cache(CACHE_FILE)
        self.__cache.fetch()
        with open(SECRET_FILE, 'r') as fp:
            secret = json.load(fp)
        self.twitterApi = twitter.Api(
                consumer_key=secret['CONSUMER_KEY'],
                consumer_secret=secret['CONSUMER_SECRET'],
                access_token_key=secret['ACCESS_TOKEN_KEY'],
                access_token_secret=secret['ACCESS_TOKEN_SECRET'])

    def checkCurrentState(self):
        for folder in WOT_FOLDERS:
            path = os.path.normpath(os.path.join(BASE_DIR, folder, TARGET_FILE))
            logger.debug(f'check file status: {path}')
            self.__queue.put(PathToken(path))

    def startObserver(self, path):
        logger.info('observer start')
        event_handler = MyHandler(self.__queue, regexes=[FILE_PATTERN])
        observer = Observer()
        observer.schedule(event_handler, os.path.normpath(path), recursive=True)
        observer.start()
        thread = threading.Thread(target=self.worker)
        thread.start()
        logger.debug(f'thread num = {threading.active_count()}')
        schedule.every(6).hours.do(lambda: self.checkCurrentState())
        self.checkCurrentState()
        try:
            while True:
                schedule.run_pending()
                time.sleep(60)
        except KeyboardInterrupt as e:
            logger.info(f'KeyboardInterrupt')
            observer.stop()
            self.__queue.put(EventToken(e))
        observer.join()
        thread.join()

    def worker(self):
        while True:
            token = self.__queue.get()
            logger.debug(f"queue: {{type: '{token.type}', data: '{token.data}'}}")
            if token.isEvent():
                break
            try:
                versionInfo = self.getVersion(token.data)
            except FileNotFoundError:
                continue
            if versionInfo is None:
                continue
            region, version, build = versionInfo
            if self.__cache.compare(region, version, build):
                logger.debug(f'version is changed: {[region, version, build]}')
            else:
                logger.debug(f'version is not change: {[region, version, build]}')
                if self.__cache.isDone(region):
                    continue
                logger.debug(f'version is not tweeted: {[region, version, build]}')
            self.__cache.update(region, version, build, False)
            message = TWEET_MESSAGE_TEMPLATE.format(region=region, version=version, build=build)
            try:
                result = self.postTwitter(message)
                self.__cache.update(region, version, build, result)
                self.__queue.task_done()
            except twitter.error.TwitterError:
                pass

    def postTwitter(self, message):
        logger.debug('twitter post: {}'.format(message.replace('\n', '\\n')))
        try:
            status = self.twitterApi.PostUpdate(message)
        except twitter.error.TwitterError as error:
            logger.error(f'TwitterError: {error}')
            if error.message[0]['code'] == 187:   # Status is a duplicate.
                return True
            raise error
        except RequestException as error:
            logger.error(f'RequestException: {error}')
            #raise error
            return False
        return True

    def getVersion(self, path):
        # detect region
        match = re.search(FILE_PATTERN, path, flags=re.IGNORECASE)
        if not match:
            return None
        region = WOT_FOLDERS[match.group(1)]
        # read version
        version = ET.parse(path).findtext('version')
        match = re.search(r'v\.(\S+)[^#]+(#\d+)', version)
        if not match:
            return None
        result = [ region, match.group(1), match.group(2) ]
        logger.debug(f'read file: {path}: {json.dumps(result)}')
        return result


if __name__ == '__main__':
    logger.info(f'### {SCRIPT_NAME} start ###')
    logger.debug(f'CWD = {os.getcwd()}')
    bot = WotVersionBot()
    bot.startObserver(BASE_DIR)
    logger.info(f'### {SCRIPT_NAME} end ###')
