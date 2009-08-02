#
#  subunit: extensions to Python unittest to get test results from subprocesses.
#  Copyright (C) 2005  Robert Collins <robertc@robertcollins.net>
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#

import datetime
import os
import re
from StringIO import StringIO
import subprocess
import sys
import unittest

import iso8601


SEEK_CUR = os.SEEK_CUR
SEEK_SET = os.SEEK_SET


def test_suite():
    import subunit.tests
    return subunit.tests.test_suite()


def join_dir(base_path, path):
    """
    Returns an absolute path to C{path}, calculated relative to the parent
    of C{base_path}.

    @param base_path: A path to a file or directory.
    @param path: An absolute path, or a path relative to the containing
    directory of C{base_path}.

    @return: An absolute path to C{path}.
    """
    return os.path.join(os.path.dirname(os.path.abspath(base_path)), path)


def tags_to_new_gone(tags):
    """Split a list of tags into a new_set and a gone_set."""
    new_tags = set()
    gone_tags = set()
    for tag in tags:
        if tag[0] == '-':
            gone_tags.add(tag[1:])
        else:
            new_tags.add(tag)
    return new_tags, gone_tags


class DiscardStream(object):
    """A filelike object which discards what is written to it."""

    def write(self, bytes):
        pass


class TestProtocolServer(object):
    """A class for receiving results from a TestProtocol client.
    
    :ivar tags: The current tags associated with the protocol stream.
    """

    OUTSIDE_TEST = 0
    TEST_STARTED = 1
    READING_FAILURE = 2
    READING_ERROR = 3
    READING_SKIP = 4
    READING_XFAIL = 5
    READING_SUCCESS = 6

    def __init__(self, client, stream=None):
        """Create a TestProtocol server instance.

        :param client: An object meeting the unittest.TestResult protocol.
        :param stream: The stream that lines received which are not part of the
            subunit protocol should be written to. This allows custom handling
            of mixed protocols. By default, sys.stdout will be used for
            convenience.
        """
        self.state = TestProtocolServer.OUTSIDE_TEST
        self.client = client
        if stream is None:
            stream = sys.stdout
        self._stream = stream
        self.tags = set()

    def _addError(self, offset, line):
        if (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description == line[offset:-1]):
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self.client.addError(self._current_test, RemoteError(""))
            self.client.stopTest(self._current_test)
            self._current_test = None
        elif (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description + " [" == line[offset:-1]):
            self.state = TestProtocolServer.READING_ERROR
            self._message = ""
        else:
            self.stdOutLineReceived(line)

    def _addExpectedFail(self, offset, line):
        if (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description == line[offset:-1]):
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self.client.addSuccess(self._current_test)
            self.client.stopTest(self._current_test)
        elif (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description + " [" == line[offset:-1]):
            self.state = TestProtocolServer.READING_XFAIL
            self._message = ""
        else:
            self.stdOutLineReceived(line)

    def _addFailure(self, offset, line):
        if (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description == line[offset:-1]):
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self.client.addFailure(self._current_test, RemoteError())
            self.client.stopTest(self._current_test)
        elif (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description + " [" == line[offset:-1]):
            self.state = TestProtocolServer.READING_FAILURE
            self._message = ""
        else:
            self.stdOutLineReceived(line)

    def _addSkip(self, offset, line):
        if (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description == line[offset:-1]):
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self._skip_or_error()
            self.client.stopTest(self._current_test)
        elif (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description + " [" == line[offset:-1]):
            self.state = TestProtocolServer.READING_SKIP
            self._message = ""
        else:
            self.stdOutLineReceived(line)

    def _skip_or_error(self, message=None):
        """Report the current test as a skip if possible, or else an error."""
        addSkip = getattr(self.client, 'addSkip', None)
        if not callable(addSkip):
            self.client.addError(self._current_test, RemoteError(message))
        else:
            if not message:
                message = "No reason given"
            addSkip(self._current_test, message)

    def _addSuccess(self, offset, line):
        if (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description == line[offset:-1]):
            self._succeedTest()
        elif (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description + " [" == line[offset:-1]):
            self.state = TestProtocolServer.READING_SUCCESS
            self._message = ""
        else:
            self.stdOutLineReceived(line)

    def _appendMessage(self, line):
        if line[0:2] == " ]":
            # quoted ] start
            self._message += line[1:]
        else:
            self._message += line

    def endQuote(self, line):
        if self.state == TestProtocolServer.READING_FAILURE:
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self.client.addFailure(self._current_test,
                                   RemoteError(self._message))
            self.client.stopTest(self._current_test)
        elif self.state == TestProtocolServer.READING_ERROR:
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self.client.addError(self._current_test,
                                 RemoteError(self._message))
            self.client.stopTest(self._current_test)
        elif self.state == TestProtocolServer.READING_SKIP:
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self._skip_or_error(self._message)
            self.client.stopTest(self._current_test)
        elif self.state in (
            TestProtocolServer.READING_SUCCESS,
            TestProtocolServer.READING_XFAIL,
            ):
            self._succeedTest()
        else:
            self.stdOutLineReceived(line)

    def _handleProgress(self, offset, line):
        """Process a progress directive."""
        line = line[offset:].strip()
        if line[0] in '+-':
            whence = SEEK_CUR
        else:
            whence = SEEK_SET
        delta = int(line)
        progress_method = getattr(self.client, 'progress', None)
        if callable(progress_method):
            progress_method(delta, whence)

    def _handleTags(self, offset, line):
        """Process a tags command."""
        tags = line[offset:].split()
        new_tags, gone_tags = tags_to_new_gone(tags)
        if self.state == TestProtocolServer.OUTSIDE_TEST:
            update_tags = self.tags
        else:
            update_tags = self._current_test.tags
        update_tags.update(new_tags)
        update_tags.difference_update(gone_tags)

    def _handleTime(self, offset, line):
        # Accept it, but do not do anything with it yet.
        try:
            event_time = iso8601.parse_date(line[offset:-1])
        except TypeError, e:
            raise TypeError("Failed to parse %r, got %r" % (line, e))
        time_method = getattr(self.client, 'time', None)
        if callable(time_method):
            time_method(event_time)

    def lineReceived(self, line):
        """Call the appropriate local method for the received line."""
        if line == "]\n":
            self.endQuote(line)
        elif self.state in (TestProtocolServer.READING_FAILURE,
            TestProtocolServer.READING_ERROR, TestProtocolServer.READING_SKIP,
            TestProtocolServer.READING_SUCCESS,
            TestProtocolServer.READING_XFAIL
            ):
            self._appendMessage(line)
        else:
            parts = line.split(None, 1)
            if len(parts) == 2:
                cmd, rest = parts
                offset = len(cmd) + 1
                cmd = cmd.strip(':')
                if cmd in ('test', 'testing'):
                    self._startTest(offset, line)
                elif cmd == 'error':
                    self._addError(offset, line)
                elif cmd == 'failure':
                    self._addFailure(offset, line)
                elif cmd == 'progress':
                    self._handleProgress(offset, line)
                elif cmd == 'skip':
                    self._addSkip(offset, line)
                elif cmd in ('success', 'successful'):
                    self._addSuccess(offset, line)
                elif cmd in ('tags',):
                    self._handleTags(offset, line)
                elif cmd in ('time',):
                    self._handleTime(offset, line)
                elif cmd == 'xfail':
                    self._addExpectedFail(offset, line)
                else:
                    self.stdOutLineReceived(line)
            else:
                self.stdOutLineReceived(line)

    def _lostConnectionInTest(self, state_string):
        error_string = "lost connection during %stest '%s'" % (
            state_string, self.current_test_description)
        self.client.addError(self._current_test, RemoteError(error_string))
        self.client.stopTest(self._current_test)

    def lostConnection(self):
        """The input connection has finished."""
        if self.state == TestProtocolServer.OUTSIDE_TEST:
            return
        if self.state == TestProtocolServer.TEST_STARTED:
            self._lostConnectionInTest('')
        elif self.state == TestProtocolServer.READING_ERROR:
            self._lostConnectionInTest('error report of ')
        elif self.state == TestProtocolServer.READING_FAILURE:
            self._lostConnectionInTest('failure report of ')
        elif self.state == TestProtocolServer.READING_SUCCESS:
            self._lostConnectionInTest('success report of ')
        elif self.state == TestProtocolServer.READING_SKIP:
            self._lostConnectionInTest('skip report of ')
        elif self.state == TestProtocolServer.READING_XFAIL:
            self._lostConnectionInTest('xfail report of ')
        else:
            self._lostConnectionInTest('unknown state of ')

    def readFrom(self, pipe):
        for line in pipe.readlines():
            self.lineReceived(line)
        self.lostConnection()

    def _startTest(self, offset, line):
        """Internal call to change state machine. Override startTest()."""
        if self.state == TestProtocolServer.OUTSIDE_TEST:
            self.state = TestProtocolServer.TEST_STARTED
            self._current_test = RemotedTestCase(line[offset:-1])
            self.current_test_description = line[offset:-1]
            self.client.startTest(self._current_test)
            self._current_test.tags = set(self.tags)
        else:
            self.stdOutLineReceived(line)

    def stdOutLineReceived(self, line):
        self._stream.write(line)

    def _succeedTest(self):
        self.client.addSuccess(self._current_test)
        self.client.stopTest(self._current_test)
        self.current_test_description = None
        self._current_test = None
        self.state = TestProtocolServer.OUTSIDE_TEST


class RemoteException(Exception):
    """An exception that occured remotely to Python."""

    def __eq__(self, other):
        try:
            return self.args == other.args
        except AttributeError:
            return False


class TestProtocolClient(unittest.TestResult):
    """A class that looks like a TestResult and informs a TestProtocolServer."""

    def __init__(self, stream):
        unittest.TestResult.__init__(self)
        self._stream = stream

    def addError(self, test, error):
        """Report an error in test test."""
        self._stream.write("error: %s [\n" % test.id())
        for line in self._exc_info_to_string(error, test).splitlines():
            self._stream.write("%s\n" % line)
        self._stream.write("]\n")

    def addFailure(self, test, error):
        """Report a failure in test test."""
        self._stream.write("failure: %s [\n" % test.id())
        for line in self._exc_info_to_string(error, test).splitlines():
            self._stream.write("%s\n" % line)
        self._stream.write("]\n")

    def addSkip(self, test, reason):
        """Report a skipped test."""
        self._stream.write("skip: %s [\n" % test.id())
        self._stream.write("%s\n" % reason)
        self._stream.write("]\n")

    def addSuccess(self, test):
        """Report a success in a test."""
        self._stream.write("successful: %s\n" % test.id())

    def startTest(self, test):
        """Mark a test as starting its test run."""
        self._stream.write("test: %s\n" % test.id())

    def progress(self, offset, whence):
        """Provide indication about the progress/length of the test run.

        :param offset: Information about the number of tests remaining. If
            whence is SEEK_CUR, then offset increases/decreases the remaining
            test count. If whence is SEEK_SET, then offset specifies exactly
            the remaining test count.
        :param whence: One of SEEK_CUR or SEEK_SET.
        """
        if whence == SEEK_CUR and offset > -1:
            prefix = "+"
        else:
            prefix = ""
        self._stream.write("progress: %s%s\n" % (prefix, offset))

    def time(self, a_datetime):
        """Inform the client of the time.

        ":param datetime: A datetime.datetime object.
        """
        time = a_datetime.astimezone(iso8601.Utc())
        self._stream.write("time: %04d-%02d-%02d %02d:%02d:%02d.%06dZ\n" % (
            time.year, time.month, time.day, time.hour, time.minute,
            time.second, time.microsecond))

    def done(self):
        """Obey the testtools result.done() interface."""


def RemoteError(description=""):
    if description == "":
        description = "\n"
    return (RemoteException, RemoteException(description), None)


class RemotedTestCase(unittest.TestCase):
    """A class to represent test cases run in child processes.
    
    Instances of this class are used to provide the Python test API a TestCase
    that can be printed to the screen, introspected for metadata and so on.
    However, as they are a simply a memoisation of a test that was actually
    run in the past by a separate process, they cannot perform any interactive
    actions.
    """

    def __eq__ (self, other):
        try:
            return self.__description == other.__description
        except AttributeError:
            return False

    def __init__(self, description):
        """Create a psuedo test case with description description."""
        self.__description = description

    def error(self, label):
        raise NotImplementedError("%s on RemotedTestCases is not permitted." %
            label)

    def setUp(self):
        self.error("setUp")

    def tearDown(self):
        self.error("tearDown")

    def shortDescription(self):
        return self.__description

    def id(self):
        return "%s" % (self.__description,)

    def __str__(self):
        return "%s (%s)" % (self.__description, self._strclass())

    def __repr__(self):
        return "<%s description='%s'>" % \
               (self._strclass(), self.__description)

    def run(self, result=None):
        if result is None: result = self.defaultTestResult()
        result.startTest(self)
        result.addError(self, RemoteError("Cannot run RemotedTestCases.\n"))
        result.stopTest(self)

    def _strclass(self):
        cls = self.__class__
        return "%s.%s" % (cls.__module__, cls.__name__)


class ExecTestCase(unittest.TestCase):
    """A test case which runs external scripts for test fixtures."""

    def __init__(self, methodName='runTest'):
        """Create an instance of the class that will use the named test
           method when executed. Raises a ValueError if the instance does
           not have a method with the specified name.
        """
        unittest.TestCase.__init__(self, methodName)
        testMethod = getattr(self, methodName)
        self.script = join_dir(sys.modules[self.__class__.__module__].__file__,
                               testMethod.__doc__)

    def countTestCases(self):
        return 1

    def run(self, result=None):
        if result is None: result = self.defaultTestResult()
        self._run(result)

    def debug(self):
        """Run the test without collecting errors in a TestResult"""
        self._run(unittest.TestResult())

    def _run(self, result):
        protocol = TestProtocolServer(result)
        output = subprocess.Popen(self.script, shell=True,
            stdout=subprocess.PIPE).communicate()[0]
        protocol.readFrom(StringIO(output))


class IsolatedTestCase(unittest.TestCase):
    """A TestCase which runs its tests in a forked process."""

    def run(self, result=None):
        if result is None: result = self.defaultTestResult()
        run_isolated(unittest.TestCase, self, result)


class IsolatedTestSuite(unittest.TestSuite):
    """A TestCase which runs its tests in a forked process."""

    def run(self, result=None):
        if result is None: result = unittest.TestResult()
        run_isolated(unittest.TestSuite, self, result)


def run_isolated(klass, self, result):
    """Run a test suite or case in a subprocess, using the run method on klass.
    """
    c2pread, c2pwrite = os.pipe()
    # fixme - error -> result
    # now fork
    pid = os.fork()
    if pid == 0:
        # Child
        # Close parent's pipe ends
        os.close(c2pread)
        # Dup fds for child
        os.dup2(c2pwrite, 1)
        # Close pipe fds.
        os.close(c2pwrite)

        # at this point, sys.stdin is redirected, now we want
        # to filter it to escape ]'s.
        ### XXX: test and write that bit.

        result = TestProtocolClient(sys.stdout)
        klass.run(self, result)
        sys.stdout.flush()
        sys.stderr.flush()
        # exit HARD, exit NOW.
        os._exit(0)
    else:
        # Parent
        # Close child pipe ends
        os.close(c2pwrite)
        # hookup a protocol engine
        protocol = TestProtocolServer(result)
        protocol.readFrom(os.fdopen(c2pread, 'rU'))
        os.waitpid(pid, 0)
        # TODO return code evaluation.
    return result


def TAP2SubUnit(tap, subunit):
    """Filter a TAP pipe into a subunit pipe.
    
    :param tap: A tap pipe/stream/file object.
    :param subunit: A pipe/stream/file object to write subunit results to.
    :return: The exit code to exit with.
    """
    BEFORE_PLAN = 0
    AFTER_PLAN = 1
    SKIP_STREAM = 2
    client = TestProtocolClient(subunit)
    state = BEFORE_PLAN
    plan_start = 1
    plan_stop = 0
    def _skipped_test(subunit, plan_start):
        # Some tests were skipped.
        subunit.write('test test %d\n' % plan_start)
        subunit.write('error test %d [\n' % plan_start)
        subunit.write('test missing from TAP output\n')
        subunit.write(']\n')
        return plan_start + 1
    # Test data for the next test to emit
    test_name = None
    log = []
    result = None
    def _emit_test():
        "write out a test"
        if test_name is None:
            return
        subunit.write("test %s\n" % test_name)
        if not log:
            subunit.write("%s %s\n" % (result, test_name))
        else:
            subunit.write("%s %s [\n" % (result, test_name))
        if log:
            for line in log:
                subunit.write("%s\n" % line)
            subunit.write("]\n")
        del log[:]
    for line in tap:
        if state == BEFORE_PLAN:
            match = re.match("(\d+)\.\.(\d+)\s*(?:\#\s+(.*))?\n", line)
            if match:
                state = AFTER_PLAN
                _, plan_stop, comment = match.groups()
                plan_stop = int(plan_stop)
                if plan_start > plan_stop and plan_stop == 0:
                    # skipped file
                    state = SKIP_STREAM
                    subunit.write("test file skip\n")
                    subunit.write("skip file skip [\n")
                    subunit.write("%s\n" % comment)
                    subunit.write("]\n")
                continue
        # not a plan line, or have seen one before
        match = re.match("(ok|not ok)(?:\s+(\d+)?)?(?:\s+([^#]*[^#\s]+)\s*)?(?:\s+#\s+(TODO|SKIP)(?:\s+(.*))?)?\n", line)
        if match:
            # new test, emit current one.
            _emit_test()
            status, number, description, directive, directive_comment = match.groups()
            if status == 'ok':
                result = 'success'
            else:
                result = "failure"
            if description is None:
                description = ''
            else:
                description = ' ' + description
            if directive is not None:
                if directive == 'TODO':
                    result = 'xfail'
                elif directive == 'SKIP':
                    result = 'skip'
                if directive_comment is not None:
                    log.append(directive_comment)
            if number is not None:
                number = int(number)
                while plan_start < number:
                    plan_start = _skipped_test(subunit, plan_start)
            test_name = "test %d%s" % (plan_start, description)
            plan_start += 1
            continue
        match = re.match("Bail out\!(?:\s*(.*))?\n", line)
        if match:
            reason, = match.groups()
            if reason is None:
                extra = ''
            else:
                extra = ' %s' % reason
            _emit_test()
            test_name = "Bail out!%s" % extra
            result = "error"
            state = SKIP_STREAM
            continue
        match = re.match("\#.*\n", line)
        if match:
            log.append(line[:-1])
            continue
        subunit.write(line)
    _emit_test()
    while plan_start <= plan_stop:
        # record missed tests
        plan_start = _skipped_test(subunit, plan_start)
    return 0


def tag_stream(original, filtered, tags):
    """Alter tags on a stream.

    :param original: The input stream.
    :param filtered: The output stream.
    :param tags: The tags to apply. As in a normal stream - a list of 'TAG' or
        '-TAG' commands.

        A 'TAG' command will add the tag to the output stream,
        and override any existing '-TAG' command in that stream.
        Specifically:
         * A global 'tags: TAG' will be added to the start of the stream.
         * Any tags commands with -TAG will have the -TAG removed.

        A '-TAG' command will remove the TAG command from the stream.
        Specifically:
         * A 'tags: -TAG' command will be added to the start of the stream.
         * Any 'tags: TAG' command will have 'TAG' removed from it.
        Additionally, any redundant tagging commands (adding a tag globally
        present, or removing a tag globally removed) are stripped as a
        by-product of the filtering.
    :return: 0
    """
    new_tags, gone_tags = tags_to_new_gone(tags)
    def write_tags(new_tags, gone_tags):
        if new_tags or gone_tags:
            filtered.write("tags: " + ' '.join(new_tags))
            if gone_tags:
                for tag in gone_tags:
                    filtered.write("-" + tag)
            filtered.write("\n")
    write_tags(new_tags, gone_tags)
    # TODO: use the protocol parser and thus don't mangle test comments.
    for line in original:
        if line.startswith("tags:"):
            line_tags = line[5:].split()
            line_new, line_gone = tags_to_new_gone(line_tags)
            line_new = line_new - gone_tags
            line_gone = line_gone - new_tags
            write_tags(line_new, line_gone)
        else:
            filtered.write(line)
    return 0


class ProtocolTestCase(object):
    """A test case which reports a subunit stream."""

    def __init__(self, stream, passthrough=None):
        """Create a ProtocolTestCase reading from stream.

        :param stream: A filelike object which a subunit stream can be read
            from.
        :param passthrough: A stream pass non subunit input on to. If not
            supplied, the TestProtocolServer default is used.
        """
        self._stream = stream
        self._passthrough = passthrough

    def __call__(self, result=None):
        return self.run(result)

    def run(self, result=None):
        if result is None:
            result = self.defaultTestResult()
        protocol = TestProtocolServer(result, self._passthrough)
        line = self._stream.readline()
        while line:
            protocol.lineReceived(line)
            line = self._stream.readline()
        protocol.lostConnection()


class TestResultStats(unittest.TestResult):
    """A pyunit TestResult interface implementation for making statistics.
    
    :ivar total_tests: The total tests seen.
    :ivar passed_tests: The tests that passed.
    :ivar failed_tests: The tests that failed.
    :ivar tags: The tags seen across all tests.
    """

    def __init__(self, stream):
        """Create a TestResultStats which outputs to stream."""
        unittest.TestResult.__init__(self)
        self._stream = stream
        self.failed_tests = 0
        self.skipped_tests = 0
        self.tags = set()

    @property
    def total_tests(self):
        return self.testsRun

    def addError(self, test, err):
        self.failed_tests += 1

    def addFailure(self, test, err):
        self.failed_tests += 1

    def addSkip(self, test, reason):
        self.skipped_tests += 1

    def formatStats(self):
        self._stream.write("Total tests:   %5d\n" % self.total_tests)
        self._stream.write("Passed tests:  %5d\n" % self.passed_tests)
        self._stream.write("Failed tests:  %5d\n" % self.failed_tests)
        self._stream.write("Skipped tests: %5d\n" % self.skipped_tests)
        tags = sorted(self.tags)
        self._stream.write("Tags: %s\n" % (", ".join(tags)))

    @property
    def passed_tests(self):
        return self.total_tests - self.failed_tests - self.skipped_tests

    def stopTest(self, test):
        unittest.TestResult.stopTest(self, test)
        self.tags.update(test.tags)

    def wasSuccessful(self):
        """Tells whether or not this result was a success"""
        return self.failed_tests == 0


class TestResultFilter(unittest.TestResult):
    """A pyunit TestResult interface implementation which filters tests.

    Tests that pass the filter are handed on to another TestResult instance
    for further processing/reporting. To obtain the filtered results, 
    the other instance must be interrogated.

    :ivar result: The result that tests are passed to after filtering.
    """

    def __init__(self, result, filter_error=False, filter_failure=False,
        filter_success=True, filter_skip=False):
        """Create a FilterResult object filtering to result.
        
        :param filter_error: Filter out errors.
        :param filter_failure: Filter out failures.
        :param filter_success: Filter out successful tests.
        :param filter_skip: Filter out skipped tests.
        """
        unittest.TestResult.__init__(self)
        self.result = result
        self._filter_error = filter_error
        self._filter_failure = filter_failure
        self._filter_success = filter_success
        self._filter_skip = filter_skip
        
    def addError(self, test, err):
        if not self._filter_error:
            self.result.startTest(test)
            self.result.addError(test, err)
            self.result.stopTest(test)

    def addFailure(self, test, err):
        if not self._filter_failure:
            self.result.startTest(test)
            self.result.addFailure(test, err)
            self.result.stopTest(test)

    def addSkip(self, test, reason):
        if not self._filter_skip:
            self.result.startTest(test)
            # This is duplicated, it would be nice to have on a 'calls
            # TestResults' mixin perhaps.
            addSkip = getattr(self.result, 'addSkip', None)
            if not callable(addSkip):
                self.result.addError(test, RemoteError(reason))
            else:
                self.result.addSkip(test, reason)
            self.result.stopTest(test)

    def addSuccess(self, test):
        if not self._filter_success:
            self.result.startTest(test)
            self.result.addSuccess(test)
            self.result.stopTest(test)

    def id_to_orig_id(self, id):
        if id.startswith("subunit.RemotedTestCase."):
            return id[len("subunit.RemotedTestCase."):]
        return id

