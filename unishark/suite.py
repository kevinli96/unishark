# Copyright 2015 Twitter, Inc and other contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from unittest.suite import TestSuite as UnitTestSuite
import sys
from unishark.util import get_module_name
from unishark.result import combine_results
import concurrent.futures
import logging

log = logging.getLogger(__name__)


def _is_suite(test):
    try:
        iter(test)
    except TypeError:
        return False
    return True


def _get_level(test):
    if not _is_suite(test):
        return TestSuite.METHOD_LEVEL
    else:
        for t in test:
            return _get_level(t) - 1
        return -1


def _get_current_module(test):
    # test is a module level suite
    current_module = None
    for tt in test:  # tt is a class level suite
        for t in tt:  # t is a test case instance
            current_module = t.__class__.__module__
            break
        break
    return current_module


def _get_current_class(test):
    # test is a class level suite
    current_class = None
    for t in test:  # t is a test case instance
        current_class = t.__class__
        break
    return current_class


def _call_if_exists(parent, attr):
    func = getattr(parent, attr, lambda: None)
    func()


def _group_test_cases(test, dic):
    if not _is_suite(test):
        mod = get_module_name(test)
        if mod not in dic:
            dic[mod] = dict()
        cls = test.__class__
        if cls not in dic[mod]:
            dic[mod][cls] = []
        dic[mod][cls].append(test)
    else:
        for t in test:
            _group_test_cases(t, dic)


def convert(test):
    suite = TestSuite()
    dic = dict()
    _group_test_cases(test, dic)
    for mod_dic in dic.values():
        mod_suite = TestSuite()
        for cases in mod_dic.values():
            cls_suite = TestSuite()
            cls_suite.addTests(cases)
            mod_suite.addTest(cls_suite)
        suite.addTest(mod_suite)
    log.debug('Converted tests: %r' % suite)
    return suite


class TestSuite(UnitTestSuite):
    ROOT_LEVEL = 0
    MODULE_LEVEL = 1
    CLASS_LEVEL = 2
    METHOD_LEVEL = 3

    def __init__(self, tests=()):
        super(TestSuite, self).__init__(tests)
        self._successful_fixtures = set()
        self._failed_fixtures = set()

    def run(self, result, debug=False, concurrency_level=ROOT_LEVEL, max_workers=1, timeout=None):
        if concurrency_level < TestSuite.ROOT_LEVEL or concurrency_level > TestSuite.METHOD_LEVEL:
            raise ValueError('concurrency_level must be between %d and %d.'
                             % (TestSuite.ROOT_LEVEL, TestSuite.METHOD_LEVEL))
        if debug or concurrency_level == TestSuite.ROOT_LEVEL or max_workers <= 1:
            return super(TestSuite, self).run(result, debug)
        if _get_level(self) != TestSuite.ROOT_LEVEL:
            raise RuntimeError('Test suite is not well-formed.')
        with concurrent.futures.ThreadPoolExecutor(max_workers) as executor:
            self._run(self, result, TestSuite.ROOT_LEVEL, concurrency_level, executor, timeout)
        return result

    def _run(self, test, result, current_level, concurrency_level, executor, timeout):
        # test is a test suite instance which must be well-formed.
        # A well-formed test suite has a 4-level self-embedding structure:
        #                                   suite obj(top level)
        #                                    /                \
        #                             suite obj(mod1)      suite obj(mod2)
        #                                /      \                    \
        #               suite obj(mod1.cls1)  suite obj(mod1.cls2)  ...
        #                 /            \                       \
        # case obj(mod1.cls1.mth1)  case obj(mod1.cls1.mth2)  ...
        if current_level == concurrency_level:
            self._seq_run(test, result)
        elif current_level < concurrency_level:
            results = result.children  # Divide: for each sub-suite/case in the test, there is a child result
            if current_level == TestSuite.ROOT_LEVEL:
                futures_of_setup = [executor.submit(self._setup_module, t, r) for t, r in zip(test, results)]
                futures = []
                for done in concurrent.futures.as_completed(futures_of_setup, timeout=timeout):
                    t, r = done.result()
                    futures.append(executor.submit(self._run, t, r, current_level+1, concurrency_level, executor, timeout))
                futures_of_teardown = []
                for done in concurrent.futures.as_completed(futures, timeout=timeout):
                    t, r = done.result()
                    futures_of_teardown.append(executor.submit(self._teardown_module, t, r))
                concurrent.futures.wait(futures_of_teardown, timeout=timeout)
            elif current_level == TestSuite.MODULE_LEVEL:
                futures_of_setup = [executor.submit(self._setup_class, t, r) for t, r in zip(test, results)]
                futures = []
                for done in concurrent.futures.as_completed(futures_of_setup, timeout=timeout):
                    t, r = done.result()
                    futures.append(executor.submit(self._run, t, r, current_level+1, concurrency_level, executor, timeout))
                futures_of_teardown = []
                for done in concurrent.futures.as_completed(futures, timeout=timeout):
                    t, r = done.result()
                    futures_of_teardown.append(executor.submit(self._teardown_class, t, r))
                concurrent.futures.wait(futures_of_teardown, timeout=timeout)
            elif current_level == TestSuite.CLASS_LEVEL:
                futures = [executor.submit(self._run, t, r, current_level+1, concurrency_level, executor, timeout)
                           for t, r in zip(test, results)]
                concurrent.futures.wait(futures, timeout=timeout)
            combine_results(result, results)  # Conquer: collect child results into parent result
        return test, result

    def _seq_run(self, test, result):
        if not _is_suite(test):
            test(result)
        else:
            if _get_level(test) == TestSuite.MODULE_LEVEL:
                self._setup_module(test, result)
                for t in test:
                    self._seq_run(t, result)
                self._teardown_module(test, result)
            elif _get_level(test) == TestSuite.CLASS_LEVEL:
                self._setup_class(test, result)
                for t in test:
                    self._seq_run(t, result)
                self._teardown_class(test, result)

    def _setup_module(self, test, result):
        # test must be a module level suite
        current_module = _get_current_module(test)
        fixture_name = current_module + '.setUpModule'
        if fixture_name in self._successful_fixtures or fixture_name in self._failed_fixtures:
            return test, result

        try:
            module = sys.modules[current_module]
        except KeyError:
            return test, result
        setUpModule = getattr(module, 'setUpModule', None)
        if setUpModule is not None:
            _call_if_exists(result, '_setupStdout')
            try:
                setUpModule()
                self._successful_fixtures.add(fixture_name)
            except Exception as e:
                self._failed_fixtures.add(fixture_name)
                error_name = 'setUpModule (%s)' % current_module
                self._addClassOrModuleLevelException(result, e, error_name)
            finally:
                _call_if_exists(result, '_restoreStdout')
        return test, result

    def _teardown_module(self, test, result):
        # test must be a module level suite
        current_module = _get_current_module(test)
        fixture_name = current_module + '.tearDownModule'
        if fixture_name in self._successful_fixtures or fixture_name in self._failed_fixtures:
            return
        if current_module + '.setUpModule' in self._failed_fixtures:
            return

        try:
            module = sys.modules[current_module]
        except KeyError:
            return
        tearDownModule = getattr(module, 'tearDownModule', None)
        if tearDownModule is not None:
            _call_if_exists(result, '_setupStdout')
            try:
                tearDownModule()
                self._successful_fixtures.add(fixture_name)
            except Exception as e:
                self._failed_fixtures.add(fixture_name)
                error_name = 'tearDownModule (%s)' % current_module
                self._addClassOrModuleLevelException(result, e, error_name)
            finally:
                _call_if_exists(result, '_restoreStdout')

    def _setup_class(self, test, result):
        # test must be a class level suite
        current_class = _get_current_class(test)
        current_module = current_class.__module__
        class_name = '.'.join((current_module, current_class.__name__))
        fixture_name = class_name + '.setUpClass'
        if fixture_name in self._successful_fixtures or fixture_name in self._failed_fixtures:
            return test, result
        if current_module + '.setUpModule' in self._failed_fixtures:
            return test, result
        if getattr(current_class, '__unittest_skip__', False):
            return test, result

        setUpClass = getattr(current_class, 'setUpClass', None)
        if setUpClass is not None:
            _call_if_exists(result, '_setupStdout')
            try:
                setUpClass()
                self._successful_fixtures.add(fixture_name)
            except Exception as e:
                self._failed_fixtures.add(fixture_name)
                current_class._classSetupFailed = True
                error_name = 'setUpClass (%s)' % class_name
                self._addClassOrModuleLevelException(result, e, error_name)
            finally:
                _call_if_exists(result, '_restoreStdout')
        return test, result

    def _teardown_class(self, test, result):
        # test must be a class level suite
        current_class = _get_current_class(test)
        current_module = current_class.__module__
        class_name = '.'.join((current_module, current_class.__name__))
        fixture_name = class_name + '.tearDownClass'
        if fixture_name in self._successful_fixtures or fixture_name in self._failed_fixtures:
            return
        if class_name + '.setUpClass' in self._failed_fixtures:
            return
        if current_module + '.setUpModule' in self._failed_fixtures:
            return
        if getattr(current_class, "__unittest_skip__", False):
            return

        tearDownClass = getattr(current_class, 'tearDownClass', None)
        if tearDownClass is not None:
            _call_if_exists(result, '_setupStdout')
            try:
                tearDownClass()
                self._successful_fixtures.add(fixture_name)
            except Exception as e:
                self._failed_fixtures.add(fixture_name)
                error_name = 'tearDownClass (%s)' % class_name
                self._addClassOrModuleLevelException(result, e, error_name)
            finally:
                _call_if_exists(result, '_restoreStdout')