# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Tests for ceilometer.exceptions"""

from ceilometer import exceptions
from ceilometer.tests import base


class TestExceptions(base.BaseTestCase):

    def test_ceilometer_exception_with_none(self):
        """Test CeilometerException with None returns class name."""
        exc = exceptions.CeilometerException()
        self.assertEqual('CeilometerException', str(exc))
        self.assertEqual('CeilometerException', exc.message)

    def test_ceilometer_exception_with_message(self):
        """Test CeilometerException with message returns message."""
        exc = exceptions.CeilometerException('Test message')
        self.assertEqual('Test message', str(exc))
        self.assertEqual('Test message', exc.message)

    def test_not_found_with_message(self):
        """Test NotFound with message returns message."""
        exc = exceptions.NotFound('test msg')
        self.assertEqual('test msg', str(exc))
        self.assertEqual('test msg', exc.message)

    def test_not_found_with_details(self):
        """Test NotFound stores details correctly."""
        exc = exceptions.NotFound('Resource not found', details={'id': 123})
        self.assertEqual('Resource not found', str(exc))
        self.assertEqual('Resource not found', exc.message)
        self.assertEqual({'id': 123}, exc.details)

    def test_not_found_without_details(self):
        """Test NotFound without details defaults to empty dict."""
        exc = exceptions.NotFound('Resource not found')
        self.assertEqual('Resource not found', str(exc))
        self.assertEqual({}, exc.details)

    def test_no_unique_match_inherits_from_base(self):
        """Test NoUniqueMatch is a CeilometerException."""
        exc = exceptions.NoUniqueMatch('Multiple matches')
        self.assertIsInstance(exc, exceptions.CeilometerException)
        self.assertEqual('Multiple matches', str(exc))
