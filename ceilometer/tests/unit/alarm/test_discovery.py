#
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

from unittest import mock

from aodhclient import exceptions as aodh_exc
import fixtures

from ceilometer.alarm import discovery
from ceilometer import service
from ceilometer.tests import base


class TestAlarmDiscovery(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.conf = service.prepare_service([], [])
        self.useFixture(
            fixtures.MockPatch('ceilometer.keystone_client.get_session'))
        self.useFixture(
            fixtures.MockPatch('aodhclient.client.Client'))
        self.discovery = discovery.AlarmDiscovery(self.conf)
        self.manager = mock.Mock()

    def test_discover_returns_metrics(self):
        fake_metrics = {'evaluation_results': [{'alarm_id': 'fake'}]}
        self.discovery.aodh_client.metrics.get.return_value = fake_metrics

        resources = self.discovery.discover(self.manager)

        self.assertEqual([fake_metrics], resources)
        self.discovery.aodh_client.metrics.get.assert_called_once_with(
            all_projects=True)

    def test_discover_forbidden_does_not_raise(self):
        self.discovery.aodh_client.metrics.get.side_effect = (
            aodh_exc.Forbidden())

        resources = self.discovery.discover(self.manager)

        self.assertEqual([], resources)

    def test_discover_forbidden_logs_error(self):
        self.discovery.aodh_client.metrics.get.side_effect = (
            aodh_exc.Forbidden('metric endpoint disabled'))

        with mock.patch.object(discovery.LOG, 'error') as mock_log:
            self.discovery.discover(self.manager)

        mock_log.assert_called_once()
        self.assertIn('Skipping alarm discovery',
                      mock_log.call_args[0][0])
