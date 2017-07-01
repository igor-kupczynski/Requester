import sublime

import os
import sys
import imp
import re
import json
from collections import OrderedDict
from threading import Thread
from time import time
from queue import Queue

from .responses import ResponseThreadPool
from .parsers import prepare_request


class RequestCommandMixin:
    """This mixin is the motor for parsing an env, executing requests in parallel
    in the context of this env, invoking activity indicator methods, and invoking
    response handling methods. These methods can be overridden to control the
    behavior of classes that inherit from this mixin.

    It must be mixed in to classes that also inherit from
    `sublime_plugin.TextCommand`.
    """
    REFRESH_MS = 200  # period of checks on async operations, e.g. requests
    ACTIVITY_SPACES = 9  # number of spaces in activity indicator
    MAX_WORKERS = 10  # default request concurrency
    PREPARE_REQUESTS = True
    RESPONSE_POOLS = Queue()
    MAX_NUM_RESPONSE_POOLS = 10  # up to N response pools can be stored

    def get_requests(self):
        """This must be overridden to return a list of request strings.
        """
        raise NotImplementedError(
            '"get_requests" must be overridden to return a list of request strings')

    def show_activity_for_pending_requests(self, requests, count, activity):
        """Override this method to customize user feedback for pending requests.
        `activity` string is passed for convenience, it is generated by
        `get_activity_indicator`.
        """
        pass

    def handle_response(self, response, num_requests):
        """Override this method to handle a response from a single request. This
        method is called as each response is returned.
        """
        pass

    def handle_responses(self, responses):
        """Override this method to handle responses from all requests executed.
        This method is called after all responses have been returned.
        """
        pass

    def default_handle_errors(self, responses):
        """Override this method to change Requester's default error handling. This
        is a convenience method that is called on all responses after they are
        returned.
        """
        errors = ['{}\n{}'.format(r.request, r.error) for r in responses if r.error]
        if errors:
            sublime.error_message('\n\n'.join(errors))

    def run(self, edit):
        self.reset_status()
        self.config = sublime.load_settings('Requester.sublime-settings')
        # `run` runs first, which means `self.config` is available to all methods
        self.reset_env_string()
        self.reset_file()
        self.reset_env_file()
        thread = Thread(target=self._get_env)
        thread.start()
        self._run(thread)

    def _run(self, thread, count=0):
        """Evaluate environment in a separate thread and show an activity
        indicator. Inspect thread at regular intervals until it's finished, at
        which point `make_requests` can be invoked. Return if thread times out.
        """
        REFRESH_MULTIPLIER = 4
        activity = self.get_activity_indicator(count//REFRESH_MULTIPLIER, self.ACTIVITY_SPACES)
        if count > 0:  # don't distract user with RequesterEnv status if env can be evaluated quickly
            self.view.set_status('requester.activity', '{} {}'.format('RequesterEnv', activity))

        if thread.is_alive():
            timeout = self.config.get('timeout_env', None)
            if timeout is not None and count * self.REFRESH_MS/REFRESH_MULTIPLIER > timeout * 1000:
                sublime.error_message('Timeout Error: environment took too long to parse')
                self.view.set_status('requester.activity', '')
                return
            sublime.set_timeout(lambda: self._run(thread, count+1), self.REFRESH_MS/REFRESH_MULTIPLIER)

        else:
            requests = self.get_requests()
            if not self.is_requester_view() and self.PREPARE_REQUESTS:
                requests = [prepare_request(
                    r, timeout=self.config.get('timeout', None)
                ) for r in requests]
            self.view.set_status('requester.activity', '')
            self.make_requests(requests, self._env)

    def is_requester_view(self):
        """Was this view opened by a Requester command? This is useful, e.g., to
        avoid resetting `env_file` and `env_string` on these views.
        """
        if self.view.settings().get('requester.response_view', False):
            return True
        if self.view.settings().get('requester.test_view', False):
            return True
        return False

    def reset_env_string(self):
        """(Re)sets the `requester.env_string` setting on the view, if appropriate.
        """
        if self.is_requester_view():
            return
        env_string = self.parse_env_block(self.view.substr(
            sublime.Region(0, self.view.size())
        ))
        self.view.settings().set('requester.env_string', env_string)

    def reset_file(self):
        """(Re)sets the `requester.file` setting on the view, if appropriate.
        """
        if self.is_requester_view():
            return
        self.view.settings().set('requester.file', self.view.file_name())

    def reset_env_file(self):
        """(Re)sets the `requester.env_file` setting on the view, if appropriate.
        """
        if self.is_requester_view():
            return

        scope = {}
        p = re.compile('\s*env_file\s*=.*')  # `env_file` can be overridden from within requester file
        for line in self.view.substr(
                sublime.Region(0, self.view.size())
        ).splitlines():
            if p.match(line):  # matches only at beginning of string
                try:
                    exec(line, scope)  # add `env_file` to `scope` dict
                except:
                    pass
                break  # stop looking after first match

        env_file = scope.get('env_file')
        if env_file:
            env_file = str(env_file)
            if os.path.isabs(env_file):
                self.view.settings().set('requester.env_file', env_file)
            else:
                file_path = self.view.file_name()
                if file_path:
                    self.view.settings().set('requester.env_file',
                                             os.path.join(os.path.dirname(file_path), env_file))
        else:
            self.view.settings().set('requester.env_file', None)

    def get_env(self):
        """Computes an env from various settings: `requester.env_string`,
        `requester.file`, `requester.env_file`, settings. Returns an env
        dictionary.

        http://stackoverflow.com/questions/67631/how-to-import-a-module-given-the-full-path
        """
        env_string = self.view.settings().get('requester.env_string', None)
        env_dict = self.get_env_dict_from_string(env_string)

        file = self.view.settings().get('requester.file', None)
        if file:
            try:
                with open(file, 'r') as f:
                    text = f.read()
            except Exception as e:
                self.add_error_status_bar(str(e))
            else:
                env_block = self.parse_env_block(text)
                # env computed from `file` takes precedence over `env_string`
                env_dict.update(self.get_env_dict_from_string(env_block))

        env_file = self.view.settings().get('requester.env_file', None)
        if env_file:
            try:
                env = imp.load_source('requester.env', env_file)
            except Exception as e:
                self.add_error_status_bar(str(e))
            else:
                env_dict_ = vars(env)
                env_dict.update(env_dict_)  # env computed from `env_file` takes precedence
        return env_dict or None

    def _get_env(self):
        """Wrapper calls `get_env` and assigns return value to instance property.
        """
        self._env = self.get_env()

    def set_env_settings_on_view(self, view):
        """Convenience method that copies env settings from this view to `view`.
        """
        for setting in ['requester.file', 'requester.env_string', 'requester.env_file']:
            view.settings().set(setting, self.view.settings().get(setting, None))

    def make_requests(self, requests, env=None):
        """Make requests concurrently using a `ThreadPool`, which itself runs on
        an alternate thread so as not to block the UI.
        """
        pools = self.RESPONSE_POOLS
        pool = ResponseThreadPool(requests, env, self.MAX_WORKERS)  # pass along env vars to thread pool
        pools.put(pool)
        while pools.qsize() > self.MAX_NUM_RESPONSE_POOLS:
            old_pool = pools.get()
            old_pool.is_done = True  # don't display responses for a pool which has already been removed
        sublime.set_timeout_async(lambda: pool.run(), 0)  # run on an alternate thread
        sublime.set_timeout(lambda: self.gather_responses(pool), 0)

    def _show_activity_for_pending_requests(self, requests, count):
        """Show activity indicator in status bar.
        """
        activity = self.get_activity_indicator(count, self.ACTIVITY_SPACES)
        self.view.set_status('requester.activity', '{} {}'.format('Requester', activity))
        self.show_activity_for_pending_requests(requests, count, activity)

    def gather_responses(self, pool, count=0, responses=None):
        """Inspect thread pool at regular intervals to remove completed responses
        and handle them, and show activity for pending requests.

        Clients can handle responses and errors one at a time as they are
        completed, or as a group when they're all finished. Each response objects
        contains `request`, `response`, `error`, and `ordering` keys.
        """
        self._show_activity_for_pending_requests(pool.pending_requests, count)
        is_done = pool.is_done  # cache `is_done` before removing responses from pool

        if responses is None:
            responses = []

        while len(pool.responses):  # remove completed responses from thread pool and display them
            r = pool.responses.pop(0)  # O(N) but who cares, this list will never have more than 10 elements
            responses.append(r)
            self.handle_response(r, num_requests=len(pool.requests))

        if is_done:
            responses.sort(key=lambda response: response.ordering)  # parsing order is preserved
            self.handle_responses(responses)
            self.default_handle_errors(responses)
            self.persist_requests(responses)
            self.view.set_status('requester.activity', '')  # remove activity indicator from status bar
            return

        sublime.set_timeout(lambda: self.gather_responses(pool, count+1, responses), self.REFRESH_MS)

    def persist_requests(self, responses):
        """Persist up to N requests to a history file, along with the context
        needed to rebuild the env for these requests. One entry per unique
        request. Old requests are removed when requests exceed file capacity.

        Requests in history are keyed for uniqueness on (method + url/qs), a
        compromise to minimize duplicate requests without clobbering requests
        whose meanings could be very different. Imagine GET requests to a GraphQL
        API, where the querystring determines most everything about the response.
        """
        history_file = self.config.get('history_file', None)
        if not history_file:
            return
        history_path = os.path.join(sublime.packages_path(), 'User', history_file)

        try:
            with open(history_path, 'r') as f:
                text = f.read() or '{}'
        except FileNotFoundError:
            open(history_path, 'w').close()  # create history file if it didn't exist
            text = '{}'
        except Exception as e:
            sublime.error_message('HistoryFile Error:\n{}'.format(e))
            return
        rh = json.loads(text, object_pairs_hook=OrderedDict)

        ts = int(time())
        for response in responses:  # insert new requests
            res = response.response
            if res is None:
                continue
            method, url = res.request.method, res.url
            # uniqueness of request in history is determined by method and url + qs
            key = '{}: {}'.format(method, url)
            if key in rh:
                rh.pop(key, None)  # remove duplicate requests
            rh[key] = {
                'ts': ts,
                'env_string': self.view.settings().get('requester.env_string', None),
                'file': self.view.settings().get('requester.file', None),
                'env_file': self.view.settings().get('requester.env_file', None),
                'method': method,
                'url': url,
                'code': res.status_code,
                'request': response.request
            }

        # remove oldest requests if number of requests has exceeded `history_max_entries`
        history_max_entries = self.config.get('history_max_entries', 100)
        to_delete = len(rh) - history_max_entries
        if to_delete > 0:
            keys = []
            iter_ = iter(rh.keys())
            for i in range(to_delete):
                try:
                    keys.append(next(iter_))
                except StopIteration:
                    break
            for key in keys:
                try:
                    del rh[key]
                except KeyError:
                    pass

        # rewrite all requests to history file
        with open(history_path, 'w') as f:
            f.write(json.dumps(rh, f))

    def add_error_status_bar(self, error):
        """Logs error to console, and adds error in status bar. Not as obtrusive
        as `sublime.error_message`.
        """
        self._status_errors.append(error)
        print('{}: {}'.format('Requester Error', error))
        self.view.set_status('requester.errors', '{}: {}'.format(
            'RequesterErrors', ', '.join(self._status_errors)
        ))

    def reset_status(self):
        """Make sure this is called before `add_error_status_bar`.
        """
        self._status_errors = []
        self.view.set_status('requester.errors', '')
        self.view.set_status('requester.download', '')

    @staticmethod
    def parse_env_block(text):
        """Parses `text` for first env block, and returns text within this env
        block.
        """
        delimeter = '###env'
        in_block = False
        env_lines = []
        for line in text.splitlines():
            if in_block:
                if line == delimeter:
                    in_block = False
                    break
                env_lines.append(line)
            else:
                if line == delimeter:
                    in_block = True
        if not len(env_lines) or in_block:  # env block must be closed
            return None
        return '\n'.join(env_lines)

    @staticmethod
    def get_env_dict_from_string(s):
        """What it sounds like.

        http://stackoverflow.com/questions/5362771/load-module-from-string-in-python
        """
        try:
            del sys.modules['requester.env']  # this avoids a subtle bug, DON'T REMOVE
        except KeyError:
            pass

        if not s:
            return {}

        env = imp.new_module('requester.env')
        try:
            exec(s, env.__dict__)
        except Exception as e:
            sublime.error_message('EnvBlock Error:\n{}'.format(e))
            return {}
        else:
            return dict(env.__dict__)

    @staticmethod
    def get_activity_indicator(count, spaces):
        """Return activity indicator string.
        """
        cycle = count // spaces
        if cycle % 2 == 0:
            before = count % spaces
        else:
            before = spaces - (count % spaces)
        after = spaces - before
        return '[{}={}]'.format(' ' * before, ' ' * after)
