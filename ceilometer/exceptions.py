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

"""Ceilometer exception definitions."""


class CeilometerException(Exception):
    """Base exception class for Ceilometer."""
    def __init__(self, message=None):
        self.message = self.__class__.__name__ if message is None else message
        super().__init__(self.message)


class NotFound(CeilometerException):
    """Resource not found.

    Raised when a find operation matches no resources.
    """

    def __init__(self, message, details=None):
        self.details = details if details is not None else {}
        super().__init__(message)


class NoUniqueMatch(CeilometerException):
    """Multiple entities found instead of one.

    Raised when a find() operation matches multiple resources
    when exactly one was expected.
    """
    pass
