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

from ceilometer.compute.virt import inspector
from ceilometer import service
from ceilometer.tests import base


class TestHypervisorInspector(base.BaseTestCase):
    def setUp(self):
        super().setUp()
        self.conf = service.prepare_service([], [])

    def test_default_is_libvirt(self):
        self.assertEqual('libvirt', self.conf.hypervisor_inspector)

    def test_an_entry_point_of_another_driver_is_loaded(self):
        self.conf.set_override('hypervisor_inspector', 'kata')
        with mock.patch('stevedore.driver.DriverManager') as manager:
            loaded = inspector.get_hypervisor_inspector(self.conf)
        manager.assert_called_once_with(
            'ceilometer.compute.virt', 'kata', invoke_on_load=True,
            invoke_args=(self.conf,))
        self.assertIs(manager.return_value.driver, loaded)
